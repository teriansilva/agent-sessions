import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { AskComposer } from "../components/ask/AskComposer";
import a from "../components/ask/AskConsole.module.css";
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
import { api } from "../lib/api";
import { ASK_PATH } from "../lib/routes";
import { coerceRecentWindowDays } from "../lib/recentWindow";
import { settingsPath } from "./settingsTabs";
import styles from "./Ask.module.css";

/** The BattleLab dashboard (#1123): RECENT WORK and NEEDS YOU (#1086), active agents, missions,
 *  quota — and the Ask field, docked on the bottom edge.
 *
 *  **Asking here OPENS a conversation on Ask's own page (#1171).** The question is handed to
 *  `ASK_PATH` in router state and asked there once. Before #1171 the conversation took over this
 *  page with every tile pinned above it as a bar; the operator found it crowded the thread, so the
 *  conversation moved under Dashboard in the nav, the way the map sits under Sessions, and its
 *  only chrome is a way back here.
 *
 *  `configured` is the AI endpoint's own flag. Ask itself has no local fallback; NEEDS YOU and a
 *  locally-listed RECENT WORK work without one. */
export default function Dashboard() {
  const cfg = useConfig();
  const refreshConfig = useConfigRefresh();
  const configured = cfg?.pulse?.configured ?? false;

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
  const navigate = useNavigate();

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
            <h1 className={styles.h1}>BattleLab dashboard</h1>
            <p className={styles.sub}>
              What is running, what needs you and what you did — and below, ask
              about anything: answers are read from the transcripts and missions
              this install can already see.
            </p>
            {!configured ? (
              <div
                className={styles.needsEndpoint}
                data-testid="ask-needs-endpoint"
              >
                Ask needs an AI endpoint — it has no local fallback. Set one up
                in{" "}
                <Link to={settingsPath("ai-endpoint")}>
                  Settings → Endpoint &amp; model
                </Link>
                , then come back.
              </div>
            ) : null}
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
        {/* Asking opens the conversation on its own page (#1171). */}
        <div className={m.composerDock}>
          <div className={a.measure}>
            <AskComposer
              configured={configured}
              onAsk={(q) => navigate(ASK_PATH, { state: { ask: q } })}
            />
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
