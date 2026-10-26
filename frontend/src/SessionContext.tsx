// Session bootstrap + global SessionState. Wrap the app in <SessionProvider>; read with useSession().
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Navigate } from "react-router-dom";
import { ApiError, createSession, deleteSession, errorMessage, getSession } from "./api";
import { ErrorBanner } from "./components/ErrorBanner";
import { PageLoader } from "./components/Spinner";
import { clearSessionId, clearSessionScopedStorage, clearShortlist, getSessionId, setSessionId } from "./session";
import type { Country, SessionState } from "./types";

export type SessionStatus = "loading" | "ready" | "none" | "error";

export interface SessionContextValue {
  /** Latest SessionState from the server, or null when there is no session. */
  session: SessionState | null;
  /** "loading" while restoring from localStorage; "none" = no/unknown session; "error" = server unreachable. */
  status: SessionStatus;
  /** Message when status === "error". */
  error: string | null;
  /** Replace the state (e.g. with a response that returns SessionState) and persist its id. */
  setSession: (session: SessionState) => void;
  /** Functional local update, e.g. updateSession(s => ({ ...s, context: items })). No-op without a session. */
  updateSession: (fn: (session: SessionState) => SessionState) => void;
  /** Re-fetch GET /api/session/{id}. Resolves null (and clears the id) if the session is gone. */
  refresh: () => Promise<SessionState | null>;
  /** Create a session for `country` (POST /api/session), store its id, and return it. */
  startSession: (country: Country) => Promise<SessionState>;
  /** "Start over": delete the session on the server (best effort; orders stay) and forget it and the
   *  shortlist locally. Navigate to "/" yourself afterwards. */
  reset: () => void;
}

const SessionContext = createContext<SessionContextValue | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [session, setSessionState] = useState<SessionState | null>(null);
  const [status, setStatus] = useState<SessionStatus>(() => (getSessionId() ? "loading" : "none"));
  const [error, setError] = useState<string | null>(null);
  const requestSeq = useRef(0); // ignore responses that arrive after a newer request/reset

  const setSession = useCallback((next: SessionState) => {
    requestSeq.current++;
    setSessionId(next.id);
    setSessionState(next);
    setStatus("ready");
    setError(null);
  }, []);

  const updateSession = useCallback((fn: (s: SessionState) => SessionState) => {
    setSessionState((prev) => (prev ? fn(prev) : prev));
  }, []);

  const refresh = useCallback(async (): Promise<SessionState | null> => {
    const id = getSessionId();
    const seq = ++requestSeq.current;
    if (!id) {
      setSessionState(null);
      setStatus("none");
      return null;
    }
    try {
      const fresh = await getSession(id);
      if (seq === requestSeq.current) {
        setSessionState(fresh);
        setStatus("ready");
        setError(null);
      }
      return fresh;
    } catch (err) {
      if (seq !== requestSeq.current) return null;
      if (err instanceof ApiError && err.status === 404) {
        clearSessionId();
        setSessionState(null);
        setStatus("none");
        return null;
      }
      setStatus("error");
      setError(errorMessage(err));
      throw err;
    }
  }, []);

  const startSession = useCallback(
    async (country: Country) => {
      const created = await createSession(country);
      setSession(created);
      return created;
    },
    [setSession],
  );

  const reset = useCallback(() => {
    requestSeq.current++;
    const id = getSessionId();
    if (id) void deleteSession(id).catch(() => undefined); // uploads' text leaves the server too
    clearSessionId();
    clearShortlist();
    clearSessionScopedStorage();
    setSessionState(null);
    setStatus("none");
    setError(null);
  }, []);

  useEffect(() => {
    if (getSessionId()) refresh().catch(() => undefined);
  }, [refresh]);

  const value = useMemo<SessionContextValue>(
    () => ({ session, status, error, setSession, updateSession, refresh, startSession, reset }),
    [session, status, error, setSession, updateSession, refresh, startSession, reset],
  );
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionContextValue {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession must be used inside <SessionProvider>");
  return ctx;
}

/** Like useSession(), but for pages rendered inside <RequireSession>: `session` is never null. */
export function useRequiredSession(): SessionContextValue & { session: SessionState } {
  const ctx = useSession();
  if (!ctx.session) throw new Error("useRequiredSession must be used inside <RequireSession>");
  return ctx as SessionContextValue & { session: SessionState };
}

/** Route guard: shows a loader while restoring, redirects to "/" when there is no session. */
export function RequireSession({ children }: { children: ReactNode }) {
  const { session, status, error, refresh } = useSession();
  if (status === "loading") return <PageLoader label="Restoring your session…" />;
  if (status === "error" && !session) {
    return (
      <div className="container section">
        <ErrorBanner message={error ?? "Couldn't load your session."} onRetry={() => void refresh().catch(() => undefined)} />
      </div>
    );
  }
  if (!session) return <Navigate to="/" replace />;
  return <>{children}</>;
}
