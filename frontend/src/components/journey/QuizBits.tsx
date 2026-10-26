// Balance-game chrome: VS badge, "Round N / 10" progress with depth chip, and the duel layout.
import type { ReactNode } from "react";
import type { Depth } from "../../types";
import { DEPTH_META, depthForRound } from "./journeyUtils";
import { OptionCardSkeleton } from "./OptionCard";

export function VsBadge() {
  return (
    <div className="j-vs">
      <span aria-hidden="true">VS</span>
      <span className="visually-hidden">or</span>
    </div>
  );
}

export function DepthChip({ depth }: { depth: Depth }) {
  const meta = DEPTH_META[depth];
  return (
    <span className={`chip j-depth j-depth--${depth}`} title={meta.hint}>
      <span aria-hidden="true">{meta.icon}</span>
      {meta.label}
    </span>
  );
}

interface QuizProgressProps {
  /** 1-based round being shown. */
  index: number;
  total: number;
  depth: Depth;
}

/** Segmented bar: answered rounds filled, current round pulsing, segments tinted by depth zone. */
export function QuizProgress({ index, total, depth }: QuizProgressProps) {
  const round = Math.max(1, Math.min(index, total));
  return (
    <div className="j-progress">
      <div className="j-progress__top">
        <p className="j-progress__round">
          Round <strong>{round}</strong> <span className="muted">/ {total}</span>
        </p>
        <DepthChip depth={depth} />
      </div>
      <div
        className="j-progress__bar"
        role="progressbar"
        aria-label="Balance game progress"
        aria-valuemin={0}
        aria-valuemax={total}
        aria-valuenow={round - 1}
        aria-valuetext={`Round ${round} of ${total}`}
      >
        {Array.from({ length: total }, (_, i) => {
          const state = i < round - 1 ? " is-done" : i === round - 1 ? " is-current" : "";
          return <span key={i} className={`j-progress__seg j-progress__seg--${depthForRound(i + 1)}${state}`} />;
        })}
      </div>
    </div>
  );
}

/** Labels longer than this get a smaller font on BOTH cards, so they read in full (not "…"). */
export const LONG_LABEL_CHARS = 38;

/**
 * Two equal cards with the VS badge between them (side-by-side >= 640px, stacked below).
 * `long`: either label is long (see LONG_LABEL_CHARS); both labels step down a font size.
 */
export function Duel({ a, b, long = false }: { a: ReactNode; b: ReactNode; long?: boolean }) {
  return (
    <div className={`j-duel${long ? " j-duel--long" : ""}`}>
      {a}
      <VsBadge />
      {b}
    </div>
  );
}

export function DuelSkeleton({ caption }: { caption: ReactNode }) {
  return (
    <div className="j-round" aria-busy="true">
      <div className="j-round__prompt-skel skeleton skeleton-text" aria-hidden="true" />
      <Duel a={<OptionCardSkeleton />} b={<OptionCardSkeleton />} />
      <div className="j-round__caption">{caption}</div>
    </div>
  );
}
