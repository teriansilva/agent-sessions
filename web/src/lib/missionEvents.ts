import type { SessionMissionRef } from "../types/api";

/** Same-tab "this session's mission changed" (#948 P5).
 *
 *  Adoption can start from two surfaces — the sidebar row's ⋯ menu and the session pane's header
 *  — and both must flip to the held state at once. The sidebar's list owns its rows and copies
 *  them into the shared store, so a header that wrote the store directly would be overwritten by
 *  the list's next render. Announcing the change lets the list apply it to its OWN rows, which is
 *  the one update every consumer then sees. The 15s poll converges anything this misses.
 *
 *  Its own module, like `actionEvents`, so a listener needs the event name and not the API
 *  client — and so test files that mock `lib/api` wholesale do not lose the constant.
 */
export const SESSION_MISSION_CHANGED_EVENT = "agent-sessions:session-mission-changed";

export interface SessionMissionChange {
  /** The engine-qualified session key the server acted on. */
  key: string;
  /** The mission that now holds the session, or `null` when none does. */
  mission: SessionMissionRef | null;
}

/** Announce a membership change. Never throws: by the time this runs the server has already
 *  recorded the adoption, and a missing `window` must not turn that success into a failure. */
export function announceSessionMissionChanged(key: string, mission: SessionMissionRef | null): void {
  try {
    window.dispatchEvent(
      new CustomEvent<SessionMissionChange>(SESSION_MISSION_CHANGED_EVENT, {
        detail: { key, mission },
      }),
    );
  } catch {
    // no-op — the adoption stands regardless of whether anything is listening
  }
}
