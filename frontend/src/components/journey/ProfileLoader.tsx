// Profile synthesis wait (~10-20 s): bobbing tee, playful rotating lines that quote the
// player's own picks, an elapsed counter and an easing progress bar.
import { useMemo } from "react";
import type { SessionState } from "../../types";
import { useElapsedSeconds } from "./hooks";
import { chosenOption } from "./journeyUtils";
import { RotatingMessage } from "./RotatingMessage";

function buildLines(session: SessionState): string[] {
  const picks = session.answers.map((a) => chosenOption(a).label).filter(Boolean);
  const at = (frac: number) => picks[Math.min(picks.length - 1, Math.floor(picks.length * frac))];
  const lines = [
    `Reading between your ${picks.length || 10} picks…`,
    picks.length ? `Still thinking about “${at(0.1)}”…` : null,
    "Mixing your color palette…",
    `Adding a pinch of ${session.country.name}…`,
    "Measuring your sleeve-to-soul ratio…",
    picks.length > 4 ? `So… “${at(0.5)}”. Bold choice.` : null,
    session.context.length ? "Skimming what you shared…" : null,
    "Picking fonts that match your energy…",
    "Consulting the vibes committee…",
    picks.length > 8 ? `Cross-checking “${at(0.9)}” with your aesthetic…` : null,
    "Folding it all into one t-shirt brief…",
  ];
  return lines.filter((l): l is string => Boolean(l));
}

export function TeeGlyph({ className = "" }: { className?: string }) {
  return (
    <svg className={`j-tee ${className}`.trim()} viewBox="0 0 120 110" aria-hidden="true" focusable="false">
      <path
        className="j-tee__body"
        d="M42 8 18 18 4 40l16 12 8-8v60h64V44l8 8 16-12-14-22L78 8c-4 10-32 10-36 0Z"
      />
      <circle className="j-tee__dot" cx="60" cy="60" r="13" />
    </svg>
  );
}

export function ProfileLoader({ session, regenerating = false }: { session: SessionState; regenerating?: boolean }) {
  const lines = useMemo(() => buildLines(session), [session]);
  const elapsed = useElapsedSeconds();
  const progress = Math.round(94 * (1 - Math.exp(-elapsed / 9))); // eases toward ~94% by ~25 s

  return (
    <div className="j-loader card">
      <TeeGlyph />
      <p className="eyebrow">{regenerating ? "Taking another look" : "Building your profile"}</p>
      <p className="j-loader__line">
        <RotatingMessage messages={lines} intervalMs={2400} />
      </p>
      <span className="visually-hidden" role="status">
        Building your profile. This usually takes 10 to 20 seconds.
      </span>
      <div className="j-loader__bar" aria-hidden="true">
        <span style={{ width: `${Math.max(4, progress)}%` }} />
      </div>
      <p className="tiny muted">{elapsed}s · usually 10–20 seconds</p>
    </div>
  );
}
