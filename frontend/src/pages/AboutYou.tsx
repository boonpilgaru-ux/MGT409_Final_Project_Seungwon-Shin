// About you (SPEC §2.3), optional. One chat-style composer: free text (links in it are detected
// and sent as URLs) plus attached files (📎 picker or drag & drop anywhere on the composer).
// Submitted items come back with a status badge and a one-line summary; each can be deleted.
import { useMemo, useRef, useState, type KeyboardEvent } from "react";
import { useNavigate } from "react-router-dom";
import { addContext, ApiError, errorMessage, removeContext } from "../api";
import { ErrorBanner } from "../components/ErrorBanner";
import { ContextItemPending, ContextItemRow } from "../components/journey/ContextItemRow";
import { ACCEPT_ATTR, ACCEPTED_EXTENSIONS, useFileDrop } from "../components/journey/fileDrop";
import { useAliveRef, useLatest } from "../components/journey/hooks";
import { formatBytes } from "../components/journey/journeyUtils";
import { extractLinks, walledSite } from "../components/journey/links";
import { markProfileStale } from "../components/journey/profileFreshness";
import { RequireQuizDone } from "../components/journey/RequireQuizDone";
import { Spinner } from "../components/Spinner";
import { useRequiredSession } from "../SessionContext";
import "../styles/journey.css";
import type { ContextItem, ContextKind } from "../types";

// Mirrors backend models.py CONTEXT_* limits (per session).
const MAX_FILES = 5;
const MAX_URLS = 5;
const MAX_NOTES = 5;
const MAX_FILE_MB = 10;
const MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024;
const MAX_TEXT = 20_000;

interface Failure {
  message: string;
  retry?: () => void;
}

const extension = (name: string) => {
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot).toLowerCase() : "";
};

export default function AboutYouPage() {
  return (
    <RequireQuizDone>
      <AboutYou />
    </RequireQuizDone>
  );
}

function AboutYou() {
  const { session, updateSession, refresh } = useRequiredSession();
  const navigate = useNavigate();
  const alive = useAliveRef();
  const sessionId = session.id;

  const [files, setFiles] = useState<File[]>([]);
  const [text, setText] = useState("");
  const [fileNotes, setFileNotes] = useState<string[]>([]);
  const [processing, setProcessing] = useState<Array<{ kind: ContextKind; name: string }>>([]);
  const [removing, setRemoving] = useState<ReadonlySet<string>>(new Set());
  const [failure, setFailure] = useState<Failure | null>(null);
  const pickerRef = useRef<HTMLInputElement>(null);

  const items = session.context;
  const savedFiles = items.filter((i) => i.kind === "file").length;
  const savedUrls = items.filter((i) => i.kind === "url").length;
  const notesFull = items.filter((i) => i.kind === "text").length >= MAX_NOTES;
  const filesLeft = MAX_FILES - savedFiles - files.length;
  const urlRoom = Math.max(0, MAX_URLS - savedUrls);
  const { urls, rest: notes, overflow } = useMemo(() => extractLinks(text, urlRoom), [text, urlRoom]);
  const textBlocked = notesFull && notes.length > 0; // only 5 notes per session; links and files still go through
  const hasPending = files.length > 0 || urls.length > 0 || notes.length > 0;
  const busy = processing.length > 0;
  const canAdd = hasPending && !busy && !textBlocked;

  const addFiles = (incoming: File[]) => {
    const notes: string[] = [];
    const next = [...files];
    for (const file of incoming) {
      if (!ACCEPTED_EXTENSIONS.includes(extension(file.name))) {
        notes.push(`“${file.name}” isn’t a PDF, DOCX, TXT or MD file.`);
      } else if (file.size > MAX_FILE_BYTES) {
        notes.push(`“${file.name}” is ${formatBytes(file.size)}. The limit is ${MAX_FILE_MB} MB.`);
      } else if (file.size === 0) {
        notes.push(`“${file.name}” is empty.`);
      } else if (next.some((f) => f.name === file.name && f.size === file.size)) {
        notes.push(`“${file.name}” is already in the list.`);
      } else if (savedFiles + next.length >= MAX_FILES) {
        notes.push(`Skipped “${file.name}”: you can add up to ${MAX_FILES} files.`);
      } else {
        next.push(file);
      }
    }
    setFiles(next);
    setFileNotes(notes);
  };

  const drop = useFileDrop(addFiles, busy || filesLeft <= 0);

  /** Send everything pending in one multipart request. Resolves true on success. */
  const submit = async (): Promise<boolean> => {
    if (!canAdd) return false;
    setProcessing([
      ...files.map((f) => ({ kind: "file" as const, name: f.name })),
      ...urls.map((u) => ({ kind: "url" as const, name: u.replace(/^https?:\/\//, "") })),
      ...(notes ? [{ kind: "text" as const, name: "Notes" }] : []),
    ]);
    setFailure(null);
    try {
      const res = await addContext(sessionId, { files, urls, text: notes || null });
      // Saved server-side: sync shared state even if the user already left this page.
      updateSession((s) => (s.id === sessionId ? { ...s, context: res.items } : s));
      if (session.profile) markProfileStale(sessionId);
      if (!alive.current) return false;
      setFiles([]);
      setText("");
      setFileNotes([]);
      return true;
    } catch (err) {
      if (!alive.current) return false;
      if (err instanceof ApiError && err.isNotFound) {
        void refresh().catch(() => undefined);
        return false;
      }
      const retryable = err instanceof ApiError && (err.isUpstream || err.status === 0 || err.status >= 500);
      setFailure({ message: errorMessage(err), retry: retryable ? () => void latest.current.submit() : undefined });
      return false;
    } finally {
      if (alive.current) setProcessing([]);
    }
  };

  const remove = async (item: ContextItem) => {
    setRemoving((s) => new Set(s).add(item.id));
    setFailure(null);
    try {
      const res = await removeContext(sessionId, item.id);
      updateSession((s) => (s.id === sessionId ? { ...s, context: res.items } : s));
      if (session.profile) markProfileStale(sessionId);
    } catch (err) {
      if (!alive.current) return;
      if (err instanceof ApiError && err.isNotFound) {
        void refresh().catch(() => undefined); // already gone (or session gone): resync
      } else {
        setFailure({ message: errorMessage(err), retry: () => void latest.current.remove(item) });
      }
    } finally {
      if (alive.current) {
        setRemoving((s) => {
          const next = new Set(s);
          next.delete(item.id);
          return next;
        });
      }
    }
  };

  const latest = useLatest({ submit, remove });

  const walledNotes = [...new Set(urls.map(walledSite).filter((n): n is string => n !== null))];

  const onComposerKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      void submit();
    }
  };

  const onContinue = async () => {
    if (textBlocked) {
      setFailure({ message: `You’ve already added ${MAX_NOTES} notes. Delete one, or clear the text box, to continue.` });
      return;
    }
    if (hasPending && !(await submit())) return;
    if (alive.current) navigate("/profile");
  };

  const onSkip = () => {
    if (hasPending && !window.confirm("Skip without adding what you’ve entered here?")) return;
    navigate("/profile");
  };

  return (
    <section className="container section j-about">
      <header className="j-about__head j-about__head--split">
        <div className="stack-sm">
          <p className="eyebrow">Optional · Totally skippable</p>
          <h1>
            Tell us more about <span className="accent">you</span>.
          </h1>
          <p className="lead">
            A resume, an essay, a portfolio, a playlist. Anything you share helps your profile go from “pretty close”
            to “how did it know that?”
          </p>
        </div>
        <button
          type="button"
          className="btn btn-primary btn-lg j-about__top-cta"
          onClick={() => void onContinue()}
          disabled={busy}
        >
          {hasPending ? "Add & see my profile →" : "See my profile →"}
        </button>
      </header>

      <div className="j-about__grid">
        <div className="stack">
          <div
            className={`card j-compose${drop.over ? " is-over" : ""}${busy ? " is-busy" : ""}`}
            {...drop.handlers}
          >
            <label className="visually-hidden" htmlFor="j-compose-text">
              Tell us about yourself
            </label>
            <textarea
              id="j-compose-text"
              className="j-compose__text"
              rows={5}
              maxLength={MAX_TEXT}
              placeholder="Tell us anything: favorite bands, the shirt you never take off, a color you hate… Paste links to your Instagram, portfolio or blog too."
              value={text}
              disabled={busy}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={onComposerKey}
            />

            {(files.length > 0 || urls.length > 0) && (
              <ul className="j-compose__chips" aria-label="Ready to add">
                {files.map((file) => (
                  <li key={`${file.name}-${file.size}`} className="j-compose__chip">
                    <span aria-hidden="true">📄</span>
                    <span className="j-compose__chip-name" title={file.name}>
                      {file.name}
                    </span>
                    <span className="tiny muted">{formatBytes(file.size)}</span>
                    <button
                      type="button"
                      className="j-compose__chip-x"
                      aria-label={`Remove ${file.name}`}
                      onClick={() => setFiles((list) => list.filter((f) => f !== file))}
                      disabled={busy}
                    >
                      ×
                    </button>
                  </li>
                ))}
                {urls.map((url) => (
                  <li key={url} className="j-compose__chip j-compose__chip--link" title={url}>
                    <span aria-hidden="true">🔗</span>
                    <span className="j-compose__chip-name">{url.replace(/^https?:\/\//, "").replace(/\/$/, "")}</span>
                  </li>
                ))}
              </ul>
            )}

            <div className="j-compose__bar">
              <button
                type="button"
                className="btn btn-sm btn-ghost j-compose__attach"
                onClick={() => pickerRef.current?.click()}
                disabled={busy || filesLeft <= 0}
                title={`PDF, DOCX, TXT or MD · up to ${MAX_FILE_MB} MB each · ${Math.max(0, filesLeft)} of ${MAX_FILES} left`}
              >
                <span aria-hidden="true">📎</span> Attach resume / essay
              </button>
              <input
                ref={pickerRef}
                className="visually-hidden"
                type="file"
                multiple
                accept={ACCEPT_ATTR}
                tabIndex={-1}
                aria-hidden="true"
                onChange={(e) => {
                  const picked = Array.from(e.target.files ?? []);
                  e.target.value = ""; // allow re-picking the same file after removing it
                  if (picked.length) addFiles(picked);
                }}
              />
              <span className="hint j-compose__hint">
                {drop.over
                  ? "Drop it like it’s hot."
                  : text.length > MAX_TEXT * 0.8
                    ? `${text.length.toLocaleString()} / ${MAX_TEXT.toLocaleString()}`
                    : `PDF, DOCX, TXT, MD · ≤ ${MAX_FILE_MB} MB · ${Math.max(0, filesLeft)}/${MAX_FILES} files, ${Math.max(0, urlRoom - urls.length)}/${MAX_URLS} links left`}
              </span>
              <button type="button" className="btn btn-blue j-compose__send" onClick={() => void submit()} disabled={!canAdd}>
                {busy ? (
                  <>
                    <Spinner size={16} label="Adding" /> Reading…
                  </>
                ) : (
                  "Add"
                )}
              </button>
            </div>
          </div>

          {(fileNotes.length > 0 || walledNotes.length > 0 || overflow > 0 || textBlocked) && (
            <ul className="j-notes" role="alert">
              {fileNotes.map((note) => (
                <li key={note}>{note}</li>
              ))}
              {walledNotes.map((name) => (
                <li key={name}>{name} often hides profiles behind a login, so we may only get the basics.</li>
              ))}
              {overflow > 0 && (
                <li>
                  You can add up to {MAX_URLS} links. {overflow === 1 ? "One extra link" : `${overflow} extra links`} will
                  be kept as plain text.
                </li>
              )}
              {textBlocked && (
                <li>
                  You’ve added {MAX_NOTES} notes. Delete one to add more text. Links and files still work.
                </li>
              )}
            </ul>
          )}

          {failure && <ErrorBanner message={failure.message} onRetry={failure.retry} onDismiss={() => setFailure(null)} />}

          <p className="tiny muted j-about__privacy">
            We only use this to shape your profile. Please skip anything sensitive (IDs, passwords, addresses).
          </p>
        </div>

        <aside className="j-about__side" aria-labelledby="j-about-items">
          <div className="card stack">
            <div className="row-between">
              <h2 id="j-about-items" className="j-about__h">
                What we’ve got
              </h2>
              <span className="chip chip-soft">{items.length}</span>
            </div>
            {items.length === 0 && processing.length === 0 ? (
              <div className="j-about__empty">
                <span aria-hidden="true">🗂️</span>
                <p className="small muted">
                  Nothing yet. Everything you add shows up here with a one-line read of what it says about you.
                </p>
              </div>
            ) : (
              <ul className="j-ctx-list" aria-live="polite" aria-busy={busy}>
                {items.map((item) => (
                  <ContextItemRow
                    key={item.id}
                    item={item}
                    removing={removing.has(item.id)}
                    onRemove={(i) => void remove(i)}
                  />
                ))}
                {processing.map((p, i) => (
                  <ContextItemPending key={`pending-${i}`} kind={p.kind} name={p.name} />
                ))}
              </ul>
            )}
          </div>
        </aside>
      </div>

      <footer className="j-about__footer">
        <button type="button" className="btn btn-ghost" onClick={onSkip} disabled={busy}>
          Skip for now
        </button>
        <button type="button" className="btn btn-primary btn-lg" onClick={() => void onContinue()} disabled={busy}>
          {hasPending ? "Add & continue →" : "Continue to my profile →"}
        </button>
      </footer>
    </section>
  );
}
