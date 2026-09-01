/** The supervisor's follow-through board (#885).
 *
 *  The operator's complaint this answers is "the supervisor did nothing" — which is
 *  indistinguishable from a broken feature unless the silence is *named*. So every row here
 *  renders one of four boards, and three of them are reasons not to act:
 *
 *    HELD    the operator stood this objective down    — quiet on purpose, not a fault
 *    SPENT   the per-episode nudge budget is gone      — needs a human
 *    WAITING a nudge is in flight or its fate is unknown
 *    READY   the supervisor may nudge this next pass
 *
 *  Two rules hold this together, and both are deliberate:
 *
 *  1. **The state is classified from the STRUCTURED fields, the sentence comes from the server.**
 *     `stood_down` / `remaining` / `may_nudge` pick the board; `why_not` is printed verbatim.
 *     The client never parses the prose and never writes its own copy of the refusal vocabulary —
 *     `mission_supervisor.may_nudge` is the single author of those sentences, so the two cannot
 *     drift into disagreeing about why nothing happened.
 *
 *  2. **An absent assessment is not an empty one.** The route attaches `supervisor` only when
 *     `assess()` succeeded; when it could not run the key is missing, and this renders "could not
 *     be read" rather than a clean board. "Nothing to follow up" and "we could not look" are
 *     different claims and the operator acts differently on each.
 */
import type { MissionSupervisor, SupervisorObjective } from "../../types/api";

import styles from "./mission.module.css";
import { BOARD_LABEL, boardFor, type Board } from "./supervisorBoard";

const CLASS: Record<Board, string> = {
  met: styles.supMet,
  held: styles.supHeld,
  spent: styles.supSpent,
  waiting: styles.supWaiting,
  unknown: styles.supUnknown,
  ready: styles.supReady,
};

function ObjectiveRow({ o, budget }: { o: SupervisorObjective; budget: number }) {
  const board = boardFor(o);
  return (
    <li className={styles.supRow} data-board={board} data-key={o.key}>
      <span className={`${styles.supBadge} ${CLASS[board]}`}>{BOARD_LABEL[board]}</span>
      <span className={styles.supTitle}>
        {o.title || o.key}
        {o.gate ? <span className={styles.supGate} title="A gate — the mission cannot finish until this is met">GATE</span> : null}
      </span>
      <span className={styles.supBudget} title={`${o.spent} of ${budget} nudges spent this episode`}>
        {o.spent}/{budget}
        {o.episode > 1 ? <span className={styles.supEpisode}> · ep {o.episode}</span> : null}
      </span>
      {/* The server's sentence, verbatim. Absent when the supervisor is free to act. */}
      {o.why_not ? <span className={styles.supWhy}>{o.why_not}</span> : null}
    </li>
  );
}

export function MissionSupervisorBoard({
  supervisor,
  budget = 3,
}: {
  supervisor: MissionSupervisor | undefined;
  budget?: number;
}) {
  if (!supervisor) {
    return (
      <div className={styles.supEmpty} data-testid="supervisor-unreadable">
        The supervisor's reading could not be produced for this mission. This is not a claim that
        there is nothing to follow up — it means the assessment did not run.
      </div>
    );
  }
  const objectives = supervisor.objectives ?? [];
  if (objectives.length === 0) {
    return (
      <div className={styles.supEmpty} data-testid="supervisor-unmeasured">
        No objectives, so there is nothing to follow through on. A mission with no objectives is
        unmeasured, which is not the same as done.
      </div>
    );
  }
  return (
    <div data-testid="supervisor-board">
      {supervisor.likely_done ? (
        <div className={styles.supProposal} data-testid="supervisor-likely-done">
          Every gate is met — this mission <strong>looks</strong> finished. Nothing has been closed;
          the call is yours.
        </div>
      ) : supervisor.unmet_gates > 0 ? (
        <div className={styles.supGates} data-testid="supervisor-unmet-gates">
          {supervisor.unmet_gates} unmet {supervisor.unmet_gates === 1 ? "gate" : "gates"}
        </div>
      ) : null}
      <ul className={styles.supList}>
        {objectives.map((o) => (
          <ObjectiveRow key={o.key} o={o} budget={budget} />
        ))}
      </ul>
    </div>
  );
}
