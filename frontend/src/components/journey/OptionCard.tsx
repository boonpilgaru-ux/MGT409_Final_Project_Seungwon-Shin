// One side of a balance-game round: square (1:1, object-fit: cover) image or gradient+emoji
// fallback, bold label, muted sublabel, tiny image credit linking to the source.
// The credit link is a sibling of the <button> (links can't nest inside buttons).
import { useState, type CSSProperties } from "react";
import type { Choice, Option } from "../../types";
import { safeColor, safeHttpUrl } from "./journeyUtils";

export type OptionCardState = "idle" | "chosen" | "faded";

interface OptionCardProps {
  side: Choice;
  option: Option;
  state: OptionCardState;
  /** Ignore clicks (e.g. while an answer is in flight). Stays focusable. */
  locked: boolean;
  onChoose: (side: Choice) => void;
}

export function OptionCard({ side, option, state, locked, onChoose }: OptionCardProps) {
  const [failed, setFailed] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const imageUrl = failed ? null : safeHttpUrl(option.image?.url);
  const creditUrl = safeHttpUrl(option.image?.source_url);
  const credit = imageUrl ? option.image?.credit?.trim() : "";
  const style = { "--j-opt": safeColor(option.color) } as CSSProperties;
  const name = `Option ${side}: ${option.label}${option.sublabel ? ` (${option.sublabel})` : ""}`;

  return (
    <div className={`j-card j-card--${side.toLowerCase()} is-${state}`} style={style}>
      <button
        type="button"
        className="j-card__btn"
        aria-label={name}
        aria-disabled={locked || undefined}
        aria-keyshortcuts={side === "A" ? "A ArrowLeft" : "B ArrowRight"}
        onClick={() => !locked && onChoose(side)}
      >
        <span className="j-card__media">
          <span className="j-card__emoji" aria-hidden="true">
            {option.emoji || "✨"}
          </span>
          {imageUrl && (
            <img
              className={`j-card__img${loaded ? " is-loaded" : ""}`}
              src={imageUrl}
              alt={option.label}
              decoding="async"
              referrerPolicy="no-referrer"
              draggable={false}
              onLoad={() => setLoaded(true)}
              onError={() => setFailed(true)}
            />
          )}
          <span className="j-card__key" aria-hidden="true">
            {side}
          </span>
          {state === "chosen" && (
            <span className="j-card__stamp" aria-hidden="true">
              Picked!
            </span>
          )}
        </span>
        <span className="j-card__body">
          <span className="j-card__label" title={option.label}>
            {option.label}
          </span>
          {option.sublabel && (
            <span className="j-card__sub" title={option.sublabel}>
              {option.sublabel}
            </span>
          )}
        </span>
      </button>

      {credit && (
        <span className="j-card__overlay">
          {creditUrl ? (
            <a className="j-card__credit" href={creditUrl} target="_blank" rel="noopener noreferrer" title={credit}>
              📷 {credit}
            </a>
          ) : (
            <span className="j-card__credit" title={credit}>
              📷 {credit}
            </span>
          )}
        </span>
      )}
    </div>
  );
}

/** Placeholder card with the exact same geometry, shown while the next round is generated. */
export function OptionCardSkeleton() {
  return (
    <div className="j-card is-skeleton" aria-hidden="true">
      <div className="j-card__btn">
        <div className="j-card__media skeleton" />
        <div className="j-card__body">
          <div className="skeleton skeleton-text" style={{ width: "72%", height: "1.1em" }} />
          <div className="skeleton skeleton-text" style={{ width: "48%" }} />
        </div>
      </div>
    </div>
  );
}
