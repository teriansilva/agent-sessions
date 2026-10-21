import { useState } from "react";
import {
  ChevronDown,
  Crosshair,
  FolderPlus,
  Plus,
  TerminalSquare,
} from "lucide-react";
import { Link } from "react-router-dom";

import a from "../components/ask/AskConsole.module.css";
import { NeedsYou } from "../components/ask/NeedsYou";
import { NeedsYouDetailsDialog } from "../components/ask/NeedsYouDetailsDialog";
import { RecentWork } from "../components/ask/RecentWork";
import { PrimaryContact } from "../components/hq/PrimaryContact";
import { useNeedsYou } from "../components/ask/useNeedsYou";
import {
  KpiStrip,
  MissionsTile,
  QuotaTile,
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

/** The BattleLab dashboard: overview cards, NEEDS YOU, running sessions, missions and quota.
 *  RECENT WORK is opt-in (#1467): opening it mounts and revalidates its retained snapshot; closing
 *  it retires its reads. Session history has one link instead of another list of the same sessions.
 *
 *  **The start page (#1525).** `/` lands here, greeting the operator: "Welcome, Commander." HQ (Ask
 *  until #1525) is the right-hand sidebar the corner icon opens on every route (#1294); here the
 *  primary HQ contact's card opens the same sidebar, and it is all the dashboard holds of it. Its
 *  own HQ button is gone. **New** is the page's primary action and the one way to start work from
 *  here: a session, a mission or a project (#1187's wizard, which comes back here when it is done). */
export default function Dashboard() {
  const cfg = useConfig();
  const refreshConfig = useConfigRefresh();

  // ONE window for Needs you and Recent work: the stored preference, changed in the recap.
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
  const [recentOpen, setRecentOpen] = useState(false);
  // #1223: the bar runs while any PAINTED source re-reads — a cold source shows its own skeleton,
  // and is not "refreshing" anything. It stays until the last of them settles.
  const revalidating =
    (sessionsRefreshing && sessions.status === "ok") ||
    (missionsRefreshing && missionsRes.status === "ok") ||
    (quotaRefreshing && quota.status === "ok") ||
    // NEEDS YOU keeps its list through a failed read (`error` WITH data), so its gate is "is a
    // list painted", not "was the last read ok" — a Retry over it is a refresh (review 5407).
    (needsRefreshing && state.data !== null) ||
    (recentOpen && recentRefreshing);
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
            {/* #1525: the headline row takes the KPI strip's columns, so the contact card in the
                last one is exactly as wide as the Plan quota tile below it. New sits directly left
                of it, top edges aligned. */}
            <div className={d.top} data-testid="dashboard-top">
            <div className={d.headBlock}>
            <div className={`${styles.kicker} ${d.kicker}`}>Dashboard // what is going on</div>
            <h1 className={`${styles.h1} ${d.welcome}`}>Welcome, Commander.</h1>
            <p className={`${styles.sub} ${d.sub}`}>
              What is running, what needs you and what you did.
            </p>
            </div>
              <div className={d.headActions}>
                {/* New (#1294): a session, a mission or a project. The project entry keeps the
                    wizard's way back here (#1187). Since #1525 it is the page's primary action. */}
                <AnchoredMenu
                  label="New"
                  trigger={
                    <>
                      <Plus size={14} aria-hidden="true" />
                      <span aria-hidden="true">New</span>
                      <ChevronDown size={14} aria-hidden="true" />
                    </>
                  }
                  triggerClassName={d.newPrimary}
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
              <PrimaryContact className={d.topCard} />
            </div>
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
                <button
                  type="button"
                  id="dashboard-recent-toggle"
                  className={d.recentToggle}
                  aria-expanded={recentOpen}
                  aria-controls="dashboard-details-content"
                  onClick={() => setRecentOpen((open) => !open)}
                  data-testid="dashboard-details-toggle"
                >
                  Recent work
                  <ChevronDown size={14} aria-hidden="true" />
                </button>
                <div
                  id="dashboard-details-content"
                  role="region"
                  aria-labelledby="dashboard-recent-toggle"
                  hidden={!recentOpen}
                >
                  {recentOpen ? recent : null}
                </div>
                <div className={d.history}>
                  <Link
                    className={d.go}
                    to={SESSIONS_PATH}
                    data-testid="dashboard-sessions-history"
                  >
                    All sessions →
                  </Link>
                </div>
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
