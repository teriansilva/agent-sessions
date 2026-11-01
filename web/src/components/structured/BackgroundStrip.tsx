/** The background strip (#1726): one 44 px line glued above the composer while the LIVE
 *  generation has background work, expanding (the shared `<Disclosure>`, grid rows) into the
 *  job list. It replaces the inline transcript block of #1461 and reuses its task rows and its
 *  unknown-metadata fallback.
 *
 *  Display rule: only what `native.background_tasks` reports for the live worker — #1724 settles
 *  that inventory; this adds no lifecycle of its own. Jobs a generation's end cut off are history
 *  rows in the transcript, never strip rows, and with nothing running the strip is not rendered
 *  at all. Agent tasks and the processes the process floor observed (#1724: rows with
 *  `source: "process"`; a row without `source` is an agent task) are counted apart, never summed.
 *  The open list is height-capped (≤ 40 % of the pane) and scrolls inside, so the composer and
 *  the newest transcript row stay visible however many jobs run. Engine-agnostic: it reads the
 *  normalized snapshot and never names a client. */
import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { humanDuration } from "../../lib/format";
import type { StructuredBackgroundTask, StructuredSnapshot } from "../../types/api";
import { Disclosure } from "../hud/Controls";
import { PausedUnresumed, ProcessRow, ScanUnknown, TaskRow } from "./BackgroundWork";
import { backgroundWorking } from "./structuredView";
import styles from "./BackgroundStrip.module.css";

const described = (t: StructuredBackgroundTask) =>
  t.source === "process" || !!(t.description || t.task_type || t.last_tool_name || t.summary);

function plural(n: number, one: string, many: string) {
  return `${n} ${n === 1 ? one : many}`;
}

function jobName(t: StructuredBackgroundTask) {
  if (t.source === "process") return t.comm ? `${t.comm} · pid ${t.pid ?? "?"}` : "Process";
  return t.description || t.task_type || "Background task";
}

function since(t: StructuredBackgroundTask, now: number) {
  return typeof t.started_at === "number" && Number.isFinite(t.started_at)
    ? humanDuration(Math.max(0, now - t.started_at))
    : null;
}

/** Live work of the live generation only: reported tasks, or the aggregate activity flag of a
 *  worker that reports no task identities. */
function hasBackgroundJobs(snap: StructuredSnapshot | null): boolean {
  const native = snap?.native;
  if (!native?.worker) return false;
  return (
    (native.background_tasks?.length ?? 0) > 0 ||
    (!!native.background_active && backgroundWorking(snap))
  );
}

export function BackgroundStrip({
  snap,
  onStop,
  onStopEverything,
}: {
  snap: StructuredSnapshot | null;
  /** The pane's Stop (#1724), offered beside an unreadable process scan. */
  onStop?: () => void;
  /** The pane's Stop everything (#1724), offered beside jobs that could not be resumed. */
  onStopEverything?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [now, setNow] = useState(() => Date.now() / 1000);
  const rootRef = useRef<HTMLDivElement>(null);
  const [cap, setCap] = useState<number | null>(null);
  const jobs = hasBackgroundJobs(snap);
  // The notices of #1724 apply whatever else runs, so either one renders the strip by itself:
  // an unreadable process scan (never idle, so it won't idle out) and jobs an interrupted Stop
  // could not resume. Always visible, outside the collapsible list.
  const unknown = !!snap?.native?.worker && snap.native.stop_inventory?.scan === "unknown";
  const paused = snap?.native?.worker ? (snap.native.paused_unresumed ?? 0) : 0;
  const shown = jobs || unknown || paused > 0;
  // A minute-resolution clock for the one-line summary; the open rows re-render with it too.
  useEffect(() => {
    if (!shown) return;
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(timer);
  }, [shown]);
  // The open list never takes more than 40 % of the pane it sits in (a map window's body or the
  // full-screen pane — measured, not the viewport), so the log and the composer keep the rest.
  useLayoutEffect(() => {
    const pane = rootRef.current?.parentElement;
    if (!pane) return;
    const measure = () => setCap(Math.round(pane.clientHeight * 0.4));
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(pane);
    return () => ro.disconnect();
  }, [shown]);
  if (!shown || !snap?.native?.worker) return null;
  const worker = snap.native.worker;
  const tasks = (snap.native.background_tasks ?? []) as StructuredBackgroundTask[];
  const named = tasks.some(described);
  const engine = tasks.filter((t) => t.source !== "process");
  const processes = tasks.filter((t) => t.source === "process");
  const count = named
    ? [
        engine.length ? plural(engine.length, "agent task", "agent tasks") : null,
        processes.length ? plural(processes.length, "process", "processes") : null,
      ]
        .filter(Boolean)
        .join(" · ")
    : "Background task";
  const lead = named ? tasks.find(described) : undefined;
  const leadSince = lead ? since(lead, now) : null;
  return (
    <div
      ref={rootRef}
      className={styles.strip}
      data-testid="structured-background-work"
      data-open={open ? "" : undefined}
      style={cap ? ({ "--strip-cap": `${cap}px` } as CSSProperties) : undefined}
    >
      {(paused > 0 || unknown) && (
        <div className={styles.notices}>
          {paused > 0 && <PausedUnresumed count={paused} onStopEverything={onStopEverything} />}
          {unknown && <ScanUnknown onStop={onStop} />}
        </div>
      )}
      {jobs && (
        <Disclosure
          open={open}
          onOpenChange={setOpen}
          className={styles.disclosure}
          testId="structured-background-strip"
          summary={
            <span className={styles.summary}>
              <span className={`${styles.led} ${styles.ledUp}`} aria-hidden="true" />
              <span className={styles.count} role="status">
                {count} running
              </span>
              {lead && (
                <span className={styles.lead} aria-hidden="true">
                  <span className={styles.leadName}>{jobName(lead)}</span>
                  {leadSince && <span className={styles.leadSince}>{leadSince}</span>}
                </span>
              )}
              <span className={styles.toggle}>{open ? "Hide" : "Show"}</span>
            </span>
          }
        >
          {/* Its own scroll region, reachable by keyboard: a list of processes has no other stop. */}
          <div
            className={styles.list}
            data-testid="structured-background-list"
            role="region"
            aria-label="Background jobs"
            tabIndex={0}
          >
            {named ? (
              <>
                {engine.map((task) => (
                  <TaskRow
                    key={`${worker}:${task.task_id}`}
                    task={task as StructuredBackgroundTask}
                    now={now}
                  />
                ))}
                {processes.map((task) => (
                  <ProcessRow key={`${worker}:${task.task_id}`} task={task} now={now} />
                ))}
              </>
            ) : (
              <p className={styles.generic}>
                A background task is running.{" "}
                <span>Not a turn: there is nothing to interrupt. Stop ends the worker.</span>
              </p>
            )}
            {named && (
              <p className={styles.note}>
                Runs outside a conversation turn. Stop ends the session’s worker and these jobs.
              </p>
            )}
          </div>
        </Disclosure>
      )}
    </div>
  );
}
