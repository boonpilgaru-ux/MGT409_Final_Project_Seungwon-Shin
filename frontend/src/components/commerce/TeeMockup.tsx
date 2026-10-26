// Hand-drawn, parametric SVG t-shirt. The silhouette varies by fit, sleeve and neckline; it's filled
// with the shirt color plus shading, and the design is overlaid at its print placement.
// The design SVG is shown ONLY via <image href="data:image/svg+xml,..."> (image context: no
// scripts, no external loads), never injected as markup.
import { useEffect, useId, useState } from "react";
import { svgDataUrl } from "../../api";
import type { Design, Fit, Neckline, Placement, ProductSpec, Sleeve } from "../../types";
import { colorName, darken, isDark, luminance, normalizeHex } from "./color";
import { FITS, NECKLINES, PLACEMENTS, labelOf } from "./specOptions";

type MockSpec = Pick<ProductSpec, "shirt_color" | "fit" | "sleeve" | "neckline" | "placement" | "print_method">;
export type TeeView = "front" | "back";

export interface TeeMockupProps {
  spec: MockSpec;
  design?: Pick<Design, "svg" | "title"> | null;
  /** Pulse the print-area placeholder (design still generating). */
  loading?: boolean;
  /** Extra named colors (e.g. the profile palette) for the accessible color name. */
  colorNames?: { hex: string; name: string }[];
  className?: string;
}

// ------------------------------------------------------------------ geometry
const W = 680;
const H = 600;
const CX = W / 2;
const TOP = 84; // y of the neck points; the mock-neck collar rises above this
const INK = "#141414";

interface Pt {
  x: number;
  y: number;
}

interface FitGeom {
  neck: number; // half-width of the neck opening
  shoulder: number; // half-width at the shoulder seam
  chest: number; // half-width at the armpit
  waist: number; // half-width control at mid body
  hem: number; // half-width at the hem
  armpit: number; // armpit depth below the neck points
  length: number; // neck points to hem
  slope: number; // shoulder drop (bigger = dropped shoulder)
  sleeve: number; // sleeve size multiplier
}

const GEOM: Record<Fit, FitGeom> = {
  regular: { neck: 50, shoulder: 122, chest: 150, waist: 145, hem: 150, armpit: 150, length: 440, slope: 26, sleeve: 1 },
  slim: { neck: 47, shoulder: 113, chest: 134, waist: 118, hem: 130, armpit: 142, length: 448, slope: 26, sleeve: 0.9 },
  oversized: { neck: 54, shoulder: 162, chest: 184, waist: 186, hem: 184, armpit: 186, length: 476, slope: 42, sleeve: 1.14 },
  boxy: { neck: 54, shoulder: 146, chest: 176, waist: 178, hem: 178, armpit: 160, length: 398, slope: 30, sleeve: 1.04 },
  cropped: { neck: 50, shoulder: 126, chest: 156, waist: 158, hem: 160, armpit: 150, length: 304, slope: 26, sleeve: 1 },
};

const SLEEVE_SHAPE = {
  short: { angle: 30, length: 118, opening: 98 },
  long: { angle: 66, length: 350, opening: 64 },
} as const;

const r = (n: number) => Math.round(n * 10) / 10;
const pt = (p: Pt) => `${r(p.x)} ${r(p.y)}`;
const mirror = (p: Pt): Pt => ({ x: 2 * CX - p.x, y: p.y });
const add = (a: Pt, b: Pt, k = 1): Pt => ({ x: a.x + b.x * k, y: a.y + b.y * k });

/** Quadratic control point that bows the segment a→b sideways by `amount` (hand-drawn wobble). */
function bow(a: Pt, b: Pt, amount: number): Pt {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len = Math.hypot(dx, dy) || 1;
  return { x: (a.x + b.x) / 2 + (dy / len) * amount, y: (a.y + b.y) / 2 - (dx / len) * amount };
}

interface Frame {
  nl: Pt; // neck point (left = viewer's left)
  sl: Pt; // shoulder seam point
  al: Pt; // armpit
  armCtl: Pt; // armhole curve control
}

function frame(g: FitGeom, sleeve: Sleeve): Frame {
  const nl = { x: CX - g.neck, y: TOP };
  if (sleeve === "sleeveless") {
    // Tank: narrow strap, deep scooped armhole.
    const sl = { x: CX - g.neck - 46, y: TOP + 11 };
    const al = { x: CX - g.chest + 6, y: TOP + g.armpit + 20 };
    return { nl, sl, al, armCtl: { x: sl.x + 4, y: al.y - 6 } };
  }
  const sl = { x: CX - g.shoulder, y: TOP + g.slope };
  const al = { x: CX - g.chest, y: TOP + g.armpit };
  return { nl, sl, al, armCtl: { x: sl.x - 3, y: al.y - (al.y - sl.y) * 0.32 } };
}

/** Neckline from the right neck point back to the left one (closes the body path). */
function neckCurve(g: FitGeom, neckline: Neckline, view: TeeView): string {
  const nl = { x: CX - g.neck, y: TOP };
  if (view === "back") return `Q ${CX} ${TOP + 18} ${pt(nl)}`;
  if (neckline === "v_neck") {
    const k = g.neck * 0.35;
    return `Q ${CX + k} ${TOP + 50} ${CX} ${TOP + 98} Q ${CX - k} ${TOP + 50} ${pt(nl)}`;
  }
  return `Q ${CX} ${TOP + (neckline === "mock_neck" ? 28 : 76)} ${pt(nl)}`;
}

function bodyPath(g: FitGeom, f: Frame, neckline: Neckline, view: TeeView): string {
  const hemY = TOP + g.length;
  const waist = { x: CX - g.waist, y: TOP + g.length * 0.58 };
  const hemL = { x: CX - g.hem, y: hemY };
  const shoulderCtl = bow(f.nl, f.sl, 3);
  return [
    `M ${pt(f.nl)}`,
    `Q ${pt(shoulderCtl)} ${pt(f.sl)}`,
    `Q ${pt(f.armCtl)} ${pt(f.al)}`,
    `Q ${pt(waist)} ${pt(hemL)}`,
    `Q ${CX} ${hemY + (g.length < 340 ? 4 : 10)} ${pt(mirror(hemL))}`,
    `Q ${pt(mirror(waist))} ${pt(mirror(f.al))}`,
    `Q ${pt(mirror(f.armCtl))} ${pt(mirror(f.sl))}`,
    `Q ${pt(mirror(shoulderCtl))} ${pt(mirror(f.nl))}`,
    neckCurve(g, neckline, view),
    "Z",
  ].join(" ");
}

interface SleeveGeom {
  path: string; // left sleeve (mirror for right)
  bottom: number; // lowest y of the sleeve
  hem: [Pt, Pt]; // stitch line near the opening
  cuff: [Pt, Pt] | null; // rib line for long sleeves
}

function sleeveGeom(g: FitGeom, f: Frame, sleeve: "short" | "long"): SleeveGeom {
  const s = SLEEVE_SHAPE[sleeve];
  const len = s.length * g.sleeve;
  const open = s.opening * g.sleeve;
  const rad = (s.angle * Math.PI) / 180;
  const dir = { x: -Math.cos(rad), y: Math.sin(rad) }; // along the sleeve, outward
  const across = { x: Math.sin(rad), y: Math.cos(rad) }; // across the opening, toward the body
  const outer = add(f.sl, dir, len);
  const inner = add(outer, across, open);
  const tuck = { x: f.sl.x + 34, y: (f.sl.y + f.al.y) / 2 }; // hidden under the body
  const path = [
    `M ${pt(f.sl)}`,
    `Q ${pt(bow(f.sl, outer, 7))} ${pt(outer)}`,
    `Q ${pt(bow(outer, inner, -3))} ${pt(inner)}`,
    `Q ${pt(bow(inner, f.al, -5))} ${pt(f.al)}`,
    `L ${pt(tuck)} Z`,
  ].join(" ");
  const inset = (k: number): [Pt, Pt] => [add(outer, dir, -k), add(add(inner, dir, -k), across, -4)];
  return { path, bottom: Math.max(outer.y, inner.y), hem: inset(15), cuff: sleeve === "long" ? inset(32) : null };
}

/** Transform that mirrors left-side pieces onto the right. */
const MIRROR = `translate(${2 * CX} 0) scale(-1 1)`;

interface Box {
  x: number;
  y: number;
  size: number;
}

/** Where the print sits for this view, or null if the design isn't on this side. */
function printBox(g: FitGeom, neckline: Neckline, placement: Placement, view: TeeView): Box | null {
  const bodyW = 2 * Math.min(g.chest, g.hem);
  if (view === "front") {
    if (placement === "left_chest") {
      const size = bodyW * 0.2;
      return { x: CX + g.chest * 0.46 - size / 2, y: TOP + (neckline === "v_neck" ? 92 : 78), size };
    }
    if (placement === "front_center" || placement === "front_and_back") {
      const yOff = neckline === "v_neck" ? 124 : neckline === "mock_neck" ? 70 : 92;
      const size = Math.min(bodyW * 0.6, g.length - yOff - 26);
      return { x: CX - size / 2, y: TOP + yOff, size };
    }
    return null;
  }
  if (placement === "back" || placement === "front_and_back") {
    const size = Math.min(bodyW * 0.64, g.length - 92);
    return { x: CX - size / 2, y: TOP + 58, size };
  }
  return null;
}

const onFront = (p: Placement) => p !== "back";
const onBack = (p: Placement) => p === "back" || p === "front_and_back";
const defaultView = (p: Placement): TeeView => (p === "back" ? "back" : "front");

// ------------------------------------------------------------------ component
export function TeeMockup({ spec, design, loading = false, colorNames = [], className = "" }: TeeMockupProps) {
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const [view, setView] = useState<TeeView>(() => defaultView(spec.placement));

  // Follow the placement: picking "Back" flips to the back, "Front"/"Left chest" flips to the front.
  useEffect(() => {
    setView((v) => (spec.placement === "front_and_back" ? v : defaultView(spec.placement)));
  }, [spec.placement]);

  const color = normalizeHex(spec.shirt_color) ?? "#ffffff";
  const g = GEOM[spec.fit] ?? GEOM.regular;
  const f = frame(g, spec.sleeve);
  const dark = isDark(color);
  const veryDark = luminance(color) < 0.03;
  const detail = dark ? "rgb(255 255 255 / 0.24)" : "rgb(0 0 0 / 0.16)";
  const detailStrong = dark ? "rgb(255 255 255 / 0.55)" : "rgb(20 20 20 / 0.38)";
  const popShadow = veryDark ? "#cbc3b6" : INK;

  const body = bodyPath(g, f, spec.neckline, view);
  const sleeves = spec.sleeve === "sleeveless" ? null : sleeveGeom(g, f, spec.sleeve);
  const collar = spec.neckline === "mock_neck" ? mockCollar(g, view) : null;
  const box = printBox(g, spec.neckline, spec.placement, view);
  const hemY = TOP + g.length;
  // Center the garment vertically in the canvas (cropped tees would otherwise float high).
  const garmentTop = TOP - (collar ? 40 : 6);
  const garmentBottom = Math.max(hemY + 10, sleeves?.bottom ?? 0) + 12; // + offset shadow
  const shiftY = r((H - garmentBottom - garmentTop) / 2);

  const ids = {
    shadeX: `${uid}-sx`,
    shadeY: `${uid}-sy`,
    clip: `${uid}-clip`,
    print: `${uid}-print`,
  };
  const printFilter = spec.print_method === "puff" || spec.print_method === "embroidery" ? `url(#${ids.print})` : undefined;

  const shapes = (fill: string, stroke?: string) => (
    <>
      {sleeves && (
        <>
          <path d={sleeves.path} fill={fill} stroke={stroke} />
          <path d={sleeves.path} fill={fill} stroke={stroke} transform={MIRROR} />
        </>
      )}
      <path d={body} fill={fill} stroke={stroke} />
      {collar && <path d={collar.outer} fill={fill} stroke={stroke} />}
    </>
  );

  const designHere = view === "front" ? onFront(spec.placement) : onBack(spec.placement);
  const placementLabel = labelOf(PLACEMENTS, spec.placement).toLowerCase();
  const label =
    `${colorName(color, colorNames)} ${labelOf(FITS, spec.fit).toLowerCase()} t-shirt, ` +
    `${spec.sleeve === "sleeveless" ? "sleeveless" : `${spec.sleeve} sleeves`}, ` +
    `${labelOf(NECKLINES, spec.neckline).toLowerCase()}, ${view} view` +
    (design && designHere ? `, with “${design.title}” printed ${placementLabel === "front + back" ? "on both sides" : `on the ${placementLabel}`}` : "");

  return (
    <figure className={`tee-mockup ${className}`.trim()}>
      <div className="tee-mockup__stage">
        {view === "back" && <span className="tee-mockup__badge">Back</span>}
        <svg
          className="tee-mockup__svg"
          viewBox={`0 0 ${W} ${H}`}
          role="img"
          aria-label={label}
          strokeLinejoin="round"
          strokeLinecap="round"
        >
          <defs>
            <linearGradient id={ids.shadeX} gradientUnits="userSpaceOnUse" x1={CX - 270} y1="0" x2={CX + 270} y2="0">
              <stop offset="0" stopColor="#000" stopOpacity="0.17" />
              <stop offset="0.24" stopColor="#000" stopOpacity="0.03" />
              <stop offset="0.48" stopColor="#fff" stopOpacity={dark ? 0.06 : 0.1} />
              <stop offset="0.76" stopColor="#000" stopOpacity="0.03" />
              <stop offset="1" stopColor="#000" stopOpacity="0.2" />
            </linearGradient>
            <linearGradient id={ids.shadeY} gradientUnits="userSpaceOnUse" x1="0" y1={TOP - 30} x2="0" y2={TOP + 500}>
              <stop offset="0" stopColor="#fff" stopOpacity={dark ? 0.07 : 0.12} />
              <stop offset="0.5" stopColor="#fff" stopOpacity="0" />
              <stop offset="1" stopColor="#000" stopOpacity="0.1" />
            </linearGradient>
            <clipPath id={ids.clip}>
              <path d={body} />
            </clipPath>
            <filter id={ids.print} x="-10%" y="-10%" width="120%" height="120%">
              {spec.print_method === "puff" ? (
                <feDropShadow dx="0" dy="3" stdDeviation="1.8" floodColor="#000" floodOpacity="0.38" />
              ) : (
                <feDropShadow dx="0" dy="1.2" stdDeviation="0.7" floodColor="#000" floodOpacity="0.5" />
              )}
            </filter>
          </defs>

          <g transform={`translate(0 ${shiftY})`}>
            {/* poster-style offset shadow */}
            <g transform="translate(9 10)">{shapes(popShadow)}</g>

            {/* garment */}
            <g strokeWidth="3.2">{shapes(color, INK)}</g>
            <g pointerEvents="none">
              {shapes(`url(#${ids.shadeX})`)}
              {shapes(`url(#${ids.shadeY})`)}
            </g>

            {/* neck details */}
            {collar ? (
              <>
                {collar.opening && <path d={collar.opening} fill={darken(color, 0.32)} stroke={INK} strokeWidth="2.4" />}
                <path d={collar.stitch} fill="none" stroke={detail} strokeWidth="2" strokeDasharray="5 6" />
              </>
            ) : (
              <NeckDetails g={g} neckline={spec.neckline} view={view} color={color} detail={detail} />
            )}

            {/* stitching and folds */}
            <g fill="none" stroke={detail} strokeWidth="2">
              <path
                d={`M ${r(CX - g.hem + 8)} ${hemY - 15} Q ${CX} ${hemY - 15 + (g.length < 340 ? 4 : 10)} ${r(CX + g.hem - 8)} ${hemY - 15}`}
                strokeDasharray="6 6"
              />
              {sleeves && (
                <>
                  <path d={`M ${pt(sleeves.hem[0])} L ${pt(sleeves.hem[1])}`} strokeDasharray="6 6" />
                  <path d={`M ${pt(sleeves.hem[0])} L ${pt(sleeves.hem[1])}`} strokeDasharray="6 6" transform={MIRROR} />
                  {sleeves.cuff && (
                    <>
                      <path d={`M ${pt(sleeves.cuff[0])} L ${pt(sleeves.cuff[1])}`} />
                      <path d={`M ${pt(sleeves.cuff[0])} L ${pt(sleeves.cuff[1])}`} transform={MIRROR} />
                    </>
                  )}
                </>
              )}
              {spec.sleeve === "sleeveless" && (
                <>
                  <path d={`M ${pt(add(f.sl, { x: 9, y: 2 }))} Q ${pt(add(f.armCtl, { x: 10, y: -4 }))} ${pt(add(f.al, { x: 6, y: -6 }))}`} strokeDasharray="6 6" />
                  <path
                    d={`M ${pt(add(f.sl, { x: 9, y: 2 }))} Q ${pt(add(f.armCtl, { x: 10, y: -4 }))} ${pt(add(f.al, { x: 6, y: -6 }))}`}
                    strokeDasharray="6 6"
                    transform={MIRROR}
                  />
                </>
              )}
              <path d={`M ${pt(add(f.al, { x: 14, y: 28 }))} q 26 9 50 3`} opacity="0.8" />
              <path d={`M ${pt(add(mirror(f.al), { x: -14, y: 28 }))} q -26 9 -50 3`} opacity="0.8" />
              <path d={`M ${r(CX - g.hem + 30)} ${hemY - 40} q 22 -8 46 -2`} opacity="0.6" />
            </g>

            {/* print */}
            {box &&
              (design ? (
                <image
                  href={svgDataUrl(design.svg)}
                  x={r(box.x)}
                  y={r(box.y)}
                  width={r(box.size)}
                  height={r(box.size)}
                  preserveAspectRatio="xMidYMid meet"
                  clipPath={`url(#${ids.clip})`}
                  filter={printFilter}
                />
              ) : (
                <g className={loading ? "tee-mockup__pulse" : undefined}>
                  <rect
                    x={r(box.x)}
                    y={r(box.y)}
                    width={r(box.size)}
                    height={r(box.size)}
                    rx="14"
                    fill="none"
                    stroke={detailStrong}
                    strokeWidth="2.5"
                    strokeDasharray="10 8"
                  />
                  {box.size > 90 && (
                    <text
                      x={r(box.x + box.size / 2)}
                      y={r(box.y + box.size / 2)}
                      textAnchor="middle"
                      dominantBaseline="middle"
                      fill={detailStrong}
                      fontFamily="system-ui, sans-serif"
                      fontWeight="800"
                      fontSize="20"
                      letterSpacing="2"
                    >
                      {loading ? "SKETCHING…" : "YOUR DESIGN"}
                    </text>
                  )}
                </g>
              ))}
          </g>
        </svg>
      </div>

      <figcaption className="tee-mockup__bar">
        <div className="tee-mockup__toggle" role="group" aria-label="Mockup view">
          {(["front", "back"] as const).map((side) => {
            const has = side === "front" ? onFront(spec.placement) : onBack(spec.placement);
            return (
              <button key={side} type="button" aria-pressed={view === side} onClick={() => setView(side)}>
                {side === "front" ? "Front" : "Back"}
                {has && (
                  <>
                    <span className="tee-mockup__dot" aria-hidden="true" />
                    <span className="visually-hidden"> (printed)</span>
                  </>
                )}
              </button>
            );
          })}
        </div>
        {!designHere && (
          <span className="tee-mockup__hint">
            Print is on the {view === "front" ? "back" : "front"}.{" "}
            <button type="button" className="tee-mockup__flip" onClick={() => setView(view === "front" ? "back" : "front")}>
              Flip
            </button>
          </span>
        )}
      </figcaption>
    </figure>
  );
}

// ------------------------------------------------------------------ neck pieces
function NeckDetails({ g, neckline, view, color, detail }: { g: FitGeom; neckline: Neckline; view: TeeView; color: string; detail: string }) {
  const nl = { x: CX - g.neck, y: TOP };
  const nr = mirror(nl);
  if (view === "back") {
    // Collar band seen from behind.
    const band = `M ${pt(nl)} Q ${CX} ${TOP + 18} ${pt(nr)} L ${r(nr.x + 9)} ${TOP + 4} Q ${CX} ${TOP + 40} ${r(nl.x - 9)} ${TOP + 4} Z`;
    return (
      <>
        <path d={band} fill={darken(color, 0.06)} stroke={INK} strokeWidth="2.6" />
        <path d={`M ${r(nl.x - 4)} ${TOP + 9} Q ${CX} ${TOP + 36} ${r(nr.x + 4)} ${TOP + 9}`} fill="none" stroke={detail} strokeWidth="2" strokeDasharray="5 6" />
      </>
    );
  }
  // Inside of the back, visible through the front opening, plus its rib.
  const front = neckCurve(g, neckline, "front");
  const inside = `M ${pt(nl)} Q ${CX} ${TOP + 18} ${pt(nr)} ${front} Z`;
  const backRib = `M ${pt(nl)} Q ${CX} ${TOP + 18} ${pt(nr)} L ${r(nr.x - 3)} ${TOP + 8} Q ${CX} ${TOP + 36} ${r(nl.x + 3)} ${TOP + 8} Z`;
  let rib: string;
  let seam: string | null = null;
  if (neckline === "v_neck") {
    const k = g.neck * 0.35;
    rib =
      `M ${pt(nl)} Q ${CX - k} ${TOP + 50} ${CX} ${TOP + 98} Q ${CX + k} ${TOP + 50} ${pt(nr)} ` +
      `L ${r(nr.x + 11)} ${TOP + 3} Q ${CX + k + 9} ${TOP + 58} ${CX} ${TOP + 118} Q ${CX - k - 9} ${TOP + 58} ${r(nl.x - 11)} ${TOP + 3} Z`;
    seam = `M ${CX} ${TOP + 98} L ${CX} ${TOP + 118}`;
  } else {
    rib = `M ${pt(nl)} Q ${CX} ${TOP + 76} ${pt(nr)} L ${r(nr.x + 9)} ${TOP + 3} Q ${CX} ${TOP + 104} ${r(nl.x - 9)} ${TOP + 3} Z`;
  }
  return (
    <>
      <path d={inside} fill={darken(color, 0.34)} stroke={INK} strokeWidth="2.4" />
      <path d={backRib} fill={darken(color, 0.16)} />
      <path d={rib} fill={darken(color, 0.05)} stroke={INK} strokeWidth="2.6" />
      {seam && <path d={seam} stroke={INK} strokeWidth="2.2" />}
    </>
  );
}

/** Standing mock-neck collar (outer silhouette, inner opening on the front, stitch line). */
function mockCollar(g: FitGeom, view: TeeView): { outer: string; opening: string | null; stitch: string } {
  const nl = { x: CX - g.neck, y: TOP };
  const nr = mirror(nl);
  const topL = { x: nl.x + 4, y: TOP - 28 };
  const topR = mirror(topL);
  const rim = view === "front" ? TOP - 38 : TOP - 32;
  return {
    outer: `M ${pt(topL)} Q ${CX} ${rim} ${pt(topR)} L ${r(nr.x + 7)} ${TOP + 2} Q ${CX} ${TOP + 38} ${r(nl.x - 7)} ${TOP + 2} Z`,
    opening: view === "front" ? `M ${pt(topL)} Q ${CX} ${rim} ${pt(topR)} Q ${CX} ${TOP - 10} ${pt(topL)} Z` : null,
    stitch: `M ${r(nl.x - 2)} ${TOP - 6} Q ${CX} ${TOP + 26} ${r(nr.x + 2)} ${TOP - 6}`,
  };
}
