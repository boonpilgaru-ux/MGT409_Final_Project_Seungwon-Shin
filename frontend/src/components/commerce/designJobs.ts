// Design-generation jobs (POST /api/design/{id}/generate, ~12-60 s). Like shopSearch.ts, one job
// per session lives at module level so StrictMode double-mounts and Studio revisits re-attach to
// it instead of generating twice. The job merges its result into the shared SessionState itself,
// so the designs land even if the user has navigated away by the time it finishes (which is what
// lets the Profile page start the first concepts early, see prefetchDesigns).
import { generateDesigns } from "../../api";
import type { Design, DesignGenerateRequest } from "../../types";

export type DesignJobKind = "initial" | "more" | "revise";

export interface DesignJob {
  sessionId: string;
  kind: DesignJobKind;
  req: DesignGenerateRequest;
  startedAt: number;
  promise: Promise<Design[]>;
}

const jobs = new Map<string, DesignJob>();

/** Append designs that aren't already present (by id). */
export function mergeDesigns(existing: Design[], incoming: Design[]): Design[] {
  const seen = new Set(existing.map((d) => d.id));
  return [...existing, ...incoming.filter((d) => !seen.has(d.id))];
}

export function pendingDesignJob(sessionId: string): DesignJob | null {
  return jobs.get(sessionId) ?? null;
}

/**
 * Start a generation (or return the one already running for this session).
 * `onDesigns` receives the NEW designs once, when the request succeeds.
 */
export function startDesignJob(
  sessionId: string,
  kind: DesignJobKind,
  req: DesignGenerateRequest,
  onDesigns: (designs: Design[]) => void,
): DesignJob {
  const running = jobs.get(sessionId);
  if (running) return running;

  const job: DesignJob = {
    sessionId,
    kind,
    req,
    startedAt: Date.now(),
    promise: generateDesigns(sessionId, req)
      .then((res) => {
        onDesigns(res.designs);
        return res.designs;
      })
      .finally(() => {
        if (jobs.get(sessionId) === job) jobs.delete(sessionId);
      }),
  };
  jobs.set(sessionId, job);
  return job;
}

// ------------------------------------------------------------------ prefetch from the Profile page
// The first 3 concepts take ~12-17 s. The Profile page starts them in the background as soon as
// the profile is shown (like the shop search), so Studio usually opens with them ready. One record
// per browser ("tee.designPrefetch", newest session wins) remembers which profile they were drawn
// for and whether Studio has been opened since: if the profile was regenerated before the first
// Studio visit, Studio draws fresh concepts for the current profile instead of showing stale ones.
const PREFETCH_KEY = "tee.designPrefetch";

interface DesignPrefetch {
  sessionId: string;
  profileKey: string;
  studioOpened: boolean;
}

function readPrefetch(sessionId: string): DesignPrefetch | null {
  try {
    const data = JSON.parse(window.localStorage.getItem(PREFETCH_KEY) ?? "null") as Partial<DesignPrefetch> | null;
    if (!data || data.sessionId !== sessionId || typeof data.profileKey !== "string") return null;
    return { sessionId, profileKey: data.profileKey, studioOpened: Boolean(data.studioOpened) };
  } catch {
    return null;
  }
}

function writePrefetch(entry: DesignPrefetch): void {
  try {
    window.localStorage.setItem(PREFETCH_KEY, JSON.stringify(entry));
  } catch {
    /* storage unavailable: Studio then behaves as if nothing was prefetched */
  }
}

/**
 * Profile page: start the first 3 concepts for a revealed profile in the background, unless the
 * session already has designs, a design job is running, or Studio was already opened.
 */
export function prefetchDesigns(
  sessionId: string,
  profileKey: string,
  hasDesigns: boolean,
  onDesigns: (designs: Design[]) => void,
): void {
  if (hasDesigns || pendingDesignJob(sessionId) || readPrefetch(sessionId)?.studioOpened) return;
  startDesignJob(sessionId, "initial", { feedback: null, base_design_id: null, count: 3 }, onDesigns).promise.catch(
    () => undefined, // Studio shows (and retries) any error itself
  );
  writePrefetch({ sessionId, profileKey, studioOpened: false });
}

/** Studio, first visit: were the existing / running concepts prefetched for an older profile? */
export function prefetchIsStale(sessionId: string, profileKey: string): boolean {
  const prior = readPrefetch(sessionId);
  return Boolean(prior && !prior.studioOpened && prior.profileKey !== profileKey);
}

/** Studio has shown concepts for this profile: later profile changes no longer redraw them. */
export function markStudioOpened(sessionId: string, profileKey: string): void {
  writePrefetch({ sessionId, profileKey, studioOpened: true });
}
