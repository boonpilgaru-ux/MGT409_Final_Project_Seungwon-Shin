// Cycles through playful loading lines. Decorative (aria-hidden): pair it with a static
// role="status" label so screen readers aren't spammed every few seconds.
import { useCycle } from "./hooks";

interface RotatingMessageProps {
  messages: string[];
  intervalMs?: number;
  className?: string;
}

export function RotatingMessage({ messages, intervalMs = 2600, className = "" }: RotatingMessageProps) {
  const index = useCycle(messages.length, intervalMs);
  if (!messages.length) return null;
  return (
    <span key={index} className={`j-rotating ${className}`.trim()} aria-hidden="true">
      {messages[index]}
    </span>
  );
}
