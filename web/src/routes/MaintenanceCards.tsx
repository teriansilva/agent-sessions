import { Archive, RefreshCw, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api, ApiError, type CompactInfo, type CompactJob } from "../lib/api";
import styles from "./Settings.module.css";

/** Settings → Maintenance (#993): archive missions, prune caches, and compact the OpenCode database.
 *
 *  Both cards show a dry run before anything runs, confirm in place naming the side effects, and
 *  report skips and failures on their own lines rather than folding them into a success. The
 *  server runs every maintenance job through one runner, so a submission while another job runs
 *  is refused (409) — the copy says so and asks for a retry; nothing is queued.
 *
 *  Two rules the review (#1000) made explicit:
 *
 *  * **A busy snapshot is not a dead end.** Seeing another job disables the action, so the card
 *    polls while busy and always offers Refresh — it recovers in place when the job ends, with no
 *    navigation or remount.
 *  * **An unmeasured category is never submitted as zero.** A category whose dry run errored says
 *    so, and while it is selected the action is blocked: a confirmation that totals only the
 *    measured categories must not delete the contents of one nobody counted. */

type PruneInfo = Awaited<ReturnType<typeof api.pruneInfo>>;
type PruneCategory = Parameters<typeof api.prune>[0][number];
type MissionsInfo = Awaited<ReturnType<typeof api.archiveOldMissionsInfo>>;

type Tone = "up" | "idle" | "down" | "attention";
interface ResultLine {
  tone: Tone;
  text: string;
}

/** A dry run is usable only when its body actually carries the fields the card totals. A 200
 *  whose body does not — a version-skewed server, a stub answering every unknown route — takes
 *  the same path as a rejected dry run: "couldn't measure", the action blocked, Refresh offered.
 *  Reading straight through such a body threw inside render (`info.categories[…]`,
 *  `info.unresolved.length`) and took the whole Settings route down to its error boundary,
 *  detaching every control in the panel — including the older cards' buttons. Absence of
 *  evidence stays distinct from a measured zero. */
function usablePruneInfo(r: PruneInfo): boolean {
  return !!r && typeof r.categories === "object" && r.categories !== null;
}

function usableMissionsInfo(r: MissionsInfo): boolean {
  return (
    !!r &&
    typeof r.eligible === "number" &&
    typeof r.sessions === "number" &&
    typeof r.live_sessions === "number" &&
    Array.isArray(r.unresolved)
  );
}

const MAINTENANCE_BUSY_COPY =
  "Another maintenance job is running — unavailable; retry when maintenance finishes.";

const DAYS_MAX = 3650;
/** How often a card re-asks while another job holds the runner. */
const BUSY_POLL_MS = 3000;

/** Bytes for small figures too (a socket is 0 B) — `humanBytes` starts at MB. */
function compactBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "—";
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v >= 10 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

const plural = (n: number, one: string, many = `${one}s`) =>
  `${n.toLocaleString()} ${n === 1 ? one : many}`;

function ResultLines({ lines }: { lines: ResultLine[] }) {
  return (
    <ul className={styles.resultList} aria-live="polite">
      {lines.map((l, i) => (
        <li key={i} className={styles.resultLine} data-tone={l.tone}>
          <span className={`hud-led ${l.tone}`} aria-hidden="true" />
          <span>{l.text}</span>
        </li>
      ))}
    </ul>
  );
}

function busyLine(job: string | undefined): ResultLine {
  return {
    tone: "attention",
    text: job
      ? `A maintenance job is running (${job}) — unavailable; retry when maintenance finishes.`
      : MAINTENANCE_BUSY_COPY,
  };
}

function RefreshButton({ onClick, label }: { onClick: () => void; label: string }) {
  return (
    <button
      type="button"
      className={`${styles.secBtnGhost} ${styles.actionBtn}`}
      onClick={onClick}
      aria-label={label}
    >
      <RefreshCw size={16} /> Refresh
    </button>
  );
}

/** Archive old missions (#993): finished missions older than N days, with their sessions. A
 *  running mission is never touched; a mission with an unresolved turn is skipped by the server's
 *  own fence and reported as such. Reversible — archived missions can be unarchived. */
export function ArchiveMissionsCard() {
  const [days, setDays] = useState(30);
  const [fetched, setFetched] = useState<{ days: number; info: MissionsInfo } | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [reload, setReload] = useState(0);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ResultLine[] | null>(null);

  const valid = Number.isInteger(days) && days >= 1 && days <= DAYS_MAX;

  useEffect(() => {
    if (!valid) return;
    let cancelled = false;
    api
      .archiveOldMissionsInfo(days)
      .then((r) => {
        if (cancelled) return;
        if (!usableMissionsInfo(r)) {
          setFetched(null);
          setLoadError(true);
          return;
        }
        setLoadError(false);
        setFetched({ days, info: r });
      })
      .catch(() => {
        if (cancelled) return;
        setFetched(null);
        setLoadError(true);
      });
    return () => {
      cancelled = true;
    };
  }, [days, valid, reload]);

  // Only a dry run for the age currently in the field counts; an invalid or not-yet-fetched age
  // shows nothing rather than another age's numbers.
  const info: MissionsInfo | null =
    valid && fetched !== null && fetched.days === days ? fetched.info : null;
  const runner = info?.runner ?? null;

  // While another job holds the runner, keep asking: the card must come back on its own.
  useEffect(() => {
    if (!runner) return;
    const t = setInterval(() => setReload((n) => n + 1), BUSY_POLL_MS);
    return () => clearInterval(t);
  }, [runner]);

  const run = async () => {
    setBusy(true);
    setResult(null);
    try {
      const r = await api.archiveOldMissions(days);
      const lines: ResultLine[] = [
        {
          tone: "up",
          text:
            `Archived ${plural(r.archived, "mission")} and ${plural(r.sessions_archived, "of their sessions", "of their sessions")}; ` +
            `stopped ${plural(r.terminals_stopped, "live terminal")}. Transcripts are kept.`,
        },
      ];
      for (const s of r.skipped) {
        lines.push({ tone: "idle", text: `Skipped mission ${s.mission_id}: ${s.reason}` });
      }
      for (const f of r.failed) {
        lines.push({
          tone: "down",
          text: f.session_key
            ? `Session ${f.session_key} was not archived (mission ${f.mission_id} is archived): ${f.reason}`
            : `Mission ${f.mission_id} was not archived: ${f.reason}`,
        });
      }
      setResult(lines);
    } catch (e) {
      setResult([
        e instanceof ApiError && e.status === 409
          ? busyLine((e.record as { busy?: { job?: string } } | undefined)?.busy?.job)
          : { tone: "down", text: "Couldn’t archive missions — please try again." },
      ]);
    } finally {
      setBusy(false);
      setConfirming(false);
      setReload((n) => n + 1);
    }
  };

  const eligible = info?.eligible ?? 0;
  const unresolved = info?.unresolved.length ?? 0;

  return (
    <section className={styles.section} aria-labelledby="missions-archive-h">
      <h2 id="missions-archive-h">Archive old missions</h2>
      <p className={styles.hint}>
        Archive finished missions (done, failed or abandoned). Their sessions
        are archived with them: live terminals are stopped, transcripts are
        kept. A mission with an unresolved turn is skipped. Archived missions
        can be unarchived.
      </p>
      {confirming && info ? (
        <div>
          <p className={styles.confirmText}>
            Archive {plural(eligible, "mission")} older than {plural(days, "day")}?
          </p>
          <p className={styles.hint}>
            {plural(info.sessions, "session")} {info.sessions === 1 ? "is" : "are"}{" "}
            archived with them and {plural(info.live_sessions, "live terminal")}{" "}
            will be stopped; transcripts are kept.
            {unresolved
              ? ` ${plural(unresolved, "mission")} with an unresolved turn will be skipped.`
              : ""}
          </p>
          <span className={styles.confirmRow}>
            <button
              type="button"
              className={`${styles.danger} ${styles.actionBtn}`}
              disabled={busy}
              onClick={() => void run()}
            >
              <Archive size={16} /> {busy ? "Archiving…" : "Confirm mission archive"}
            </button>
            <button
              type="button"
              className={`${styles.secBtnGhost} ${styles.actionBtn}`}
              onClick={() => setConfirming(false)}
              disabled={busy}
            >
              Cancel
            </button>
          </span>
        </div>
      ) : (
        <div className={styles.cleanupRow}>
          <label className={styles.cleanupLabel}>
            Older than
            <input
              className={styles.hoursInput}
              type="number"
              min={1}
              max={DAYS_MAX}
              value={days}
              onChange={(e) => setDays(Number(e.target.value))}
              aria-label="Mission age in days"
            />
            days
          </label>
          <button
            type="button"
            className={`${styles.secBtnGhost} ${styles.actionBtn}`}
            disabled={!valid || !info || eligible === 0 || busy || runner !== null}
            onClick={() => {
              setResult(null);
              setConfirming(true);
            }}
          >
            <Archive size={16} /> Archive old missions ({eligible})
          </button>
          {(runner !== null || loadError) && (
            <RefreshButton
              onClick={() => setReload((n) => n + 1)}
              label="Refresh the mission counts"
            />
          )}
          {info && (
            <span className={styles.inlineHint}>
              {plural(info.sessions, "session")} · {info.live_sessions.toLocaleString()} live
            </span>
          )}
        </div>
      )}
      {loadError && (
        <ResultLines
          lines={[
            {
              tone: "down",
              // A refresh that failed AFTER a run must not deny what that run did (#1000).
              text: result
                ? "Couldn’t refresh the counts — the result above still stands."
                : "Couldn’t count missions (dry run failed). Nothing was archived.",
            },
          ]}
        />
      )}
      {runner && !confirming && <ResultLines lines={[busyLine(runner.job)]} />}
      {info && eligible === 0 && !loadError && !runner && (
        <p className={styles.hint}>No finished missions older than {plural(days, "day")}.</p>
      )}
      {result && <ResultLines lines={result} />}
    </section>
  );
}

const CATEGORIES: { id: PruneCategory; label: string; hint: string }[] = [
  {
    id: "stale_sockets",
    label: "Stale terminal sockets",
    hint: "No live session behind them. A socket a live session holds is never touched.",
  },
  {
    id: "archived_scrollback",
    label: "Archived sessions’ scrollback",
    hint: "Same as Scrollback cache → Clear archived sessions’ cache.",
  },
];

const labelOf = (id: string) => CATEGORIES.find((c) => c.id === id)?.label ?? id;

/** Prune (#993): BattleLab's own leftovers — never session history. Dry-run counts come from
 *  the server before anything runs; the confirm says exactly what will be removed. */
export function PruneCard() {
  const [info, setInfo] = useState<PruneInfo | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [reload, setReload] = useState(0);
  const [selected, setSelected] = useState<Set<PruneCategory>>(
    () => new Set<PruneCategory>(["stale_sockets"]),
  );
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ResultLine[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .pruneInfo()
      .then((r) => {
        if (cancelled) return;
        if (!usablePruneInfo(r)) {
          setInfo(null);
          setLoadError(true);
          return;
        }
        setLoadError(false);
        setInfo(r);
      })
      .catch(() => {
        if (cancelled) return;
        setInfo(null);
        setLoadError(true);
      });
    return () => {
      cancelled = true;
    };
  }, [reload]);

  const chosen = CATEGORIES.filter((c) => selected.has(c.id));
  const items = chosen.reduce((n, c) => n + (info?.categories[c.id]?.items ?? 0), 0);
  const bytes = chosen.reduce((n, c) => n + (info?.categories[c.id]?.bytes ?? 0), 0);
  const runner = info?.runner ?? null;
  // A selected category whose measurement failed has UNKNOWN contents: `{items: 0}` is not a
  // count, so the total below would understate what the POST would delete.
  const unmeasured = chosen.filter((c) => info?.categories[c.id]?.error);
  // Every selected category must have a SUCCESSFUL measurement — present, and without an error.
  // Testing only for `error` was a hole: a rejected refresh sets `info` to null, so the optional
  // chaining above yields an empty `unmeasured` and the guard read "nothing failed" when in truth
  // nothing had been measured at all (review 4915/4919, finding 5). Absence of evidence is not
  // evidence of an empty cache.
  const measuredAll =
    info !== null &&
    chosen.every((c) => {
      const m = info.categories[c.id];
      return m !== undefined && !m.error;
    });
  // The confirmation is DERIVED, never a latched flag. A dry run can land while it is open — the
  // busy poll and an in-flight Refresh both do that — and turn a selected category unknown or
  // unmeasured, at which point a latched confirmation would keep showing a total that no longer
  // describes what would be deleted ("Permanently remove 0 items"). Deriving it drops the operator
  // back to the disabled button and the explanation instead, with no effect to write and no second
  // copy of the state to fall out of sync (Hermes on PR #1000, review 4898).
  const confirmOpen = confirming && measuredAll;

  useEffect(() => {
    if (!runner) return;
    const t = setInterval(() => setReload((n) => n + 1), BUSY_POLL_MS);
    return () => clearInterval(t);
  }, [runner]);

  const toggle = (id: PruneCategory) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const run = async () => {
    // Re-validated at SUBMISSION, not only when the confirmation opened: the button's guard is a
    // display gate, and this POST is what actually deletes. A measurement that went unknown — or
    // was never obtained at all, because the refresh was rejected — has to stop it here too
    // (review 4898, tightened in 4915/4919).
    if (!measuredAll) {
      setConfirming(false);
      return;
    }
    setBusy(true);
    setResult(null);
    try {
      const r = await api.prune(chosen.map((c) => c.id));
      const lines: ResultLine[] = [
        {
          tone: "up",
          text: `Removed ${plural(r.removed, "item")} (${compactBytes(r.bytes_freed)} freed).`,
        },
      ];
      for (const s of r.skipped) {
        lines.push({
          tone: "idle",
          text: `Skipped ${s.count.toLocaleString()} (${labelOf(s.category)}): ${s.reason}.`,
        });
      }
      for (const f of r.failed) {
        lines.push({
          tone: "down",
          text: `Couldn’t remove ${f.item} (${labelOf(f.category)}): ${f.reason}`,
        });
      }
      if (r.failed_total > r.failed.length) {
        lines.push({
          tone: "down",
          text: `…and ${plural(r.failed_total - r.failed.length, "more failure")}.`,
        });
      }
      setResult(lines);
    } catch (e) {
      setResult([
        e instanceof ApiError && e.status === 409
          ? busyLine((e.record as { busy?: { job?: string } } | undefined)?.busy?.job)
          : { tone: "down", text: "Couldn’t prune — please try again." },
      ]);
    } finally {
      setBusy(false);
      setConfirming(false);
      setReload((n) => n + 1);
    }
  };

  return (
    <section className={styles.section} aria-labelledby="prune-h">
      <h2 id="prune-h">Prune</h2>
      <p className={styles.hint}>
        Remove BattleLab’s own leftovers. Session history is never deleted, and
        nothing is removed until you confirm. Sizes are a dry run.
      </p>
      <h3 className={styles.subhead}>Caches</h3>
      <ul className={styles.pruneList}>
        {CATEGORIES.map((c) => {
          const m = info?.categories[c.id];
          return (
            <li key={c.id} className={styles.pruneRow}>
              <label className={styles.pruneLabel}>
                <input
                  type="checkbox"
                  checked={selected.has(c.id)}
                  disabled={busy || confirmOpen}
                  onChange={() => toggle(c.id)}
                />
                <span className={styles.pruneText}>
                  <span className={styles.pruneName}>{c.label}</span>
                  <span className={styles.pruneHint}>{c.hint}</span>
                </span>
              </label>
              <span className={styles.pruneCount} data-testid={`prune-count-${c.id}`}>
                {m
                  ? m.error
                    ? "couldn’t measure"
                    : `${m.items.toLocaleString()} · ${compactBytes(m.bytes)}`
                  : "…"}
              </span>
            </li>
          );
        })}
      </ul>
      {confirmOpen ? (
        <div>
          <p className={styles.confirmText}>
            Permanently remove {plural(items, "item")} ({compactBytes(bytes)})?
          </p>
          <p className={styles.hint}>
            {chosen.map((c) => c.label).join(" · ")}. Session history is not
            touched.
          </p>
          <span className={styles.confirmRow}>
            <button
              type="button"
              className={`${styles.danger} ${styles.actionBtn}`}
              disabled={busy}
              onClick={() => void run()}
            >
              <Trash2 size={16} /> {busy ? "Pruning…" : "Confirm prune"}
            </button>
            <button
              type="button"
              className={`${styles.secBtnGhost} ${styles.actionBtn}`}
              onClick={() => setConfirming(false)}
              disabled={busy}
            >
              Cancel
            </button>
          </span>
        </div>
      ) : (
        <div className={styles.cleanupRow}>
          <button
            type="button"
            className={`${styles.secBtnGhost} ${styles.actionBtn}`}
            disabled={
              !info ||
              chosen.length === 0 ||
              items === 0 ||
              unmeasured.length > 0 ||
              busy ||
              runner !== null
            }
            onClick={() => {
              setResult(null);
              setConfirming(true);
            }}
          >
            <Trash2 size={16} /> Prune selected ({chosen.length})
          </button>
          {(runner !== null || loadError || unmeasured.length > 0) && (
            <RefreshButton
              onClick={() => setReload((n) => n + 1)}
              label="Refresh the cache measurements"
            />
          )}
          {info && (
            <span className={styles.inlineHint}>
              {plural(items, "item")} · {compactBytes(bytes)}
            </span>
          )}
        </div>
      )}
      {unmeasured.length > 0 && !confirmOpen && (
        <ResultLines
          lines={[
            {
              tone: "down",
              text: `${unmeasured
                .map((c) => c.label)
                .join(" · ")} couldn’t be measured, so its contents are unknown — deselect it or refresh before pruning.`,
            },
          ]}
        />
      )}
      {loadError && (
        <ResultLines
          lines={[
            {
              tone: "down",
              // A refresh that failed AFTER a prune must not deny what that prune removed (#1000).
              text: result
                ? "Couldn’t refresh the measurements — the result above still stands."
                : "Couldn’t measure the caches (dry run failed). Nothing was removed.",
            },
          ]}
        />
      )}
      {runner && !confirming && <ResultLines lines={[busyLine(runner.job)]} />}
      {info && items === 0 && unmeasured.length === 0 && !runner && (
        <p className={styles.hint}>Nothing to prune right now.</p>
      )}
      {result && <ResultLines lines={result} />}
      <CompactDatabase />
    </section>
  );
}

const COMPACT_JOB_KEY = "tr-maintenance-compact-job";
function rememberedJob(): string | undefined {
  try {
    return sessionStorage.getItem(COMPACT_JOB_KEY) || undefined;
  } catch {
    return undefined;
  }
}
function rememberJob(id?: string) {
  try {
    if (id) sessionStorage.setItem(COMPACT_JOB_KEY, id);
    else sessionStorage.removeItem(COMPACT_JOB_KEY);
  } catch {
    /* Storage is optional; identity still lives in this mounted card. */
  }
}
const jobRunning = (job: CompactJob | null) =>
  !!job && ["checking", "vacuum", "checkpoint"].includes(job.state);
const measuredBytes = (n: number | null) =>
  n === null ? "unknown" : compactBytes(n);

function measuredNumber(n: unknown): n is number | null {
  return n === null || (typeof n === "number" && Number.isFinite(n) && n >= 0);
}
function usableCompactJob(job: CompactJob | null): boolean {
  return (
    job === null ||
    (!!job &&
      typeof job.id === "string" &&
      !!job.id &&
      typeof job.state === "string" &&
      typeof job.started_at === "number" &&
      Number.isFinite(job.started_at) &&
      (job.result === null ||
        (typeof job.result?.vacuum === "string" &&
          typeof job.result?.checkpoint === "string" &&
          measuredNumber(job.result?.bytes_freed) &&
          Array.isArray(job.result?.blockers) &&
          job.result.blockers.every((b) => typeof b?.detail === "string"))))
  );
}
function usableCompactInfo(r: CompactInfo): boolean {
  const c = r?.compact;
  return (
    !!c &&
    typeof c.available === "boolean" &&
    measuredNumber(c.db_bytes) &&
    measuredNumber(c.wal_bytes) &&
    measuredNumber(c.reclaimable_bytes) &&
    Array.isArray(c.blockers) &&
    c.blockers.every((b) => typeof b?.detail === "string") &&
    (c.holders === null ||
      (Array.isArray(c.holders?.pids) &&
        typeof c.holders?.unknown === "boolean")) &&
    (c.disk === null ||
      [
        c.disk?.database_required,
        c.disk?.database_free,
        c.disk?.temp_required,
        c.disk?.temp_free,
      ].every((n) => typeof n === "number" && Number.isFinite(n) && n >= 0)) &&
    (r.runner === null || typeof r.runner?.job === "string") &&
    usableCompactJob(r.job)
  );
}

/** The server retains one job until its replacement/restart. Pin the displayed id across
 *  navigation and polling; 404 is an explicit lost result, never a different job's success.
 *  A lost POST response is uncertain and must be refreshed, never automatically resubmitted.
 *  Only a GET started after that outcome may reconcile it; older responses cannot clear it. */
export function CompactDatabase() {
  const [info, setInfo] = useState<CompactInfo | null>(null);
  const [job, setJob] = useState<CompactJob | null>(null);
  const wanted = useRef(rememberedJob());
  const statusEpoch = useRef(0);
  const [reload, setReload] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [uncertain, setUncertain] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const epoch = statusEpoch.current;
    const stale = () => cancelled || epoch !== statusEpoch.current;
    api
      .compactInfo(wanted.current)
      .then((r) => {
        if (stale()) return;
        if (!usableCompactInfo(r))
          throw new Error("Incomplete database measurement");
        setInfo(r);
        setJob(r.job);
        if (r.job) {
          wanted.current = r.job.id;
          rememberJob(r.job.id);
        }
        setError(null);
        setUncertain(false);
      })
      .catch((e: unknown) => {
        if (stale()) return;
        setInfo(null);
        setConfirming(false);
        if (e instanceof ApiError && e.status === 404) {
          setNotice(
            "The previous compaction result is no longer available: the server restarted or a newer job replaced it. Refresh to view the latest job.",
          );
          setJob(null);
          wanted.current = undefined;
          rememberJob();
        }
        setError(
          "Couldn’t refresh the database status. Refresh before trying again; a running job continues on the server.",
        );
      });
    return () => {
      cancelled = true;
    };
  }, [reload]);

  const running = jobRunning(job);
  const runner = info?.runner;
  useEffect(() => {
    if (!running && !runner) return;
    const t = setInterval(() => setReload((n) => n + 1), BUSY_POLL_MS);
    return () => clearInterval(t);
  }, [running, runner]);

  const c = info?.compact;
  const canStart =
    !!c &&
    c.available &&
    c.blockers.length === 0 &&
    c.reclaimable_bytes !== null &&
    c.reclaimable_bytes > 0 &&
    c.db_bytes !== null &&
    c.wal_bytes !== null &&
    c.disk !== null &&
    c.holders !== null &&
    !c.holders.unknown &&
    c.holders.pids.length === 0 &&
    !runner &&
    !running &&
    !submitting &&
    !error &&
    !uncertain;
  const confirmOpen = confirming && canStart;

  const submit = async () => {
    if (!canStart) return;
    // Synchronous: a GET already in flight must not overwrite the submission's state.
    statusEpoch.current += 1;
    setSubmitting(true);
    setConfirming(false);
    setNotice(null);
    try {
      const r = await api.compact();
      if (!r.job?.id || !usableCompactJob(r.job))
        throw new Error("Missing compaction job");
      wanted.current = r.job.id;
      rememberJob(r.job.id);
      setJob(r.job);
      setInfo(null);
      setReload((n) => n + 1);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setNotice(
          "Compaction was refused. Refreshing the current blockers and maintenance job.",
        );
        wanted.current = undefined;
        rememberJob();
        setInfo(null);
        setReload((n) => n + 1);
      } else {
        setUncertain(true);
        setError(
          "Couldn’t confirm whether compaction started. Refresh to check its status before trying again.",
        );
      }
    } finally {
      // Also retire requests started while the POST was pending. In particular, none can
      // clear an uncertain result; reconciliation needs a request begun after this point.
      statusEpoch.current += 1;
      setSubmitting(false);
    }
  };

  const result = job?.result;
  const lines: ResultLine[] = [];
  if (running)
    lines.push({
      tone: "attention",
      text: `${job?.state === "checkpoint" ? "Finishing the WAL checkpoint" : job?.state === "checking" ? "Checking database availability" : "Compacting the database"}… BattleLab’s OpenCode launches are unavailable; retry when maintenance finishes.`,
    });
  if (result?.vacuum === "done") {
    lines.push({
      tone: "up",
      text: `Compaction completed. ${result.bytes_freed === null ? "Reclaimed space could not be measured." : `${compactBytes(result.bytes_freed)} reclaimed.`}`,
    });
    if (result.checkpoint === "done")
      lines.push({ tone: "up", text: "WAL checkpoint completed." });
    if (result.checkpoint === "deferred" || result.checkpoint === "failed")
      lines.push({
        tone: "attention",
        text: `WAL checkpoint ${result.checkpoint}. Compaction remains complete; the WAL may shrink after a later successful truncating checkpoint.`,
      });
  } else if (result?.vacuum === "rolled_back") {
    lines.push({
      tone: "down",
      text: "Compaction stopped and rolled back. This compaction made no database changes.",
    });
  } else if (result) {
    lines.push({
      tone: "attention",
      text:
        result.vacuum === "not_started"
          ? "Compaction did not start."
          : "The compaction outcome is unknown.",
    });
  }
  for (const b of result?.blockers ?? [])
    lines.push({ tone: "attention", text: b.detail });

  return (
    <div className={styles.databaseSection} aria-label="OpenCode database">
      <h3 className={styles.subhead}>OpenCode database</h3>
      <dl className={styles.databaseStats}>
        <div>
          <dt>Size</dt>
          <dd>{c ? measuredBytes(c.db_bytes) : "…"}</dd>
        </div>
        <div>
          <dt>WAL</dt>
          <dd>{c ? measuredBytes(c.wal_bytes) : "…"}</dd>
        </div>
        <div>
          <dt>Reclaimable</dt>
          <dd>{c ? measuredBytes(c.reclaimable_bytes) : "…"}</dd>
        </div>
        <div>
          <dt>Holders</dt>
          <dd>
            {!c
              ? "…"
              : !c.holders || c.holders.unknown
                ? "unknown"
                : c.holders.pids.length}
          </dd>
        </div>
      </dl>
      {c && c.blockers.length > 0 && (
        <ResultLines
          lines={c.blockers.map((b) => ({ tone: "attention", text: b.detail }))}
        />
      )}
      {c?.disk && (
        <p className={styles.hint}>
          Disk needed: {compactBytes(c.disk.database_required)} on the database
          filesystem ({compactBytes(c.disk.database_free)} available).
          {c.disk.shared_filesystem
            ? " Includes SQLite’s temporary space on the same filesystem."
            : ` Temporary filesystem: ${compactBytes(c.disk.temp_required)} needed (${compactBytes(c.disk.temp_free)} available).`}
        </p>
      )}
      <p className={styles.hint}>
        Reclaims pages freed by OpenCode; session history is kept. While
        compacting, BattleLab won’t start OpenCode sessions. OpenCode started
        outside BattleLab may report “database busy”; SQLite keeps the file
        consistent.
      </p>
      {confirmOpen ? (
        <div>
          <p className={styles.confirmText}>
            Compact the OpenCode database ({compactBytes(c.reclaimable_bytes!)}{" "}
            reclaimable)?
          </p>
          <p className={styles.hint}>
            Availability and disk space are checked again before compaction.
            OpenCode launches from BattleLab will be unavailable until it
            finishes.
          </p>
          <div className={styles.confirmRow}>
            <button
              type="button"
              className={`${styles.danger} ${styles.actionBtn}`}
              onClick={() => void submit()}
            >
              Confirm compaction
            </button>
            <button
              type="button"
              className={`${styles.secBtnGhost} ${styles.actionBtn}`}
              onClick={() => setConfirming(false)}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className={styles.cleanupRow}>
          <button
            type="button"
            className={`${styles.secBtnGhost} ${styles.actionBtn}`}
            disabled={!canStart}
            onClick={() => setConfirming(true)}
          >
            {submitting
              ? "Starting compaction…"
              : running
                ? "Compacting…"
                : "Compact database"}
          </button>
          <RefreshButton
            label="Refresh the database status"
            onClick={() => setReload((n) => n + 1)}
          />
        </div>
      )}
      {notice && <ResultLines lines={[{ tone: "attention", text: notice }]} />}
      {error && <ResultLines lines={[{ tone: "down", text: error }]} />}
      {runner && !running && <ResultLines lines={[busyLine(runner.job)]} />}
      {job && (
        <p className={styles.hint}>
          Compaction job {job.id.slice(0, 8)} ·{" "}
          {new Date(job.started_at * 1000).toLocaleString()}
        </p>
      )}
      {lines.length > 0 && <ResultLines lines={lines} />}
    </div>
  );
}
