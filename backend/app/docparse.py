"""PDF / DOCX text extraction with hard resource limits (used by context.py).

Hostile documents are cheap to make and expensive to parse: a few KB of PDF can decompress into
tens of MB of content-stream operators (pypdf keeps one graphics state per ``q``, ~140 MB of RAM
per decoded MB), sixty pages can all point at that one stream, and a tiny .docx can hold MBs of
empty paragraphs. A thread can't be stopped once it starts, so uploads and linked PDFs are parsed
in a short-lived **child process** (``python -m app.docparse``, ~0.4 s to start) that is killed
when ``timeout`` runs out:

    text = await docparse.extract_isolated("pdf" | "docx", data, timeout, owner=client_ip)

At most ``PARSE_CONCURRENCY`` children run at once and at most one per ``owner`` (one client can't
fill the queue), and a parse waits at most ``QUEUE_WAIT_S`` for its turn before failing with a
friendly "busy" error (memory stays bounded, and the shared ``asyncio.to_thread`` pool is never tied
up by parsing). Each child caps its own memory at ``TEE_PARSE_MEMORY_MB`` (default 512 MB; a Windows
job object, RLIMIT_AS elsewhere) before it reads any input, so an allocation past it fails with
MemoryError instead of growing. Inside the child, defense in depth:

  PDF   pages whose /Contents was already read are skipped; a page whose decoded content, plus the
        form XObjects it paints (counted once per mention of the form's name, nested up to
        FORM_MAX_DEPTH), is over PDF_PAGE_MAX_CONTENT is skipped; reading stops past
        PDF_TOTAL_MAX_CONTENT; pypdf's own
        decompression limits are lowered (pypdf >= 6 Configuration; ignored on older versions).
  DOCX  at most DOCX_MAX_ENTRIES archive entries; REAL (not declared) uncompressed sizes are
        measured with bounded reads; word/document.xml at most DOCX_MAX_DOCUMENT_XML; paragraph /
        table-cell and run count caps before python-docx walks the tree.

This module imports nothing heavy at top level, so a child starts quickly. ``extract`` (sync,
in-process) is the same logic for tests and trusted callers.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import weakref
import zipfile
from collections import Counter
from itertools import islice
from pathlib import Path

__all__ = ["BUSY_MESSAGE", "ExtractError", "extract", "extract_isolated", "pdf_text", "docx_text"]

_BACKEND_DIR = str(Path(__file__).resolve().parent.parent)

PARSE_CONCURRENCY = 2  # child processes parsing at the same time (each capped at CHILD_MEMORY_MB)
QUEUE_WAIT_S = 30.0  # longest wait for a free parse slot (this client's and a global one)
RAW_MAX_CHARS = 80_000  # raw text handed back (callers clean and cut to 20k)
MEMORY_ENV = "TEE_PARSE_MEMORY_MB"
CHILD_MEMORY_MB = 512  # default per-child memory cap (a normal parse needs ~40-80 MB)

PDF_MAX_PAGES = 60
PDF_PAGE_MAX_CONTENT = 1 * 1024 * 1024  # decoded content-stream bytes; real text pages are ~5-100 KB
PDF_TOTAL_MAX_CONTENT = 4 * 1024 * 1024  # ~2 s of parsing per MB of operators in the worst case
PDF_STREAM_MAX_OUTPUT = 16 * 1024 * 1024  # pypdf decompression cap per stream (default 75 MB)
FORM_MAX_DEPTH = 3  # nested form XObjects followed when costing a page; deeper nesting skips the page

DOCX_MAX_ENTRIES = 200
DOCX_MAX_UNCOMPRESSED = 64 * 1024 * 1024  # all entries together, measured, not declared
DOCX_MAX_DOCUMENT_XML = 8 * 1024 * 1024
DOCX_MAX_BLOCKS = 20_000  # <w:p> paragraphs + <w:tc> table cells; a 300-page book has ~10k
DOCX_MAX_RUNS = 100_000  # <w:r> runs (~45 us each in python-docx); a 300-page book has ~30-60k

_MSG_PDF_DAMAGED = "We couldn't read this PDF. It may be damaged."
_MSG_DOCX_DAMAGED = "We couldn't read this Word document. It may be damaged."
_MSG_TOO_LARGE = "This document is too large to read."
BUSY_MESSAGE = "Our file reader is busy right now. Please try again in a minute."


class ExtractError(Exception):
    """A file couldn't be read. ``str(exc)`` is a friendly reason that is safe to show."""


# --------------------------------------------------------------------------- PDF


def _pypdf_limits():
    """Lower pypdf's decompression limits (pypdf >= 6); a no-op context on older versions."""
    try:
        from pypdf import apply_configuration

        return apply_configuration(
            zlib_maximum_output_length=PDF_STREAM_MAX_OUTPUT,
            maximum_declared_stream_length=PDF_STREAM_MAX_OUTPUT,
            array_based_stream_maximum_output_length=PDF_STREAM_MAX_OUTPUT,
            lzw_maximum_output_length=PDF_STREAM_MAX_OUTPUT,
            run_length_maximum_output_length=PDF_STREAM_MAX_OUTPUT,
        )
    except (ImportError, TypeError):  # pragma: no cover - older pypdf without Configuration
        return contextlib.nullcontext()


def _contents_key(page) -> tuple | None:
    """Identity of a page's /Contents (indirect references), so shared streams are read once."""
    try:
        raw = page.raw_get("/Contents")
    except (KeyError, AttributeError):
        return None
    refs = raw if isinstance(raw, list) else [raw]
    keys = tuple((getattr(r, "idnum", None), getattr(r, "generation", None)) for r in refs)
    return keys if keys and all(k[0] is not None for k in keys) else None


# Every name token ("/X0"); names end at whitespace or a delimiter, as in pypdf's own parser, and
# "#xx" escapes are decoded. ``_form_cost`` counts EVERY occurrence of a form's name, not just a
# "/Name Do" pattern: pypdf paints whatever name token is the operand before "Do", and a pattern is
# easy to dodge ("/X0[]Do", "/X0()Do", or a "%" inside a string literal that hides the rest of the
# line from a comment-stripping regex). A name that is mentioned without being painted only
# over-counts, which is the safe direction.
_NAME_RE = re.compile(rb"/([^\s/\[\]()<>{}%]+)")
_NAME_ESCAPE_RE = re.compile(rb"#([0-9A-Fa-f]{2})")


def _resolve(obj):
    return obj.get_object() if hasattr(obj, "get_object") else obj


def _form_cost(data: bytes, resources, budget: int, depth: int, sizes: dict, stack: set) -> int:
    """Decoded bytes of the form XObjects that content ``data`` paints, counted once PER mention.

    pypdf's text extraction walks a form's operators every time it is painted, so a 1.7 KB page
    saying "/X0 Do" 2000 times over a 0.9 MB form costs as much as 1.8 GB of page content. Every
    occurrence of a form's name counts as a paint (see ``_NAME_RE``). Nested forms are costed too
    (``sizes`` memoizes each form's total). Returns early once over ``budget``; a cycle or nesting
    deeper than FORM_MAX_DEPTH counts as over budget.
    """
    names = Counter(_NAME_RE.findall(data))
    if not names:
        return 0
    xobjects = _resolve(_resolve(resources).get("/XObject")) if resources is not None else None
    if not isinstance(xobjects, dict):
        return 0
    total = 0
    for raw, count in names.items():
        name = "/" + _NAME_ESCAPE_RE.sub(lambda m: bytes([int(m.group(1), 16)]), raw).decode("latin-1")
        ref = xobjects.get(name)
        if ref is None:
            continue
        key = (ref.idnum, ref.generation) if hasattr(ref, "idnum") else id(ref)
        xobj = _resolve(ref)
        if not isinstance(xobj, dict) or _resolve(xobj.get("/Subtype")) != "/Form":
            continue  # images and missing names cost nothing to text extraction
        if key not in sizes:
            if key in stack or depth >= FORM_MAX_DEPTH:
                return budget + 1
            body = xobj.get_data()
            stack.add(key)
            try:
                inner = _form_cost(body, xobj.get("/Resources") or resources, budget, depth + 1, sizes, stack)
            finally:
                stack.discard(key)
            sizes[key] = len(body) + inner
        total += sizes[key] * count
        if total > budget:
            return total
    return total


def pdf_text(data: bytes) -> str:
    """Text of up to PDF_MAX_PAGES pages (blocking). Raises ExtractError with a friendly reason."""
    from pypdf import PdfReader  # deferred: only needed when a PDF actually arrives

    if b"%PDF-" not in data[:1024]:
        raise ExtractError("This doesn't look like a valid PDF.")
    try:
        with _pypdf_limits():
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                try:
                    unlocked = reader.decrypt("")
                except Exception:
                    unlocked = 0
                if not unlocked:
                    raise ExtractError("This PDF is password-protected.")
            parts: list[str] = []
            chars = decoded = 0
            seen: set[tuple] = set()
            form_sizes: dict = {}  # form XObject -> decoded bytes incl. nested forms (shared by pages)
            for page in islice(reader.pages, PDF_MAX_PAGES):
                key = _contents_key(page)
                if key is not None:
                    if key in seen:
                        continue  # 60 pages pointing at one stream: read it once
                    seen.add(key)
                try:
                    contents = page.get_contents()
                    body = contents.get_data() if contents is not None else b""
                    size = len(body)
                    if size <= PDF_PAGE_MAX_CONTENT:  # + the forms it paints, each time it paints them
                        size += _form_cost(
                            body, page.get("/Resources"), PDF_PAGE_MAX_CONTENT - size, 0, form_sizes, set()
                        )
                    if size > PDF_PAGE_MAX_CONTENT:
                        continue  # no real page needs this much; parsing it costs ~140 MB per MB
                    decoded += size
                    if decoded > PDF_TOTAL_MAX_CONTENT:
                        break
                    chunk = page.extract_text() or ""
                except ExtractError:
                    raise
                except Exception:  # one broken page shouldn't sink the whole file
                    continue
                parts.append(chunk)
                chars += len(chunk)
                if chars >= RAW_MAX_CHARS // 2:  # cleaning shrinks text, so read a little past the cap
                    break
            return "\n\n".join(parts)[:RAW_MAX_CHARS]
    except ExtractError:
        raise
    except Exception as exc:
        raise ExtractError(_MSG_PDF_DAMAGED) from exc


# --------------------------------------------------------------------------- DOCX

_BLOCK_RE = re.compile(rb"<w:(?:p|tc)[\s>/]")
_RUN_RE = re.compile(rb"<w:r[\s>/]")


def _check_docx_archive(data: bytes) -> None:
    """Zip-bomb guard on REAL sizes: declared sizes in a zip are attacker-controlled."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > DOCX_MAX_ENTRIES:
                raise ExtractError(_MSG_TOO_LARGE)
            if "word/document.xml" not in {e.filename for e in entries}:
                raise ExtractError("This doesn't look like a Word (.docx) document.")
            budget = DOCX_MAX_UNCOMPRESSED
            for entry in entries:
                limit = min(budget, DOCX_MAX_DOCUMENT_XML) if entry.filename == "word/document.xml" else budget
                with archive.open(entry) as member:
                    body = member.read(limit + 1)
                if len(body) > limit:
                    raise ExtractError(_MSG_TOO_LARGE)
                if entry.filename == "word/document.xml" and (
                    len(_BLOCK_RE.findall(body)) > DOCX_MAX_BLOCKS or len(_RUN_RE.findall(body)) > DOCX_MAX_RUNS
                ):
                    raise ExtractError(_MSG_TOO_LARGE)
                budget -= len(body)
    except ExtractError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, OSError, RuntimeError, ValueError) as exc:
        raise ExtractError("This doesn't look like a valid .docx file.") from exc
    except Exception as exc:  # zlib errors, unsupported compression, ...
        raise ExtractError(_MSG_DOCX_DAMAGED) from exc


def docx_text(data: bytes) -> str:
    """Paragraphs and table rows in reading order (blocking). Raises ExtractError."""
    import docx  # deferred: python-docx pulls in lxml
    from docx.table import Table

    _check_docx_archive(data)
    try:
        document = docx.Document(io.BytesIO(data))
        lines: list[str] = []
        size = 0
        for block in document.iter_inner_content():  # paragraphs and tables in reading order
            if isinstance(block, Table):
                for row in block.rows:
                    cells: list[str] = []
                    for cell in row.cells:  # merged cells repeat; keep each value once
                        value = cell.text.strip()
                        if value and (not cells or cells[-1] != value):
                            cells.append(value)
                    if cells:
                        lines.append(" | ".join(cells))
                        size += len(lines[-1]) + 1
            else:
                lines.append(block.text)
                size += len(block.text) + 1
            if size >= RAW_MAX_CHARS:
                break
        return "\n".join(lines)[:RAW_MAX_CHARS]
    except Exception as exc:
        raise ExtractError(_MSG_DOCX_DAMAGED) from exc


# --------------------------------------------------------------------------- dispatch

_EXTRACTORS = {"pdf": pdf_text, "docx": docx_text}


def extract(kind: str, data: bytes) -> str:
    """Raw text of a ``kind`` ("pdf" | "docx") document, in this process (blocking)."""
    return _EXTRACTORS[kind](data)


def _damaged(kind: str) -> str:
    return _MSG_PDF_DAMAGED if kind == "pdf" else _MSG_DOCX_DAMAGED


def _memory_limit_bytes() -> int:
    try:
        megabytes = int(os.environ.get(MEMORY_ENV, "").strip() or CHILD_MEMORY_MB)
    except ValueError:
        megabytes = CHILD_MEMORY_MB
    return max(0, megabytes) * 1024 * 1024


def _limit_memory(limit: int) -> None:
    """Cap THIS process's memory at ``limit`` bytes (0 = no cap); past it, allocations raise MemoryError.

    Windows: a job object with JOB_OBJECT_LIMIT_PROCESS_MEMORY (committed memory) that the process
    assigns itself to. Elsewhere: RLIMIT_AS. The parser child calls this before it reads any input.
    """
    if limit <= 0:
        return
    if sys.platform != "win32":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        return
    import ctypes
    from ctypes import wintypes

    class _Basic(ctypes.Structure):  # JOBOBJECT_BASIC_LIMIT_INFORMATION
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
        ]

    class _Extended(ctypes.Structure):  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        _fields_ = [
            ("BasicLimitInformation", _Basic), ("IoInfo", ctypes.c_uint64 * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = kernel32.CreateJobObjectW(None, None)  # the handle lives as long as this short process
    info = _Extended()
    info.BasicLimitInformation.LimitFlags = 0x100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
    info.ProcessMemoryLimit = limit
    if not (
        job
        and kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))  # extended info
        and kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess())
    ):
        raise OSError(ctypes.get_last_error(), "could not limit the parser's memory")


def _child_main() -> None:
    """``python -m app.docparse``: stdin = b"<kind>\\n" + document bytes; stdout = one JSON result."""
    try:
        _limit_memory(_memory_limit_bytes())  # before touching the (untrusted) input
    except (OSError, ValueError):
        pass  # still bounded in time: the parent kills this process at its deadline
    payload = sys.stdin.buffer.read()
    kind, _, data = payload.partition(b"\n")
    name = kind.decode("ascii", "replace")
    try:
        result = {"ok": True, "text": extract(name, data)}
    except ExtractError as exc:
        result = {"ok": False, "error": str(exc)}
    except BaseException:  # MemoryError, RecursionError, unknown kind...: report, never hang the parent
        result = {"ok": False, "error": _damaged(name)}
    sys.stdout.buffer.write(json.dumps(result).encode("ascii"))
    sys.stdout.buffer.flush()


def _child_command() -> tuple[list[str], dict[str, str] | None]:
    """Interpreter + args + env for a parser child.

    In a Windows venv ``sys.executable`` is a small redirector that starts the real interpreter
    as ITS child, so killing it might not stop the parse. Like ``multiprocessing``, launch the
    base interpreter directly and point it at the venv with ``__PYVENV_LAUNCHER__``.
    """
    exe, env = sys.executable, None
    base = getattr(sys, "_base_executable", exe)
    if sys.platform == "win32" and base and base != exe:
        exe, env = base, {**os.environ, "__PYVENV_LAUNCHER__": sys.executable}
    return [exe, "-E", "-s", "-m", "app.docparse"], env


def _spawn() -> subprocess.Popen:
    cmd, env = _child_command()
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=_BACKEND_DIR,  # so "-m app.docparse" finds the package
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _reap(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    with contextlib.suppress(Exception):
        proc.wait(timeout=5)


# Semaphores per event loop (tests run a fresh loop per test): PARSE_CONCURRENCY slots overall, and
# one per owner (client), kept only while someone holds or waits for it.
_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = weakref.WeakKeyDictionary()
_owner_slots: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, weakref.WeakValueDictionary[str, asyncio.Semaphore]
] = weakref.WeakKeyDictionary()


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    slot = _slots.get(loop)
    if slot is None:
        slot = _slots[loop] = asyncio.Semaphore(PARSE_CONCURRENCY)
    return slot


def _owner_semaphore(owner: str) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    owners = _owner_slots.get(loop)
    if owners is None:
        owners = _owner_slots[loop] = weakref.WeakValueDictionary()
    slot = owners.get(owner)
    if slot is None:
        slot = owners[owner] = asyncio.Semaphore(1)
    return slot


@contextlib.asynccontextmanager
async def _parse_slot(owner: str | None):
    """This owner's single slot, then one of the PARSE_CONCURRENCY global ones (always in that order,
    so no deadlock). Waiting for both together is capped at QUEUE_WAIT_S: a queue of slow hostile
    files can't make other people's uploads wait without end."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + QUEUE_WAIT_S
    held: list[asyncio.Semaphore] = []
    try:
        for slot in ([_owner_semaphore(owner)] if owner else []) + [_semaphore()]:
            try:
                async with asyncio.timeout(max(0.0, deadline - loop.time())):
                    await slot.acquire()
            except TimeoutError:
                raise ExtractError(BUSY_MESSAGE) from None
            held.append(slot)
        yield
    finally:
        for slot in reversed(held):
            slot.release()


async def extract_isolated(kind: str, data: bytes, timeout: float, owner: str | None = None) -> str:
    """``extract`` in a killable child process (``python -m app.docparse``, ~0.4 s to start).

    Raises ExtractError (friendly reason) or TimeoutError when the child needs longer than
    ``timeout`` seconds; the child is then killed, so no CPU or memory outlives the request (a
    thread could not be stopped). ``owner`` (the client) gets one parse at a time; waiting for a
    slot doesn't count against ``timeout`` but is capped at QUEUE_WAIT_S ("busy" ExtractError).
    """
    if kind not in _EXTRACTORS:
        raise ExtractError("Unsupported file type. Use PDF, DOCX, TXT or MD.")
    async with _parse_slot(owner):
        proc = await asyncio.to_thread(_spawn)  # CreateProcess blocks for a moment
        try:
            out, _ = await asyncio.to_thread(proc.communicate, kind.encode("ascii") + b"\n" + data, timeout)
        except subprocess.TimeoutExpired:
            raise TimeoutError from None
        finally:
            if proc.poll() is None:  # timed out, or this request was cancelled mid-parse
                proc.kill()
                await asyncio.to_thread(_reap, proc)
    try:
        result = json.loads(out)
    except ValueError:  # killed by the OS (out of memory), crashed interpreter, ...
        raise ExtractError(_damaged(kind)) from None
    if not result.get("ok"):
        raise ExtractError(str(result.get("error") or _damaged(kind)))
    return str(result.get("text") or "")


if __name__ == "__main__":
    _child_main()
