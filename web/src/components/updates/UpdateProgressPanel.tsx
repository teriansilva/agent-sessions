/** Settings → Updates: how far along a self-update is (#1085).
 *
 *  "Update now" used to answer with one line — "Updating… reload in a moment" — for the several
 *  minutes a build takes. The installer now writes a record per milestone (`install.sh`
 *  `progress`), the server validates and labels it (`GET /api/update/progress`), and this panel
 *  draws it: the step, an overall bar, the elapsed time, and how long the last update took.
 *
 *  **The update restarts the server this page is polling.** A failed read while an update is in
 *  flight is therefore not an error, it is the restart: the panel says "Restarting…" and keeps
 *  asking until the new process answers from the same record on disk. Only the record's own
 *  `failed` / `rolled_back` / `stale` are shown as failures.
 */
import { CircleAlert, CircleCheck, RotateCw } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../../lib/api";
import type { UpdateProgress } from "../../types/api";

import styles from "./UpdateProgressPanel.module.css";

export const PROGRESS_POLL_MS = 2_000;
/** Ceiling for the backoff after failed reads when no run has been seen yet. */
export const PROGRESS_RETRY_MAX_MS = 30_000;
/** A finished run older than this is history, not something to show when the card opens. */
const SHOW_FINISHED_FOR_S = 60 * 60;

const TERMINAL = new Set(["done", "failed", "rolled_back", "stale"]);

/** When this page was loaded — a finished update only needs a reload if it ended AFTER this. A
 *  `done` from a run that finished before the page loaded is already what the page is running. */
const PAGE_LOADED_AT = (performance.timeOrigin || Date.now()) / 1000;

function clock(total: number): string {
  const s = Math.max(0, Math.round(total));
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, "0")}`;
}

function minutes(total: number): string {
  const m = Math.max(1, Math.round(total / 60));
  return `${m} min`;
}

export function UpdateProgressPanel({
  started,
}: {
  /** Bumped when this card starts an update, so the panel begins polling at once. */
  started: number;
}) {
  const [p, setP] = useState<UpdateProgress | null>(null);
  /** A read failed while an update is in flight — the service is restarting. */
  const [restarting, setRestarting] = useState(false);
  const [now, setNow] = useState(() => Date.now() / 1000);
  const polling = useRef(false);

  const read = useCallback(async (): Promise<UpdateProgress | null> => {
    try {
      const r = await api.updateProgress();
      setP(r);
      setRestarting(false);
      return r;
    } catch {
      if (polling.current) setRestarting(true);
      return null;
    }
  }, []);

  // One read on open: an update started elsewhere (the daily auto-update, another tab) is shown
  // the same way, and a run in flight starts the poll.
  //
  // A FAILED READ IS RETRIED EVEN BEFORE A RUN HAS BEEN SEEN (Hermes on #1089). Opening this page
  // during the restart window — an update started elsewhere, or coming back to one you started —
  // makes the very first read fail; stopping there left the panel absent for good while the new
  // server was already answering. So a failure always schedules another read: at the poll
  // interval once a run is known, with a doubling backoff (capped) until then. Only a successful
  // read that is not `running` ends the loop.
  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    let failures = 0;
    const tick = async () => {
      const r = await read();
      if (!alive) return;
      if (r === null) {
        failures += 1;
        const delay = polling.current
          ? PROGRESS_POLL_MS
          : Math.min(PROGRESS_POLL_MS * 2 ** (failures - 1), PROGRESS_RETRY_MAX_MS);
        timer = window.setTimeout(tick, delay);
        return;
      }
      failures = 0;
      if (r.state === "running") {
        polling.current = true;
        timer = window.setTimeout(tick, PROGRESS_POLL_MS);
        return;
      }
      polling.current = false;
    };
    if (started > 0) polling.current = true;
    void tick();
    return () => {
      alive = false;
      if (timer != null) window.clearTimeout(timer);
    };
  }, [read, started]);

  // The elapsed clock moves every second between polls.
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now() / 1000), 1_000);
    return () => window.clearInterval(id);
  }, []);

  if (!p) return null;
  const inFlight = p.state === "running" || (started > 0 && !TERMINAL.has(p.state));
  const finished = TERMINAL.has(p.state);
  if (!inFlight && !finished) {
    return p.last_duration_s ? (
      <p className={styles.hint} data-testid="update-last-duration">
        The last update took {minutes(p.last_duration_s)}.
      </p>
    ) : null;
  }
  const endedAt = (p.started_at ?? 0) + (p.elapsed_s ?? 0);
  if (finished && started === 0 && now - endedAt > SHOW_FINISHED_FOR_S) {
    // An old run is history — but how long it took is still worth saying (Hermes on #1089).
    return p.last_duration_s ? (
      <p className={styles.hint} data-testid="update-last-duration">
        The last update took {minutes(p.last_duration_s)}.
      </p>
    ) : null;
  }

  const steps = p.steps || 7;
  const index = p.step_index ?? 0;
  const elapsed =
    p.state === "running" && p.started_at ? now - p.started_at : (p.elapsed_s ?? 0);
  const pct =
    p.state === "done" ? 100 : Math.round((Math.max(0, index - 0.5) / steps) * 100);

  let head: React.ReactNode;
  if (p.state === "done") {
    head = (
      <span className={styles.ok}>
        <CircleCheck size={14} aria-hidden="true" /> Update finished
      </span>
    );
  } else if (p.state === "failed" || p.state === "rolled_back" || p.state === "stale") {
    head = (
      <span className={styles.bad}>
        <CircleAlert size={14} aria-hidden="true" />{" "}
        {p.state === "rolled_back"
          ? `Update failed at "${p.label}" — rolled back to the previous release`
          : p.state === "stale"
            ? `No progress since "${p.label}" — the update may have stalled`
            : `Update failed at "${p.label}"`}
      </span>
    );
  } else if (restarting) {
    head = (
      <span>
        <RotateCw size={14} aria-hidden="true" className={styles.spin} /> Restarting… reconnecting
      </span>
    );
  } else {
    head = (
      <span>
        {index ? `Step ${index} of ${steps} · ${p.label}` : "Starting the installer…"}
      </span>
    );
  }

  return (
    <div className={styles.panel} data-testid="update-progress" data-state={p.state}>
      <div className={styles.head} role="status" aria-live="polite">
        {head}
        <span className={styles.time}>
          {clock(elapsed)} {finished ? "total" : "elapsed"}
          {p.last_duration_s && !finished ? ` · last update took ${minutes(p.last_duration_s)}` : ""}
        </span>
      </div>
      <div
        className={styles.track}
        role="progressbar"
        aria-label="Update progress"
        aria-valuemin={0}
        aria-valuemax={steps}
        aria-valuenow={p.state === "done" ? steps : index}
        aria-valuetext={p.state === "done" ? "Finished" : `Step ${index} of ${steps}`}
      >
        <div
          className={`${styles.fill} ${p.state === "failed" || p.state === "rolled_back" ? styles.fillBad : ""}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className={styles.steps} aria-hidden="true">
        {Array.from({ length: steps }, (_, i) => (
          <span
            key={i}
            className={
              i + 1 < index || p.state === "done"
                ? styles.stepDone
                : i + 1 === index
                  ? styles.stepNow
                  : undefined
            }
          />
        ))}
      </div>
      {p.state === "done" && endedAt > PAGE_LOADED_AT ? (
        <button
          type="button"
          className={`${styles.reload} shine`}
          onClick={() => window.location.reload()}
          data-testid="update-reload"
        >
          Reload to finish
        </button>
      ) : inFlight ? (
        <p className={styles.hint}>
          The app restarts near the end — open terminals reconnect on their own. You can leave
          this page.
        </p>
      ) : null}
    </div>
  );
}
