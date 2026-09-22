/** The supervisor's follow-through, ON the objective rows (#885, folded by #942).
 *
 *  The operator's complaint this answers is "the supervisor did nothing" — which is
 *  indistinguishable from a broken feature unless the silence is *named*. So every objective
 *  carries one of four boards, and three of them are reasons not to act:
 *
 *    HELD    the operator stood this objective down    — quiet on purpose, not a fault
 *    SPENT   the per-episode nudge budget is gone      — needs a human
 *    WAITING a nudge is in flight or its fate is unknown
 *    READY   the supervisor may nudge this next pass
 *
 *  **This used to be a panel of its own, and that was the mistake #942 names.** FOLLOW-THROUGH sat
 *  beside OBJECTIVES in a 340px stack, printing a second list of the same objectives under
 *  different titles — so reading "why has nothing happened to objective 3" meant finding row 3 in
 *  one list and row 3 in the other and trusting they were the same row. `assess()` iterates the
 *  mission's own objective rows (`mission_supervisor.assess`), so they always were. The board is
 *  now the row: `SupervisorCell` renders on the objective it describes.
 *
 *  What could NOT fold is the part that is about the MISSION rather than an objective — an
 *  assessment that could not run, a roster that could not be read, a mission holding no session,
 *  every gate met. Those have no row to attach to, and one of them (`unmeasured`) is precisely the
 *  case where there are no rows at all. They stay mission-level, above the list, as
 *  `MissionSupervisorNotices`.
 *
 *  Three rules hold this together, and all three are deliberate:
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
 *
 *  3. **STAND DOWN sends the episode the row was RENDERED at**, never "whatever is current". If
 *     the objective has moved since, the server answers 409 and the console re-renders — a stale
 *     tap must not silence a report nobody has seen. Folding the control onto the objective row
 *     changes where it sits and nothing else about that fence.
 */
import type { ReactNode } from "react";

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

/** The supervisor's reading of ONE objective: the meta line under that objective's title.
 *
 *  Everything here was a cell of the old board's grid. The badge, GATE, the `spent/budget` counter
 *  with its episode suffix, and the server's verbatim sentence — in that order, which is the order
 *  they were in. STAND DOWN moved into the row's ⋯ menu (#967 P3), with the rest of the row's
 *  actions; it still sends the episode the row was rendered at (`MissionObjectives`). */
export function SupervisorCell({
  o,
  budget = 3,
  lead,
  hideWhy = false,
}: {
  o: SupervisorObjective;
  budget?: number;
  /** Rendered first on the line — the objective's own state word. */
  lead?: ReactNode;
  /** The sentence is already said once above the list (#967 P3, `sharedReason`). */
  hideWhy?: boolean;
}) {
  const board = boardFor(o);
  return (
    <span className={styles.supCell} data-testid="supervisor-cell">
      <span className={`${styles.supBadge} ${CLASS[board]}`}>
        {BOARD_LABEL[board]}
      </span>
      {/* A GATE is a fact about the objective, and it travels here rather than on the objective
          row's own markup because the objective read does not carry it — `assess()` does. */}
      {o.gate ? (
        <span
          className={styles.supGate}
          title="A gate — the mission cannot finish until this is met"
        >
          GATE
        </span>
      ) : null}
      <span
        className={styles.supBudget}
        title={`${o.spent} of ${budget} nudges spent this episode`}
      >
        {o.spent}/{budget}
        {o.episode > 1 ? (
          <span className={styles.supEpisode}> · ep {o.episode}</span>
        ) : null}
      </span>
      {/* The objective's own state word, after the counter: the badge stays the line's first mark. */}
      {lead}
      {/* The server's sentence, verbatim. Absent when the supervisor is free to act, and when the
          section already says it once above the list (#967 P3). */}
      {o.why_not && !hideWhy ? (
        <span className={styles.supWhy}>{o.why_not}</span>
      ) : null}
    </span>
  );
}

/** What the supervisor has to say about the MISSION — the part with no objective to sit on.
 *
 *  Rendered above the objective list, and it renders whether or not there is a list: "no
 *  objectives, so there is nothing to follow through on" is the one notice whose whole meaning is
 *  that the list is empty. */
export function MissionSupervisorNotices({
  supervisor,
}: {
  supervisor: MissionSupervisor | undefined;
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
    <div data-testid="supervisor-notices">
      {/* NOTHING TO FOLLOW THROUGH WITH, said out loud (#896 review 7, finding 2).
          Releasing the last session leaves a `running` mission the supervisor cannot act on: it
          iterates held sessions and there are none. The server already refuses every nudge for
          that reason and says so per objective; this is the one sentence that explains the whole
          list at once, so the operator is not left inferring it from five identical rows.
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
      {/* NOTHING TO CHECK IS NOT PROGRESS (#1063). The fallback below reads `unmet_gates`, and
          `0` is what a mission with no gates reports for the same reason a finished one does —
          so a checklist that gates nothing rendered as silence here while `likely_done` (which
          counted the same way) congratulated it above. The server now sends the gate count, and
          the two cases are told apart rather than sharing a number. */}
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
      ) : supervisor.gates === 0 && supervisor.objectives.length > 0 ? (
        <div className={styles.supGates} data-testid="supervisor-no-gates">
          Nothing on this checklist gates completion, so this mission cannot
          confirm itself finished — the call is yours.
        </div>
      ) : null}
    </div>
  );
}
