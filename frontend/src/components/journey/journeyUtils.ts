// Pure helpers for the journey pages: depth labels, answer bookkeeping, safe URL/color handling.
import { QUIZ_TOTAL, type AnsweredQuestion, type Depth, type Option, type SessionState } from "../../types";

/** Same curve as backend quiz.depth_for_round: 1-3 broad, 4-7 focused, 8-10 specific. */
export function depthForRound(index: number): Depth {
  if (index <= 3) return "broad";
  if (index <= 7) return "focused";
  return "specific";
}

export const DEPTH_META: Record<Depth, { label: string; icon: string; hint: string }> = {
  broad: { label: "Broad strokes", icon: "🌍", hint: "Food fights and everyday vibes." },
  focused: { label: "Zooming in", icon: "🔍", hint: "Culture, ideas and joke laws." },
  specific: { label: "Oddly specific", icon: "🎯", hint: "Your vibe, down to the details." },
};

export const chosenOption = (a: AnsweredQuestion): Option =>
  a.choice === "A" ? a.question.option_a : a.question.option_b;

export const rejectedOption = (a: AnsweredQuestion): Option =>
  a.choice === "A" ? a.question.option_b : a.question.option_a;

/** Append without duplicating a question that's already recorded (e.g. after a resync). */
export function appendAnswer(answers: AnsweredQuestion[], next: AnsweredQuestion): AnsweredQuestion[] {
  return answers.some((a) => a.question.id === next.question.id) ? answers : [...answers, next];
}

/** Where a returning player should land. */
export function resumePath(session: SessionState): string {
  if (session.profile) return "/profile";
  if (session.quiz_done) return "/about-you";
  return "/quiz";
}

/** Short human status for a session ("Round 4 of 10", "Profile ready", ...). */
export function progressLabel(session: SessionState): string {
  if (session.profile) return "Profile ready";
  if (session.quiz_done) return "Game complete";
  if (!session.answers.length && !session.current_question) return "Not started yet";
  return `Round ${Math.min(session.answers.length + 1, QUIZ_TOTAL)} of ${QUIZ_TOTAL}`;
}

const HEX_RE = /^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/i;

/** Only accept plain hex colors for inline CSS custom properties. */
export const safeColor = (value: string | null | undefined, fallback = "#ff3b30"): string =>
  value && HEX_RE.test(value.trim()) ? value.trim() : fallback;

/** Only http(s) URLs may become img src / link href. */
export function safeHttpUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" ? url.toString() : null;
  } catch {
    return null;
  }
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
