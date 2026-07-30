import type { Session } from "../types/api";

/** What a session's leading status dot means (#477, extracted in #744).
 *
 *  `variant` is a `.hud-led` class; `label` is the accessible name so the state is never carried
 *  by colour alone. `role` is `undefined` for idle — an idle dot is decorative and would only add
 *  noise to a screen reader. */
export interface SessionStatus {
  variant: "attention" | "up" | "draft" | "idle";
  role: "img" | "status" | undefined;
  label: string | undefined;
  title: string;
}

/** The layer UNDER intervention: the status inputs an AI review cannot change. Split out so the
 *  session brief — which re-reviews in place and therefore holds a fresher intervention pair than
 *  the store does — can take only these from the row and supply the pair itself, instead of
 *  carrying two sources for one fact. A `Session` satisfies it. */
export type SessionStatusBase = Pick<Session, "review_excluded" | "working" | "has_draft">;

/** Exactly the row fields the dot is resolved from. */
export type SessionStatusInput = SessionStatusBase &
  Pick<Session, "intervention_required" | "intervention_reason">;

/** THE session-status resolver: one dot carries the whole row state, highest precedence first —
 *  intervention (orange, never masked by a draft) > working (green) > unsent draft (blue) > idle
 *  (grey).
 *
 *  Extracted from `SessionList` in #744 so the sidebar row and the session brief resolve the SAME
 *  state from the SAME row. This is deliberately NOT the terminal's `headStatus()`, which answers
 *  a different question — whether THIS browser's socket is attached (`LIVE` / `OFFLINE`). Socket
 *  `LIVE` is not "agent working", and the two must never be shown through one dot. */
export function sessionStatus(s: SessionStatusInput): SessionStatus {
  if (s.intervention_required && !s.review_excluded) {
    return {
      variant: "attention",
      role: "img",
      label: `intervention required: ${s.intervention_reason || "see session"}`,
      title: s.intervention_reason || "Intervention required",
    };
  }
  if (s.working) {
    return { variant: "up", role: "status", label: "agent working", title: "agent working" };
  }
  if (s.has_draft) {
    return { variant: "draft", role: "img", label: "unsent draft", title: "Unsent draft" };
  }
  return { variant: "idle", role: undefined, label: undefined, title: "idle" };
}
