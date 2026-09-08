/** Start a bounded, approval-gated SUB-AGENT alongside one session of a mission (#894).
 *
 *  **This control IS the approval.** There is no pending-spawn queue for something else to drain
 *  and nothing in the auto-verb set can reach the route: an operator typed a brief and pressed a
 *  button. That is the whole grant, and it is why the copy says what will happen rather than
 *  asking "are you sure?".
 *
 *  **It is withheld rather than allowed-to-fail at the cap.** The server is the only thing that
 *  enforces the limit — `claim_spawn` counts and reserves in one transaction — but a button that
 *  can only produce a 409 teaches the operator to distrust the ones that work, so the count comes
 *  down on the mission and the control says why it is unavailable. Same reasoning as the plan
 *  card's `ready`.
 *
 *  **The directory is shown, and the same value is asserted back on START (review 1, finding 1).**
 *  The server resolving the path stops a client naming one; it does not stop the path MOVING under
 *  a panel the operator is already reading. Re-resolving under the fence only pins the value from
 *  the tap onward, which is later than the approval. So the panel names the directory in the
 *  consequence line and sends it as `expect_cwd`; the server compares and discards it, and a
 *  mismatch is a refusal rather than a quiet relocation. An approval the operator could not read
 *  is not an approval.
 *
 *  **The cap is a resource guard and the copy says so.** "The limit bounds how much of this
 *  machine one mission may use — it is not a permission." Describing a fan-out bound as a safety
 *  boundary is the misreading the review warned about, and the operator is the person most likely
 *  to make it.
 */
import { useState } from "react";

import { ApiError, api } from "../../lib/api";

import styles from "./mission.module.css";

export function MissionSpawn({
  missionId,
  parentKey,
  engine,
  cwd,
  live,
  cap,
  busy,
  onChanged,
  onNote,
}: {
  missionId: string;
  /** The session this sub-agent is being started to work alongside. */
  parentKey: string;
  /** Which agent to start. The server re-checks it against the capability allowlist. */
  engine: string;
  /** Where it will run — SHOWN to the operator and asserted back on START, so the approval binds
   *  to a directory rather than to whatever the project resolves to at the moment of the tap. */
  cwd: string;
  /** How many live sub-agents this mission already holds, or `null` when the server could not
   *  determine it. **Not the same as zero** — an unknown budget rendered as "0 of 2 used" tells
   *  the operator every slot is free, which is the one reading the number must never support. */
  live: number | null;
  cap: number;
  busy: boolean;
  onChanged: (opts?: { membershipChanged?: boolean }) => void;
  onNote: (msg: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [brief, setBrief] = useState("");
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // UNKNOWN IS NOT AT CAP, AND IT IS NOT FREE EITHER (#894 review 5, carry-forward). The server
  // is the only admission control — `claim_spawn` counts and reserves in one transaction — so
  // withholding the button on an unknown would be stricter than the thing that enforces the
  // limit, and showing `0 of 2` would be a claim the server never made. The control stays
  // available and the count says it does not know; the server still refuses if there is no slot.
  const atCap = live !== null && live >= cap;
  const used = live === null ? "?" : String(live);

  const start = async () => {
    if (working || !brief.trim()) return;
    setWorking(true);
    setError(null);
    try {
      const out = await api.spawnSubAgent(missionId, parentKey, brief, engine, cwd);
      // THE ATTEMPT'S VERDICT, NOT THE MISSION'S STATE (review 3, finding 2).
      //
      // This read `out.state !== "running"`, which was correct only while a failed child also
      // failed its parent. It no longer does — deliberately, because a child's failure must not
      // end a running mission — so a start-evidence timeout came back as `state: "running"` and
      // this branch called it success: card closed, brief cleared, reason never shown. The
      // operator was told an agent had started when none had.
      //
      // `outcome` is the child's own answer. Falling back to the old comparison keeps a server
      // that has not been updated readable, but a present `outcome` always wins.
      const started =
        out.outcome != null ? out.outcome === "started" : out.state === "running";
      if (!started) {
        onNote(out.reason || `The sub-agent ended as ${out.state}.`);
      } else {
        // Only a real start clears the draft: a refusal that eats what you typed teaches you not
        // to type.
        setBrief("");
        setOpen(false);
      }
    } catch (err) {
      setError(
        err instanceof ApiError && err.message
          ? err.message
          : "That sub-agent could not be started.",
      );
    } finally {
      setWorking(false);
      // ON SETTLEMENT, not on success: a spawn that failed still moved the mission through
      // `dispatching` and may have left a durable record, so the console re-reads either way.
      onChanged({ membershipChanged: true });
    }
  };

  if (!open) {
    return (
      <button
        type="button"
        className={styles.objEditBtn}
        disabled={busy || atCap}
        onClick={() => setOpen(true)}
        title={
          atCap
            ? `This mission already holds ${used} of ${cap} sub-agents. A slot comes back when one of them STOPS — releasing a session hands over ownership but does not stop the agent, so it does not return capacity. The limit bounds how much of this machine one mission may use; it is not a permission.`
            : `Start a sub-agent alongside ${parentKey}`
        }
        data-testid="spawn-open"
      >
        {atCap ? `SUB-AGENT ${used}/${cap}` : "SUB-AGENT"}
      </button>
    );
  }

  return (
    <div className={styles.planCard} data-testid="spawn-card">
      <div className={styles.roster} data-testid="spawn-cap">
        {used} of {cap} used
      </div>
      <textarea
        className={styles.planBrief}
        value={brief}
        onChange={(e) => setBrief(e.target.value)}
        placeholder={`What should this sub-agent do alongside ${parentKey}?`}
        aria-label="What the sub-agent should do"
        data-testid="spawn-brief"
      />
      <div
        className={styles.planConfirm}
        id={`spawn-consequence-${parentKey}`}
        role="status"
        aria-live="polite"
        data-testid="spawn-consequence"
      >
        This starts {engine}, unattended, alongside {parentKey}, in{" "}
        <code data-testid="spawn-cwd">{cwd}</code>.
      </div>
      {error ? (
        <div
          className={styles.planError}
          role="alert"
          data-testid="spawn-error"
        >
          {error}
        </div>
      ) : null}
      <div>
        <button
          type="button"
          className={styles.send}
          disabled={working || !brief.trim()}
          aria-describedby={`spawn-consequence-${parentKey}`}
          onClick={() => void start()}
          data-testid="spawn-start"
        >
          {working ? "…" : "START SUB-AGENT"}
        </button>
        <button
          type="button"
          className={styles.objEditBtn}
          disabled={working}
          onClick={() => setOpen(false)}
          data-testid="spawn-cancel"
        >
          CANCEL
        </button>
      </div>
    </div>
  );
}
