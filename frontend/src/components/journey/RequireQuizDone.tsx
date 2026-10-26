// Route guard for pages after the game (About you, Profile): unfinished quiz -> /quiz.
// Use inside <RequireSession> (App.tsx already wraps those routes).
import type { ReactNode } from "react";
import { Navigate } from "react-router-dom";
import { useRequiredSession } from "../../SessionContext";

export function RequireQuizDone({ children }: { children: ReactNode }) {
  const { session } = useRequiredSession();
  if (!session.quiz_done) return <Navigate to="/quiz" replace />;
  return <>{children}</>;
}
