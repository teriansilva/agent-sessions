import { useState } from "react";
import { Link } from "react-router-dom";

import { AskConsole } from "../components/ask/AskConsole";
import s from "../components/ask/AskHome.module.css";
import { NeedsYou } from "../components/ask/NeedsYou";
import { NeedsYouDetailsDialog } from "../components/ask/NeedsYouDetailsDialog";
import { needsYouIds } from "../components/ask/needsYouLabels";
import { RecentWork } from "../components/ask/RecentWork";
import { useNeedsYou } from "../components/ask/useNeedsYou";
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
  const { state, facets, membership, refresh } = useNeedsYou(windowDays, engine, project);
  const [details, setDetails] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<null | "needs" | "recent">(null);

  const recent = <RecentWork windowDays={windowDays} onWindowDays={changeWindow} />;
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
                onClick={() => setExpanded((e) => (e === "needs" ? null : "needs"))}
              >
                {count ? <span className={s.dot} aria-hidden="true" /> : null}
                Needs you{count !== null ? ` · ${count}` : ""} ▾
              </button>
              <button
                type="button"
                className={s.btn}
                aria-label="Recent work"
                aria-expanded={expanded === "recent"}
                onClick={() => setExpanded((e) => (e === "recent" ? null : "recent"))}
              >
                <span className={s.long}>Recent work</span>
                <span className={s.short}>Recent</span> ▾
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
              <div className={s.panel}>{expanded === "needs" ? needs : recent}</div>
            ) : null}
          </>
        )}
        intro={
          <>
            <div className={styles.kicker}>Ask // your work</div>
            <h1 className={styles.h1}>Ask about your work</h1>
            <p className={styles.sub}>
              Find a session or a mission by what happened in it, or ask what
              you did and when. Answers are read from the transcripts and
              missions this install can already see.
            </p>
            {!configured ? (
              <div className={styles.needsEndpoint} data-testid="ask-needs-endpoint">
                Ask needs an AI endpoint — it has no local fallback. Set one up in{" "}
                <Link to={settingsPath("ai-endpoint")}>
                  Settings → Endpoint &amp; model
                </Link>
                , then come back.
              </div>
            ) : null}
            {recent}
            {needs}
          </>
        }
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
