"""Live end-to-end smoke test against a RUNNING Tee:dentity API (real Portkey, real web).

Opt-in and slow (~3-5 minutes, real model calls cost money). Start the API first, then from backend/:

    .\\.venv\\Scripts\\python.exe -m uvicorn app.main:app --port 8000
    .\\.venv\\Scripts\\python.exe scripts\\smoke_live.py                  # full run
    .\\.venv\\Scripts\\python.exe scripts\\smoke_live.py --skip-shop      # skip the ~75 s web search

Flow: health -> session -> 10 quiz rounds (alternating A/B, with a simulated "think time" so the
speculative prefetch has time to work) -> about-you text + URL -> profile -> 3 designs -> 1 revision
-> order -> request -> print.svg -> shop suggest. Prints one line per step with status and timing.

This script never reads or prints the Portkey key; the server holds it. Exit code 0 = every step OK.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import httpx

# The console may not be UTF-8 on Windows (Hangul sublabels, emoji); never crash on output.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

QUIZ_TOTAL = 10


@dataclass
class Step:
    name: str
    status: int | str
    seconds: float
    note: str = ""
    ok: bool = True


@dataclass
class Report:
    steps: list[Step] = field(default_factory=list)

    def add(self, name: str, status: int | str, seconds: float, note: str = "", ok: bool = True) -> None:
        step = Step(name, status, seconds, note, ok)
        self.steps.append(step)
        mark = "ok  " if ok else "FAIL"
        print(f"[{mark}] {seconds:6.1f}s  {str(status):>4}  {name:<24} {note}", flush=True)

    @property
    def failed(self) -> list[Step]:
        return [s for s in self.steps if not s.ok]


class Api:
    """Thin JSON client that times every call and never raises on HTTP errors."""

    def __init__(self, base: str, timeout: float) -> None:
        self.client = httpx.Client(base_url=base.rstrip("/") + "/api", timeout=timeout)

    def call(self, method: str, path: str, **kwargs: Any) -> tuple[int, Any, float, httpx.Response | None]:
        started = time.perf_counter()
        try:
            resp = self.client.request(method, path, **kwargs)
        except httpx.HTTPError as err:
            return 0, {"detail": f"{type(err).__name__}: {err}"}, time.perf_counter() - started, None
        elapsed = time.perf_counter() - started
        try:
            body = resp.json()
        except ValueError:
            body = None
        return resp.status_code, body, elapsed, resp


def _detail(body: Any) -> str:
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])[:160]
    return ""


def _short(text: str | None, limit: int = 60) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _option(opt: dict) -> str:
    sub = f" [{opt['sublabel']}]" if opt.get("sublabel") else ""
    img = "img" if opt.get("image") else "no-img"
    return f"{opt['label']}{sub} ({img})"


def run(args: argparse.Namespace) -> int:
    api = Api(args.base, args.timeout)
    report = Report()
    print(f"Tee:dentity live smoke -> {args.base}  (country {args.country}, think {args.think}s)\n")

    # ------------------------------------------------------------------ health + session
    status, body, secs, _ = api.call("GET", "/health")
    key_set = bool(body and body.get("portkey_key_set"))
    report.add("health", status, secs, f"portkey_key_set={key_set}", ok=status == 200 and key_set)
    if status != 200 or not key_set:
        print("\nThe API isn't reachable or has no Portkey key; aborting.")
        return 1

    status, body, secs, _ = api.call("POST", "/session", json={"country": {"code": args.country, "name": args.country_name}})
    report.add("session.create", status, secs, body.get("id", "") if status == 200 else _detail(body), ok=status == 200)
    if status != 200:
        return 1
    sid = body["id"]

    # ------------------------------------------------------------------ balance game
    status, body, secs, _ = api.call("POST", f"/quiz/{sid}/start")
    report.add("quiz.start", status, secs, _short(body.get("prompt")) if status == 200 else _detail(body), ok=status == 200)
    if status != 200:
        return 1
    question = body
    images = Counter()
    answer_times: list[float] = []
    local_scripts = 0
    for round_no in range(1, QUIZ_TOTAL + 1):
        a, b = question["option_a"], question["option_b"]
        images.update([bool(a.get("image")), bool(b.get("image"))])
        local_scripts += any(o.get("sublabel") for o in (a, b))
        if args.verbose:
            print(f"        R{question['index']:>2} {question['depth']:<8} {question['prompt']}")
            print(f"            A: {_option(a)}\n            B: {_option(b)}")
        if question["index"] != round_no:
            report.add("quiz.sequence", "-", 0, f"expected round {round_no}, got {question['index']}", ok=False)
            return 1
        time.sleep(args.think)  # a human reads the cards; prefetch runs meanwhile
        choice = "A" if round_no % 2 else "B"
        status, body, secs, _ = api.call(
            "POST", f"/quiz/{sid}/answer", json={"question_id": question["id"], "choice": choice}
        )
        answer_times.append(secs)
        ok = status == 200 and bool(body) and body["done"] == (round_no == QUIZ_TOTAL)
        nxt = body.get("next") if ok else None
        note = f"-> R{nxt['index']} {_short(nxt['prompt'], 40)}" if nxt else ("done" if ok else _detail(body))
        report.add(f"quiz.answer R{round_no} ({choice})", status, secs, note, ok=ok)
        if not ok:
            return 1
        question = nxt

    fast = sum(1 for t in answer_times[:-1] if t < 3.0)
    print(
        f"        answers: median {statistics.median(answer_times):.1f}s, max {max(answer_times):.1f}s, "
        f"{fast}/{QUIZ_TOTAL - 1} under 3 s (prefetch hits); images {images[True]}/{sum(images.values())}; "
        f"rounds with native-script sublabels {local_scripts}/{QUIZ_TOTAL}"
    )

    status, body, secs, _ = api.call("GET", f"/session/{sid}")
    ok = status == 200 and body["quiz_done"] and len(body["answers"]) == QUIZ_TOTAL and body["current_question"] is None
    # Hidden engine data (hypothesis, dimensions, scratch space) must never reach the client.
    leaked = [k for k in ("hypothesis", "extra", "products") if k in body]
    leaked += ["dimension"] if any("dimension" in a["question"] for a in body.get("answers", [])) else []
    note = f"quiz_done={body.get('quiz_done')} answers={len(body.get('answers', []))}"
    report.add("session.get", status, secs, note + (f" LEAKED {leaked}" if leaked else ""), ok=ok and not leaked)

    # ------------------------------------------------------------------ about you
    note_text = (
        "I collect old Seoul subway maps and film photos from Euljiro print alleys. Weekends I cycle "
        "along the Han River and end at a pojangmacha. I like quiet, well-made things more than logos."
    )
    status, body, secs, _ = api.call("POST", f"/context/{sid}", files={"text": (None, note_text)})
    items = body.get("items", []) if status == 200 else []
    txt = next((i for i in items if i["kind"] == "text"), None)
    report.add(
        "context.text", status, secs,
        f"{txt['status']}: {_short(txt['summary'], 70)}" if txt else _detail(body),
        ok=status == 200 and txt is not None and txt["status"] == "ok",
    )

    for url in args.urls:
        status, body, secs, _ = api.call("POST", f"/context/{sid}", files={"urls": (None, json.dumps([url]))})
        items = body.get("items", []) if status == 200 else []
        item = next((i for i in reversed(items) if i["kind"] == "url"), None)
        detail = f"{item['status']}: {_short(item['summary'] or item['error'], 70)}" if item else _detail(body)
        # An error item is still a correct response (the site refused us); only HTTP failures fail the step.
        report.add("context.url", status, secs, f"{_short(url, 40)} {detail}", ok=status == 200 and item is not None)

    # ------------------------------------------------------------------ profile
    status, body, secs, _ = api.call("POST", f"/profile/{sid}")
    ok = status == 200 and 5 <= len(body.get("traits", [])) <= 7 and 4 <= len(body.get("taste", {}).get("colors", [])) <= 6
    note = (
        f"\"{body['archetype']}\" traits={len(body['traits'])} colors={len(body['taste']['colors'])}"
        if status == 200 else _detail(body)
    )
    report.add("profile.build", status, secs, note, ok=ok)
    if status != 200:
        return 1
    if args.verbose:
        print(f"        tagline: {body['tagline']}\n        brief: {_short(body['tshirt_brief'], 200)}")

    # ------------------------------------------------------------------ studio
    status, body, secs, _ = api.call("POST", f"/design/{sid}/generate", json={"feedback": None, "base_design_id": None, "count": 3})
    designs = body.get("designs", []) if status == 200 else []
    good = [d for d in designs if d["svg"].startswith("<svg") and "<script" not in d["svg"].lower()]
    report.add(
        "design.generate x3", status, secs,
        " | ".join(f"{_short(d['title'], 28)} ({len(d['svg']) // 1024 + 1} KB)" for d in designs) or _detail(body),
        ok=status == 200 and len(good) == len(designs) >= 2,
    )
    if not designs:
        return 1

    base = designs[0]
    status, body, secs, _ = api.call(
        "POST", f"/design/{sid}/generate",
        json={"feedback": "Bolder, and add a little Hangul lettering.", "base_design_id": base["id"], "count": 1},
    )
    revised = body.get("designs", []) if status == 200 else []
    report.add(
        "design.revise", status, secs,
        f"{_short(revised[0]['title'], 40)}" if revised else _detail(body), ok=status == 200 and len(revised) == 1,
    )
    chosen = revised[0] if revised else base

    # ------------------------------------------------------------------ orders
    spec = dict(chosen["recommended_spec"], quantity=12, size="L")
    status, body, secs, _ = api.call("POST", "/orders", json={"session_id": sid, "design_id": chosen["id"], "spec": spec})
    ok = status == 200 and body.get("status") == "draft"
    report.add("order.create", status, secs, f"{body['id']} ${body['price']['total']:.2f} for 12" if ok else _detail(body), ok=ok)
    if not ok:
        return 1
    order_id = body["id"]

    status, body, secs, _ = api.call("GET", f"/orders/{order_id}")
    report.add("order.get", status, secs, body.get("status", _detail(body)) if body else "", ok=status == 200)

    customer = {"name": "Smoke Test", "email": "smoke@example.com", "address": "1 Test Street, Seoul"}
    status, body, secs, _ = api.call("POST", f"/orders/{order_id}/request", json=customer)
    report.add("order.request", status, secs, body.get("status", "") if status == 200 else _detail(body), ok=status == 200 and body.get("status") == "requested")

    status, _, secs, resp = api.call("GET", f"/orders/{order_id}/print.svg")
    ctype = resp.headers.get("content-type", "") if resp is not None else ""
    size = len(resp.content) if resp is not None else 0
    report.add(
        "order.print.svg", status, secs, f"{ctype.split(';')[0]} {size // 1024 + 1} KB",
        ok=status == 200 and ctype.startswith("image/svg+xml") and resp is not None and resp.text.lstrip().startswith("<svg"),
    )

    # ------------------------------------------------------------------ shop
    if args.skip_shop:
        print("        (shop skipped)")
    else:
        status, body, secs, _ = api.call("POST", f"/shop/{sid}/suggest")
        products = body.get("products", []) if status == 200 else []
        origins = Counter(p["origin"] for p in products)
        with_img = sum(1 for p in products if p.get("image_url"))
        report.add(
            "shop.suggest", status, secs,
            f"{len(products)} products {dict(origins)} images {with_img}/{len(products)}" if status == 200 else _detail(body),
            ok=status == 200 and len(products) >= 1,
        )
        for p in products[: args.show_products]:
            price = f"{p['price']} {p['currency'] or ''}".strip() if p.get("price") else "n/a"
            print(f"        - [{p['origin']:<6}] {_short(p['brand'], 18)} | {_short(p['title'], 46)} | {price} | {_short(p['url'], 70)}")

    # ------------------------------------------------------------------ summary
    total = sum(s.seconds for s in report.steps)
    print(f"\n{len(report.steps) - len(report.failed)}/{len(report.steps)} steps ok, {total:.0f}s of API time. Session {sid}")
    for s in report.failed:
        print(f"  FAILED: {s.name} ({s.status}) {s.note}")
    return 1 if report.failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="API origin (default %(default)s)")
    parser.add_argument("--country", default="KR", help="ISO alpha-2 code (default %(default)s)")
    parser.add_argument("--country-name", default="South Korea")
    parser.add_argument("--think", type=float, default=3.0, help="seconds to 'read' each question (default %(default)s)")
    parser.add_argument("--url", dest="urls", action="append", help="about-you link(s); repeatable")
    parser.add_argument("--skip-shop", action="store_true", help="skip the slow live web search")
    parser.add_argument("--show-products", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=200.0, help="per-request timeout in seconds")
    parser.add_argument("-v", "--verbose", action="store_true", help="print every question and profile text")
    args = parser.parse_args()
    args.urls = args.urls or ["https://en.wikipedia.org/wiki/Seoul"]
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
