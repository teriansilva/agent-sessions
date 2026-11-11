/** Background task observations (#1461), separate from an interruptible agent-owned turn: the
 *  rows and notices the background strip (#1726, `BackgroundStrip.tsx`) shows.
 *  Native reports are inert text. Clocks mean time since observation, never measured work. */
import { humanDuration } from "../../lib/format";
import type { StructuredBackgroundTask } from "../../types/api";
import pane from "./StructuredPane.module.css";
import styles from "./BackgroundWork.module.css";

const observedFormat = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium", timeStyle: "medium",
});

function observedTime(epoch: number | null) {
  if (epoch === null || !Number.isFinite(epoch) || epoch < 0) return null;
  const date = new Date(epoch * 1000);
  if (!Number.isFinite(date.getTime())) return null;
  return { iso: date.toISOString(), label: observedFormat.format(date) };
}

function statusLabel(state: string) {
  return state ? state[0].toUpperCase() + state.slice(1).replace(/_/g, " ") : "Unknown";
}

/** A process the agent started (#1724, the process floor): program name and pid, and when it
 *  started. Never its command line. */
export function ProcessRow({ task, now }: { task: StructuredBackgroundTask; now: number }) {
  const started = observedTime(task.started_at);
  return (
    <article className={`${styles.task} ${styles.process}`} data-testid="structured-background-process"
      data-state={task.state}>
      <div className={styles.heading}>
        <b className={styles.title}>{task.comm} · pid {task.pid}</b>
        <span className={styles.status}>
          <span className={`${pane.led} ${pane.ledUp}`} aria-hidden="true" />
          Process
        </span>
      </div>
      {started && task.started_at !== null && <p className={styles.elapsed} aria-hidden="true">
        Started {humanDuration(Math.max(0, now - task.started_at))} ago
      </p>}
      <details className={styles.details}>
        <summary>Details</summary>
        <dl className={styles.facts}>
          <dt>Program</dt><dd>{task.comm}</dd>
          <dt>PID</dt><dd>{task.pid}</dd>
          {started && <><dt>Started</dt><dd><time dateTime={started.iso}>{started.label}</time></dd></>}
        </dl>
      </details>
    </article>
  );
}

/** One engine-reported task (#1461), shared with the background strip (#1726). */
export function TaskRow({ task, now }: { task: StructuredBackgroundTask; now: number }) {
  const first = observedTime(task.started_at);
  const last = observedTime(task.updated_at);
  const report = task.summary || (task.last_tool_name ? `Last tool: ${task.last_tool_name}` : null);
  return (
    <article className={styles.task} data-testid="structured-background-task" data-state={task.state}>
      <div className={styles.heading}>
        <b className={styles.title}>{task.description || "Background task"}</b>
        <span className={styles.status}>
          <span className={`${pane.led} ${task.state === "running" ? pane.ledUp : pane.ledIdle}`} aria-hidden="true" />
          {statusLabel(task.state)}
        </span>
      </div>
      {/* Seconds changing inside the live transcript should never repeat an announcement.
          Accessible, stable observation times remain inspectable in Details. */}
      {first && task.started_at !== null && <p className={styles.elapsed} aria-hidden="true">
        Since first observed {humanDuration(Math.max(0, now - task.started_at))}
        {last && task.updated_at !== null && ` · Updated ${humanDuration(Math.max(0, now - task.updated_at))} ago`}
      </p>}
      {task.state === "uncertain" && <p className={styles.report}>
        A report of this task was too large to read, so it may have ended, and its requests are refused until the agent lists it again.
      </p>}
      {report && <p className={styles.report}>
        <span className={styles.label}>Latest report</span>
        <span>{report}</span>
      </p>}
      <details className={styles.details}>
        <summary>Details</summary>
        <dl className={styles.facts}>
          {task.task_type && <><dt>Task type</dt><dd>{task.task_type}</dd></>}
          {task.last_tool_name && <><dt>Last tool</dt><dd>{task.last_tool_name}</dd></>}
          <dt>Task ID</dt><dd>{task.task_id}</dd>
          {first && <><dt>First observed</dt><dd><time dateTime={first.iso}>{first.label}</time></dd></>}
          {last && <><dt>Last reported</dt><dd><time dateTime={last.iso}>{last.label}</time></dd></>}
        </dl>
      </details>
    </article>
  );
}

/** The live worker's process scan can't be read (#1724): an unknown scan is never idle, so this
 *  session won't idle out. Said out loud, with the way to end it: Stop, which asks first and
 *  offers Stop everything for exactly this state. */
export function ScanUnknown({ onStop }: { onStop?: () => void }) {
  return (
    <div className={styles.scanUnknown} role="status" data-testid="structured-scan-unknown">
      <span className={`${pane.led} ${pane.ledIdle}`} aria-hidden="true" />
      <span className={styles.scanUnknownText}>
        BattleLab can’t list this session’s processes — it won’t idle out.
      </span>
      {onStop && (
        <button type="button" className={styles.scanUnknownStop} onClick={onStop}>
          Stop…
        </button>
      )}
    </div>
  );
}

/** Jobs an interrupted exact Stop paused and could not resume (#1724): never left silently
 *  suspended. Said out loud, with Stop everything, which ends them. */
export function PausedUnresumed({ count, onStopEverything }: { count: number; onStopEverything?: () => void }) {
  return (
    <div className={styles.scanUnknown} role="alert" data-testid="structured-paused-unresumed">
      <span className={`${pane.led} ${pane.ledWarn}`} aria-hidden="true" />
      <span className={styles.scanUnknownText}>
        {count === 1 ? "1 job was" : `${count} jobs were`} paused by an interrupted Stop and could
        not be resumed — Stop everything ends them.
      </span>
      {onStopEverything && (
        <button type="button" className={styles.scanUnknownStop} onClick={onStopEverything}>
          Stop everything…
        </button>
      )}
    </div>
  );
}

/** Frames this worker couldn't read, outside any turn (#1725): those a turn carried are rows on that
 *  turn; this is the rest, and it is never silence. A fact, not a failure. */
export function FramesSkipped({ count }: { count: number }) {
  return (
    <div className={styles.scanUnknown} role="status" data-testid="structured-frames-skipped">
      <span className={`${pane.led} ${pane.ledIdle}`} aria-hidden="true" />
      <span className={styles.scanUnknownText}>
        {count === 1 ? "1 frame" : `${count} frames`} too large to read (over 1 MiB) {count === 1 ? "was" : "were"} skipped.
      </span>
    </div>
  );
}

/** Requests refused because their task id had already ended (#1725), with no turn to carry a row:
 *  never silence, and nothing for the operator to answer. */
export function RequestsRefused({ count }: { count: number }) {
  return (
    <div className={styles.scanUnknown} role="status" data-testid="structured-requests-refused">
      <span className={`${pane.led} ${pane.ledIdle}`} aria-hidden="true" />
      <span className={styles.scanUnknownText}>
        {count === 1 ? "1 request" : `${count} requests`} from a background task that had already
        ended {count === 1 ? "was" : "were"} refused.
      </span>
    </div>
  );
}

/** How long past its due time a wakeup still shows (#1927): the worker's own grace for the turn
 *  it starts. Past it, the wake didn't fire and the session idles out as before. */
export const WAKE_GRACE_SECONDS = 120;

/** The agent scheduled its own next check (#1927): the session stays up until then. A fact, not a
 *  job — nothing to stop or answer, so no control. */
export function WakeupPending({ at, reason }: { at: number; reason?: string }) {
  const time = new Date(at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return (
    <div className={styles.scanUnknown} role="status" data-testid="structured-wakeup">
      <span className={`${pane.led} ${pane.ledIdle}`} aria-hidden="true" />
      <span className={styles.scanUnknownText}>
        Wakes at {time}
        {reason ? ` · ${reason}` : ""}
      </span>
    </div>
  );
}

/** A background task's request is waiting in the conversation (#1725). The card itself stays there,
 *  with what it asks and its decision; this is what says so while reading further up. */
export function BackgroundRequestsWaiting({ count }: { count: number }) {
  return (
    <div className={styles.scanUnknown} role="status" data-testid="structured-background-waiting">
      <span className={`${pane.led} ${pane.ledWarn}`} aria-hidden="true" />
      <span className={styles.scanUnknownText}>
        {count === 1 ? "1 background request is" : `${count} background requests are`} waiting for you in the conversation.
      </span>
    </div>
  );
}

/** A full conversation waiting for what still runs (#1725): held, not ended. A state in grey with
 *  its word, the count as honest as the list, and the way out now. Nothing is lost. */
export function RolloverHeld({ sentence, onStopResume }: { sentence: string; onStopResume?: () => void }) {
  return (
    <section className={styles.held} role="status" aria-label="Conversation held"
      data-testid="structured-rollover-held">
      <p className={styles.heldTag}>
        <span className={`${pane.led} ${pane.ledIdle}`} aria-hidden="true" />
        Held
      </p>
      <p className={styles.heldSentence}>{sentence}</p>
      <p className={styles.heldNote}>
        Nothing is lost: the next message starts a fresh connection. Or stop now and resume.
      </p>
      {onStopResume && (
        <button type="button" className={styles.scanUnknownStop} onClick={onStopResume}
          data-testid="structured-rollover-stop">
          Stop and resume
        </button>
      )}
    </section>
  );
}
