import type { OrchestratorAction } from "../types/api";

/** The verbs the actuator can actually deliver. The server ships the real set on the
 *  orchestrator state; this is the fallback when it has not arrived yet. */
export const DELIVERING_FALLBACK = new Set(["continue", "choose", "answer"]);

/** States in which an action is still waiting on the OPERATOR — mirrors the server's
 *  `orchestrator_ledger.OPERATOR_PENDING_STATES`, which decides whether a card carries a
 *  `pending_action` at all. `claimed` is excluded: the bytes are already going out. */
export const OPERATOR_PENDING = new Set(["proposed", "approved", "escalated"]);

export function sessionPath(a: OrchestratorAction): string {
  const uuid = a.session_id.slice(a.session_id.indexOf(":") + 1);
  return `/s/${encodeURIComponent(a.engine)}/${encodeURIComponent(uuid)}`;
}
