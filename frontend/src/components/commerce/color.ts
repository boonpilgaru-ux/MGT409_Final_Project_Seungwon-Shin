// Color helpers for the shirt mockup, swatches and contrast hints. Hex in, lowercase "#rrggbb" out.

export interface NamedColor {
  hex: string;
  name: string;
}

const HEX_RE = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i;

/** "#ABC" | "abc" | "#AABBCC" -> "#aabbcc"; null when the input isn't a hex color. */
export function normalizeHex(value: string | null | undefined): string | null {
  const match = HEX_RE.exec((value ?? "").trim());
  if (!match) return null;
  let digits = match[1].toLowerCase();
  if (digits.length === 3) digits = [...digits].map((c) => c + c).join("");
  return `#${digits}`;
}

type Rgb = [number, number, number];

function toRgb(hex: string): Rgb {
  const h = normalizeHex(hex) ?? "#ffffff";
  return [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];
}

function toHex(rgb: Rgb): string {
  return `#${rgb.map((v) => Math.round(Math.min(255, Math.max(0, v))).toString(16).padStart(2, "0")).join("")}`;
}

/** Blend `hex` toward `target` by `t` (0 = hex, 1 = target). */
export function mix(hex: string, target: string, t: number): string {
  const a = toRgb(hex);
  const b = toRgb(target);
  return toHex([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t]);
}

export const darken = (hex: string, t: number) => mix(hex, "#000000", t);
export const lighten = (hex: string, t: number) => mix(hex, "#ffffff", t);

/** WCAG relative luminance, 0 (black) to 1 (white). */
export function luminance(hex: string): number {
  const [r, g, b] = toRgb(hex).map((v) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** WCAG contrast ratio between two colors, 1 to 21. */
export function contrastRatio(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/** True for colors where light ink/details read better than dark ones. */
export const isDark = (hex: string) => luminance(hex) < 0.22;

/** Ink or paper, whichever reads better on `hex`. */
export const readableOn = (hex: string) => (isDark(hex) ? "#ffffff" : "#141414");

/** Blank-tee colors most print shops stock. */
export const SHIRT_COLORS: NamedColor[] = [
  { hex: "#ffffff", name: "White" },
  { hex: "#f3eee3", name: "Natural" },
  { hex: "#b8b6af", name: "Heather grey" },
  { hex: "#4a4a48", name: "Charcoal" },
  { hex: "#141414", name: "Black" },
  { hex: "#1f2a44", name: "Navy" },
  { hex: "#2f5bff", name: "Electric blue" },
  { hex: "#a9c8e8", name: "Sky" },
  { hex: "#2e4a3b", name: "Forest" },
  { hex: "#74724a", name: "Olive" },
  { hex: "#d8c5a3", name: "Sand" },
  { hex: "#e0a526", name: "Mustard" },
  { hex: "#ff3b30", name: "Hot red" },
  { hex: "#6e1f2c", name: "Burgundy" },
  { hex: "#f3c4cf", name: "Pink" },
  { hex: "#b9a6d9", name: "Lavender" },
];

/** Name for a hex from the given lists (first match wins), else the uppercase hex. */
export function colorName(hex: string, ...lists: NamedColor[][]): string {
  const target = normalizeHex(hex);
  if (!target) return hex;
  for (const list of [...lists, SHIRT_COLORS]) {
    const hit = list.find((c) => normalizeHex(c.hex) === target);
    if (hit) return hit.name;
  }
  return target.toUpperCase();
}

/**
 * Human name for a color that has no exact name: "≈ Natural" when a stock shirt color is close,
 * else "Custom". Used where the hex is already printed next to it (spec sheet).
 */
export function approxColorName(hex: string): string {
  const target = normalizeHex(hex);
  if (!target) return "Custom";
  const [r, g, b] = toRgb(target);
  let best: NamedColor | null = null;
  let bestDistance = Infinity;
  for (const color of SHIRT_COLORS) {
    const [cr, cg, cb] = toRgb(color.hex);
    // "Redmean" weighting: a cheap, decent approximation of perceived color distance.
    const rm = (r + cr) / 2;
    const d = Math.sqrt((2 + rm / 256) * (r - cr) ** 2 + 4 * (g - cg) ** 2 + (2 + (255 - rm) / 256) * (b - cb) ** 2);
    if (d < bestDistance) {
      best = color;
      bestDistance = d;
    }
  }
  return best && bestDistance < 90 ? `≈ ${best.name}` : "Custom";
}
