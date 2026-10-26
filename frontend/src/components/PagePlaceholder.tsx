import type { ReactNode } from "react";

/** Temporary page body used by scaffold placeholders until the real page lands. */
export function PagePlaceholder({ eyebrow, title, children }: { eyebrow: string; title: string; children?: ReactNode }) {
  return (
    <section className="container section placeholder">
      <div className="stack" style={{ maxWidth: 560, alignItems: "center" }}>
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        {children}
      </div>
    </section>
  );
}
