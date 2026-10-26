interface SpinnerProps {
  /** Diameter in px (default 28). */
  size?: number;
  /** Screen-reader label (default "Loading"). */
  label?: string;
  className?: string;
}

/** Accessible spinning ring in the accent color. */
export function Spinner({ size = 28, label = "Loading", className = "" }: SpinnerProps) {
  return (
    <span className={`spinner ${className}`.trim()} role="status" style={{ width: size, height: size }}>
      <span className="visually-hidden">{label}</span>
    </span>
  );
}

/** Centered spinner with a visible caption, for whole-page waits. */
export function PageLoader({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="page-loader">
      <Spinner size={44} label={label} />
      <p className="muted">{label}</p>
    </div>
  );
}
