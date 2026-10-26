// Link helpers for the About-you composer: find URLs typed or pasted into free text,
// normalize them (adds https://), and flag login-walled networks.

const WALLED = [
  { re: /(^|\.)instagram\.com$/i, name: "Instagram" },
  { re: /(^|\.)(x|twitter)\.com$/i, name: "X / Twitter" },
  { re: /(^|\.)tiktok\.com$/i, name: "TikTok" },
  { re: /(^|\.)(facebook|fb)\.com$/i, name: "Facebook" },
  { re: /(^|\.)linkedin\.com$/i, name: "LinkedIn" },
  { re: /(^|\.)threads\.(net|com)$/i, name: "Threads" },
];

/** Name of a login-walled network for this URL, if any. */
export function walledSite(url: string): string | null {
  try {
    const host = new URL(url).hostname;
    return WALLED.find((w) => w.re.test(host))?.name ?? null;
  } catch {
    return null;
  }
}

export function normalizeUrl(raw: string): { url: string } | { error: string } {
  let text = raw.trim();
  if (!text) return { error: "Paste a link first." };
  if (!/^[a-z][a-z\d+.-]*:\/\//i.test(text)) text = `https://${text}`;
  if (text.length > 2048) return { error: "That link is too long." };
  let url: URL;
  try {
    url = new URL(text);
  } catch {
    return { error: "That doesn’t look like a link." };
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return { error: "Only http(s) links work." };
  if (!url.hostname.includes(".")) return { error: "That doesn’t look like a public website." };
  return { url: url.toString() };
}

// Bare domains count as links when they use a common TLD (github.com) or carry a path (site.xyz/you),
// so ordinary words like "Node.js" or "resume.pdf" stay text.
const TLDS = "com|net|org|io|dev|app|ai|xyz|ly|fm|tv|page|site|blog|art|design|edu|gg|link|bio|studio|online|info|me|kr|uk|jp";
const LABELS = String.raw`(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+`;
const TAIL = String.raw`[^\s<>"']`;
const LINK_RE = new RegExp(
  [
    String.raw`\bhttps?:\/\/${TAIL}+`,
    String.raw`\bwww\.${TAIL}+`,
    String.raw`\b${LABELS}(?:${TLDS})\b(?:\/${TAIL}*)?`,
    String.raw`\b${LABELS}[a-z]{2,}\/${TAIL}*`,
  ].join("|"),
  "gi",
);
const TRAILING = /[.,;:!?)\]}'"”’]+$/;

/** Strip trailing punctuation, but keep a ")" that closes a "(" inside the link (wiki/Slam_Dunk_(manga)). */
function trimTrailing(raw: string): string {
  let out = raw.replace(TRAILING, "");
  while (raw.length > out.length && raw[out.length] === ")" &&
         (out.match(/\(/g)?.length ?? 0) > (out.match(/\)/g)?.length ?? 0)) {
    out += ")";
  }
  return out;
}

export interface ExtractedLinks {
  /** Normalized, de-duplicated links, at most `max`. */
  urls: string[];
  /** The text with those links taken out. */
  rest: string;
  /** Valid links found beyond `max` (left in `rest`). */
  overflow: number;
}

/** Pull up to `max` links out of free text; everything else stays as text. */
export function extractLinks(text: string, max: number): ExtractedLinks {
  const urls: string[] = [];
  const cut: Array<[number, number]> = [];
  let overflow = 0;
  for (const m of text.matchAll(LINK_RE)) {
    const start = m.index ?? 0;
    const before = start > 0 ? text[start - 1] : "";
    if (before === "@" || before === "." || before === "/") continue; // emails, file paths
    const raw = trimTrailing(m[0]);
    const result = normalizeUrl(raw);
    if ("error" in result) continue;
    if (urls.includes(result.url)) {
      cut.push([start, start + raw.length]); // duplicate: drop it from the text too
      continue;
    }
    if (urls.length >= max) {
      overflow += 1;
      continue;
    }
    urls.push(result.url);
    cut.push([start, start + raw.length]);
  }
  let rest = text;
  for (const [s, e] of [...cut].reverse()) rest = rest.slice(0, s) + rest.slice(e);
  rest = rest
    .split("\n")
    .map((line) => line.replace(/[ \t]{2,}/g, " ").replace(/ ([,;:!?])/g, "$1").trimEnd())
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
  return { urls, rest, overflow };
}
