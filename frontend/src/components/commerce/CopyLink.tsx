// Share box: read-only URL field + copy button (Clipboard API, falls back to selecting the text).
import { useEffect, useRef, useState } from "react";

export function CopyLink({ url, label = "Share link" }: { url: string; label?: string }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [state, setState] = useState<"idle" | "copied" | "manual">("idle");

  useEffect(() => {
    if (state === "idle") return;
    const timer = window.setTimeout(() => setState("idle"), 2400);
    return () => window.clearTimeout(timer);
  }, [state]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(url);
      setState("copied");
    } catch {
      // Clipboard blocked (insecure origin, permissions): select it so Ctrl/Cmd+C works.
      inputRef.current?.focus();
      inputRef.current?.select();
      setState("manual");
    }
  };

  return (
    <div className="cm-copy">
      <label className="label" htmlFor="cm-copy-url">
        {label}
      </label>
      <div className="cm-copy__row">
        <input
          id="cm-copy-url"
          ref={inputRef}
          className="input mono cm-copy__input"
          value={url}
          readOnly
          onFocus={(e) => e.currentTarget.select()}
        />
        <button type="button" className="btn btn-blue" onClick={copy}>
          {state === "copied" ? "Copied!" : "Copy link"}
        </button>
      </div>
      <p className="tiny muted" aria-live="polite">
        {state === "manual" ? "Press Ctrl+C (or Cmd+C) to copy." : state === "copied" ? "Link copied to your clipboard." : "Anyone with this link can view the order."}
      </p>
    </div>
  );
}
