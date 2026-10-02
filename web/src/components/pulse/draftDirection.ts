/** An AI-drafted direction as a decision, decided without a DOM (#983 P3).
 *
 *  The supervisor's model may draft a direction for an objective the operator has not written one
 *  for. The text is model-authored, so it is only ever a PROPOSAL: the card shows it verbatim and
 *  waits for a tap. Nothing on this surface can make it send on its own, and nothing here decides
 *  whether it may be sent. That is the server's projection (`can_approve` / `can_reject`), read
 *  as given. */
import type { OrchestratorAction } from "../../types/api";

/** The verb, spelled as the server spells it (`prefs.DRAFT_DIRECTION_VERB`). */
export const DRAFT_VERB = "draft_direction";

export interface DraftView {
  /** The objective's title, or its key when the title could not be read. */
  objective: string;
  /** Verbatim: the stored, sanitized draft. Never trimmed, re-wrapped or re-rendered. */
  text: string;
  /** Send as written: only when the server offers Approve. */
  canSend: boolean;
  /** Edit: only where a composer can take it, and only while the draft is still waiting. */
  canEdit: boolean;
  canDismiss: boolean;
  /** THE OPERATOR'S OPT-IN (#983 P4). The threshold at or above which a draft is typed with nobody
   *  reading it, or `null` when the mode is off — which is the default, and in which case the card
   *  says exactly what P3 said. A card that still promises "never sent on its own" while the mode
   *  is on would be the most misleading sentence in the app, so the copy follows the pref. */
  autoThreshold: number | null;
}

/** The opt-in as the card needs it: on/off plus the threshold. */
export interface AutoDirections {
  on: boolean;
  threshold: number;
}

export function isDraftDirection(a: OrchestratorAction): boolean {
  return a.verb === DRAFT_VERB && a.source === "supervisor" && typeof a.draft === "string";
}

export function draftView(
  a: OrchestratorAction,
  controls: {
    /** The server's `can_approve`. There is no legacy fallback: drafts postdate the projection. */
    approvable: boolean;
    rejectable: boolean;
    /** Whether this surface has a composer to open the draft in. */
    editable: boolean;
    /** The orchestrator block's opt-in, when the surface knows it. Absent reads as off. */
    auto?: AutoDirections | null;
  },
): DraftView | null {
  if (!isDraftDirection(a)) return null;
  const auto = controls.auto;
  return {
    objective: a.objective_title?.trim() || a.objective_key || "an objective",
    text: a.draft as string,
    autoThreshold:
      auto && auto.on && Number.isFinite(auto.threshold) ? auto.threshold : null,
    canSend: controls.approvable,
    // An edit REPLACES the draft, which the server does only while the draft is still waiting:
    // the same condition under which it can be dismissed.
    canEdit: controls.editable && controls.rejectable,
    canDismiss: controls.rejectable,
  };
}

/** A draft handed to the composer by Edit. `nonce` makes a second Edit of the same draft a fresh
 *  prefill, so text the operator cleared comes back when they ask for it again. */
export interface DraftEdit {
  actionId: string;
  sessionKey: string;
  text: string;
  nonce: number;
}
