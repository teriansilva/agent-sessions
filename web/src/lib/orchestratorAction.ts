import type { OrchestratorAction, TerminalActionState } from "../types/api";

/** The verbs the actuator can actually deliver. The server ships the real set on the
 *  orchestrator state; this is the fallback when it has not arrived yet. */
export const DELIVERING_FALLBACK = new Set(["continue", "choose", "answer"]);

/** States in which an action is still waiting on the OPERATOR — mirrors the server's
 *  `orchestrator_ledger.OPERATOR_PENDING_STATES`, which decides whether a card carries a
 *  `pending_action` at all. `claimed` is excluded: the bytes are already going out. */
export const OPERATOR_PENDING = new Set(["proposed", "approved", "escalated"]);

/** Why an escalation escalated, in the operator's words. Keyed on the SERVER's
 *  `escalation_reason` — never on the state, and never re-derived here. Three paths reach
 *  `escalated` and only one of them is the confidence gate, so the old unconditional
 *  "below threshold" was false on the other two — and false on all three at any tier but
 *  `yolo`, where nothing reads `confidence_min` at all. */
const ESCALATION_SUFFIX: Record<string, string> = {
  confidence: "below threshold",
  model: "needs your call",
  degraded: "nothing deliverable",
};

/** The suffix for an action's confidence line, or `undefined` for none.
 *
 *  Two ways to get nothing, both deliberate. A record written before the server recorded a
 *  reason cannot be explained after the fact — the server does not know, so the row says
 *  nothing rather than guessing, and a wrong explanation is worse than an absent one. A reason
 *  this client does not recognise is the same situation from the other side: the server owns
 *  this vocabulary, and a client meeting a newer value must not invent copy for it. */
export function escalationSuffix(a: OrchestratorAction): string | undefined {
  if (a.state !== "escalated" || !a.escalation_reason) return undefined;
  return ESCALATION_SUFFIX[a.escalation_reason];
}

/** What became of a SETTLED action, for the history line on a Pulse card. The line used to
 *  print the ledger's own state name — `EXPIRED`, `STALE`, `INDETERMINATE` — which names a
 *  transition in a state machine the operator never sees.
 *
 *  Typed `Record<TerminalActionState, …>` so it cannot silently fall behind the ledger: adding
 *  a terminal state to `OrchestratorState` fails the build here rather than shipping an
 *  unworded row. */
const ACTION_OUTCOMES: Record<TerminalActionState, string> = {
  delivered: "sent",
  failed: "failed",
  indeterminate: "outcome unknown",
  rejected: "dismissed",
  stale: "session moved on",
  expired: "no decision in time",
  observed: "left alone",
};

/** The plain-words outcome, falling back to the raw state name. The fallback is not dead code
 *  guarded by the type above: the state arrives from the server at runtime, so a build compiled
 *  against an older vocabulary must degrade to the raw name rather than render an empty cell. */
export function actionOutcome(state: string): string {
  return (ACTION_OUTCOMES as Record<string, string | undefined>)[state] ?? state;
}

export function sessionPath(a: OrchestratorAction): string {
  const uuid = a.session_id.slice(a.session_id.indexOf(":") + 1);
  return `/s/${encodeURIComponent(a.engine)}/${encodeURIComponent(uuid)}`;
}
