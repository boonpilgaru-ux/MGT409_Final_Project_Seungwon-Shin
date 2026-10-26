// Thumbnail strip of every generated design. Designs render as <img src="data:..."> only.
import { svgDataUrl } from "../../api";
import type { Design } from "../../types";

interface ConceptPickerProps {
  designs: Design[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  /** Tile background: the current shirt color, so you see each concept on the tee. */
  background: string;
  /** Number of skeleton tiles to append while new concepts generate. */
  pending?: number;
  disabled?: boolean;
}

export function ConceptPicker({ designs, selectedId, onSelect, background, pending = 0, disabled }: ConceptPickerProps) {
  return (
    <div className="cm-concepts" role="group" aria-label="Design concepts">
      {designs.map((d, i) => {
        const on = d.id === selectedId;
        return (
          <button
            key={d.id}
            type="button"
            className={`cm-concept${on ? " is-on" : ""}`}
            aria-pressed={on}
            disabled={disabled}
            onClick={() => onSelect(d.id)}
            title={d.title}
          >
            <span className="cm-concept__tile" style={{ background }}>
              <img src={svgDataUrl(d.svg)} alt="" draggable={false} />
            </span>
            <span className="cm-concept__label">
              <span className="cm-concept__num" aria-hidden="true">
                {i + 1}
              </span>
              <span className="cm-concept__title">{d.title}</span>
            </span>
          </button>
        );
      })}
      {Array.from({ length: pending }, (_, i) => (
        <div key={`pending-${i}`} className="cm-concept cm-concept--pending" aria-hidden="true">
          <span className="cm-concept__tile skeleton" />
          <span className="skeleton skeleton-text" style={{ width: "70%" }} />
        </div>
      ))}
    </div>
  );
}
