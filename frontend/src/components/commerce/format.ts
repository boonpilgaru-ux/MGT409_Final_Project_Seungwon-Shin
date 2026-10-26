// Small formatting / safety helpers shared by the Shop, Studio and Order pages.

/** The URL if it is an absolute http(s) URL, else null (blocks javascript:, data:, relative junk). */
export function safeHttpUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value.trim());
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

/** "https://www.uniqlo.com/us/en/..." -> "uniqlo.com". */
export function hostOf(value: string | null | undefined): string {
  const safe = safeHttpUrl(value);
  if (!safe) return "";
  return new URL(safe).hostname.replace(/^www\./, "");
}

const NUMERIC_PRICE = /^\d{1,7}(?:[.,]\d{1,2})?$/;

/**
 * Product price for display. Bare numbers are formatted with the currency ("29" + "USD" -> "$29.00");
 * strings that already carry a symbol or code are shown as-is, with the code appended if missing.
 */
export function formatProductPrice(price: string | null, currency: string | null): string | null {
  const p = price?.trim();
  if (!p) return null;
  const code = currency?.trim().toUpperCase() ?? "";
  if (NUMERIC_PRICE.test(p) && /^[A-Z]{3}$/.test(code)) {
    try {
      return new Intl.NumberFormat(undefined, { style: "currency", currency: code }).format(Number(p.replace(",", ".")));
    } catch {
      /* unknown currency code: fall through */
    }
  }
  const hasMarker = /[^\d\s.,-]/.test(p); // any symbol or letters (e.g. "$", "₩", "EUR")
  return hasMarker || !code ? p : `${p} ${code}`;
}

/** 83 -> "1:23". */
export function formatElapsed(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** Relative time like "just now", "5 min ago", "2 h ago", "3 days ago". */
export function timeAgo(timestamp: number, now = Date.now()): string {
  const mins = Math.round((now - timestamp) / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

/** "name@example.com" -> "n•••@example.com" (order pages are shareable; don't echo full emails). */
export function maskEmail(email: string): string {
  const [user, domain] = email.split("@");
  if (!domain) return "•••";
  return `${user.slice(0, 1)}•••@${domain}`;
}

/** Same loose check as the backend (something@something.tld). */
export const looksLikeEmail = (value: string) => /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(value.trim());

/** Stable small hash for picking fallback colors etc. */
export function hashString(value: string): number {
  let h = 2166136261;
  for (let i = 0; i < value.length; i++) {
    h ^= value.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}
