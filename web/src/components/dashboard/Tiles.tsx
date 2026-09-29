import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Link } from "react-router-dom";

import { api } from "../../lib/api";
import {
  billable,
  shortTokens,
  quotaReadable,
  stalenessNote,
  usageCaption,
  worstWindow,
} from "../../lib/agentUsage";
import { relTime } from "../../lib/format";
import { missionLink, MISSION_PATH } from "../../lib/missionLink";
import { SESSIONS_PATH } from "../../lib/routes";
import type {
  AgentUsageResponse,
  AgentUsageRow,
  DashboardSessions,
  Session,
} from "../../types/api";
import s from "../ask/AskHome.module.css";
import { sessionRoute } from "../ask/needsYouLabels";
import {
  BAND_LABEL,
  forecastLine,
  lowestPlanLeft,
  resetsAt,
} from "./dashboard";
import d from "./Dashboard.module.css";
import { readAllRunning, type MissionsSnapshot } from "./sources";
import type { Polled } from "./usePolled";

/** The BattleLab dashboard's tiles (#1123). Presentational: each is handed its own read and its
 *  own Retry, so one failing source never blanks another. Every number links to — or opens —
 *  exactly what it counts, and every string that came from an agent is rendered as TEXT. */

function Section({
  id,
  title,
  meta,
  end,
  children,
  testid,
}: {
  id: string;
  title: string;
  meta?: ReactNode;
  end?: ReactNode;
  children: ReactNode;
  testid: string;
}) {
  return (
    <section
      className={s.sec}
      aria-labelledby={`${id}-h`}
      id={id}
      data-testid={testid}
    >
      <div className={s.head}>
        <span className={s.sq} aria-hidden="true" />
        <h2 id={`${id}-h`} className={s.title}>
          {title}
        </h2>
        {meta ? <span className={s.meta}>{meta}</span> : null}
        {end ? <div className={s.headEnd}>{end}</div> : null}
      </div>
      {children}
    </section>
  );
}

/** Loading / failed-first-read, or null when there is data to draw. A failed read is never drawn
 *  as "nothing": that is a claim only a successful read can make. */
function Unready<T>({
  res,
  retry,
  what,
}: {
  res: Polled<T>;
  retry: () => void;
  what: string;
}) {
  if (res.status === "loading") {
    return (
      <p className={s.note} data-testid="tile-loading">
        Reading {what}…
      </p>
    );
  }
  if (res.status === "error") {
    return (
      <p
        className={`${s.note} ${s.err} ${d.failRow}`}
        role="alert"
        data-testid="tile-error"
      >
        <span>
          Couldn’t read {what} — nothing is counted rather than a count that may
          be wrong.
        </span>
        <button type="button" className={s.btn} onClick={retry}>
          Retry
        </button>
      </p>
    );
  }
  return null;
}

function RefreshFailed({ show, retry }: { show: boolean; retry: () => void }) {
  if (!show) return null;
  return (
    <p
      className={`${s.note} ${s.err} ${d.failRow}`}
      role="alert"
      data-testid="tile-refresh-error"
    >
      <span>Couldn’t refresh — showing the last read.</span>
      <button type="button" className={s.btn} onClick={retry}>
        Retry
      </button>
    </p>
  );
}

// ---- the strip -------------------------------------------------------------------------------

export function KpiStrip({
  sessions,
  missions,
  quota,
  needsYou,
  onJump,
}: {
  sessions: Polled<DashboardSessions>;
  missions: Polled<MissionsSnapshot>;
  quota: Polled<AgentUsageResponse>;
  needsYou: number | null;
  /** Open (scroll to) the tile a number counts. */
  onJump: (tileId: string) => void;
}) {
  const live = sessions.status === "ok" ? sessions.data.live : null;
  const plan = quota.status === "ok" ? lowestPlanLeft(quota.data.agents) : null;
  return (
    <div className={d.kpis} data-testid="dash-kpis">
      <Kpi
        label="Active agents"
        testid="kpi-agents"
        onClick={() => onJump("dash-running")}
        value={
          live && live.health === "ok" ? (
            <>
              {live.total} <small>live · {live.working} working</small>
            </>
          ) : sessions.status === "loading" ? (
            "…"
          ) : (
            <>
              — <small>couldn’t read</small>
            </>
          )
        }
        sub={
          live && live.health === "ok" && Object.keys(live.by_engine).length ? (
            <span className={d.per}>
              {Object.entries(live.by_engine).map(([e, c]) => (
                <span key={e}>
                  <b>{e}</b> {c.live}/{c.working}
                </span>
              ))}
            </span>
          ) : null
        }
      />
      <Kpi
        label="Missions"
        testid="kpi-missions"
        onClick={() => onJump("dash-missions")}
        value={
          missions.status === "ok" ? (
            <>
              {missions.data.activeTotal + missions.data.reviewTotal}{" "}
              <small>
                {missions.data.activeTotal} active · {missions.data.reviewTotal}{" "}
                in review
              </small>
            </>
          ) : missions.status === "loading" ? (
            "…"
          ) : (
            <>
              — <small>couldn’t read</small>
            </>
          )
        }
      />
      <Kpi
        label="Needs you"
        testid="kpi-needs"
        onClick={() => onJump("needs-you-h")}
        value={
          needsYou === null ? (
            "…"
          ) : (
            <>
              {needsYou}{" "}
              <small>{needsYou === 1 ? "session" : "sessions"}</small>
            </>
          )
        }
      />
      <Kpi
        label="Plan quota — lowest left"
        testid="kpi-quota"
        onClick={() => onJump("dash-quota")}
        value={
          quota.status === "error" ? (
            <>
              — <small>couldn’t read</small>
            </>
          ) : quota.status === "loading" ? (
            "…"
          ) : plan ? (
            <>
              {Math.round(plan.left)}%{" "}
              <small>
                {plan.engine} · {plan.label}
              </small>
            </>
          ) : (
            <small>no agent reports a plan quota</small>
          )
        }
        sub={
          plan ? (
            <span className={plan.stale ? d.warnText : undefined}>
              {plan.resets_at ? `resets ${resetsAt(plan.resets_at)} · ` : ""}
              {plan.stale ? "stale · " : ""}
              {plan.at ? `checked ${relTime(plan.at)}` : "not asked yet"}
            </span>
          ) : null
        }
      />
    </div>
  );
}

function Kpi({
  label,
  value,
  sub,
  onClick,
  testid,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  onClick: () => void;
  testid: string;
}) {
  return (
    <button
      type="button"
      className={d.kpi}
      onClick={onClick}
      data-testid={testid}
    >
      <span className={d.kpiLabel}>
        <span className={s.sq} aria-hidden="true" />
        {label}
      </span>
      <span className={d.kpiValue}>{value}</span>
      {sub ? <span className={d.kpiSub}>{sub}</span> : null}
    </button>
  );
}

// ---- running sessions ------------------------------------------------------------------------

/** The expanded "All N running" list: the drill-down behind the count, read page by page so it is
 *  EVERY running session, never the first page passed off as all (Hermes 5240, finding 3). */
type Expanded =
  | { status: "off" }
  | { status: "loading"; rows: Session[] | null }
  | { status: "ok"; rows: Session[]; total: number }
  | { status: "error"; message: string; rows: Session[] | null };

export function RunningTile({
  res,
  retry,
}: {
  res: Polled<DashboardSessions>;
  retry: () => void;
}) {
  const [expanded, setExpanded] = useState<Expanded>({ status: "off" });
  const gen = useRef(0);
  const live = res.status === "ok" ? res.data.live : null;
  const on = expanded.status !== "off";

  const load = useCallback(async () => {
    const mine = ++gen.current;
    setExpanded((prev) => ({
      status: "loading",
      rows:
        prev.status === "ok" || prev.status === "loading" ? prev.rows : null,
    }));
    try {
      const got = await readAllRunning();
      if (mine === gen.current) setExpanded({ status: "ok", ...got });
    } catch (e) {
      if (mine !== gen.current) return;
      setExpanded((prev) => ({
        status: "error",
        message: e instanceof Error ? e.message : "Couldn’t read.",
        rows:
          prev.status === "ok" || prev.status === "loading" ? prev.rows : null,
      }));
    }
  }, []);

  // The expanded list FOLLOWS the live read (finding 2): every successful poll re-reads it, and a
  // later answer always wins over an earlier one (the generation fence above).
  const pollStamp = res.status === "ok" ? res.data : null;
  useEffect(() => {
    if (on && pollStamp) void load();
    // `on` flips only through showAll/hide, which load/cancel themselves.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pollStamp]);

  const showAll = () => void load();
  const hide = () => {
    gen.current++;
    setExpanded({ status: "off" });
  };

  const listed =
    expanded.status === "ok" ||
    expanded.status === "loading" ||
    expanded.status === "error"
      ? expanded.rows
      : null;

  return (
    <Section
      id="dash-running"
      testid="dash-running"
      title={
        live && live.health === "ok"
          ? `Running sessions · ${live.total}`
          : "Running sessions"
      }
      meta={
        live && live.health === "ok"
          ? `live agent · ${live.working} working`
          : undefined
      }
    >
      <Unready res={res} retry={retry} what="running sessions" />
      {res.status === "ok" ? (
        <RefreshFailed show={res.refreshFailed} retry={retry} />
      ) : null}
      {live && live.health === "unavailable" ? (
        <p
          className={`${s.note} ${s.err} ${d.failRow}`}
          role="alert"
          data-testid="running-unavailable"
        >
          <span>
            Couldn’t read which agents are running — nothing is counted rather
            than a count that may be wrong.
          </span>
          <button type="button" className={s.btn} onClick={retry}>
            Retry
          </button>
        </p>
      ) : null}
      {live && live.health === "ok" ? (
        live.total === 0 && !on ? (
          <p className={s.note} data-testid="running-empty">
            No agent is running.
          </p>
        ) : (
          <>
            {on ? (
              <div className={d.scrollList} data-testid="running-all">
                {listed && listed.length === 0 ? (
                  <p className={s.note}>No agent is running.</p>
                ) : null}
                {(listed ?? []).map((r) => (
                  <LiveRow
                    key={r.id}
                    id={r.id}
                    title={r.title || r.id}
                    meta={`${r.engine}${r.project?.name ? ` · ${r.project.name}` : ""}${
                      r.working ? " · working" : " · live, idle"
                    }`}
                    working={Boolean(r.working)}
                    right={relTime(r.last_mtime)}
                  />
                ))}
                {expanded.status === "loading" && !listed ? (
                  <p className={s.note}>Reading…</p>
                ) : null}
              </div>
            ) : (
              live.rows.map((r) => (
                <LiveRow
                  key={r.id}
                  id={r.id}
                  title={r.title || r.id}
                  meta={`${r.engine}${r.project.name ? ` · ${r.project.name}` : ""} · ${
                    r.working ? "working" : "live, idle"
                  }${r.state_line ? ` · ${r.state_line}` : ""}`}
                  working={r.working}
                  right={relTime(r.last_activity)}
                />
              ))
            )}
            {expanded.status === "error" ? (
              <p className={`${s.note} ${s.err} ${d.failRow}`} role="alert">
                <span>{expanded.message}</span>
                <button type="button" className={s.btn} onClick={showAll}>
                  Retry
                </button>
              </p>
            ) : null}
            <div className={d.foot}>
              <span data-testid="running-foot">
                {expanded.status === "ok"
                  ? expanded.rows.length < expanded.total
                    ? `showing ${expanded.rows.length} of ${expanded.total} running`
                    : `all ${expanded.total} running`
                  : on
                    ? "reading every running session…"
                    : `showing ${live.rows.length} of ${live.total}`}
              </span>
              {on ? (
                <button type="button" className={d.go} onClick={hide}>
                  Show fewer
                </button>
              ) : live.total > live.rows.length ? (
                <button
                  type="button"
                  className={d.go}
                  onClick={showAll}
                  data-testid="running-show-all"
                >
                  All {live.total} running →
                </button>
              ) : null}
            </div>
          </>
        )
      ) : null}
    </Section>
  );
}

function LiveRow({
  id,
  title,
  meta,
  working,
  right,
}: {
  id: string;
  title: string;
  meta: string;
  working: boolean;
  right: string;
}) {
  return (
    <Link className={d.lrow} to={sessionRoute(id)} data-testid="running-row">
      <span
        className={`${d.dot} ${working ? d.up : d.idle}`}
        aria-hidden="true"
      />
      <span className={d.lbody}>
        <span className={d.tt}>{title}</span>
        <span className={d.mm}>{meta}</span>
      </span>
      <span className={d.rr}>{right}</span>
    </Link>
  );
}

// ---- missions --------------------------------------------------------------------------------

export function MissionsTile({
  res,
  retry,
}: {
  res: Polled<MissionsSnapshot>;
  retry: () => void;
}) {
  const m = res.status === "ok" ? res.data : null;
  return (
    <Section
      id="dash-missions"
      testid="dash-missions"
      title={m ? `Missions · ${m.activeTotal + m.reviewTotal}` : "Missions"}
      meta={
        m ? `${m.activeTotal} active · ${m.reviewTotal} in review` : undefined
      }
    >
      <Unready res={res} retry={retry} what="missions" />
      {res.status === "ok" ? (
        <RefreshFailed show={res.refreshFailed} retry={retry} />
      ) : null}
      {m ? (
        m.activeTotal + m.reviewTotal === 0 ? (
          <p className={s.note} data-testid="missions-empty">
            No mission is active or in review.{" "}
            <Link to={MISSION_PATH}>Start one →</Link>
          </p>
        ) : (
          <>
            {[...m.review, ...m.active].map((x) => (
              <Link
                key={x.id}
                className={d.lrow}
                to={missionLink(x.id)}
                data-testid="mission-row"
              >
                <span
                  className={`${d.dot} ${x.state === "review" || x.needs_you ? d.deg : d.up}`}
                  aria-hidden="true"
                />
                <span className={d.lbody}>
                  <span className={d.tt}>{x.title || x.id}</span>
                  <span className={d.mm}>
                    {x.state === "review"
                      ? "in review · waiting on you"
                      : x.state}
                    {x.needs_you && x.state !== "review" ? " · needs you" : ""}{" "}
                    · {x.session_keys.length}{" "}
                    {x.session_keys.length === 1 ? "session" : "sessions"}
                  </span>
                </span>
                <span className={d.rr}>{relTime(x.updated_at)}</span>
              </Link>
            ))}
            <div className={d.foot}>
              <span>
                showing {m.review.length + m.active.length} of{" "}
                {m.activeTotal + m.reviewTotal}
              </span>
              <Link className={d.go} to={MISSION_PATH}>
                All missions →
              </Link>
            </div>
          </>
        )
      ) : null}
    </Section>
  );
}

// ---- quota -----------------------------------------------------------------------------------

export function QuotaTile({
  res,
  retry,
}: {
  res: Polled<AgentUsageResponse>;
  retry: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Refresh is the ONE thing on this page that asks the vendors: the operator's explicit press.
  const refresh = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.agentUsageRefresh();
      await retry();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn’t refresh.");
    } finally {
      setBusy(false);
    }
  };
  const rows =
    res.status === "ok" ? res.data.agents.filter(quotaReadable) : null;
  return (
    <Section
      id="dash-quota"
      testid="dash-quota"
      title="Quota left"
      meta="from the cache · never asked on load"
      end={
        <button
          type="button"
          className={s.btn}
          disabled={busy}
          onClick={() => void refresh()}
        >
          {busy ? "Refreshing…" : "Refresh"}
        </button>
      }
    >
      <Unready res={res} retry={retry} what="agent quotas" />
      {res.status === "ok" ? (
        <RefreshFailed show={res.refreshFailed} retry={retry} />
      ) : null}
      {error ? (
        <p className={`${s.note} ${s.err}`} role="alert">
          {error}
        </p>
      ) : null}
      {rows ? (
        rows.length === 0 ? (
          <p className={s.note}>
            No agent’s quota can be read right now. An agent without a plan
            quota shows here once it has a limit in Settings → Agents & usage.
          </p>
        ) : (
          rows.map((r) => <QuotaRow key={r.engine} row={r} />)
        )
      ) : null}
    </Section>
  );
}

function QuotaRow({ row }: { row: AgentUsageRow }) {
  // Only rows `quotaReadable` admits get here: a read, current quota — never "not measured" or
  // a refused account; those are left off the tile altogether.
  const w = row.source === "plan" ? worstWindow(row) : null;
  const leftPct = w ? Math.max(0, 100 - w.used_pct) : null;
  const usedPct = row.used_pct;
  // Only a PLAN window is the agent's own percentage. A token count against the operator's limit and
  // the operator's own counter are said as counts (mockup v2), never dressed up as "% left".
  const headline = w
    ? `${Math.round(leftPct!)}% left · ${w.label}`
    : `${shortTokens(billable(row))} of ${shortTokens(row.limit_tokens)}`;
  const fill = w
    ? leftPct!
    : usedPct !== null && Number.isFinite(usedPct)
      ? 100 - usedPct
      : null;
  const tone = fill === null ? "" : fill <= 0 ? d.down : fill < 25 ? d.deg : "";
  const stale = stalenessNote(row);
  const fc = forecastLine(row);
  return (
    <div className={d.q} data-testid="quota-row" data-engine={row.engine}>
      <div className={d.qTop}>
        <span>{row.engine}</span>
        <span>{headline}</span>
      </div>
      <div
        className={`${d.bar} ${fill !== null ? "" : d.barNone}`}
        aria-hidden="true"
      >
        {fill !== null ? (
          <i
            className={tone}
            style={{ width: `${Math.max(0, Math.min(100, fill))}%` }}
          />
        ) : null}
      </div>
      <div className={`${d.qSub} ${stale ? d.warnText : ""}`}>
        {usageCaption(row)}
        {stale ? ` · ${row.error ? "" : "stale — taken "}${stale}` : ""}
      </div>
      {fc ? (
        <div
          className={`${d.qFc} ${fc.tone === "over" ? d.fcOver : fc.tone === "warn" ? d.fcWarn : ""}`}
          data-testid="quota-forecast"
          data-tone={fc.tone}
        >
          {fc.text}
        </div>
      ) : null}
    </div>
  );
}

// ---- most recent sessions --------------------------------------------------------------------

export function RecentSessionsTile({
  res,
  retry,
}: {
  res: Polled<DashboardSessions>;
  retry: () => void;
}) {
  const rows = res.status === "ok" ? res.data.recent.rows : null;
  return (
    <Section
      id="dash-recent"
      testid="dash-recent"
      title="Most recent sessions"
      meta="by last activity · newest first"
    >
      <Unready res={res} retry={retry} what="sessions" />
      {/* A failed refresh keeps the last list — and SAYS so, like every other tile (finding 5). */}
      {res.status === "ok" ? (
        <RefreshFailed show={res.refreshFailed} retry={retry} />
      ) : null}
      {rows ? (
        rows.length === 0 ? (
          <p className={s.note}>No sessions yet.</p>
        ) : (
          <>
            <table className={d.rtable}>
              <thead>
                <tr>
                  <th>Session</th>
                  <th>State</th>
                  <th className={d.wideOnly}>Agent · project</th>
                  <th>Last activity</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id} data-testid="recent-session-row">
                    <td className={d.tdTitle}>
                      <Link to={sessionRoute(r.id)}>{r.title || r.id}</Link>
                      <span className={d.narrowMeta}>
                        {r.engine}
                        {r.project.name ? ` · ${r.project.name}` : ""}
                      </span>
                    </td>
                    <td>
                      <span
                        className={`${d.band} ${d[`band_${r.band}`] ?? ""}`}
                      >
                        {BAND_LABEL[r.band]}
                      </span>
                    </td>
                    <td className={`${d.tdMeta} ${d.wideOnly}`}>
                      {r.engine}
                      {r.project.name ? ` · ${r.project.name}` : ""}
                    </td>
                    <td className={d.tdMeta}>{relTime(r.last_activity)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className={d.foot}>
              <span>latest {rows.length}</span>
              <Link className={d.go} to={SESSIONS_PATH}>
                All sessions →
              </Link>
            </div>
          </>
        )
      ) : null}
    </Section>
  );
}
