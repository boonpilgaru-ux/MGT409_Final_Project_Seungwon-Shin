// A personality trait as a labelled 0-100 meter between its low and high poles.
import type { CSSProperties } from "react";
import type { Trait } from "../../types";

export function TraitBar({ trait, index }: { trait: Trait; index: number }) {
  const score = Math.max(0, Math.min(100, Math.round(Number(trait.score) || 0)));
  const leansHigh = score >= 50;
  const style = { "--score": `${score}%`, "--i": index } as CSSProperties;
  return (
    <li className="j-trait" style={style}>
      <div className="j-trait__head">
        <span className="j-trait__name">{trait.name}</span>
        <span className="j-trait__score">{score}</span>
      </div>
      <div
        className="j-trait__track"
        role="meter"
        aria-label={trait.name}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={score}
        aria-valuetext={`${score} out of 100, leaning ${leansHigh ? trait.high_label : trait.low_label}`}
      >
        <span className="j-trait__fill" />
      </div>
      <div className="j-trait__ends" aria-hidden="true">
        <span className={leansHigh ? "" : "is-strong"}>{trait.low_label}</span>
        <span className={leansHigh ? "is-strong" : ""}>{trait.high_label}</span>
      </div>
      {trait.description && <p className="j-trait__desc">{trait.description}</p>}
    </li>
  );
}
