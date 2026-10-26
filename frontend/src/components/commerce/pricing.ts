// Client-side mirror of the SPEC section 5 price table (orders.py), used for the Studio's live
// estimate. The order page always shows the server's price, which is authoritative.
import type { Material, Price, PriceLine, PrintMethod, ProductSpec } from "../../types";

export const MATERIAL_BASE: Record<Material, number> = {
  cotton: 12,
  organic_cotton: 16,
  heavyweight_cotton: 18,
  tri_blend: 15,
  linen_blend: 22,
  recycled_poly: 14,
};

export const PRINT_COST: Record<PrintMethod, number> = { dtg: 8, screen: 6, embroidery: 12, puff: 10 };

// Line labels match backend/app/orders.py so the estimate reads like the final quote.
const MATERIAL_LINE: Record<Material, string> = {
  cotton: "Cotton tee",
  organic_cotton: "Organic cotton tee",
  heavyweight_cotton: "Heavyweight cotton tee",
  tri_blend: "Tri-blend tee",
  linen_blend: "Linen-blend tee",
  recycled_poly: "Recycled poly tee",
};
const PRINT_LINE: Record<PrintMethod, string> = { dtg: "DTG print", screen: "Screen print", embroidery: "Embroidery", puff: "Puff print" };

export const SCREEN_SETUP_FEE = 30; // one-off, per order
export const BACK_PRINT_EXTRA = 7; // placement "front_and_back"
export const LONG_SLEEVE_EXTRA = 5;
export const XXL_EXTRA = 3;
export const MAX_QUANTITY = 50;

/** Quantity discount tiers, highest first. */
export const DISCOUNT_TIERS = [
  { min: 25, rate: 0.2 },
  { min: 10, rate: 0.1 },
] as const;

export const discountRate = (quantity: number) => DISCOUNT_TIERS.find((t) => quantity >= t.min)?.rate ?? 0;

/** The next discount tier the quantity hasn't reached yet, if any. */
export function nextDiscountTier(quantity: number): { min: number; rate: number } | null {
  const upcoming = [...DISCOUNT_TIERS].reverse().find((t) => quantity < t.min);
  return upcoming ? { min: upcoming.min, rate: upcoming.rate } : null;
}

const cents = (n: number) => Math.round(n * 100) / 100;

/**
 * Estimate in USD. Per shirt: material base + print method (+7 front & back, +5 long sleeve,
 * +3 XXL). Screen printing adds a $30 setup fee per order. The quantity discount (10% at 10+,
 * 20% at 25+) applies to the shirts, not the setup fee. Breakdown lines sum to `total`;
 * `unit` is the per-shirt price after the discount.
 */
export function estimatePrice(spec: ProductSpec): Price {
  const qty = Math.min(MAX_QUANTITY, Math.max(1, Math.round(spec.quantity) || 1));
  const each = (label: string, amount: number): PriceLine => ({
    label: qty > 1 ? `${label} × ${qty}` : label,
    amount: cents(amount * qty),
  });

  const lines: PriceLine[] = [
    each(MATERIAL_LINE[spec.material], MATERIAL_BASE[spec.material]),
    each(PRINT_LINE[spec.print_method], PRINT_COST[spec.print_method]),
  ];
  if (spec.placement === "front_and_back") lines.push(each("Front + back print", BACK_PRINT_EXTRA));
  if (spec.sleeve === "long") lines.push(each("Long sleeves", LONG_SLEEVE_EXTRA));
  if (spec.size === "XXL") lines.push(each("XXL size", XXL_EXTRA));

  const shirts = cents(lines.reduce((sum, l) => sum + l.amount, 0));
  const rate = discountRate(qty);
  const discount = rate ? cents(shirts * rate) : 0;
  if (discount) lines.push({ label: `Quantity discount (${Math.round(rate * 100)}% off)`, amount: -discount });
  if (spec.print_method === "screen") lines.push({ label: "Screen setup (one-time)", amount: SCREEN_SETUP_FEE });

  return {
    unit: cents((shirts - discount) / qty),
    total: cents(lines.reduce((sum, l) => sum + l.amount, 0)),
    currency: "USD",
    breakdown: lines,
  };
}

const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

/** 27 -> "$27.00"; negative amounts get a real minus sign. */
export function formatUSD(amount: number): string {
  return amount < 0 ? `−${usd.format(-amount)}` : usd.format(amount);
}

/** "+$5" style surcharge label for option pickers. */
export const surcharge = (amount: number) => `+$${amount}`;
