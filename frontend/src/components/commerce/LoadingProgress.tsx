// Progress panel for long AI waits (shop search 30-75 s, design generation 20-60 s):
// rotating status messages, an easing progress bar that never claims 100%, and an elapsed timer.
import { useEffect, useState } from "react";
import { Spinner } from "../Spinner";
import { formatElapsed } from "./format";

/** Seconds since `startedAt` (ms epoch), ticking once a second while `startedAt` is set. */
export function useElapsed(startedAt: number | null): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (startedAt === null) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [startedAt]);
  return startedAt === null ? 0 : Math.max(0, (now - startedAt) / 1000);
}

interface LoadingProgressProps {
  title: string;
  messages: string[];
  startedAt: number;
  /** Typical duration in seconds; the bar reaches ~85% around then. */
  expectedSeconds: number;
  /** Seconds each message stays up (the last one stays until done). */
  messageEvery?: number;
  className?: string;
}

export function LoadingProgress({ title, messages, startedAt, expectedSeconds, messageEvery = 7, className = "" }: LoadingProgressProps) {
  const elapsed = useElapsed(startedAt);
  const step = Math.min(messages.length - 1, Math.floor(elapsed / messageEvery));
  const pct = Math.min(96, 100 * (1 - Math.exp((-1.9 * elapsed) / expectedSeconds)));
  const slow = elapsed > expectedSeconds * 1.25;

  return (
    <div className={`cm-progress card ${className}`.trim()} aria-busy="true">
      <div className="cm-progress__head">
        <Spinner size={30} label={title} />
        <div className="cm-progress__text">
          <p className="cm-progress__title">{title}</p>
          <p className="cm-progress__msg" aria-live="polite">
            {slow ? "Taking a little longer than usual. Still on it…" : messages[step]}
          </p>
        </div>
        <span className="cm-progress__time mono" aria-hidden="true">
          {formatElapsed(elapsed)}
        </span>
      </div>
      <div
        className="cm-progress__bar"
        role="progressbar"
        aria-label={title}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(pct)}
      >
        <span style={{ width: `${pct}%` }} />
      </div>
      <ol className="cm-progress__steps" aria-hidden="true">
        {messages.map((m, i) => (
          <li key={m} className={i < step ? "is-done" : i === step ? "is-now" : undefined} />
        ))}
      </ol>
    </div>
  );
}
