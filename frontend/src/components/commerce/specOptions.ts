// Human labels and short descriptions for every ProductSpec choice (Studio controls + spec sheet).
import type { Fit, Material, Neckline, Placement, PrintMethod, ProductSpec, Size, Sleeve } from "../../types";
import { DEFAULT_SPEC } from "../../types";
import { normalizeHex } from "./color";

export interface ChoiceDef<T extends string> {
  value: T;
  label: string;
  /** One-line description shown under the control / in the spec sheet. */
  hint?: string;
}

export const FITS: ChoiceDef<Fit>[] = [
  { value: "regular", label: "Regular", hint: "Classic straight cut, true to size." },
  { value: "oversized", label: "Oversized", hint: "Dropped shoulders, roomy body, longer length." },
  { value: "slim", label: "Slim", hint: "Close to the body with a tapered waist." },
  { value: "boxy", label: "Boxy", hint: "Wide and square, a little shorter." },
  { value: "cropped", label: "Cropped", hint: "Hits above the waist." },
];

export const MATERIALS: ChoiceDef<Material>[] = [
  { value: "cotton", label: "Cotton", hint: "Soft everyday ring-spun cotton." },
  { value: "organic_cotton", label: "Organic cotton", hint: "Certified organic, soft and breathable." },
  { value: "heavyweight_cotton", label: "Heavyweight cotton", hint: "Thick, structured drape that lasts for years." },
  { value: "tri_blend", label: "Tri-blend", hint: "Cotton, poly and rayon: vintage-soft with stretch." },
  { value: "linen_blend", label: "Linen blend", hint: "Airy and textured; made for hot days." },
  { value: "recycled_poly", label: "Recycled poly", hint: "From recycled bottles; light and quick-dry." },
];

export const NECKLINES: ChoiceDef<Neckline>[] = [
  { value: "crew", label: "Crew", hint: "Round, close-fitting collar." },
  { value: "v_neck", label: "V-neck", hint: "V-shaped opening." },
  { value: "mock_neck", label: "Mock neck", hint: "Short standing collar." },
];

export const SLEEVES: ChoiceDef<Sleeve>[] = [
  { value: "short", label: "Short" },
  { value: "long", label: "Long" },
  { value: "sleeveless", label: "Sleeveless" },
];

export const SIZES: ChoiceDef<Size>[] = (["XS", "S", "M", "L", "XL", "XXL"] as const).map((s) => ({ value: s, label: s }));

export const PRINT_METHODS: ChoiceDef<PrintMethod>[] = [
  { value: "dtg", label: "DTG", hint: "Direct-to-garment: full color and fine detail, great for one-offs." },
  { value: "screen", label: "Screen", hint: "Bold, durable ink layers. One-off setup fee, best in bulk." },
  { value: "embroidery", label: "Embroidery", hint: "Stitched thread. Premium; best for small, simple marks." },
  { value: "puff", label: "Puff", hint: "Raised 3D ink with a tactile, retro feel." },
];

export const PLACEMENTS: ChoiceDef<Placement>[] = [
  { value: "front_center", label: "Front", hint: "Large print, centered on the chest." },
  { value: "left_chest", label: "Left chest", hint: "Small logo-size mark over the heart." },
  { value: "back", label: "Back", hint: "Large print across the upper back." },
  { value: "front_and_back", label: "Front + back", hint: "Large print on both sides." },
];

/** Label for `value` in `list` (falls back to the raw value). */
export function labelOf<T extends string>(list: ChoiceDef<T>[], value: T): string {
  return list.find((c) => c.value === value)?.label ?? value;
}

export function hintOf<T extends string>(list: ChoiceDef<T>[], value: T): string | undefined {
  return list.find((c) => c.value === value)?.hint;
}

const pick = <T extends string>(list: ChoiceDef<T>[], value: unknown, fallback: T): T =>
  list.some((c) => c.value === value) ? (value as T) : fallback;

/** Coerce anything spec-shaped (e.g. an LLM's recommended_spec) into a valid ProductSpec. */
export function normalizeSpec(raw: Partial<ProductSpec> | null | undefined): ProductSpec {
  const s = raw ?? {};
  const qty = Math.round(Number(s.quantity));
  return {
    shirt_color: normalizeHex(s.shirt_color) ?? DEFAULT_SPEC.shirt_color,
    fit: pick(FITS, s.fit, DEFAULT_SPEC.fit),
    material: pick(MATERIALS, s.material, DEFAULT_SPEC.material),
    neckline: pick(NECKLINES, s.neckline, DEFAULT_SPEC.neckline),
    sleeve: pick(SLEEVES, s.sleeve, DEFAULT_SPEC.sleeve),
    size: pick(SIZES, s.size, DEFAULT_SPEC.size),
    print_method: pick(PRINT_METHODS, s.print_method, DEFAULT_SPEC.print_method),
    placement: pick(PLACEMENTS, s.placement, DEFAULT_SPEC.placement),
    quantity: Number.isFinite(qty) ? Math.min(50, Math.max(1, qty)) : DEFAULT_SPEC.quantity,
    notes: typeof s.notes === "string" ? s.notes.slice(0, 500) : "",
  };
}

/** The "look" fields of a spec: what a concept's recommendation is allowed to change. */
export const STYLE_KEYS = ["shirt_color", "fit", "material", "neckline", "sleeve", "print_method", "placement"] as const;

/** True when `spec` already matches the style fields of `recommended`. */
export function matchesStyle(spec: ProductSpec, recommended: ProductSpec): boolean {
  return STYLE_KEYS.every((k) => spec[k] === recommended[k]);
}
