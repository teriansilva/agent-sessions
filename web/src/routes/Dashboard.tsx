import { useState } from "react";
import {
  ChevronDown,
  Crosshair,
  FolderPlus,
  MessageSquare,
  Plus,
  TerminalSquare,
} from "lucide-react";
import { Link } from "react-router-dom";

import a from "../components/ask/AskConsole.module.css";
import { useAskPanel } from "../components/ask/askPanel";
import { NeedsYou } from "../components/ask/NeedsYou";
import { NeedsYouDetailsDialog } from "../components/ask/NeedsYouDetailsDialog";
import { RecentWork } from "../components/ask/RecentWork";
import { useNeedsYou } from "../components/ask/useNeedsYou";
import {
  KpiStrip,
  MissionsTile,
  QuotaTile,
  RecentSessionsTile,
  RunningTile,
} from "../components/dashboard/Tiles";
import d from "../components/dashboard/Dashboard.module.css";
import {
  MISSIONS_POLL_MS,
  QUOTA_POLL_MS,
  SESSIONS_POLL_MS,
  readMissions,
  readQuota,
  readSessions,
} from "../components/dashboard/sources";
import { RefreshBar } from "../components/dashboard/RefreshBar";
import { usePolled } from "../components/dashboard/usePolled";
import { useConfig, useConfigRefresh } from "../app/config";
import m from "../components/pulse/mission.module.css";
import { AnchoredMenu } from "../components/ui/AnchoredMenu";
import { api } from "../lib/api";
import type { WizardEntryState } from "../lib/newProject";
import { MISSION_PATH, NEW_PROJECT_PATH, SESSIONS_PATH } from "../lib/routes";
import { coerceRecentWindowDays } from "../lib/recentWindow";
import styles from "./Ask.module.css";

/** The BattleLab dashboard (#1123): RECENT WORK and NEEDS YOU (#1086), active agents, missions,
 *  quota.
 *
 *  **Ask is not on this page (#1294).** It is the right-hand sidebar the corner icon opens on every
 *  route; the head row's Ask button opens the same sidebar, and that is all the dashboard holds of
 *  it. The docked Ask field (#1171) is gone, and so is the "needs an AI endpoint" notice, which the
 *  sidebar says itself. Beside Ask, **New** is the one way to start work from here: a session, a
 *  mission or a project (#1187's wizard, which comes back here when it is done). */
export default function Dashboard() {
  const cfg = useConfig();
  const refreshConfig = useConfigRefresh();
  const { openAsk } = useAskPanel();

  // ONE window for both sections: the stored preference, changed from either picker.
  const stored = coerceRecentWindowDays(cfg?.pulse?.window_days);
  const [windowDays, setWindowDays] = useState(stored);
  const [synced, setSynced] = useState(stored);
  if (stored !== synced) {
    setSynced(stored);
    setWindowDays(stored);
  }
  const changeWindow = (d: number) => {
    setWindowDays(d);
    void api
      .setPrefs({ pulse: { window_days: d } })
      .then(() => refreshConfig())
      .catch(() => setWindowDays(stored)); // a refused save must not look saved
  };

  const [engine, setEngine] = useState("");
  const [project, setProject] = useState("");
  const {
    state,
    facets,
    membership,
    refresh,
    refreshing: needsRefreshing,
  } = useNeedsYou(windowDays, engine, project);
  const [details, setDetails] = useState<string | null>(null);

  // The dashboard's own reads (#1123): each tile loads, fails and retries on its own.
  const [sessions, retrySessions, sessionsRefreshing] = usePolled(
    "sessions",
    readSessions,
    SESSIONS_POLL_MS,
  );
  const [missionsRes, retryMissions, missionsRefreshing] = usePolled(
    "missions",
    readMissions,
    MISSIONS_POLL_MS,
  );
  const [quota, retryQuota, quotaRefreshing] = usePolled(
    "quota",
    readQuota,
    QUOTA_POLL_MS,
  );
  const [recentRefreshing, setRecentRefreshing] = useState(false);
  // #1223: the bar runs while any PAINTED source re-reads — a cold source shows its own skeleton,
  // and is not "refreshing" anything. It stays until the last of them settles.
  const revalidating =
    (sessionsRefreshing && sessions.status === "ok") ||
    (missionsRefreshing && missionsRes.status === "ok") ||
    (quotaRefreshing && quota.status === "ok") ||
    // NEEDS YOU keeps its list through a failed read (`error` WITH data), so its gate is "is a
    // list painted", not "was the last read ok" — a Retry over it is a refresh (review 5407).
    (needsRefreshing && state.data !== null) ||
    recentRefreshing;
  const running = (
    <RunningTile res={sessions} retry={() => void retrySessions()} />
  );
  const missionsTile = (
    <MissionsTile res={missionsRes} retry={() => void retryMissions()} />
  );
  const quotaTile = <QuotaTile res={quota} retry={retryQuota} />;
  const jump = (id: string) => {
    const el = document.getElementById(id);
    el?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const recent = (
    <RecentWork
      windowDays={windowDays}
      onWindowDays={changeWindow}
      onRevalidating={setRecentRefreshing}
    />
  );
  const needs = (
    <NeedsYou
      state={state}
      facets={facets}
      engine={engine}
      project={project}
      onEngine={setEngine}
      onProject={setProject}
      onDetails={setDetails}
      onChanged={() => void refresh()}
    />
  );
  // The UNFILTERED count: the KPI strip summarises everything that needs you, never the subset a
  // list filter happens to show (review 5184).
  const count = membership
    ? membership.total
    : state.status === "ok"
      ? (state.data.total_unfiltered ?? state.data.total ?? null)
      : null;

  return (
    <div className={styles.page} data-testid="dashboard-page">
      <div className={`${m.threadCol} ${a.col}`}>
        <RefreshBar active={revalidating} />
        <div className={m.pane} data-testid="dashboard-pane">
          <div className={a.dashboard}>
            <div className={styles.kicker}>Dashboard // what is going on</div>
            <div className={d.headRow}>
              <h1 className={styles.h1}>BattleLab dashboard</h1>
              <div className={d.headActions}>
                <button
                  type="button"
                  className={d.headBtn}
                  onClick={openAsk}
                  data-testid="dashboard-ask"
                >
                  <MessageSquare size={14} aria-hidden="true" /> Ask
                </button>
                {/* New (#1294): a session, a mission or a project. The project entry keeps the
                    wizard's way back here (#1187). */}
                <AnchoredMenu
                  label="New"
                  trigger={
                    <>
                      <Plus size={14} aria-hidden="true" />
                      <span aria-hidden="true">New</span>
                      <ChevronDown size={14} aria-hidden="true" />
                    </>
                  }
                  triggerClassName={d.headBtn}
                  triggerTestId="dashboard-new"
                  menuTestId="dashboard-new-menu"
                  focus="first-item"
                  portal
                  classes={{
                    wrap: d.menuWrap,
                    panel: d.menuPanel,
                    items: d.menuItems,
                  }}
                >
                  {(close) => (
                    <>
                      <Link
                        role="menuitem"
                        className={d.menuItem}
                        to={SESSIONS_PATH}
                        onClick={close}
                        data-testid="dashboard-new-session"
                      >
                        <TerminalSquare size={15} aria-hidden="true" /> New session
                      </Link>
                      <Link
                        role="menuitem"
                        className={d.menuItem}
                        to={MISSION_PATH}
                        onClick={close}
                        data-testid="dashboard-new-mission"
                      >
                        <Crosshair size={15} aria-hidden="true" /> New mission
                      </Link>
                      <Link
                        role="menuitem"
                        className={d.menuItem}
                        to={NEW_PROJECT_PATH}
                        state={{ from: "dashboard" } satisfies WizardEntryState}
                        onClick={close}
                        data-testid="dashboard-new-project"
                      >
                        <FolderPlus size={15} aria-hidden="true" /> New project
                      </Link>
                    </>
                  )}
                </AnchoredMenu>
              </div>
            </div>
            <p className={styles.sub}>
              What is running, what needs you and what you did.
            </p>
            <KpiStrip
              sessions={sessions}
              missions={missionsRes}
              quota={quota}
              needsYou={count}
              onJump={jump}
            />
            <div className={d.cols}>
              <div>
                {needs}
                {recent}
                <RecentSessionsTile
                  res={sessions}
                  retry={() => void retrySessions()}
                />
              </div>
              <div>
                {running}
                {missionsTile}
                {quotaTile}
              </div>
            </div>
          </div>
        </div>
      </div>
      {details ? (
        <NeedsYouDetailsDialog
          key={details}
          sessionId={details}
          // Close only THIS dialog: a completion that lands later must never close a newer one.
          onClose={() => setDetails((cur) => (cur === details ? null : cur))}
          onChanged={() => void refresh()}
        />
      ) : null}
    </div>
  );
}
