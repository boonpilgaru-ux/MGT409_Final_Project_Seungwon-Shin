// Top progress stepper: Country → Game → About you → Profile → Shop / Studio.
import { useLayoutEffect, useRef } from "react";
import { Link, useLocation } from "react-router-dom";
import type { SessionState } from "../types";

interface StepLink {
  label: string;
  path: string;
}

interface Step {
  key: string;
  label: string;
  links: StepLink[]; // last step splits into Shop / Studio
  reachable: boolean;
  done: boolean;
}

export function buildSteps(session: SessionState | null): Step[] {
  const hasSession = Boolean(session);
  const quizDone = Boolean(session?.quiz_done);
  const hasProfile = Boolean(session?.profile);
  return [
    { key: "country", label: "Country", links: [{ label: "Country", path: "/" }], reachable: true, done: hasSession },
    { key: "game", label: "Game", links: [{ label: "Game", path: "/quiz" }], reachable: hasSession, done: quizDone },
    {
      key: "about",
      label: "About you",
      links: [{ label: "About you", path: "/about-you" }],
      reachable: quizDone,
      done: hasProfile || Boolean(session?.context.length),
    },
    { key: "profile", label: "Profile", links: [{ label: "Profile", path: "/profile" }], reachable: quizDone, done: hasProfile },
    {
      key: "make",
      label: "Shop / Studio",
      links: [
        { label: "Shop", path: "/shop" },
        { label: "Studio", path: "/studio" },
      ],
      reachable: hasProfile,
      done: false,
    },
  ];
}

export function Stepper({ session }: { session: SessionState | null }) {
  const { pathname } = useLocation();
  const steps = buildSteps(session);
  const navRef = useRef<HTMLElement>(null);

  // On very narrow screens (320 px) the row still overflows: scroll the stepper itself (never the
  // page) so the current step, e.g. "Shop / Studio", is fully visible.
  useLayoutEffect(() => {
    const nav = navRef.current;
    const current = nav?.querySelector<HTMLElement>(".stepper__step.is-current");
    if (!nav || !current || nav.scrollWidth <= nav.clientWidth) return;
    const navBox = nav.getBoundingClientRect();
    const box = current.getBoundingClientRect();
    if (box.right > navBox.right) nav.scrollLeft += box.right - navBox.right;
    else if (box.left < navBox.left) nav.scrollLeft -= navBox.left - box.left;
  }, [pathname, session?.quiz_done, session?.profile]);

  return (
    <nav ref={navRef} className="stepper" aria-label="Progress">
      <ol className="stepper__list">
        {steps.map((step, i) => {
          const current = step.links.some((l) => l.path === pathname);
          const state = current ? "current" : step.done ? "done" : step.reachable ? "open" : "locked";
          return (
            <li key={step.key} className={`stepper__step is-${state}`}>
              <span className="stepper__dot" aria-hidden="true">
                {step.done && !current ? "✓" : i + 1}
              </span>
              <span className="stepper__labels">
                {step.links.map((link, j) => {
                  const here = link.path === pathname;
                  return (
                    <span key={link.path} className="stepper__label-wrap">
                      {j > 0 && <span className="stepper__sep" aria-hidden="true">/</span>}
                      {step.reachable && !here ? (
                        <Link className="stepper__label" to={link.path}>
                          {link.label}
                        </Link>
                      ) : (
                        <span
                          className={`stepper__label${here ? " is-here" : ""}`}
                          aria-current={here ? "step" : undefined}
                          aria-disabled={!step.reachable || undefined}
                        >
                          {link.label}
                        </span>
                      )}
                    </span>
                  );
                })}
              </span>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
