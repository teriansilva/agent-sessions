/** Missions → Automations → one automation's run history (#1201 board 3).
 *
 *  Every run is here, including the ones that did not run: skipped, refused and interrupted slots
 *  are runs too, each with the server's reason, so "why didn't it run at 03:00?" always has an
 *  answer. A run's detail shows its trigger facts, its inputs (already masked by the server — a
 *  secret only ever reads `[secret: name]`), the step timeline in the order the server recorded it
 *  (claimed → launched → started → briefed, or the mission's create → dispatch), and links to
 *  what it started.
 *
 *  A failure notification links here (`automationPath`), and `?run=<id>` opens one run. A failed
 *  reload keeps the runs already shown and says when they were loaded. The webhook panel is
 *  Phase 3 of #1201 and is not here. */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { HudFrame } from "../components/hud/HudFrame";
import {
  InFlightNote,
  KillSwitchNotice,
  Led,
  OutcomeWord,
  StateWord,
} from "../components/automations/AutomationBits";
import { MissionRailOnly } from "../components/automations/MissionRailOnly";
import { useAutomations, useNowS } from "../components/automations/useAutomations";
import { useEnableFlow } from "../components/automations/useEnableFlow";
import { useRunPages } from "../components/automations/useRunPages";
import { refreshOrigins } from "../app/automationOrigins";
import { api } from "../lib/api";
import {
  errorWords,
  inFlightNote,
  runNowBlock,
  runTriggerWords,
  stepWords,
  whenWords,
  type Tone,
} from "../lib/automations";
import { sessionPathFromKey } from "../lib/format";
import { missionLink } from "../lib/missionLink";
import { AUTOMATIONS_PATH, automationEditPath, automationPath } from "../lib/routes";
import type { Automation, AutomationRun, RunStep } from "../types/automations";
import styles from "../components/automations/automations.module.css";

/** A run still starting (or a mission still running) is re-read at this pace while it is open… */
export const RUN_POLL_MS = 5_000;
/** …and, once it has been in progress this long, only at the slow pace: a mission can run for
 *  hours, and nobody needs its row re-read every five seconds for all of them. */
export const RUN_POLL_SLOW_AFTER_MS = 60_000;
export const RUN_POLL_SLOW_MS = 30_000;

function stepTone(step: string): Tone {
  if (["ok", "briefed", "started", "launched", "bound", "mission_created", "dispatched"].includes(step))
    return "up";
  if (step === "failed" || step === "refused" || step === "partial") return "down";
  if (step === "interrupted" || step === "review") return "degraded";
  return "idle";
}

function clock(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function inputsText(inputs: Record<string, unknown>): string {
  return Object.entries(inputs)
    .map(([k, v]) => `${k} = ${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join("\n");
}

/** Keyed by the automation, so moving to another one starts from a clean page rather than
 *  showing the last one's runs under the new name. */
export default function AutomationRuns() {
  const { id = "" } = useParams<{ id: string }>();
  return <RunsPage key={id} id={id} />;
}

function RunsPage({ id }: { id: string }) {
  const [params, setParams] = useSearchParams();
  const { data, error: listError, reload, applyRow } = useAutomations();
  const nowS = useNowS();
  const pages = useRunPages(id);
  const { runs, total, error: runsError, loadedAt: runsAt, missing, loadingMore } = pages;
  const { loadFirst, loadMore, refreshHead, prepend } = pages;
  const [detail, setDetail] = useState<AutomationRun | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ tone: "ok" | "bad"; text: string; note?: string } | null>(
    null,
  );
  const [busy, setBusy] = useState(false);

  const a: Automation | null = useMemo(
    () => data?.automations.find((x) => x.id === id) ?? null,
    [data, id],
  );
  const loopOn = data?.loop.enabled ?? true;
  const selected = params.get("run") ?? runs?.[0]?.id ?? null;

  useEffect(() => {
    void loadFirst();
  }, [loadFirst]);

  // The open run: read with its steps, and re-read while it is still in progress.
  useEffect(() => {
    if (!selected) return;
    let live = true;
    let timer: number | undefined;
    let wasPending = false;
    let pendingSince = 0;
    // A hidden tab does not poll: the next read waits for the page to be visible again.
    let waitingForVisible = false;
    const onVisible = () => {
      if (!document.hidden && waitingForVisible && live) {
        waitingForVisible = false;
        void read();
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    const schedule = () => {
      if (!pendingSince) pendingSince = Date.now();
      const slow = Date.now() - pendingSince >= RUN_POLL_SLOW_AFTER_MS;
      timer = window.setTimeout(
        () => {
          if (document.hidden) waitingForVisible = true;
          else void read();
        },
        slow ? RUN_POLL_SLOW_MS : RUN_POLL_MS,
      );
    };
    const read = async () => {
      try {
        const r = await api.automationRun(selected);
        if (!live) return;
        setDetail(r);
        setDetailError(null);
        if (r.state === "dispatching" || r.outcome === "started" || r.outcome === "review") {
          wasPending = true;
          schedule();
        } else if (wasPending) void loadRunsQuiet();
      } catch (e) {
        if (live) setDetailError(errorWords(e, "Couldn’t load this run"));
      }
    };
    // A run that finished while open also refreshes its row in the list, once.
    const loadRunsQuiet = () => void refreshHead();
    void read();
    return () => {
      live = false;
      if (timer) window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [selected, id, refreshHead]);

  const enable = useEnableFlow(
    useCallback(
      (x: Automation | null) => {
        if (x) {
          applyRow(x);
          setNotice({ tone: "ok", text: `“${x.name}” is enabled.`, note: inFlightNote(x) });
        }
        void reload();
      },
      [reload, applyRow],
    ),
  );

  async function runNow() {
    if (!a) return;
    setBusy(true);
    setNotice(null);
    try {
      const run = await api.runAutomation(a.id);
      void refreshOrigins(true);
      prepend(run);
      setParams({ run: run.id }, { replace: true });
      if (run.outcome === "skipped") setNotice({ tone: "bad", text: `It did not run: ${run.reason}` });
    } catch (e) {
      setNotice({ tone: "bad", text: `Run now was refused: ${errorWords(e, "unknown error")}` });
    } finally {
      setBusy(false);
      void reload();
    }
  }

  async function verb(v: "pause" | "resume") {
    if (!a) return;
    setBusy(true);
    try {
      const r = await api.automationVerb(a.id, v);
      applyRow(r);
      setNotice({ tone: "ok", text: v === "pause" ? "Paused." : "Resumed.", note: inFlightNote(r) });
    } catch (e) {
      setNotice({ tone: "bad", text: errorWords(e, "That didn’t work.") });
    } finally {
      setBusy(false);
      void reload();
    }
  }

  const number = (i: number) => total - i;
  const block = a ? runNowBlock(a, loopOn) : "";
  const decided = a ? a.stats.ok + a.stats.failed : 0;

  if (missing && !a) {
    return (
      <div className={styles.page}>
        <MissionRailOnly />
        <div className={`${styles.notice} ${styles.noticeBad}`} role="alert">
          <strong>This automation doesn’t exist</strong>
          <span>It may have been deleted.</span>
          <div className={styles.noticeRow}>
            <Link className={styles.ghost} to={AUTOMATIONS_PATH}>
              All automations
            </Link>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className={styles.page} data-testid="automation-runs">
      <MissionRailOnly />
      <div className={styles.stack}>
        <section className={styles.panel} aria-labelledby="runs-title">
          <HudFrame />
          <div className={styles.head}>
            <div className={styles.headText}>
              <span className={styles.kicker}>
                <Link className={styles.link} to={AUTOMATIONS_PATH}>
                  Automations
                </Link>
                {` // ${a?.name ?? "…"}`}
              </span>
              <h1 id="runs-title" className={styles.title}>
                Run history
              </h1>
              {a && (
                <div className={styles.summary}>
                  <StateWord state={a.state} />
                  <span className={styles.tagItem}>
                    OK 14 d <b>{a.stats.ok}</b> / <b>{decided}</b>
                  </span>
                  <span className={styles.tagItem}>
                    Failures in a row <b>{a.consecutive_failures}</b>
                  </span>
                  <span className={styles.tagItem}>
                    Next <b>{a.next_run ? whenWords(a.next_run.at, nowS) : "—"}</b>
                  </span>
                </div>
              )}
            </div>
            {a && (
              <div className={styles.headActions}>
                <Link className={styles.ghost} to={automationEditPath(a.id)}>
                  Edit
                </Link>
                {!a.enabled || a.needs_reapproval ? (
                  <button type="button" className={styles.ghost} onClick={() => enable.open(a)} data-testid="runs-enable">
                    {a.needs_reapproval ? "Review and approve…" : "Enable…"}
                  </button>
                ) : (
                  <button type="button" className={styles.ghost} disabled={busy} onClick={() => void verb(a.paused ? "resume" : "pause")}>
                    {a.paused ? "Resume" : "Pause"}
                  </button>
                )}
                <button
                  type="button"
                  className={styles.primary}
                  disabled={busy || !!block}
                  title={block || undefined}
                  onClick={() => void runNow()}
                  data-testid="runs-run-now"
                >
                  Run now
                </button>
              </div>
            )}
          </div>

          {!loopOn && <KillSwitchNotice />}
          {a && block && loopOn && <p className={styles.hint}>Run now is unavailable: {block}.</p>}
          {listError && !a && (
            <div className={`${styles.notice} ${styles.noticeBad}`} role="alert">
              <strong>Couldn’t load this automation</strong>
              <span>{listError}</span>
            </div>
          )}
          {notice && (
            <div
              className={`${styles.notice} ${notice.tone === "ok" ? styles.noticeOk : styles.noticeBad}`}
              role="status"
              data-testid="runs-notice"
            >
              {notice.text}
              {notice.note && <InFlightNote text={notice.note} />}
            </div>
          )}
          {runsError && (
            <div className={`${styles.notice} ${styles.noticeBad} ${styles.more}`} role="alert" data-testid="runs-load-error">
              <strong>Couldn’t load run history</strong>
              <span>
                {runsError}
                {runs && runsAt
                  ? `. Showing the runs loaded at ${new Date(runsAt).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}.`
                  : "."}
              </span>
              <div className={styles.noticeRow}>
                <button type="button" className={styles.ghost} onClick={() => void loadFirst()}>
                  Retry
                </button>
              </div>
            </div>
          )}

          <div className={`${styles.runsGrid} ${styles.more}`}>
            <div>
              {runs == null && !runsError && <p className={styles.lede}>Loading runs…</p>}
              {runs && runs.length === 0 && (
                <div className={styles.empty} data-testid="runs-empty">
                  <strong>No runs yet</strong>
                  <span className={styles.lede}>
                    {a?.next_run
                      ? `The first one is due ${whenWords(a.next_run.at, nowS)}.`
                      : a && !a.enabled
                        ? "It is off. Enable it, then it runs on its schedule or when you press Run now."
                        : "It runs when you press Run now."}
                  </span>
                </div>
              )}
              {runs && runs.length > 0 && (
                <ul className={styles.runList} aria-label="Runs" data-testid="runs-list">
                  {runs.map((r, i) => (
                    <li key={r.id}>
                      <button
                        type="button"
                        className={styles.runRow}
                        aria-current={r.id === selected ? "true" : undefined}
                        onClick={() => setParams({ run: r.id }, { replace: true })}
                        data-testid="run-row"
                      >
                        <span className={styles.runNo}>#{number(i)}</span>
                        <span className={styles.runMain}>
                          <span>
                            {whenWords(r.created_at, nowS)} · {runTriggerWords(r)}
                          </span>
                          <span className={styles.sub}>{r.reason || "—"}</span>
                        </span>
                        <span className={styles.runOutcome}>
                          <OutcomeWord outcome={r.outcome} state={r.state} />
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              {runs && runs.length < total && (
                <button
                  type="button"
                  className={`${styles.ghost} ${styles.more}`}
                  disabled={loadingMore}
                  onClick={() => void loadMore()}
                  data-testid="runs-load-more"
                >
                  {loadingMore ? "Loading…" : "Load older runs"}
                </button>
              )}
              <p className={styles.footnote}>
                Skipped, refused and interrupted slots are runs too, so “why didn’t it run?” always
                has an answer.
              </p>
            </div>

            <RunDetail
              run={detail && detail.id === selected ? detail : null}
              index={runs ? runs.findIndex((r) => r.id === selected) : -1}
              total={total}
              error={detailError}
              automationId={id}
            />
          </div>
        </section>
      </div>
      {enable.dialog}
    </div>
  );
}

function RunDetail({
  run,
  index,
  total,
  error,
  automationId,
}: {
  run: AutomationRun | null;
  index: number;
  total: number;
  error: string | null;
  /** The automation the ROUTE names: a run of another one is never shown under its header. */
  automationId: string;
}) {
  if (error && !run)
    return (
      <div className={`${styles.notice} ${styles.noticeBad}`} role="alert">
        <strong>Couldn’t load this run</strong>
        <span>{error}</span>
      </div>
    );
  if (!run) return <div />;
  if (run.automation_id !== automationId)
    return (
      <div className={`${styles.notice} ${styles.noticeWarn}`} role="status" data-testid="run-elsewhere">
        <strong>This run belongs to another automation</strong>
        <span>
          It is not part of this automation’s history.{" "}
          <Link className={styles.link} to={automationPath(run.automation_id, run.id)}>
            Open it in its own history
          </Link>
        </span>
      </div>
    );
  const sessionHref = run.session_key ? sessionPathFromKey(run.session_key) : null;
  const inputs = run.inputs && Object.keys(run.inputs).length ? inputsText(run.inputs) : "";
  const steps: RunStep[] = run.steps ?? [];
  return (
    <section className={styles.panel} aria-labelledby="run-detail-title" data-testid="run-detail">
      <HudFrame />
      <div className={styles.head}>
        <h2 id="run-detail-title" className={styles.panelTitle}>
          Run {index >= 0 ? `#${total - index}` : ""}
        </h2>
        <span className={styles.runOutcome}>
          <OutcomeWord outcome={run.outcome} state={run.state} />
        </span>
      </div>
      <dl className={styles.kv}>
        <dt>Trigger</dt>
        <dd>
          {run.trigger === "manual"
            ? "Run now, by you"
            : `${run.catch_up ? "Catch-up" : "Scheduled"} slot ${run.slot}${run.covered > 1 ? ` · covered ${run.covered} missed slots` : ""}`}
        </dd>
        <dt>Started</dt>
        <dd>{new Date(run.created_at * 1000).toLocaleString("en-GB")}</dd>
        {run.reason && (
          <>
            <dt>Outcome</dt>
            <dd>{run.reason}</dd>
          </>
        )}
        {inputs && (
          <>
            <dt>Inputs</dt>
            <dd>
              <pre className={styles.inputs} data-testid="run-inputs">
                {inputs}
              </pre>
            </dd>
          </>
        )}
      </dl>
      {run.outcome === "partial" && (
        <div className={`${styles.notice} ${styles.noticeBad} ${styles.more}`} role="alert" data-testid="run-partial">
          <strong>Typed, not sent</strong>
          <span>{run.reason || "Text was typed but not submitted — check the terminal."}</span>
          {sessionHref && (
            <div className={styles.noticeRow}>
              <Link className={styles.primary} to={sessionHref}>
                Open session
              </Link>
            </div>
          )}
        </div>
      )}
      {run.outcome === "interrupted" && (
        <div className={`${styles.notice} ${styles.noticeWarn} ${styles.more}`}>
          <strong>Outcome unknown</strong>
          <span>
            BattleLab stopped while this run was starting. Whatever it started keeps running on its
            own; it is never retried.
          </span>
        </div>
      )}
      {steps.length > 0 && (
        <ol className={styles.timeline} aria-label="Steps" data-testid="run-steps">
          {steps.map((s) => (
            <li key={s.seq} data-step={s.step}>
              <Led tone={stepTone(s.step)} />
              <time>{clock(s.at)}</time>
              <span className={styles.stepText}>
                {stepWords(s.step)}
                {s.detail ? <span className={styles.faint}> · {s.detail}</span> : null}
              </span>
            </li>
          ))}
        </ol>
      )}
      {(run.mission_id || sessionHref) && (
        <div className={`${styles.noticeRow} ${styles.more}`}>
          {run.mission_id && (
            <Link className={styles.ghost} to={missionLink(run.mission_id)}>
              Open mission
            </Link>
          )}
          {sessionHref && run.outcome !== "partial" && (
            <Link className={styles.ghost} to={sessionHref}>
              Open session
            </Link>
          )}
        </div>
      )}
    </section>
  );
}
