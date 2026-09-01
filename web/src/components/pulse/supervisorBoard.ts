/** How one objective's follow-through state is CLASSIFIED (#885).
 *
 *  Split from the component so the rule is importable without dragging a render tree along, and
 *  because the file that renders it exports components only (`react-refresh/only-export-components`).
 *
 *  The rule takes its inputs from the STRUCTURED fields the server sends and never from `why_not`.
 *  That sentence is prose authored by `mission_supervisor.may_nudge`, and parsing it here would
 *  create a second, silently-drifting copy of the supervisor's vocabulary.
 */
import type { SupervisorObjective } from "../../types/api";

export type Board = "held" | "spent" | "waiting" | "unknown" | "ready" | "met";

/** ORDER IS THE CONTRACT, not an implementation detail:
 *
 *  - `met` first — a finished objective is finished whatever its budget says;
 *  - `held` before `spent` — a stand-down is the OPERATOR'S instruction, and reporting their own
 *    choice back to them as "budget exhausted" misattributes it to a limit we hit;
 *  - `ready` before `spent` — `may_nudge` is the server's own verdict and outranks any inference
 *    from the remaining count;
 *  - `unknown` before `spent` — and this one is a correctness fix, not a preference. When the
 *    action ledger cannot be read the server reports `remaining: 0`, because no budget can be
 *    justified from a file it could not open. That is "we don't know", not "it is spent", and
 *    inferring SPENT from the number alone put the board in direct contradiction with the sentence
 *    printed beside it ("the action ledger could not be read, so the budget is unknown"). The
 *    server carries the discriminator structurally for exactly this reason; read it, don't guess.
 */
export function boardFor(o: SupervisorObjective): Board {
  if (o.met) return "met";
  if (o.stood_down) return "held";
  if (o.may_nudge) return "ready";
  if (o.unreadable) return "unknown";
  if (o.remaining <= 0) return "spent";
  return "waiting";
}

export const BOARD_LABEL: Record<Board, string> = {
  met: "MET",
  held: "HELD",
  spent: "SPENT",
  waiting: "WAITING",
  unknown: "UNKNOWN",
  ready: "READY",
};
