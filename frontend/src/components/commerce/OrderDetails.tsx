// Read-only order pieces shared by the Studio and the Order page: price table and spec sheet.
import type { ReactNode } from "react";
import type { Price, ProductSpec } from "../../types";
import { approxColorName, colorName, type NamedColor } from "./color";
import { formatUSD } from "./pricing";
import { FITS, MATERIALS, NECKLINES, PLACEMENTS, PRINT_METHODS, SLEEVES, hintOf, labelOf } from "./specOptions";

interface PriceTableProps {
  price: Price;
  quantity: number;
  /** Label the figures as a client-side estimate. */
  estimate?: boolean;
  caption?: string;
  /** Tuck the line items into a <details> (for tight spaces). */
  collapsible?: boolean;
}

export function PriceTable({ price, quantity, estimate = false, caption, collapsible = false }: PriceTableProps) {
  const table = (
    <table className="cm-table cm-price__table">
      {caption && <caption className="visually-hidden">{caption}</caption>}
      <tbody>
        {price.breakdown.map((line, i) => (
          <tr key={`${line.label}-${i}`} className={line.amount < 0 ? "is-discount" : undefined}>
            <th scope="row">{line.label}</th>
            <td className="mono">{formatUSD(line.amount)}</td>
          </tr>
        ))}
      </tbody>
      <tfoot>
        <tr>
          <th scope="row">Total ({price.currency})</th>
          <td className="mono">{formatUSD(price.total)}</td>
        </tr>
      </tfoot>
    </table>
  );
  return (
    <div className="cm-price">
      <div className="cm-price__headline">
        <div>
          <span className="cm-price__big">{formatUSD(price.unit)}</span>
          <span className="muted"> / shirt</span>
        </div>
        <div className="cm-price__total">
          <span className="muted small">
            {quantity} shirt{quantity === 1 ? "" : "s"}
          </span>
          <strong>{formatUSD(price.total)}</strong>
        </div>
      </div>
      {collapsible ? (
        <details className="cm-price__details">
          <summary>Price breakdown</summary>
          {table}
        </details>
      ) : (
        table
      )}
      <p className="tiny muted cm-price__note">
        {estimate ? "Estimate. Your order page shows the final quote." : "Demo pricing in USD. No payment is taken."}
      </p>
    </div>
  );
}

interface SpecSheetProps {
  spec: ProductSpec;
  colorNames?: NamedColor[];
}

/** Full spec table: every ProductSpec field with a human label. */
export function SpecSheet({ spec, colorNames = [] }: SpecSheetProps) {
  const hex = spec.shirt_color.toUpperCase();
  const named = colorName(spec.shirt_color, colorNames);
  // A palette color has no name here (the order doesn't carry the profile): never print the hex twice.
  const shirtName = named.toUpperCase() === hex ? approxColorName(spec.shirt_color) : named;
  const rows: { label: string; value: ReactNode; hint?: string }[] = [
    {
      label: "Shirt color",
      value: (
        <span className="cm-spec__color">
          <span className="cm-spec__chip" style={{ background: spec.shirt_color }} aria-hidden="true" />
          {shirtName} <span className="mono muted">{hex}</span>
        </span>
      ),
    },
    { label: "Fit", value: labelOf(FITS, spec.fit), hint: hintOf(FITS, spec.fit) },
    { label: "Material", value: labelOf(MATERIALS, spec.material), hint: hintOf(MATERIALS, spec.material) },
    { label: "Neckline", value: labelOf(NECKLINES, spec.neckline) },
    { label: "Sleeves", value: labelOf(SLEEVES, spec.sleeve) },
    { label: "Size", value: spec.size },
    { label: "Print method", value: labelOf(PRINT_METHODS, spec.print_method), hint: hintOf(PRINT_METHODS, spec.print_method) },
    { label: "Placement", value: labelOf(PLACEMENTS, spec.placement), hint: hintOf(PLACEMENTS, spec.placement) },
    { label: "Quantity", value: spec.quantity },
    { label: "Notes", value: spec.notes.trim() ? <span className="cm-spec__notes">{spec.notes}</span> : <span className="muted">None</span> },
  ];
  return (
    <table className="cm-table cm-spec">
      <tbody>
        {rows.map((row) => (
          <tr key={row.label}>
            <th scope="row">{row.label}</th>
            <td>
              <div className="cm-spec__value">{row.value}</div>
              {row.hint && <div className="tiny muted">{row.hint}</div>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
