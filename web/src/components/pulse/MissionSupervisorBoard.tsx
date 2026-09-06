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
  asked: styles.supAsked,
  spent: styles.supSpent,
  waiting: styles.supWaiting,
  unknown: styles.supUnknown,
  ready: styles.supReady,
};

function ObjectiveRow({
  o,
  budget,
  onStandDown,
  busy,
}: {
  o: SupervisorObjective;
  budget: number;
  /** Silence this objective for the episode it is RENDERED at. Absent ⇒ read-only. */
  onStandDown?: (key: string, episode: number) => void;
  busy: boolean;
}) {
  const board = boardFor(o);
  return (
    <li className={styles.supRow} data-board={board} data-key={o.key}>
      <span className={`${styles.supBadge} ${CLASS[board]}`}>
        {BOARD_LABEL[board]}
      </span>
      <span className={styles.supTitle}>
        {o.title || o.key}
        {o.gate ? (
          <span
            className={styles.supGate}
            title="A gate — the mission cannot finish until this is met"
          >
            GATE
          </span>
        ) : null}
      </span>
      <span
        className={styles.supBudget}
        title={`${o.spent} of ${budget} nudges spent this episode`}
      >
        {o.spent}/{budget}
        {o.episode > 1 ? (
          <span className={styles.supEpisode}> · ep {o.episode}</span>
        ) : null}
      </span>
      {/* The server's sentence, verbatim. Absent when the supervisor is free to act. */}
      {o.why_not ? <span className={styles.supWhy}>{o.why_not}</span> : null}
      {/* "Stop telling me about this one" (#889).
          Hidden once the objective is already stood down — HELD is the state this produces, so
          offering it again would suggest a second thing to do that does not exist. Also hidden on
          a settled objective: silencing something already met is a control with no effect.
          The EPISODE the row was rendered at is what gets sent, never "whatever is current". If
          the objective has moved since, the server answers 409 and the console re-renders — a
          stale tap must not silence a report nobody has seen. */}
      {onStandDown && !o.stood_down && !o.met ? (
        <button
          type="button"
          className={styles.objEditBtn}
          disabled={busy}
          onClick={() => onStandDown(o.key, o.episode)}
          data-testid="objective-stand-down"
          data-episode={o.episode}
          aria-label={`Stop following up on "${o.title || o.key}"`}
        >
          STAND DOWN
        </button>
      ) : null}
    </li>
  );
}

export function MissionSupervisorBoard({
  supervisor,
  budget = 3,
  onStandDown,
  busy = false,
}: {
  supervisor: MissionSupervisor | undefined;
  budget?: number;
  /** Absent ⇒ the board is read-only (an archived or closed mission). */
  onStandDown?: (key: string, episode: number) => void;
  busy?: boolean;
}) {
  if (!supervisor) {
    return (
      <div className={styles.supEmpty} data-testid="supervisor-unreadable">
        The supervisor's reading could not be produced for this mission. This is
        not a claim that there is nothing to follow up — it means the assessment
        did not run.
      </div>
    );
  }
  const objectives = supervisor.objectives ?? [];
  if (objectives.length === 0) {
    return (
      <div className={styles.supEmpty} data-testid="supervisor-unmeasured">
        No objectives, so there is nothing to follow through on. A mission with
        no objectives is unmeasured, which is not the same as done.
      </div>
    );
  }
  return (
    <div data-testid="supervisor-board">
      {/* NOTHING TO FOLLOW THROUGH WITH, said out loud (#896 review 7, finding 2).
          Releasing the last session leaves a `running` mission the supervisor cannot act on: it
          iterates held sessions and there are none. The server already refuses every nudge for
          that reason and says so per objective; this is the one sentence that explains the whole
          board at once, so the operator is not left inferring it from five identical rows.
          It is a claim about the MISSION, not about an agent — "idle" and "stalled" are claims
          about an agent, and there is no agent here to be either. */}
      {/* AND "WE COULD NOT LOOK" IS ITS OWN ANSWER (#896 review 8, finding 2). An unreadable
          roster reported as an empty one is a factual claim about the mission made from an I/O
          failure — the same lie the probe runner's three-way answer exists to prevent. The
          operator acts differently on each: one is "adopt a session", the other is "the store
          is unwell". */}
      {supervisor.sessions_unreadable ? (
        <div
          className={styles.supGates}
          data-testid="supervisor-sessions-unreadable"
        >
          This mission's sessions could not be read, so nothing can be sent.
          This is not a claim that it holds none.
        </div>
      ) : supervisor.no_session ? (
        <div className={styles.supGates} data-testid="supervisor-no-session">
          This mission holds no session, so there is nothing to follow through
          with. Adopt one, or close the mission.
        </div>
      ) : null}
      {supervisor.likely_done ? (
        <div
          className={styles.supProposal}
          data-testid="supervisor-likely-done"
        >
          Every gate is met — this mission <strong>looks</strong> finished.
          Nothing has been closed; the call is yours.
        </div>
      ) : supervisor.unmet_gates > 0 ? (
        <div className={styles.supGates} data-testid="supervisor-unmet-gates">
          {supervisor.unmet_gates} unmet{" "}
          {supervisor.unmet_gates === 1 ? "gate" : "gates"}
        </div>
      ) : null}
      <ul className={styles.supList}>
        {objectives.map((o) => (
          <ObjectiveRow
            key={o.key}
            o={o}
            budget={budget}
            onStandDown={onStandDown}
            busy={busy}
          />
        ))}
      </ul>
    </div>
  );
}
