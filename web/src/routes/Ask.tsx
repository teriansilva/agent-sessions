import { useState } from "react";
import { Link } from "react-router-dom";

import { AskConsole } from "../components/ask/AskConsole";
import s from "../components/ask/AskHome.module.css";
import { NeedsYou } from "../components/ask/NeedsYou";
import { NeedsYouDetailsDialog } from "../components/ask/NeedsYouDetailsDialog";
import { needsYouIds } from "../components/ask/needsYouLabels";
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
import { usePolled } from "../components/dashboard/usePolled";
import { useConfig, useConfigRefresh } from "../app/config";
import { api } from "../lib/api";
import { coerceRecentWindowDays } from "../lib/recentWindow";
import { settingsPath } from "./settingsTabs";
import styles from "./Ask.module.css";

/** ASK — its own top-level route since #1058, and since #1086 the home for sessions without a
 *  mission: RECENT WORK (what you did, 1–3 days) and NEEDS YOU (only the sessions that need you,
 *  with the one decision each can settle) above the field.
 *
 *  Once a conversation starts, the thread takes the page as it always did (#1069) and both
 *  sections collapse into a pinned bar — NEEDS YOU never leaves the screen mid-conversation, and
 *  either section drops back down over the thread on a tap. Answer rows for a session that needs
 *  you carry the marker and ⓘ, which opens the same details the list does.
 *
 *  `configured` is the AI endpoint's own flag. Ask itself has no local fallback; NEEDS YOU and a
 *  locally-listed RECENT WORK work without one. */
export default function Ask() {
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
  const { state, facets, membership, refresh } = useNeedsYou(
    windowDays,
    engine,
    project,
  );
  const [details, setDetails] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<
    null | "needs" | "recent" | "agents" | "missions" | "quota" | "more"
  >(null);

  // The dashboard's own reads (#1123): each tile loads, fails and retries on its own.
  const [sessions, retrySessions] = usePolled(readSessions, SESSIONS_POLL_MS);
  const [missionsRes, retryMissions] = usePolled(
    readMissions,
    MISSIONS_POLL_MS,
  );
  const [quota, retryQuota] = usePolled(readQuota, QUOTA_POLL_MS);
  const running = (
    <RunningTile res={sessions} retry={() => void retrySessions()} />
  );
  const missionsTile = (
    <MissionsTile res={missionsRes} retry={() => void retryMissions()} />
  );
  const quotaTile = <QuotaTile res={quota} retry={retryQuota} />;
  const liveNow =
    sessions.status === "ok" && sessions.data.live.health === "ok"
      ? sessions.data.live
      : null;
  const jump = (id: string) => {
    const el = document.getElementById(id);
    el?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const recent = (
    <RecentWork windowDays={windowDays} onWindowDays={changeWindow} />
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
  // The UNFILTERED count: the pinned bar summarises everything that needs you, never the subset a
  // list filter happens to show (review 5184).
  const count = membership
    ? membership.total
    : state.status === "ok"
      ? (state.data.total_unfiltered ?? state.data.total ?? null)
      : null;

  return (
    <div className={styles.page} data-testid="ask-page">
      <AskConsole
        configured={configured}
        needsYou={membership?.ids ?? needsYouIds(state.data?.rows)}
        onDetails={setDetails}
        pinned={(reset) => (
          <>
            <div className={s.bar} data-testid="ask-bar">
              <button
                type="button"
                className={`${s.btn} ${count ? s.hot : ""}`}
                aria-expanded={expanded === "needs"}
                onClick={() =>
                  setExpanded((e) => (e === "needs" ? null : "needs"))
                }
              >
                {count ? <span className={s.dot} aria-hidden="true" /> : null}
                Needs you{count !== null ? ` · ${count}` : ""} ▾
              </button>
              <button
                type="button"
                className={s.btn}
                aria-label="Active agents"
                aria-expanded={expanded === "agents"}
                onClick={() =>
                  setExpanded((e) => (e === "agents" ? null : "agents"))
                }
                data-testid="bar-agents"
              >
                <span className={s.long}>
                  Agents{" "}
                  {liveNow
                    ? `${liveNow.total} live · ${liveNow.working} working`
                    : ""}
                </span>
                <span className={s.short}>
                  Live {liveNow ? liveNow.total : ""}
                </span>{" "}
                ▾
              </button>
              <button
                type="button"
                className={`${s.btn} ${s.long}`}
                aria-label="Missions"
                aria-expanded={expanded === "missions"}
                onClick={() =>
                  setExpanded((e) => (e === "missions" ? null : "missions"))
                }
                data-testid="bar-missions"
              >
                Missions ▾
              </button>
              <button
                type="button"
                className={`${s.btn} ${s.long}`}
                aria-label="Quota"
                aria-expanded={expanded === "quota"}
                onClick={() =>
                  setExpanded((e) => (e === "quota" ? null : "quota"))
                }
                data-testid="bar-quota"
              >
                Quota ▾
              </button>
              <button
                type="button"
                className={`${s.btn} ${s.long}`}
                aria-label="Recent work"
                aria-expanded={expanded === "recent"}
                onClick={() =>
                  setExpanded((e) => (e === "recent" ? null : "recent"))
                }
              >
                Recent work ▾
              </button>
              {/* A phone keeps ONE row (#1086): Missions, Quota and Recent work sit behind More, each
                  still one tap from its tile over the thread (Hermes 5240, finding 1). */}
              <button
                type="button"
                className={`${s.btn} ${s.mobileOnly}`}
                aria-expanded={expanded === "more"}
                onClick={() =>
                  setExpanded((e) => (e === "more" ? null : "more"))
                }
                data-testid="bar-more"
              >
                More ▾
              </button>
              <span className={s.sp} />
              <button
                type="button"
                className={s.btn}
                aria-label="New conversation"
                onClick={() => {
                  setExpanded(null);
                  reset();
                }}
              >
                <span className={s.long}>New conversation</span>
                <span className={s.short}>New</span>
              </button>
            </div>
            {expanded ? (
              <div className={s.panel}>
                {expanded === "more" ? (
                  <div
                    className={s.moreMenu}
                    role="menu"
                    data-testid="bar-more-menu"
                  >
                    {(
                      [
                        ["missions", "Missions"],
                        ["quota", "Quota"],
                        ["recent", "Recent work"],
                      ] as const
                    ).map(([key, label]) => (
                      <button
                        key={key}
                        type="button"
                        role="menuitem"
                        className={s.btn}
                        onClick={() => setExpanded(key)}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                ) : expanded === "needs" ? (
                  needs
                ) : expanded === "agents" ? (
                  running
                ) : expanded === "missions" ? (
                  missionsTile
                ) : expanded === "quota" ? (
                  quotaTile
                ) : (
                  recent
                )}
              </div>
            ) : null}
          </>
        )}
        intro={
          <>
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
          </>
        }
        wide
      />
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
