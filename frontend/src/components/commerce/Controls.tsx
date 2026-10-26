// Form controls for the Studio: segmented radio groups, shirt-color picker, quantity stepper.
// Built on native radios/inputs so keyboard + screen-reader behavior comes for free.
import { useEffect, useId, useState, type ReactNode } from "react";
import { normalizeHex, readableOn, type NamedColor } from "./color";
import type { ChoiceDef } from "./specOptions";

// ------------------------------------------------------------------ segmented control
interface SegmentedProps<T extends string> {
  legend: string;
  value: T;
  options: ChoiceDef<T>[];
  onChange: (value: T) => void;
  /** Small text under an option label (e.g. a "+$5" surcharge). */
  extra?: (value: T) => string | undefined;
  hint?: ReactNode;
  disabled?: boolean;
}

export function Segmented<T extends string>({ legend, value, options, onChange, extra, hint, disabled }: SegmentedProps<T>) {
  const name = useId();
  return (
    <fieldset className="cm-field" disabled={disabled}>
      <legend className="label cm-field__legend">{legend}</legend>
      <div className="cm-seg">
        {options.map((o) => {
          const note = extra?.(o.value);
          return (
            <label key={o.value} className={`cm-seg__opt${o.value === value ? " is-on" : ""}`}>
              <input type="radio" name={name} value={o.value} checked={o.value === value} onChange={() => onChange(o.value)} />
              <span className="cm-seg__text">
                {o.label}
                {note && <small className="cm-seg__extra">{note}</small>}
              </span>
            </label>
          );
        })}
      </div>
      {hint && <p className="hint cm-field__hint">{hint}</p>}
    </fieldset>
  );
}

// ------------------------------------------------------------------ color picker
interface ColorPickerProps {
  value: string;
  onChange: (hex: string) => void;
  palette: NamedColor[]; // profile palette (first group)
  classics: NamedColor[]; // common blank colors
  valueName: string;
  disabled?: boolean;
}

export function ColorPicker({ value, onChange, palette, classics, valueName, disabled }: ColorPickerProps) {
  const name = useId();
  const [draft, setDraft] = useState(value.toUpperCase());
  useEffect(() => setDraft(value.toUpperCase()), [value]);
  const draftValid = normalizeHex(draft) !== null;

  // Avoid showing the same color in both groups.
  const paletteHexes = new Set(palette.map((c) => normalizeHex(c.hex)));
  const groups: { title: string; colors: NamedColor[] }[] = [
    { title: "From your palette", colors: palette },
    { title: "Classic blanks", colors: classics.filter((c) => !paletteHexes.has(normalizeHex(c.hex))) },
  ].filter((grp) => grp.colors.length > 0);

  return (
    <fieldset className="cm-field" disabled={disabled}>
      <legend className="label cm-field__legend">
        Shirt color <span className="cm-field__value">{valueName}</span>
      </legend>
      {groups.map((grp, gi) => (
        <div key={grp.title} className="cm-swatches">
          <p className="tiny muted cm-swatches__title">{grp.title}</p>
          <div className="cm-swatches__row">
            {grp.colors.map((c) => {
              const hex = normalizeHex(c.hex) ?? c.hex;
              const on = hex === value;
              return (
                <label key={`${gi}-${hex}`} className={`cm-swatch${on ? " is-on" : ""}`} title={`${c.name} (${hex.toUpperCase()})`}>
                  <input type="radio" name={`${name}-${gi}`} value={hex} checked={on} onChange={() => onChange(hex)} />
                  <span className="cm-swatch__dot" style={{ background: hex, color: readableOn(hex) }} aria-hidden="true">
                    {on ? "✓" : ""}
                  </span>
                  <span className="visually-hidden">{c.name}</span>
                </label>
              );
            })}
          </div>
        </div>
      ))}
      <div className="cm-custom-color">
        <label className="cm-custom-color__pick">
          <input type="color" value={value} onChange={(e) => onChange(e.target.value.toLowerCase())} />
          <span>Custom</span>
        </label>
        <label className="cm-custom-color__hex">
          <span className="visually-hidden">Hex color code</span>
          <input
            className="input mono"
            value={draft}
            maxLength={7}
            spellCheck={false}
            autoComplete="off"
            aria-invalid={!draftValid}
            onChange={(e) => {
              setDraft(e.target.value);
              const hex = normalizeHex(e.target.value);
              if (hex && /^#?[0-9a-f]{6}$/i.test(e.target.value.trim())) onChange(hex);
            }}
            onBlur={() => {
              const hex = normalizeHex(draft);
              if (hex) onChange(hex);
              else setDraft(value.toUpperCase());
            }}
          />
        </label>
      </div>
    </fieldset>
  );
}

// ------------------------------------------------------------------ quantity stepper
interface QuantityProps {
  value: number;
  min?: number;
  max?: number;
  onChange: (n: number) => void;
  hint?: ReactNode;
  disabled?: boolean;
}

export function QuantityStepper({ value, min = 1, max = 50, onChange, hint, disabled }: QuantityProps) {
  const id = useId();
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  const clamp = (n: number) => Math.min(max, Math.max(min, Math.round(n)));

  return (
    <div className="cm-field">
      <label className="label cm-field__legend" htmlFor={id}>
        Quantity
      </label>
      <div className="cm-qty">
        <button type="button" className="cm-qty__btn" aria-label="Decrease quantity" disabled={disabled || value <= min} onClick={() => onChange(clamp(value - 1))}>
          −
        </button>
        <input
          id={id}
          className="cm-qty__input"
          type="number"
          inputMode="numeric"
          min={min}
          max={max}
          value={draft}
          disabled={disabled}
          onChange={(e) => {
            setDraft(e.target.value);
            const n = Number(e.target.value);
            if (e.target.value !== "" && Number.isFinite(n) && n >= min && n <= max) onChange(clamp(n));
          }}
          onBlur={() => {
            const n = Number(draft);
            const next = Number.isFinite(n) && draft !== "" ? clamp(n) : value;
            setDraft(String(next));
            if (next !== value) onChange(next);
          }}
        />
        <button type="button" className="cm-qty__btn" aria-label="Increase quantity" disabled={disabled || value >= max} onClick={() => onChange(clamp(value + 1))}>
          +
        </button>
      </div>
      {hint && <p className="hint cm-field__hint">{hint}</p>}
    </div>
  );
}
