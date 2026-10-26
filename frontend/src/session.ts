// localStorage helpers: session id ("tee.session") and shop shortlist ("tee.shortlist").
// For the live SessionState use `useSession()` from SessionContext.tsx instead.
import { useEffect, useState } from "react";
import type { Product } from "./types";

export const SESSION_KEY = "tee.session";
export const SHORTLIST_KEY = "tee.shortlist";
/** Order request tokens ({orderId: token}): this browser placed that request and may update it. */
export const ORDER_TOKENS_KEY = "tee.orderTokens";
const MAX_ORDER_TOKENS = 50;
const SHORTLIST_EVENT = "tee:shortlist";

function safeGet(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null; // storage disabled (private mode, etc.)
  }
}

function safeSet(key: string, value: string | null): void {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    /* ignore quota / disabled storage */
  }
}

// ---------------------------------------------------------------- session id
export const getSessionId = (): string | null => safeGet(SESSION_KEY);
export const setSessionId = (id: string): void => safeSet(SESSION_KEY, id);
export const clearSessionId = (): void => safeSet(SESSION_KEY, null);

/** Page caches tied to a session: shop results, studio picks, profile-stale flags ("Start over"). */
const SESSION_SCOPED_KEYS = ["tee.shop", "tee.studio"];
const SESSION_SCOPED_PREFIXES = ["tee.profileStale."];

export function clearSessionScopedStorage(): void {
  try {
    const storage = window.localStorage;
    const doomed = [...SESSION_SCOPED_KEYS];
    for (let i = 0; i < storage.length; i++) {
      const key = storage.key(i);
      if (key && SESSION_SCOPED_PREFIXES.some((p) => key.startsWith(p))) doomed.push(key);
    }
    doomed.forEach((key) => storage.removeItem(key));
  } catch {
    /* storage disabled: nothing to clear */
  }
}

// ---------------------------------------------------------------- order request tokens
function readOrderTokens(): Record<string, string> {
  try {
    const parsed: unknown = JSON.parse(safeGet(ORDER_TOKENS_KEY) ?? "{}");
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? (parsed as Record<string, string>) : {};
  } catch {
    return {};
  }
}

export const getOrderToken = (orderId: string): string | null => {
  const token = readOrderTokens()[orderId];
  return typeof token === "string" && token ? token : null;
};

export function setOrderToken(orderId: string, token: string): void {
  const entries = Object.entries(readOrderTokens()).filter(([id]) => id !== orderId);
  entries.push([orderId, token]);
  safeSet(ORDER_TOKENS_KEY, JSON.stringify(Object.fromEntries(entries.slice(-MAX_ORDER_TOKENS))));
}

// ---------------------------------------------------------------- shortlist (keyed by product URL)
export function getShortlist(): Product[] {
  const raw = safeGet(SHORTLIST_KEY);
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as Product[]).filter((p) => p && typeof p.url === "string") : [];
  } catch {
    return [];
  }
}

function writeShortlist(items: Product[]): Product[] {
  safeSet(SHORTLIST_KEY, JSON.stringify(items));
  window.dispatchEvent(new Event(SHORTLIST_EVENT));
  return items;
}

export const isShortlisted = (product: Pick<Product, "url">): boolean =>
  getShortlist().some((p) => p.url === product.url);

/** Add if missing, remove if present. Returns the new list. */
export function toggleShortlist(product: Product): Product[] {
  const list = getShortlist();
  return writeShortlist(
    list.some((p) => p.url === product.url) ? list.filter((p) => p.url !== product.url) : [...list, product],
  );
}

export const removeFromShortlist = (url: string): Product[] =>
  writeShortlist(getShortlist().filter((p) => p.url !== url));

export const clearShortlist = (): Product[] => writeShortlist([]);

/** Subscribe to shortlist changes (this tab and other tabs). Returns an unsubscribe function. */
export function subscribeShortlist(callback: (items: Product[]) => void): () => void {
  const onLocal = () => callback(getShortlist());
  const onStorage = (e: StorageEvent) => {
    if (e.key === SHORTLIST_KEY) callback(getShortlist());
  };
  window.addEventListener(SHORTLIST_EVENT, onLocal);
  window.addEventListener("storage", onStorage);
  return () => {
    window.removeEventListener(SHORTLIST_EVENT, onLocal);
    window.removeEventListener("storage", onStorage);
  };
}

/** React hook: live shortlist array that re-renders on any change. */
export function useShortlist(): Product[] {
  const [items, setItems] = useState<Product[]>(getShortlist);
  useEffect(() => subscribeShortlist(setItems), []);
  return items;
}
