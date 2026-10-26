// Slide-in shortlist drawer (modal dialog): saved shirts with Buy links, persisted via session.ts.
import { useEffect, useRef, type RefObject } from "react";
import { clearShortlist, removeFromShortlist } from "../../session";
import type { Product } from "../../types";
import { formatProductPrice, safeHttpUrl } from "./format";
import { HeartIcon, ProductImage } from "./ProductCard";

interface ShortlistDrawerProps {
  open: boolean;
  items: Product[];
  onClose: () => void;
  /** Element to refocus when the drawer closes (the toggle button). */
  returnFocusRef?: RefObject<HTMLElement>;
}

const FOCUSABLE = 'a[href], button:not([disabled]), input, select, textarea, [tabindex]:not([tabindex="-1"])';

export function ShortlistDrawer({ open, items, onClose, returnFocusRef }: ShortlistDrawerProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  // Focus management, Escape to close, Tab trapped inside, background scroll locked.
  useEffect(() => {
    if (!open) return;
    const returnTo = returnFocusRef?.current;
    closeRef.current?.focus();
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key !== "Tab" || !panelRef.current) return;
      const nodes = Array.from(panelRef.current.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (!nodes.length) return;
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
      returnTo?.focus();
    };
  }, [open, onClose, returnFocusRef]);

  if (!open) return null;

  return (
    <div className="cm-drawer" role="presentation">
      <div className="cm-drawer__backdrop fade-in" onClick={onClose} aria-hidden="true" />
      <div ref={panelRef} className="cm-drawer__panel" role="dialog" aria-modal="true" aria-labelledby="shortlist-title">
        <header className="cm-drawer__head">
          <h2 id="shortlist-title" className="cm-drawer__title">
            <HeartIcon filled /> Shortlist <span className="muted">({items.length})</span>
          </h2>
          <button ref={closeRef} type="button" className="icon-btn" aria-label="Close shortlist" onClick={onClose}>
            ×
          </button>
        </header>

        {items.length === 0 ? (
          <div className="cm-drawer__empty">
            <p className="lead">Nothing saved yet.</p>
            <p className="muted">Tap the heart on any shirt to keep it here. Your shortlist stays on this device.</p>
          </div>
        ) : (
          <ul className="cm-drawer__list">
            {items.map((p) => {
              const href = safeHttpUrl(p.url);
              const price = formatProductPrice(p.price, p.currency);
              return (
                <li key={p.url} className="cm-drawer__item">
                  <ProductImage src={p.image_url} alt={p.title} seed={p.brand || p.retailer} className="cm-drawer__thumb" />
                  <div className="cm-drawer__info">
                    <p className="cm-drawer__brand">{p.brand}</p>
                    <p className="cm-drawer__name">{p.title}</p>
                    <p className="tiny muted">
                      {p.retailer}
                      {price && <> · {price}</>}
                    </p>
                    <div className="cm-drawer__actions">
                      {href && (
                        <a className="btn btn-dark btn-sm" href={href} target="_blank" rel="noopener noreferrer">
                          Buy <span aria-hidden="true">↗</span>
                          <span className="visually-hidden"> {p.title} (opens in a new tab)</span>
                        </a>
                      )}
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm cm-btn-quiet"
                        onClick={() => {
                          removeFromShortlist(p.url);
                          closeRef.current?.focus(); // the clicked button is about to unmount
                        }}
                      >
                        Remove<span className="visually-hidden"> {p.title}</span>
                      </button>
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        )}

        {items.length > 0 && (
          <footer className="cm-drawer__foot">
            <button
              type="button"
              className="btn btn-ghost btn-sm cm-btn-quiet"
              onClick={() => {
                if (window.confirm("Clear your whole shortlist?")) clearShortlist();
              }}
            >
              Clear all
            </button>
            <span className="tiny muted">Saved on this device only.</span>
          </footer>
        )}
      </div>
    </div>
  );
}
