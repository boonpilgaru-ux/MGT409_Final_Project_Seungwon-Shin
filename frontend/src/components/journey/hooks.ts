// Small hooks shared by the journey pages (Landing, Quiz, About you, Profile).
import { useEffect, useRef, useState, type MutableRefObject } from "react";

/** `ref.current` is false once the component unmounts; check it after every await. StrictMode-safe. */
export function useAliveRef(): MutableRefObject<boolean> {
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  return alive;
}

/** Ref that always holds the latest value (lets long-lived listeners call fresh closures). */
export function useLatest<T>(value: T): MutableRefObject<T> {
  const ref = useRef(value);
  ref.current = value;
  return ref;
}

/** Whole seconds elapsed since `running` last became true (0 while stopped). */
export function useElapsedSeconds(running = true): number {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    setSeconds(0);
    if (!running) return;
    const started = Date.now();
    const timer = window.setInterval(() => setSeconds(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => window.clearInterval(timer);
  }, [running]);
  return seconds;
}

/** Index that advances every `intervalMs` (wraps at `length`). */
export function useCycle(length: number, intervalMs: number, running = true): number {
  const [index, setIndex] = useState(0);
  useEffect(() => {
    if (!running || length < 2) return;
    const timer = window.setInterval(() => setIndex((i) => (i + 1) % length), intervalMs);
    return () => window.clearInterval(timer);
  }, [length, intervalMs, running]);
  return length ? index % length : 0;
}

export const sleep = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));
