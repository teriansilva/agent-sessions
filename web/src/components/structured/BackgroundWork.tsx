/** Background task observations (#1461), separate from an interruptible agent-owned turn.
 *  Native reports are inert text. Clocks mean time since observation, never measured work. */
import { useEffect, useState } from "react";
import { humanDuration } from "../../lib/format";
import type { StructuredBackgroundTask, StructuredSnapshot } from "../../types/api";
import chat from "../chat/ChatPane.module.css";
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
function ProcessRow({ task, now }: { task: StructuredBackgroundTask; now: number }) {
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

function TaskRow({ task, now }: { task: StructuredBackgroundTask; now: number }) {
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

function TaskActivity({ tasks, worker }: { tasks: StructuredBackgroundTask[]; worker: string }) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(timer);
  }, []);
  // Two kinds of observation (#1724), counted apart: an agent task and the process it runs
  // cannot be reliably matched, so they are never added up into one job count.
  const engine = tasks.filter((task) => task.source !== "process");
  const processes = tasks.filter((task) => task.source === "process");
  const head = [
    engine.length ? `${engine.length} ${engine.length === 1 ? "agent task" : "agent tasks"}` : null,
    processes.length ? `${processes.length} ${processes.length === 1 ? "process" : "processes"}` : null,
  ].filter(Boolean).join(" · ");
  return (
    <section className={styles.activity} data-testid="structured-background-work" aria-label="Background activity">
      <p className={styles.head} role="status">Background · {head} active</p>
      {engine.map((task) => <TaskRow key={`${worker}:${task.task_id}`} task={task} now={now} />)}
      {processes.map((task) => <ProcessRow key={`${worker}:${task.task_id}`} task={task} now={now} />)}
      <p className={styles.note}>Runs outside a conversation turn. Stop ends the session’s worker and these jobs.</p>
    </section>
  );
}

/** The live worker's process scan can't be read (#1724): an unknown scan is never idle, so this
 *  session won't idle out. Said out loud, with the way to end it: Stop, which asks first and
 *  offers Stop everything for exactly this state. */
function ScanUnknown({ onStop }: { onStop?: () => void }) {
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
function PausedUnresumed({ count, onStopEverything }: { count: number; onStopEverything?: () => void }) {
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

/** Older workers report only the aggregate activity flag, sometimes with bare task identities.
 *  Keep the existing fallback when no descriptive metadata is known. */
export function BackgroundWork({ native, onStop, onStopEverything }: {
  native: StructuredSnapshot["native"];
  /** The pane's Stop (#1724), offered beside an unreadable process scan. */
  onStop?: () => void;
  /** The pane's Stop everything (#1724), offered beside jobs that could not be resumed. */
  onStopEverything?: () => void;
}) {
  const unknown = !!native?.worker && native.stop_inventory?.scan === "unknown";
  const paused = native?.worker ? native.paused_unresumed ?? 0 : 0;
  const activity = backgroundActivity(native);
  return (
    <>
      {paused > 0 && <PausedUnresumed count={paused} onStopEverything={onStopEverything} />}
      {unknown && <ScanUnknown onStop={onStop} />}
      {activity}
    </>
  );
}

function backgroundActivity(native: StructuredSnapshot["native"]) {
  const tasks = native?.background_tasks ?? [];
  if (native?.worker && tasks.some((task) =>
    task.source === "process" || task.description || task.task_type || task.last_tool_name || task.summary,
  )) return <TaskActivity tasks={tasks} worker={native.worker} />;
  if (!native?.background_active && native?.stop_inventory?.scan === "unknown") return null;
  return (
    <div className={`${chat.wait} ${pane.taskRow}`} role="status" data-testid="structured-background-work">
      <span className={chat.bars} aria-hidden="true"><span /><span /><span /></span>
      A background task is running
      <span className={pane.ownNote}>Not a turn: there is nothing to interrupt. Stop ends the worker.</span>
    </div>
  );
}
