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

function observedTime(epoch: number) {
  if (!Number.isFinite(epoch) || epoch < 0) return null;
  const date = new Date(epoch * 1000);
  if (!Number.isFinite(date.getTime())) return null;
  return { iso: date.toISOString(), label: observedFormat.format(date) };
}

function statusLabel(state: string) {
  return state ? state[0].toUpperCase() + state.slice(1).replace(/_/g, " ") : "Unknown";
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
      {first && <p className={styles.elapsed} aria-hidden="true">
        Since first observed {humanDuration(Math.max(0, now - task.started_at))}
        {last && ` · Updated ${humanDuration(Math.max(0, now - task.updated_at))} ago`}
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
  return (
    <section className={styles.activity} data-testid="structured-background-work" aria-label="Background activity">
      <p className={styles.head} role="status">Background tasks · {tasks.length} active</p>
      {tasks.map((task) => <TaskRow key={`${worker}:${task.task_id}`} task={task} now={now} />)}
      <p className={styles.note}>Runs outside a conversation turn. Stop ends the session’s worker.</p>
    </section>
  );
}

/** Older workers report only the aggregate activity flag, sometimes with bare task identities.
 *  Keep the existing fallback when no descriptive metadata is known. */
export function BackgroundWork({ native }: { native: StructuredSnapshot["native"] }) {
  const tasks = native?.background_tasks ?? [];
  if (native?.worker && tasks.some((task) =>
    task.description || task.task_type || task.last_tool_name || task.summary,
  )) return <TaskActivity tasks={tasks} worker={native.worker} />;
  return (
    <div className={`${chat.wait} ${pane.taskRow}`} role="status" data-testid="structured-background-work">
      <span className={chat.bars} aria-hidden="true"><span /><span /><span /></span>
      A background task is running
      <span className={pane.ownNote}>Not a turn: there is nothing to interrupt. Stop ends the worker.</span>
    </div>
  );
}
