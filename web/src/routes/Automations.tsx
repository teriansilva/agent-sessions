/** Missions → Automations (#1201): what will run, what did run, and why something isn't running.
 *
 *  The list is the server's public shape as-is: each row's state word and LED, its trigger, its
 *  action, the next run (or why there is none), the last outcome, and the 14-day strip — each
 *  square the day's WORST outcome, the words beside it counting RUNS. Run now and a ⋯ menu (enable
 *  or turn off, pause or resume, edit, duplicate, delete) per row; at ≤800px the rows are cards.
 *  A panel lists the automations that are waiting on the operator, with what would unblock each.
 *
 *  The 7-day Upcoming view is Phase 4 of #1201 and is not here. Nothing on this page is a consent:
 *  enabling opens the consent dialog, whose lines are the server's. */
import { useCallback, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Ellipsis, Plus } from "lucide-react";

import { HudFrame } from "../components/hud/HudFrame";
import { AnchoredMenu } from "../components/ui/AnchoredMenu";
import { ConfirmDialog } from "../components/templates/ConfirmDialog";
import { MissionRailOnly } from "../components/automations/MissionRailOnly";
import {
  KillSwitchNotice,
  OutcomeWord,
  InFlightNote,
  StateWord,
  Strip,
  ToneText,
} from "../components/automations/AutomationBits";
import {
  useAutomations,
  useNowS,
  useProjects,
} from "../components/automations/useAutomations";
import { useEnableFlow } from "../components/automations/useEnableFlow";
import { refreshOrigins } from "../app/automationOrigins";
import { ApiError, api } from "../lib/api";
import {
  errorWords,
  inFlightNote,
  actionLabel,
  duplicateBody,
  needsYou,
  nextRunWords,
  runNowBlock,
  stateWord,
  stripWords,
  targetWords,
  triggerWords,
  whenWords,
  whyNotRunning,
} from "../lib/automations";
import {
  AUTOMATION_NEW_PATH,
  automationEditPath,
  automationPath,
} from "../lib/routes";
import type { Automation, AutomationRun } from "../types/automations";
import styles from "../components/automations/automations.module.css";

type Filter = "all" | "enabled" | "needs_you" | "off";

export default function Automations() {
  const navigate = useNavigate();
  const { data, loadedAt, error, loading, reload, applyRow } = useAutomations();
  const projects = useProjects();
  const nowS = useNowS();
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [project, setProject] = useState("");
  const [notice, setNotice] = useState<{
    tone: "ok" | "warn" | "bad";
    text: string;
    link?: { to: string; label: string };
    /** A run already past its last check may still complete (`in_flight`): amber, not an error. */
    note?: string;
    /** The automation this notice can take straight to "Review and approve". */
    approve?: Automation;
  } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<Automation | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleteReturn, setDeleteReturn] = useState<HTMLElement | null>(null);

  const projectName = useCallback(
    (id: string) => projects?.find((p) => p.id === id)?.name ?? id,
    [projects],
  );
  const enable = useEnableFlow(
    useCallback(
      (a: Automation | null) => {
        if (a) {
          applyRow(a); // the server's own answer, at once — then a full read behind it
          setNotice({ tone: "ok", text: `“${a.name}” is enabled.`, note: inFlightNote(a) });
        }
        void reload();
      },
      [reload, applyRow],
    ),
  );

  const rows = useMemo(() => data?.automations ?? [], [data]);
  const loopOn = data?.loop.enabled ?? true;
  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return rows.filter((a) => {
      if (needle && !a.name.toLowerCase().includes(needle)) return false;
      if (project && targetWords(a.action, projectName) !== project) return false;
      if (filter === "enabled") return a.state === "enabled";
      if (filter === "needs_you") return needsYou(a);
      if (filter === "off") return !a.enabled || a.state === "expired" || a.state === "finished";
      return true;
    });
  }, [rows, q, project, filter, projectName]);
  const targets = useMemo(
    () => [...new Set(rows.map((a) => targetWords(a.action, projectName)).filter(Boolean))].sort(),
    [rows, projectName],
  );
  const counts = useMemo(() => {
    const c = { enabled: 0, needs: 0, off: 0, runs: 0, failed: 0, next: null as number | null };
    for (const a of rows) {
      if (a.state === "enabled") c.enabled += 1;
      if (needsYou(a)) c.needs += 1;
      if (!a.enabled) c.off += 1;
      c.runs += a.stats.runs;
      c.failed += a.stats.failed;
      if (a.next_run && (c.next == null || a.next_run.at < c.next)) c.next = a.next_run.at;
    }
    return c;
  }, [rows]);
  const waiting = rows.filter(needsYou);

  async function act(
    a: Automation,
    fn: () => Promise<{ in_flight?: boolean; in_flight_detail?: string }>,
    done: string,
  ) {
    setBusy(a.id);
    setNotice(null);
    try {
      const r = await fn();
      if (r && typeof r === "object" && "id" in r && "state" in r) applyRow(r as Automation);
      setNotice({ tone: "ok", text: done, note: inFlightNote(r) });
    } catch (e) {
      setNotice({ tone: "bad", text: errorWords(e, "That didn’t work.") });
    } finally {
      setBusy(null);
      void reload();
    }
  }

  async function runNow(a: Automation) {
    setBusy(a.id);
    setNotice(null);
    try {
      const run: AutomationRun = await api.runAutomation(a.id);
      void refreshOrigins(true);
      setNotice(
        run.outcome === "skipped"
          ? { tone: "warn", text: `“${a.name}” did not run: ${run.reason}` }
          : {
              tone: "ok",
              text: `“${a.name}” is running.`,
              link: { to: automationPath(a.id, run.id), label: "View the run" },
            },
      );
    } catch (e) {
      const text = `Run now was refused: ${errorWords(e, "unknown error")}`;
      setNotice({ tone: "bad", text });
      // If the refusal left it needing approval (an out-of-date receipt is flagged as it is
      // refused), offer that approval right here, not only in the panel below.
      const fresh = await reload();
      const now = fresh.ok ? fresh.data.automations.find((x) => x.id === a.id) : undefined;
      if (now?.needs_reapproval) setNotice({ tone: "bad", text, approve: now });
      setBusy(null);
      return;
    }
    setBusy(null);
    void reload();
  }

  async function duplicate(a: Automation) {
    const body = duplicateBody(a);
    if (!body) {
      setNotice({ tone: "bad", text: "This automation’s settings can’t be read, so it can’t be copied." });
      return;
    }
    setBusy(a.id);
    try {
      const copy = await api.createAutomation(body);
      navigate(automationEditPath(copy.id));
    } catch (e) {
      setNotice({ tone: "bad", text: errorWords(e, "Couldn’t copy it.") });
      setBusy(null);
    }
  }

  async function confirmDelete() {
    const a = deleting;
    if (!a) return;
    setBusy(a.id);
    setDeleteError(null);
    try {
      const r = await api.deleteAutomation(a.id, a.revision);
      setDeleting(null);
      setNotice({
        tone: "ok",
        text: `“${a.name}” was deleted. Its run history went with it.`,
        note: inFlightNote(r),
      });
      void reload();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // STALE: it changed since this page read it. Reload and let the operator decide again on
        // what it is now, rather than deleting something they have not seen.
        const fresh = await reload();
        if (!fresh.ok) {
          // The refresh FAILED: that says nothing about whether it still exists. Stay open.
          setDeleteError(
            `It changed since you loaded it, and reading it again failed (${fresh.error}). Nothing was deleted — try again.`,
          );
          return;
        }
        const now = fresh.data.automations.find((x) => x.id === a.id) ?? null;
        if (now) {
          setDeleting(now);
          setDeleteError("It changed since you loaded it. This is how it looks now — delete it anyway?");
        } else {
          setDeleting(null);
          setNotice({ tone: "ok", text: `“${a.name}” is already gone.` });
        }
      } else {
        setDeleteError(errorWords(e, "Couldn’t delete it."));
      }
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className={styles.page} data-testid="automations-page">
      <MissionRailOnly />
      <div className={styles.stack}>
        <section className={styles.panel} aria-labelledby="automations-title">
          <HudFrame />
          <div className={styles.head}>
            <div className={styles.headText}>
              <span className={styles.kicker}>Missions // Automations</span>
              <h1 id="automations-title" className={styles.title}>
                Automations
              </h1>
              <p className={styles.lede}>
                Missions and sessions that run while you are away: once, on a schedule, or when you
                press Run now. Nothing runs until you enable it.
              </p>
            </div>
            <div className={styles.headActions}>
              <Link to={AUTOMATION_NEW_PATH} className={styles.primary} data-testid="automation-new">
                <Plus size={15} aria-hidden="true" /> New automation
              </Link>
            </div>
          </div>

          {data && (
            <div className={styles.summary} data-testid="automations-summary">
              <span className={styles.tagItem}>
                <span className={`${styles.led} ${styles.up}`} aria-hidden="true" />
                Enabled <b>{counts.enabled}</b>
              </span>
              <span className={styles.tagItem}>
                <span className={`${styles.led} ${styles.degraded}`} aria-hidden="true" />
                Need you <b>{counts.needs}</b>
              </span>
              <span className={styles.tagItem}>
                <span className={`${styles.led} ${styles.idle}`} aria-hidden="true" />
                Off <b>{counts.off}</b>
              </span>
              <span className={styles.tagItem}>
                Runs 14 d <b>{counts.runs}</b>
              </span>
              <span className={styles.tagItem}>
                Failed 14 d <b>{counts.failed}</b>
              </span>
              <span className={styles.tagItem}>
                Next <b>{counts.next ? whenWords(counts.next, nowS) : "—"}</b>
              </span>
            </div>
          )}

          {!loopOn && <KillSwitchNotice />}
          {error && data && (
            <div className={`${styles.notice} ${styles.noticeBad}`} role="alert" data-testid="automations-load-error">
              <strong>Couldn’t load automations</strong>
              <span>
                {error}. Showing what was loaded
                {loadedAt ? ` at ${new Date(loadedAt).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}` : ""}.
              </span>
              <div className={styles.noticeRow}>
                <button type="button" className={styles.ghost} onClick={() => void reload()}>
                  Retry
                </button>
              </div>
            </div>
          )}
          {notice && (
            <div
              className={`${styles.notice} ${notice.tone === "ok" ? styles.noticeOk : notice.tone === "warn" ? styles.noticeWarn : styles.noticeBad}`}
              role="status"
              data-testid="automations-notice"
            >
              <span>
                {notice.text}{" "}
                {notice.link && (
                  <Link className={styles.link} to={notice.link.to}>
                    {notice.link.label}
                  </Link>
                )}
              </span>
              {notice.note && <InFlightNote text={notice.note} />}
              {notice.approve && (
                <div className={styles.noticeRow}>
                  <button
                    type="button"
                    className={styles.primary}
                    onClick={() => enable.open(notice.approve!)}
                    data-testid="notice-approve"
                  >
                    Review and approve
                  </button>
                </div>
              )}
            </div>
          )}

          {!data && loading && <p className={styles.lede}>Loading automations…</p>}
          {!data && !loading && error && (
            <div className={`${styles.notice} ${styles.noticeBad}`} role="alert" data-testid="automations-load-error">
              <strong>Couldn’t load automations</strong>
              <span>{error}</span>
              <div className={styles.noticeRow}>
                <button type="button" className={styles.ghost} onClick={() => void reload()}>
                  Retry
                </button>
              </div>
            </div>
          )}

          {data && rows.length === 0 && (
            <div className={styles.empty} data-testid="automations-empty">
              <strong>No automations yet</strong>
              <span className={styles.lede}>
                Run a mission or a session on a schedule, once at a set time, or on Run now. Nothing
                runs until you enable it.
              </span>
              <Link to={AUTOMATION_NEW_PATH} className={styles.primary}>
                <Plus size={15} aria-hidden="true" /> New automation
              </Link>
            </div>
          )}

          {rows.length > 0 && (
            <>
              <div className={styles.toolbar}>
                <input
                  className={styles.control}
                  type="search"
                  aria-label="Search automations"
                  placeholder="Search automations…"
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                />
                <select
                  className={styles.control}
                  aria-label="State"
                  value={filter}
                  onChange={(e) => setFilter(e.target.value as Filter)}
                >
                  <option value="all">All states</option>
                  <option value="enabled">Enabled</option>
                  <option value="needs_you">Needs you</option>
                  <option value="off">Off</option>
                </select>
                <select
                  className={styles.control}
                  aria-label="Project"
                  value={project}
                  onChange={(e) => setProject(e.target.value)}
                >
                  <option value="">All projects</option>
                  {targets.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
              </div>
              <div className={styles.table} role="list" aria-label="Automations">
                <div className={`${styles.tr} ${styles.thead}`} aria-hidden="true">
                  <span>Automation</span>
                  <span>Trigger</span>
                  <span>Action</span>
                  <span>Next run</span>
                  <span>Last 14 days</span>
                  <span />
                </div>
                {shown.map((a) => (
                  <Row
                    key={a.id}
                    a={a}
                    nowS={nowS}
                    loopOn={loopOn}
                    busy={busy === a.id}
                    projectName={projectName}
                    onRunNow={() => void runNow(a)}
                    onEnable={() => enable.open(a)}
                    onVerb={(verb, done) => void act(a, () => api.automationVerb(a.id, verb), done)}
                    onEdit={() => navigate(automationEditPath(a.id))}
                    onDuplicate={() => void duplicate(a)}
                    onDelete={() => {
                      setDeleteReturn(document.activeElement as HTMLElement | null);
                      setDeleteError(null);
                      setDeleting(a);
                    }}
                  />
                ))}
                {shown.length === 0 && (
                  <p className={styles.footnote}>No automation matches these filters.</p>
                )}
              </div>
              <p className={styles.footnote}>
                Each square is one day and shows that day’s <b>worst</b> outcome: red if any run
                failed, else grey if any was skipped, else green; empty means nothing ran. The words
                beside the strip count <b>runs</b>, not days, so a mixed day is never hidden.
              </p>
            </>
          )}
        </section>

        {waiting.length > 0 && (
          <section className={styles.panel} aria-labelledby="why-title" data-testid="why-not-running">
            <HudFrame />
            <h2 id="why-title" className={styles.panelTitle}>
              Why is something not running?
            </h2>
            <div className={styles.why}>
              {waiting.map((a) => (
                <div key={a.id} className={styles.whyCard}>
                  <ToneText tone={stateWord(a.state).tone} className={styles.whyHead}>
                    {a.name} · {stateWord(a.state).word.toLowerCase()}
                  </ToneText>
                  <p className={styles.whyText}>{whyNotRunning(a)}</p>
                  <div className={styles.noticeRow}>
                    <Link className={styles.ghost} to={automationPath(a.id)}>
                      View runs
                    </Link>
                    {a.state === "needs_reapproval" ? (
                      <button type="button" className={styles.primary} onClick={() => enable.open(a)}>
                        Review and approve
                      </button>
                    ) : a.state === "paused" ? (
                      <button
                        type="button"
                        className={styles.primary}
                        disabled={busy === a.id}
                        onClick={() => void act(a, () => api.automationVerb(a.id, "resume"), `“${a.name}” resumed.`)}
                      >
                        Resume
                      </button>
                    ) : (
                      <Link className={styles.primary} to={automationEditPath(a.id)}>
                        Edit
                      </Link>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </section>
        )}
      </div>

      {enable.dialog}
      {deleting && (
        <ConfirmDialog
          tag="Delete automation"
          title={`Delete “${deleting.name}”?`}
          confirmLabel="Delete"
          danger
          busy={busy === deleting.id}
          onCancel={() => setDeleting(null)}
          onConfirm={() => void confirmDelete()}
          returnFocusTo={deleteReturn}
        >
          <p>
            It stops for good and its run history is deleted. Anything it already started keeps
            running on its own.
          </p>
          {deleteError && (
            <p className={styles.error} role="alert">
              {deleteError}
            </p>
          )}
        </ConfirmDialog>
      )}
    </div>
  );
}

function Row({
  a,
  nowS,
  loopOn,
  busy,
  projectName,
  onRunNow,
  onEnable,
  onVerb,
  onEdit,
  onDuplicate,
  onDelete,
}: {
  a: Automation;
  nowS: number;
  loopOn: boolean;
  busy: boolean;
  projectName: (id: string) => string;
  onRunNow: () => void;
  onEnable: () => void;
  onVerb: (verb: "disable" | "pause" | "resume", done: string) => void;
  onEdit: () => void;
  onDuplicate: () => void;
  onDelete: () => void;
}) {
  const trig = triggerWords(a.trigger);
  const next = nextRunWords(a, nowS);
  const block = runNowBlock(a, loopOn);
  const act = a.action;
  const actDetail =
    act?.kind === "start_mission"
      ? act.checklist_id === ":none"
        ? "no checklist"
        : act.checklist_id
          ? `checklist ${act.checklist_id}`
          : "default checklist"
      : act?.kind === "start_session"
        ? `${act.engine} · default model`
        : act?.kind === "send_to_session"
          ? act.session_key
          : "";
  return (
    <div className={styles.tr} role="listitem" data-testid="automation-row" data-id={a.id}>
      <div className={styles.cell}>
        <StateWord state={a.state} />
        <Link to={automationPath(a.id)} className={styles.name}>
          {a.name}
        </Link>
        <span className={`${styles.mono} ${styles.faint}`}>{targetWords(a.action, projectName)}</span>
      </div>
      <div className={styles.cell}>
        <span className={styles.cellLabel}>Trigger</span>
        <span>{trig.label}</span>
        <small>{trig.detail}</small>
      </div>
      <div className={styles.cell}>
        <span className={styles.cellLabel}>Action</span>
        <span>{actionLabel(act?.kind)}</span>
        <small>{actDetail}</small>
      </div>
      <div className={styles.cell}>
        <span className={styles.cellLabel}>Next · last</span>
        <span className={styles.mono}>{next.main}</span>
        <small>{next.sub}</small>
        {a.last_run ? (
          <span className={styles.lastLine}>
            <OutcomeWord outcome={a.last_run.outcome} state={a.last_run.state} />{" "}
            <span className={styles.faint}>
              {whenWords(a.last_run.created_at, nowS)}
              {a.last_run.reason ? ` · ${a.last_run.reason}` : ""}
            </span>
          </span>
        ) : (
          <span className={`${styles.lastLine} ${styles.faint}`}>No runs yet</span>
        )}
      </div>
      <div className={styles.cell}>
        <span className={styles.cellLabel}>Last 14 days</span>
        <Strip days={a.strip} />
        <small data-testid="strip-words">{stripWords(a.stats)}</small>
      </div>
      <div className={styles.rowActs}>
        <button
          type="button"
          className={styles.ghost}
          disabled={busy || !!block}
          title={block || undefined}
          onClick={onRunNow}
          data-testid="automation-run-now"
        >
          Run now
        </button>
        <AnchoredMenu
          label={`More actions for ${a.name}`}
          trigger={<Ellipsis size={18} aria-hidden="true" />}
          triggerClassName={styles.icon}
          triggerTestId="automation-more"
          menuTestId="automation-menu"
          disabled={busy}
          focus="first-item"
          portal
          classes={{ wrap: styles.menuWrap, panel: styles.menuPanel, items: styles.menuItems }}
        >
          {(close) => (
            <>
              {!a.enabled || a.needs_reapproval ? (
                <button type="button" role="menuitem" className={styles.menuItem} onClick={() => { close(); onEnable(); }}>
                  {a.needs_reapproval ? "Review and approve…" : "Enable…"}
                </button>
              ) : null}
              {a.enabled && !a.needs_reapproval && (a.paused ? (
                <button type="button" role="menuitem" className={styles.menuItem} onClick={() => { close(); onVerb("resume", `“${a.name}” resumed.`); }}>
                  Resume
                </button>
              ) : (
                <button type="button" role="menuitem" className={styles.menuItem} onClick={() => { close(); onVerb("pause", `“${a.name}” paused.`); }}>
                  Pause
                </button>
              ))}
              {a.enabled && (
                <button type="button" role="menuitem" className={styles.menuItem} onClick={() => { close(); onVerb("disable", `“${a.name}” is off.`); }}>
                  Turn off
                </button>
              )}
              <button type="button" role="menuitem" className={styles.menuItem} onClick={() => { close(); onEdit(); }}>
                Edit
              </button>
              <button type="button" role="menuitem" className={styles.menuItem} onClick={() => { close(); onDuplicate(); }}>
                Duplicate
              </button>
              <button type="button" role="menuitem" className={`${styles.menuItem} ${styles.menuDanger}`} onClick={() => { close(); onDelete(); }}>
                Delete…
              </button>
            </>
          )}
        </AnchoredMenu>
      </div>
    </div>
  );
}
