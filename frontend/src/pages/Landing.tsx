// Landing / Country (SPEC §2.1): hero, searchable country picker, create session -> /quiz.
import { useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { errorMessage } from "../api";
import { ErrorBanner } from "../components/ErrorBanner";
import { CountryPicker } from "../components/journey/CountryPicker";
import { DemoDuel } from "../components/journey/DemoDuel";
import { progressLabel, resumePath } from "../components/journey/journeyUtils";
import {
  findCountry,
  guessCountry,
  isSupportedCountry,
  SUPPORTED_COUNTRIES,
  toCountry,
  type CountryInfo,
} from "../countries";
import { clearShortlist, getSessionId } from "../session";
import { useSession } from "../SessionContext";
import "../styles/journey.css";

const STEPS = [
  { icon: "⚖️", title: "Play 10 rounds", text: "Funny this-or-thats from your country" },
  { icon: "🪞", title: "Get your vibe", text: "A personality & taste profile" },
  { icon: "👕", title: "Get your tee", text: "Shop real ones or design your own" },
];

export default function LandingPage() {
  const { session, status, error: restoreError, refresh, startSession } = useSession();
  const navigate = useNavigate();
  // Only preselect the browser's country when it's one we currently play.
  const guessed = useMemo(() => {
    const hit = guessCountry();
    return hit && isSupportedCountry(hit.code) ? hit : undefined;
  }, []);
  const [country, setCountry] = useState<CountryInfo | null>(() => guessed ?? null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const quickPicks = SUPPORTED_COUNTRIES;

  const existing = status === "ready" ? session : null;
  const restoring = status === "loading"; // wait so we know whether there's progress to protect
  // Restoring failed (server unreachable): we can't see the stored session's progress, so protect it.
  const unknownProgress = status === "error" && Boolean(getSessionId());
  const hasProgress =
    unknownProgress || Boolean(existing && (existing.answers.length || existing.current_question || existing.quiz_done));

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!country || busy || restoring) return;
    // Same country and nothing played yet: just pick up the existing session.
    if (existing && !hasProgress && existing.country.code === country.code) {
      navigate("/quiz");
      return;
    }
    const replaceMessage = unknownProgress
      ? "We couldn't load your saved game. Start a new game anyway? Your saved progress on this device will be replaced."
      : "Start a new game? Your current progress on this device will be replaced.";
    if (hasProgress && !window.confirm(replaceMessage)) {
      return;
    }
    const replacing = Boolean(getSessionId());
    setBusy(true);
    setError(null);
    try {
      await startSession(toCountry(country));
      if (replacing) clearShortlist(); // new player, new shortlist
      navigate("/quiz");
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  };

  return (
    <section className="container j-landing">
      <div className="j-landing__hero">
        <div className="j-landing__copy stack">
          <h1 className="display j-landing__title">
            10 silly questions.
            <br />
            1 t-shirt that’s <span className="accent">totally you</span>.
          </h1>
          <p className="lead j-landing__lead">
            Pick sides in a funny this-or-that game about your country’s food, culture and hot takes, and we’ll find
            real tees that match your personality, or design a custom one with you.
          </p>

          {unknownProgress && (
            <ErrorBanner
              className="j-landing__restore-error"
              message={`${restoreError ?? "We couldn't load your saved game."} Your progress is still saved.`}
              onRetry={() => void refresh().catch(() => undefined)}
            />
          )}
          {existing && (
            <div className="j-landing__resume card-flat slide-up">
              <span className="j-landing__resume-flag" aria-hidden="true">
                {findCountry(existing.country.code)?.flag ?? "🏳️"}
              </span>
              <div className="j-landing__resume-text">
                <strong>Welcome back.</strong>
                <span className="small muted">
                  {existing.country.name} · {progressLabel(existing)}
                </span>
              </div>
              <Link to={resumePath(existing)} className="btn btn-dark btn-sm">
                Continue →
              </Link>
            </div>
          )}

          <form className="card stack j-landing__form" onSubmit={onSubmit} aria-busy={busy}>
            <CountryPicker
              value={country}
              onChange={setCountry}
              label="Where are you from?"
              disabled={busy}
              countries={SUPPORTED_COUNTRIES}
              placeholder="Pick your country…"
              hint={
                guessed && country?.code === guessed.code
                  ? "Guessed from your browser. Change it if that’s not you."
                  : "The questions are tailored to your country."
              }
            />
            <div className="j-landing__quick" role="group" aria-label="Quick picks">
              {quickPicks.map((c) => (
                <button
                  key={c.code}
                  type="button"
                  className="chip j-landing__quick-chip"
                  aria-pressed={country?.code === c.code}
                  onClick={() => setCountry(c)}
                  disabled={busy}
                  title={c.name}
                >
                  <span className="j-landing__quick-flag" aria-hidden="true">
                    {c.flag}
                  </span>
                  <span aria-hidden="true">{c.code}</span>
                  <span className="visually-hidden">{c.name}</span>
                </button>
              ))}
            </div>
            <p className="small muted center j-landing__coming">
              <span aria-hidden="true">🌏</span> Now playing: {SUPPORTED_COUNTRIES.length} countries. More countries
              coming soon!
            </p>
            {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}
            <button type="submit" className="btn btn-primary btn-lg btn-block" disabled={!country || busy || restoring}>
              {busy ? "Setting the table…" : existing || unknownProgress ? "Start a new game →" : "Start the game →"}
            </button>
            <p className="tiny muted center j-landing__fine">Free · no sign-up · about 3 minutes</p>
          </form>
        </div>

        <div className="j-landing__aside">
          <div className="j-how">
            <h2 className="eyebrow j-how__label" id="j-how-label">
              How it works
            </h2>
            <ol className="j-steps" aria-labelledby="j-how-label">
              {STEPS.map((step, i) => (
                <li key={step.title} className="j-steps__item">
                  <span className="j-steps__head" aria-hidden="true">
                    <span className="j-steps__num">{i + 1}</span>
                    <span className="j-steps__icon">{step.icon}</span>
                  </span>
                  <span className="j-steps__body">
                    <strong className="j-steps__title">{step.title}</strong>
                    <span className="j-steps__text">{step.text}</span>
                  </span>
                </li>
              ))}
            </ol>
          </div>
          <DemoDuel />
        </div>
      </div>
    </section>
  );
}
