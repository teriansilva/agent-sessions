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
import type {
  AgentUsageResponse,
  AgentUsageRow,
  DashboardSessions,
  Session,
} from "../../types/api";
import s from "../ask/AskHome.module.css";
import { sessionRoute } from "../ask/needsYouLabels";
import { Gauge, Led, type LedTone, Readout } from "../hud/Meters";
import { Loader } from "../hud/Loader";
import {
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
      <Loader kind="acquire" label={`Acquiring ${what}`} className={d.acquire} testId="tile-loading" />
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
  needsYouFailed = false,
  onJump,
}: {
  sessions: Polled<DashboardSessions>;
  missions: Polled<MissionsSnapshot>;
  quota: Polled<AgentUsageResponse>;
  needsYou: number | null;
  /** The NEEDS YOU read failed with nothing painted: "couldn’t read", never an endless Acquiring. */
  needsYouFailed?: boolean;
  /** Open (scroll to) the tile a number counts. */
  onJump: (tileId: string) => void;
}) {
  const live = sessions.status === "ok" ? sessions.data.live : null;
  const liveOk = live && live.health === "ok" ? live : null;
  const plan = quota.status === "ok" ? lowestPlanLeft(quota.data.agents) : null;
  return (
    <div className={d.kpis} data-testid="dash-kpis">
      <Kpi
        label="Active agents"
        testid="kpi-agents"
        tone={liveOk ? (liveOk.working > 0 ? "up" : "idle") : sessions.status === "loading" ? "idle" : "down"}
        onClick={() => onJump("dash-running")}
        value={
          liveOk ? (
            <>
              <Readout value={liveOk.total} />{" "}
              <small>live · {liveOk.working} working</small>
            </>
          ) : sessions.status === "loading" ? (
            <Acquiring />
          ) : (
            <Unread />
          )
        }
        meter={
          liveOk && liveOk.total > 0 ? (
            <span className={`${d.kpiMeter} ${d.kpiMeterUp}`}>
              <Gauge
                value={liveOk.working / liveOk.total}
                low={-1}
                label={`${liveOk.working} of ${liveOk.total} live agents working`}
              />
            </span>
          ) : null
        }
        sub={
          liveOk && Object.keys(liveOk.by_engine).length ? (
            <span className={d.per}>
              {Object.entries(liveOk.by_engine).map(([e, c]) => (
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
        tone={
          missions.status === "ok"
            ? missions.data.reviewTotal > 0
              ? "att"
              : missions.data.activeTotal > 0
                ? "up"
                : "idle"
            : missions.status === "loading"
              ? "idle"
              : "down"
        }
        onClick={() => onJump("dash-missions")}
        value={
          missions.status === "ok" ? (
            <>
              <Readout value={missions.data.activeTotal + missions.data.reviewTotal} />{" "}
              <small>
                {missions.data.activeTotal} active · {missions.data.reviewTotal} in review
              </small>
            </>
          ) : missions.status === "loading" ? (
            <Acquiring />
          ) : (
            <Unread />
          )
        }
      />
      <Kpi
        label="Needs you"
        testid="kpi-needs"
        tone={needsYou === null ? (needsYouFailed ? "down" : "idle") : needsYou > 0 ? "att" : "idle"}
        onClick={() => onJump("needs-you-h")}
        value={
          needsYou === null ? (
            needsYouFailed ? <Unread /> : <Acquiring />
          ) : (
            <>
              <Readout value={needsYou} />{" "}
              <small>{needsYou === 1 ? "session" : "sessions"}</small>
            </>
          )
        }
      />
      <Kpi
        label="Plan quota — lowest left"
        testid="kpi-quota"
        tone={
          quota.status === "error"
            ? "down"
            : plan
              ? plan.left <= 0
                ? "down"
                : plan.left < 25 || plan.stale
                  ? "att"
                  : "up"
              : "idle"
        }
        onClick={() => onJump("dash-quota")}
        value={
          quota.status === "error" ? (
            <Unread />
          ) : quota.status === "loading" ? (
            <Acquiring />
          ) : plan ? (
            <>
              <Readout value={`${Math.round(plan.left)}%`} />{" "}
              <small>
                {plan.engine} · {plan.label}
              </small>
            </>
          ) : (
            <small>no agent reports a plan quota</small>
          )
        }
        meter={
          plan ? (
            <span className={d.kpiMeter}>
              <Gauge value={plan.left / 100} low={0.25} label={`${plan.engine} plan quota left`} />
            </span>
          ) : null
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

/** A first read in flight: the radar beside its words (a button holds phrasing content only, so
 *  this is `Loader kind="radar"` drawn in spans). */
function Acquiring() {
  return (
    <span className={`nj-loader is-radar ${d.kpiAcquire}`} role="status">
      <span className="nj-radar" aria-hidden="true" />
      <span className="nj-loader-label">Acquiring</span>
    </span>
  );
}

/** A failed read: a dash, never a zero, and the words. */
function Unread() {
  return (
    <>
      <Readout value="—" className={d.kpiNone} />{" "}
      <small>couldn’t read</small>
    </>
  );
}

function Kpi({
  label,
  value,
  sub,
  meter,
  tone,
  onClick,
  testid,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  meter?: ReactNode;
  /** The LED beside the label; the value beside it says the state in words. */
  tone: LedTone;
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
        <Led tone={tone} />
        {label}
      </span>
      <span className={d.kpiValue}>{value}</span>
      {meter}
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
      <Led tone={working ? "up" : "idle"} className={d.dot} />
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
                <Led
                  tone={x.state === "review" || x.needs_you ? "att" : "up"}
                  className={d.dot}
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
            quota shows here once a limit is set on its page in Settings → Agents.
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
  const stale = stalenessNote(row);
  const fc = forecastLine(row);
  return (
    <div className={d.q} data-testid="quota-row" data-engine={row.engine}>
      <div className={d.qTop}>
        <span>{row.engine}</span>
        <span>{headline}</span>
      </div>
      {fill !== null ? (
        <Gauge
          value={Math.max(0, Math.min(100, fill)) / 100}
          low={0.25}
          label={`${row.engine} quota left`}
        />
      ) : (
        <div className={d.barNone} aria-hidden="true" />
      )}
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
