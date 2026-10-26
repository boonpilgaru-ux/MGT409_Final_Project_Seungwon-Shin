// Shop search job + cache. The web search takes 30-75 s and the server finishes it even if the
// browser stops listening, so we keep ONE in-flight request per session at module level: React
// StrictMode double-mounts and leaving/returning to /shop re-attach to it instead of starting a
// second search. The Profile page starts it as soon as the profile is revealed (prefetchShop), so
// the wait overlaps with reading the profile. Results are cached in localStorage per session +
// profile so revisits are instant. (A reload mid-search re-posts; the server joins the running one.)
import { suggestProducts } from "../../api";
import type { Product, Profile } from "../../types";
import { hashString } from "./format";

const CACHE_KEY = "tee.shop";

interface ShopCache {
  sessionId: string;
  profileKey: string;
  products: Product[];
  fetchedAt: number;
}

export interface SearchJob {
  sessionId: string;
  profileKey: string;
  startedAt: number;
  promise: Promise<ShopCache>;
}

let current: SearchJob | null = null;

/** Products are tied to the profile they were found for; a regenerated profile means a new search. */
export function profileKey(profile: Profile): string {
  return hashString(`${profile.archetype}|${profile.tagline}|${profile.tshirt_brief}`).toString(36);
}

export function readShopCache(sessionId: string, key: string): ShopCache | null {
  try {
    const raw = window.localStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const data = JSON.parse(raw) as Partial<ShopCache>;
    if (data.sessionId !== sessionId || data.profileKey !== key || !Array.isArray(data.products)) return null;
    return { sessionId, profileKey: key, products: data.products, fetchedAt: Number(data.fetchedAt) || Date.now() };
  } catch {
    return null;
  }
}

/** Start the shop search in the background for a freshly revealed profile, unless it's cached or running. */
export function prefetchShop(sessionId: string, key: string): void {
  if (readShopCache(sessionId, key) || pendingSearch(sessionId, key)) return;
  startSearch(sessionId, key).promise.catch(() => undefined); // the Shop page shows any error itself
}

function writeShopCache(entry: ShopCache): void {
  try {
    window.localStorage.setItem(CACHE_KEY, JSON.stringify(entry)); // single entry: newest session wins
  } catch {
    /* storage full or disabled: the in-memory result still works */
  }
}

/** The search currently running for this session (and this profile, when `key` is given), if any. */
export function pendingSearch(sessionId: string, key?: string): SearchJob | null {
  return current && current.sessionId === sessionId && (key === undefined || current.profileKey === key) ? current : null;
}

/**
 * Start a search, or join the one already running for this session and profile. A search for an
 * older profile (e.g. the profile was regenerated, or prefetched before) is never handed back:
 * the new search starts once that one settles, so its results can't be shown for the wrong profile.
 */
export function startSearch(sessionId: string, key: string): SearchJob {
  const running = pendingSearch(sessionId);
  if (running && running.profileKey === key) return running;
  const previous: Promise<unknown> = running ? running.promise.catch(() => undefined) : Promise.resolve();

  const job: SearchJob = {
    sessionId,
    profileKey: key,
    startedAt: Date.now(),
    promise: previous
      .then(() => suggestProducts(sessionId))
      .then((res) => {
        const entry: ShopCache = { sessionId, profileKey: key, products: res.products, fetchedAt: Date.now() };
        writeShopCache(entry);
        return entry;
      })
      .finally(() => {
        if (current === job) current = null;
      }),
  };
  current = job;
  return job;
}
