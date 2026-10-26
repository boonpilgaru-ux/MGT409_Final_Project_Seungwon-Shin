// Balance game (SPEC §3, §6): 10 adaptive A-vs-B rounds. Click/tap or keys A/← and B/→.
// Resumes the current question after a reload; 409s resync from the server; finishing -> /about-you.
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { answerQuiz, ApiError, errorMessage, startQuiz } from "../api";
import { ErrorBanner } from "../components/ErrorBanner";
import { useAliveRef, useLatest, sleep } from "../components/journey/hooks";
import { appendAnswer, chosenOption, depthForRound, rejectedOption } from "../components/journey/journeyUtils";
import { OptionCard, type OptionCardState } from "../components/journey/OptionCard";
import { Duel, DuelSkeleton, LONG_LABEL_CHARS, QuizProgress } from "../components/journey/QuizBits";
import { RotatingMessage } from "../components/journey/RotatingMessage";
import { useRequiredSession } from "../SessionContext";
import "../styles/journey.css";
import { QUIZ_TOTAL, type Choice, type Question, type SessionState } from "../types";

/** loading = first question; picked = pick animation; waiting = skeleton for the next round. */
type Phase = "loading" | "ready" | "picked" | "waiting" | "finishing" | "error";

const PICK_ANIM_MS = 560; // let the chosen card pulse before swapping rounds
const SKELETON_AFTER_MS = 800; // if the next round isn't back by then, show skeleton cards

interface Failure {
  message: string;
  retry?: () => void;
}

export default function QuizPage() {
  const { session } = useRequiredSession();
  // Decided once on mount: when the last answer flips quiz_done, the game must stay mounted to
  // play its finish screen and navigate on to /about-you.
  const [showRecap] = useState(session.quiz_done);
  return showRecap ? <QuizRecap session={session} /> : <BalanceGame />;
}

function BalanceGame() {
  const { session, updateSession, refresh } = useRequiredSession();
  const navigate = useNavigate();
  const alive = useAliveRef();
  const sessionId = session.id;
  const total = session.current_question?.total ?? QUIZ_TOTAL;

  const [question, setQuestion] = useState<Question | null>(session.current_question);
  const [phase, setPhase] = useState<Phase>(session.current_question ? "ready" : "loading");
  const [picked, setPicked] = useState<Choice | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const started = useRef(false);
  const resyncs = useRef(0);

  const showQuestion = (q: Question) => {
    setQuestion(q);
    setPicked(null);
    setPhase("ready");
  };

  /** Re-read the session after a 409 and continue from whatever the server says is current. */
  const resync = async () => {
    const fresh = await refresh().catch(() => null);
    if (!alive.current || !fresh) return; // null = session gone; RequireSession redirects home
    if (fresh.quiz_done) {
      navigate("/about-you", { replace: true });
    } else if (fresh.current_question) {
      showQuestion(fresh.current_question);
      setNotice("Synced you to your latest round.");
    } else if (resyncs.current++ < 2) {
      void loadFirst();
    } else {
      setPhase("error");
      setFailure({ message: "We couldn't sync your game. Please reload the page." });
    }
  };

  const handleFailure = (err: unknown, retry: () => void) => {
    if (err instanceof ApiError && err.isNotFound) {
      void refresh().catch(() => undefined);
      return;
    }
    if (err instanceof ApiError && err.isConflict) {
      void resync();
      return;
    }
    setFailure({ message: errorMessage(err), retry });
  };

  const loadFirst = async () => {
    setPhase("loading");
    setFailure(null);
    try {
      const q = await startQuiz(sessionId);
      updateSession((s) => (s.id === sessionId ? { ...s, current_question: q } : s));
      if (!alive.current) return;
      resyncs.current = 0;
      showQuestion(q);
    } catch (err) {
      if (!alive.current) return;
      setPhase("error");
      handleFailure(err, () => void latest.current.loadFirst());
    }
  };

  const choose = async (choice: Choice) => {
    if (phase !== "ready" || !question) return;
    const q = question;
    setPicked(choice);
    setPhase("picked");
    setFailure(null);
    setNotice(null);
    const skeletonTimer = window.setTimeout(() => {
      if (alive.current) setPhase((p) => (p === "picked" ? "waiting" : p));
    }, SKELETON_AFTER_MS);

    try {
      const [res] = await Promise.all([answerQuiz(sessionId, { question_id: q.id, choice }), sleep(PICK_ANIM_MS)]);
      window.clearTimeout(skeletonTimer);
      const answered = { question: q, choice };
      // The server recorded the answer: sync shared state even if this page has unmounted.
      const sync = (patch: Partial<SessionState>) =>
        updateSession((s) => (s.id === sessionId ? { ...s, ...patch, answers: appendAnswer(s.answers, answered) } : s));

      if (res.done) {
        sync({ current_question: null, quiz_done: true });
        if (!alive.current) return;
        setPhase("finishing");
        await Promise.all([refresh().catch(() => null), sleep(1100)]);
        if (alive.current) navigate("/about-you");
        return;
      }
      if (res.next) sync({ current_question: res.next });
      if (!alive.current) return;
      if (res.next) showQuestion(res.next);
      else void resync();
    } catch (err) {
      window.clearTimeout(skeletonTimer);
      if (!alive.current) return;
      setPicked(null);
      setPhase("ready");
      handleFailure(err, () => void latest.current.choose(choice));
    }
  };

  const latest = useLatest({ choose, loadFirst, canChoose: phase === "ready" && Boolean(question) });

  // First mount: fetch round 1, or (when resuming) re-arm the server's prefetch for the current round.
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    if (session.current_question) startQuiz(sessionId).catch(() => undefined);
    else void loadFirst();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Keyboard: A / ← picks A, B / → picks B (ignored while typing or with modifiers).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.repeat || e.altKey || e.ctrlKey || e.metaKey) return;
      const target = e.target as HTMLElement | null;
      if (target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))) return;
      const key = e.key.toLowerCase();
      const choice: Choice | null =
        key === "a" || e.key === "ArrowLeft" ? "A" : key === "b" || e.key === "ArrowRight" ? "B" : null;
      if (!choice || !latest.current.canChoose) return;
      e.preventDefault();
      void latest.current.choose(choice);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [latest]);

  if (phase === "finishing") return <FinishScreen total={total} />;

  // While waiting for round N+1, the progress bar already shows N+1.
  const waitingIndex = question ? Math.min(question.index + 1, total) : session.answers.length + 1;
  const index = phase === "waiting" ? waitingIndex : (question?.index ?? session.answers.length + 1);
  const depth = phase === "waiting" || !question ? depthForRound(index) : question.depth;

  const cardState = (side: Choice): OptionCardState => (picked ? (picked === side ? "chosen" : "faded") : "idle");
  const loadingLines = [
    index === 1 ? "Warming up round 1…" : `Cooking up round ${index}…`,
    "Finding two equally tempting options…",
    `Thinking like someone from ${session.country.name}…`,
    "Making it hard on purpose…",
    "Checking the snack aisle for inspiration…",
  ];

  return (
    <section className="container section j-quiz">
      <QuizProgress index={index} total={total} depth={depth} />

      {notice && (
        <p className="j-quiz__notice fade-in" role="status">
          {notice}
        </p>
      )}
      {failure && question && (
        <ErrorBanner
          className="j-quiz__error"
          message={failure.message}
          onRetry={failure.retry}
          onDismiss={() => setFailure(null)}
        />
      )}

      {phase === "error" && !question ? (
        <div className="j-quiz__empty card-flat center stack" role="alert">
          <span className="j-quiz__empty-icon" aria-hidden="true">
            🫠
          </span>
          <p className="lead">The question machine stalled.</p>
          {failure && <p className="muted">{failure.message}</p>}
          <div className="j-quiz__empty-actions">
            <button type="button" className="btn btn-primary" onClick={() => void loadFirst()}>
              Try again
            </button>
          </div>
        </div>
      ) : phase === "loading" || phase === "waiting" || !question ? (
        <DuelSkeleton
          caption={
            <>
              <span className="visually-hidden" role="status">
                Loading round {index}.
              </span>
              <RotatingMessage messages={loadingLines} intervalMs={2200} />
            </>
          }
        />
      ) : (
        <div key={question.id} className="j-round j-round--enter">
          <h1 className="j-round__prompt">{question.prompt}</h1>
          <Duel
            long={Math.max(question.option_a.label.length, question.option_b.label.length) > LONG_LABEL_CHARS}
            a={
              <OptionCard
                side="A"
                option={question.option_a}
                state={cardState("A")}
                locked={phase !== "ready"}
                onChoose={(c) => void choose(c)}
              />
            }
            b={
              <OptionCard
                side="B"
                option={question.option_b}
                state={cardState("B")}
                locked={phase !== "ready"}
                onChoose={(c) => void choose(c)}
              />
            }
          />
          <p className="j-quiz__hint">
            <span className="j-quiz__hint-touch">Tap a card to pick.</span>
            <span className="j-quiz__hint-keys">
              Click a card, or press <kbd>A</kbd>/<kbd>←</kbd> or <kbd>B</kbd>/<kbd>→</kbd>.
            </span>{" "}
            No wrong answers.
          </p>
        </div>
      )}
    </section>
  );
}

function FinishScreen({ total }: { total: number }) {
  return (
    <section className="container section j-quiz">
      <QuizProgress index={total} total={total} depth="specific" />
      <div className="j-finish pop-in" role="status">
        <span className="j-finish__burst" aria-hidden="true">
          🎉
        </span>
        <h1 className="display j-finish__title">That’s a wrap!</h1>
        <p className="lead">
          {total} for {total}. Taking you to the next step…
        </p>
      </div>
    </section>
  );
}

/** Shown on /quiz after the game is over: every pick, plus the way forward. */
function QuizRecap({ session }: { session: SessionState }) {
  return (
    <section className="container section j-recap">
      <div className="stack-sm">
        <p className="eyebrow">Game complete · {session.answers.length} rounds</p>
        <h1>All your picks, in one place.</h1>
        <p className="lead">Want a bigger picture? Add a little more about you (optional), then see your profile.</p>
        <div className="row">
          <Link to="/about-you" className="btn btn-primary btn-lg">
            Next: about you →
          </Link>
          <Link to="/profile" className="btn btn-lg">
            {session.profile ? "See my profile" : "Skip to my profile"}
          </Link>
        </div>
      </div>
      <ol className="j-recap__list">
        {session.answers.map((a) => {
          const won = chosenOption(a);
          const lost = rejectedOption(a);
          return (
            <li key={a.question.id} className="j-recap__item">
              <span className="j-recap__num" aria-hidden="true">
                {a.question.index}
              </span>
              <div className="j-recap__text">
                <span className="j-recap__prompt">{a.question.prompt}</span>
                <span className="j-recap__pick">
                  <span aria-hidden="true">{won.emoji}</span> {won.label}
                </span>
                <span className="j-recap__other">over {lost.label}</span>
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
