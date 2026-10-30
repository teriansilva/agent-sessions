/** Stop, when background work is running (#1724): the app's one confirm dialog, listing what Stop
 *  ends as two kinds of observation — the agent's own tasks and the processes it started — never
 *  merged into one job count. Processes show only their program name and pid: their command
 *  lines can hold secrets and never leave the worker. Keep running comes first and holds focus.
 *
 *  Two kinds of confirmation (operator decision on PR #1757), never a heuristic:
 *  - **Exact** ("Stop and end jobs"): only for a COMPLETE list (every process and task named, a
 *    readable scan). The worker rechecks it against a fresh scan; any change comes back here
 *    ("changed since you opened this", new rows stamped NEW) and needs a new confirmation.
 *  - **Stop everything**: explicitly disclosed as ending everything the agent started in this
 *    session, listed or not, and bound to this worker generation. Offered beside the exact one
 *    on a refreshed list (so a looping job is one more tap, never an endless refresh), and as the
 *    ONLY confirm when the list exceeds what the dialog can show, the process scan can't be read,
 *    or the worker can't be reached. */
import { useEffect, useState } from "react";
import { humanDuration } from "../../lib/format";
import type { StructuredStopInventory } from "../../types/api";
import { ConfirmDialog } from "../templates/ConfirmDialog";
import pane from "./StructuredPane.module.css";
import styles from "./BackgroundWork.module.css";

const identity = (p: { pid: number; start: number }) => `${p.pid}:${p.start}`;
const DISCLOSURE =
  "Stopping ends everything the agent started in this session — including work started after this list or not listed here.";

function since(epoch: number | null | undefined, now: number): string | null {
  return typeof epoch === "number" && Number.isFinite(epoch) && epoch > 0
    ? humanDuration(Math.max(0, now - epoch))
    : null;
}

function plural(n: number, one: string, many: string) {
  return `${n} ${n === 1 ? one : many}`;
}

export function StopConfirmDialog({
  inventory,
  previous,
  busy,
  onKeep,
  onStop,
  returnFocusTo,
}: {
  inventory: StructuredStopInventory;
  /** The list the operator was looking at when the server refused (409): this one changed. */
  previous: StructuredStopInventory | null;
  busy: boolean;
  onKeep: () => void;
  /** `everything`: the Stop everything confirmation, else the exact one. */
  onStop: (everything: boolean) => void;
  returnFocusTo?: HTMLElement | null;
}) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), 10_000);
    return () => clearInterval(timer);
  }, []);
  const unreachable = inventory.reach === "unreachable";
  const unknown = unreachable || inventory.scan === "unknown";
  const moreTasks = inventory.engine_more ?? 0;
  const over = inventory.more > 0 || moreTasks > 0;
  // The exact confirmation needs a list the dialog can show in full.
  const exact = !unknown && !over && inventory.digest !== null;
  const seen = new Set((previous?.processes ?? []).map(identity));
  const seenTasks = new Set((previous?.tasks ?? []).map((t) => t.task_id));
  const processCount = inventory.processes.length + inventory.more;
  const taskCount = inventory.tasks.length + moreTasks;
  const jobs = taskCount + processCount;
  return (
    <ConfirmDialog
      tag="STOP SESSION"
      title="Stop this session?"
      cancelLabel="Keep running"
      confirmLabel={busy ? "Stopping…" : exact ? "Stop and end jobs" : "Stop everything"}
      danger
      safeFirst
      busy={busy}
      onCancel={onKeep}
      onConfirm={() => onStop(!exact)}
      secondary={
        exact && previous
          ? {
              label: "Stop everything",
              onClick: () => onStop(true),
              markers: { "data-testid": "structured-stop-everything" },
            }
          : undefined
      }
      returnFocusTo={returnFocusTo}
      markers={{
        dialog: {
          "data-testid": "structured-stop-confirm",
          "data-scan": inventory.scan,
          "data-kind": exact ? "exact" : "everything",
          ...(unreachable ? { "data-reach": "unreachable" } : {}),
        },
        confirm: { "data-testid": "structured-stop-confirm-go" },
      }}
    >
      {previous && (
        <p className={`${styles.notice} ${styles.noticeChanged}`} role="status"
          data-testid="structured-stop-changed">
          <span className={`${pane.led} ${pane.ledWarn}`} aria-hidden="true" />
          The background work changed since you opened this. Review the updated list.
        </p>
      )}
      {unreachable ? (
        <p className={styles.notice} role="status" data-testid="structured-stop-unreachable">
          <span className={`${pane.led} ${pane.ledIdle}`} aria-hidden="true" />
          BattleLab can’t reach this session’s worker. Nothing was stopped.
        </p>
      ) : unknown ? (
        <p className={styles.notice} role="status" data-testid="structured-stop-unknown">
          <span className={`${pane.led} ${pane.ledIdle}`} aria-hidden="true" />
          BattleLab can’t list this session’s processes right now.
        </p>
      ) : over ? (
        <p className={styles.notice} role="status" data-testid="structured-stop-over">
          <span className={`${pane.led} ${pane.ledIdle}`} aria-hidden="true" />
          More background work runs than this list can show.
        </p>
      ) : null}
      <p className={styles.stopLead}>
        {exact
          ? `Stopping ends ${plural(jobs, "background job", "background jobs")}:`
          : `Running now: ${unknown ? "processes unknown" : plural(processCount, "process", "processes")}, ${plural(taskCount, "agent task", "agent tasks")}.`}
      </p>
      {(inventory.tasks.length > 0 || moreTasks > 0) && (
        <section aria-label="Agent tasks">
          <h3 className={styles.stopGroup}>Agent tasks · {taskCount}</h3>
          <ul className={styles.stopList} data-testid="structured-stop-tasks">
            {inventory.tasks.map((t) => (
              <li key={t.task_id} data-new={previous && !seenTasks.has(t.task_id) ? "" : undefined}>
                <span className={`${pane.led} ${pane.ledUp}`} aria-hidden="true" />
                <span className={styles.stopName}>
                  {t.description || t.task_type || t.task_id}
                  {previous && !seenTasks.has(t.task_id) && <span className={styles.stamp}>New</span>}
                </span>
                <span className={styles.stopMeta}>
                  Agent task{since(t.started_at, now) ? ` · running ${since(t.started_at, now)}` : ""}
                </span>
              </li>
            ))}
            {moreTasks > 0 && (
              <li data-testid="structured-stop-more-tasks">
                <span className={styles.stopName}>and {plural(moreTasks, "more task", "more tasks")}</span>
              </li>
            )}
          </ul>
        </section>
      )}
      <section aria-label="Processes">
        <h3 className={styles.stopGroup}>
          Processes · {unknown ? "unknown" : processCount}
        </h3>
        {unknown ? (
          <ul className={styles.stopList} data-testid="structured-stop-processes">
            <li>
              <span className={styles.unknownMark} aria-hidden="true" />
              <span className={styles.stopName}>Can’t be listed</span>
              <span className={styles.stopMeta}>
                {unreachable ? "the worker didn’t answer" : "the process scan didn’t answer"}
              </span>
            </li>
          </ul>
        ) : processCount > 0 ? (
          <ul className={styles.stopList} data-testid="structured-stop-processes">
            {inventory.processes.map((p) => (
              <li key={identity(p)} data-new={previous && !seen.has(identity(p)) ? "" : undefined}>
                <span className={`${pane.led} ${pane.ledUp}`} aria-hidden="true" />
                <span className={styles.stopName}>
                  {p.comm} · pid {p.pid}
                  {previous && !seen.has(identity(p)) && <span className={styles.stamp}>New</span>}
                </span>
                <span className={styles.stopMeta}>
                  Process{since(p.started_at, now) ? ` · started ${since(p.started_at, now)} ago` : ""}
                </span>
              </li>
            ))}
            {inventory.more > 0 && (
              <li data-testid="structured-stop-more-processes">
                <span className={styles.stopName}>
                  and {plural(inventory.more, "more process", "more processes")}
                </span>
              </li>
            )}
          </ul>
        ) : (
          <p className={styles.stopMeta}>None running.</p>
        )}
      </section>
      {(!exact || previous) && (
        <p className={`${styles.notice} ${styles.noticeChanged}`}
          data-testid="structured-stop-everything-disclosure">
          {!exact ? "" : "Stop everything: "}{DISCLOSURE}
        </p>
      )}
      {exact && (
        <p className={styles.stopNote} data-testid="structured-stop-also-ends">
          Anything these jobs start while Stop runs also ends.
        </p>
      )}
      <p className={styles.stopNote}>
        The transcript is kept. Each job is recorded as <b>ended with worker</b>.
      </p>
    </ConfirmDialog>
  );
}
