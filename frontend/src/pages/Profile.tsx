// Profile reveal (SPEC §2.4): POST /api/profile on first visit (or when About-you changed since),
// playful loading state, reveal card, CTAs to Shop / Studio, and "Regenerate". Once a profile is
// shown, the shop search and the first 3 design concepts start in the background.
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError, buildProfile, errorMessage, getUsage } from "../api";
import { ErrorBanner } from "../components/ErrorBanner";
import { mergeDesigns, prefetchDesigns } from "../components/commerce/designJobs";
import { prefetchShop, profileKey } from "../components/commerce/shopSearch";
import { useAliveRef, useLatest } from "../components/journey/hooks";
import { clearProfileStale, isProfileStale } from "../components/journey/profileFreshness";
import { ProfileLoader } from "../components/journey/ProfileLoader";
import { ProfileReveal } from "../components/journey/ProfileReveal";
import { RequireQuizDone } from "../components/journey/RequireQuizDone";
import { useRequiredSession } from "../SessionContext";
import type { Profile, UsageSummary } from "../types";
import "../styles/journey.css";

interface Failure {
  message: string;
  retry?: () => void;
}

export default function ProfilePage() {
  return (
    <RequireQuizDone>
      <ProfileView />
    </RequireQuizDone>
  );
}

function ProfileView() {
  const { session, updateSession, refresh } = useRequiredSession();
  const navigate = useNavigate();
  const alive = useAliveRef();
  const sessionId = session.id;
  const profile = session.profile;

  // Regenerate on arrival if there's no profile yet, or About-you changed after the last one.
  const [needsBuild] = useState(() => !session.profile || isProfileStale(session.id));
  const [loading, setLoading] = useState(needsBuild);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const started = useRef(false);

  // While they read the reveal: the 30-75 s shop search and the ~15 s first design concepts.
  const startBackgroundWork = (revealed: Profile) => {
    const key = profileKey(revealed);
    prefetchShop(sessionId, key);
    prefetchDesigns(sessionId, key, session.designs.length > 0, (fresh) =>
      updateSession((s) => (s.id === sessionId ? { ...s, designs: mergeDesigns(s.designs, fresh) } : s)),
    );
  };

  const generate = async () => {
    setLoading(true);
    setFailure(null);
    try {
      const next = await buildProfile(sessionId);
      // The server already saved it: keep shared state current even if the user left this page.
      updateSession((s) => (s.id === sessionId ? { ...s, profile: next } : s));
      clearProfileStale(sessionId);
      startBackgroundWork(next);
      if (alive.current) window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (err) {
      if (!alive.current) return;
      if (err instanceof ApiError && err.isNotFound) {
        void refresh().catch(() => undefined); // session gone: RequireSession sends us home
        return;
      }
      if (err instanceof ApiError && err.isConflict) {
        const fresh = await refresh().catch(() => null);
        if (!alive.current) return;
        if (fresh && !fresh.quiz_done) {
          navigate("/quiz", { replace: true });
          return;
        }
      }
      setFailure({ message: errorMessage(err), retry: () => void latest.current.generate() });
    } finally {
      if (alive.current) setLoading(false);
    }
  };

  useEffect(() => {
    let live = true;
    const loadUsage = () => void getUsage(sessionId).then((next) => { if (live) setUsage(next); }).catch(() => undefined);
    loadUsage();
    const timer = window.setInterval(loadUsage, 3000);
    return () => { live = false; window.clearInterval(timer); };
  }, [sessionId]);

  const latest = useLatest({ generate });

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    if (needsBuild) void generate();
    else if (session.profile) startBackgroundWork(session.profile);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (loading) {
    return (
      <section className="container section">
        <ProfileLoader session={session} regenerating={Boolean(profile)} />
      </section>
    );
  }

  if (!profile) {
    return (
      <section className="container container-narrow section">
        <div className="j-profile__failed card stack center" role="alert">
          <span className="j-profile__failed-icon" aria-hidden="true">
            🪞
          </span>
          <h1>The mirror fogged up.</h1>
          <p className="lead">{failure?.message ?? "We couldn’t build your profile just now."}</p>
          <div className="j-profile__failed-actions">
            <button type="button" className="btn btn-primary btn-lg" onClick={() => void generate()}>
              Try again
            </button>
            <Link to="/about-you" className="btn btn-ghost">
              Back to About you
            </Link>
          </div>
        </div>
      </section>
    );
  }

  return (
    <section className="container section">
      {failure && (
        <ErrorBanner
          className="j-profile__banner"
          message={`Couldn’t regenerate: ${failure.message}`}
          onRetry={failure.retry}
          onDismiss={() => setFailure(null)}
        />
      )}
      <ProfileReveal
        profile={profile}
        country={session.country}
        answerCount={session.answers.length}
        contextCount={session.context.length}
        usage={usage}
        actions={
          <>
            <button type="button" className="btn btn-ghost" onClick={() => void generate()}>
              <span aria-hidden="true">↻</span> Regenerate
            </button>
            <Link to="/about-you" className="btn btn-ghost">
              <span aria-hidden="true">＋</span> Add more about you
            </Link>
          </>
        }
      />
    </section>
  );
}
