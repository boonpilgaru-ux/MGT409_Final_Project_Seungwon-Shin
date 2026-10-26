// "Profile is stale" flag: set when About-you context changes after a profile exists, so the
// Profile page regenerates instead of showing the old read. Keyed by session id in localStorage.
const key = (sessionId: string) => `tee.profileStale.${sessionId}`;

export function markProfileStale(sessionId: string): void {
  try {
    window.localStorage.setItem(key(sessionId), "1");
  } catch {
    /* storage disabled: worst case the user hits "Regenerate" */
  }
}

export function isProfileStale(sessionId: string): boolean {
  try {
    return window.localStorage.getItem(key(sessionId)) === "1";
  } catch {
    return false;
  }
}

export function clearProfileStale(sessionId: string): void {
  try {
    window.localStorage.removeItem(key(sessionId));
  } catch {
    /* ignore */
  }
}
