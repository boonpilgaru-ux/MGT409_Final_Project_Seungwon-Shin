// One About-you context item: kind icon, name, status badge, one-line summary, delete button.
import { Spinner } from "../Spinner";
import type { ContextItem, ContextKind, ContextStatus } from "../../types";

const KIND: Record<ContextKind, { icon: string; label: string }> = {
  file: { icon: "📄", label: "File" },
  url: { icon: "🔗", label: "Link" },
  text: { icon: "📝", label: "Note" },
};

const STATUS: Record<ContextStatus, { label: string; className: string }> = {
  ok: { label: "Read", className: "badge-ok" },
  limited: { label: "Limited", className: "badge-limited" },
  error: { label: "Couldn’t read", className: "badge-error" },
};

interface ContextItemRowProps {
  item: ContextItem;
  removing: boolean;
  onRemove: (item: ContextItem) => void;
}

export function ContextItemRow({ item, removing, onRemove }: ContextItemRowProps) {
  const kind = KIND[item.kind] ?? KIND.text;
  const status = STATUS[item.status] ?? STATUS.limited;
  return (
    <li className={`j-ctx is-${item.status}${removing ? " is-removing" : ""}`}>
      <span className="j-ctx__icon" aria-hidden="true">
        {kind.icon}
      </span>
      <div className="j-ctx__main">
        <div className="j-ctx__head">
          <span className="j-ctx__name" title={item.name}>
            {item.name}
          </span>
          <span className={`chip ${status.className} j-ctx__badge`}>{status.label}</span>
        </div>
        {item.summary && <p className="j-ctx__summary">{item.summary}</p>}
        {item.error && <p className="j-ctx__error">{item.error}</p>}
        <p className="j-ctx__meta">
          {kind.label} · {item.chars.toLocaleString()} characters read
        </p>
      </div>
      <button
        type="button"
        className="icon-btn j-ctx__remove"
        aria-label={`Remove ${item.name}`}
        onClick={() => onRemove(item)}
        disabled={removing}
      >
        {removing ? <Spinner size={16} label="Removing" /> : "×"}
      </button>
    </li>
  );
}

/** Placeholder row while a just-submitted item is extracted and summarized. */
export function ContextItemPending({ kind, name }: { kind: ContextKind; name: string }) {
  return (
    <li className="j-ctx is-pending">
      <span className="j-ctx__icon" aria-hidden="true">
        {KIND[kind].icon}
      </span>
      <div className="j-ctx__main">
        <div className="j-ctx__head">
          <span className="j-ctx__name" title={name}>
            {name}
          </span>
          <span className="chip chip-soft j-ctx__badge">
            <Spinner size={12} label="Processing" /> Reading…
          </span>
        </div>
        <div className="skeleton skeleton-text" style={{ width: "90%" }} />
        <div className="skeleton skeleton-text" style={{ width: "60%" }} />
      </div>
    </li>
  );
}
