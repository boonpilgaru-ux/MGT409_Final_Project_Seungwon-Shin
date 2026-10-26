// Gate shown on Shop / Studio when the session has no profile yet (the API would answer 409).
import { Link } from "react-router-dom";

interface NeedProfileProps {
  quizDone: boolean;
  /** Subject of the sentence, e.g. "The shop". */
  purpose: string;
  /** Render just the card (when already inside a page section). */
  bare?: boolean;
}

export function NeedProfile({ quizDone, purpose, bare = false }: NeedProfileProps) {
  const card = (
    <div className="card cm-need stack pop-in">
      <p className="eyebrow">One step first</p>
      <h1>{quizDone ? "Reveal your profile first" : "Finish the balance game first"}</h1>
      <p className="lead">{purpose} works from your Tee:dentity profile, which is built from your balance-game answers.</p>
      <div className="row">
        {quizDone ? (
          <Link className="btn btn-primary" to="/profile">
            Reveal my profile
          </Link>
        ) : (
          <Link className="btn btn-primary" to="/quiz">
            Back to the game
          </Link>
        )}
      </div>
    </div>
  );
  return bare ? card : <section className="container section">{card}</section>;
}
