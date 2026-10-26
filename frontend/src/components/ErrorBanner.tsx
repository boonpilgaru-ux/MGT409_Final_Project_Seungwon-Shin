interface ErrorBannerProps {
  message: string;
  /** Shows a "Try again" button. */
  onRetry?: () => void;
  /** Shows a dismiss (×) button. */
  onDismiss?: () => void;
  className?: string;
}

/** Inline error callout (role="alert") with optional retry / dismiss actions. */
export function ErrorBanner({ message, onRetry, onDismiss, className = "" }: ErrorBannerProps) {
  return (
    <div className={`error-banner ${className}`.trim()} role="alert">
      <span className="error-banner__icon" aria-hidden="true">
        !
      </span>
      <p className="error-banner__msg">{message}</p>
      {onRetry && (
        <button type="button" className="btn btn-sm" onClick={onRetry}>
          Try again
        </button>
      )}
      {onDismiss && (
        <button type="button" className="btn btn-ghost btn-sm error-banner__close" onClick={onDismiss} aria-label="Dismiss">
          ×
        </button>
      )}
    </div>
  );
}
