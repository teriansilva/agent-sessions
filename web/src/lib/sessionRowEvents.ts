import type { ProjectRef, Session } from "../types/api";
import type { api } from "./api";

/** Same-tab "this session's row changed" (#2173).
 *
 *  The session menu can be opened from the session pane itself, and a change made there (rename,
 *  tag, favorite, project, archive, an AI-review flag) must show on the sidebar row and on the
 *  pane's own row at once. The sidebar's list owns its rows and mirrors them into the shared
 *  store, so a pane that wrote the store directly would be overwritten by the list's next render:
 *  the same reason `missionEvents` exists. Announcing the change lets each owner apply it to what
 *  it owns: the list to its rows (`useSessionsList`), the store to the looked-up row of a session
 *  the list does not carry (`SessionsProvider`). The 15 s poll converges anything this misses.
 *
 *  `patch` holds the server's echoed values, never a guess: the announcer has already been
 *  answered. Its own module, like `missionEvents`, so a listener needs the event name and not the
 *  API client (the import below is a type). The two patch builders at the foot are here for the
 *  same reason the event is: the list's own handlers and the pane's must build the same patch.
 */
export const SESSION_ROW_CHANGED_EVENT = "agent-sessions:session-row-changed";

export interface SessionRowChange {
  /** The engine-qualified session key the server acted on. */
  key: string;
  /** The fields that changed, as the server reported them. */
  patch: Partial<Session>;
}

/** Announce a row change. Never throws: by the time this runs the server has recorded the change,
 *  and a missing `window` must not turn that success into a failure. */
export function announceSessionRowChanged(key: string, patch: Partial<Session>): void {
  try {
    window.dispatchEvent(
      new CustomEvent<SessionRowChange>(SESSION_ROW_CHANGED_EVENT, { detail: { key, patch } }),
    );
  } catch {
    // no-op: the change stands whether or not anything is listening
  }
}

/** What a finished "Review now" changes on a row (#356). The server's RESOLVED title is adopted
 *  verbatim (#1510): with reviewed titles kept off the display, "" is a real answer (an untitled
 *  session), not a missing one; `null` means the server could not resolve it, so the row keeps
 *  its own. The recap rides along (#481) so the session brief opened right after shows the new
 *  one. */
export function reviewPatch(r: Awaited<ReturnType<typeof api.reviewNow>>): Partial<Session> {
  return {
    ...(typeof r.title === "string" ? { title: r.title } : {}),
    ai_summary: r.ai_summary,
    ai_title: r.ai_title,
    intervention_required: r.intervention_required,
    intervention_reason: r.intervention_reason,
    reviewed_at: r.reviewed_at,
    review_excluded: r.review_excluded,
    ai_recap: r.ai_recap,
  };
}

/** A row's project after "Move to project": the chosen entity, or the folder fallback. */
export function projectAfterMove(ref: ProjectRef | null, cwd: string): ProjectRef {
  return ref && ref.kind === "project" ? ref : { kind: "folder", id: cwd, name: cwd };
}
