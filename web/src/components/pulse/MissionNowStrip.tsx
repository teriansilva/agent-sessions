/** "What is going on right now" above the mission thread (#1064 Phase 2).
 *
 *  One line per held session, polled from `GET /api/missions/{id}/now` every `NOW_POLL_MS` while
 *  the tab is visible. Neutral on purpose: only `producing` carries the live colour. "At a prompt"
 *  is information here, not an alert — the bell and the decision cards own asking the operator for
 *  something, and a second amber surface would compete with them.
 *
 *  Fenced per mission: every reading carries the mission it was requested for and renders only on
 *  that mission, and the effect's `live` flag drops a response that lands after a switch — mission
 *  A's sessions never paint on B. */
import { useEffect, useState } from "react";

import { api } from "../../lib/api";
import type { MissionNow } from "../../types/api";

import styles from "./mission.module.css";
import {
  NOW_POLL_MS,
  nowPhrase,
  recapPhrase,
  sessionLabel,
} from "./missionNow";
import { useEngineRoster } from "../../app/engineRoster";

const DOT: Record<string, string> = {
  producing: styles.nowProducing,
  at_prompt: styles.nowAtPrompt,
  quiet: styles.nowQuiet,
  unobserved: styles.nowUnobserved,
};

interface Reading {
  /** The mission this reading was requested for. A reading for any other mission is not shown. */
  id: string;
  now: MissionNow | null;
  failed: boolean;
}

export function MissionNowStrip({ missionId }: { missionId: string }) {
  // `sessionLabel` strips each engine's id prefix from the roster (#853 P4): re-render when it lands.
  useEngineRoster();
  const [reading, setReading] = useState<Reading | null>(null);
  const mine = reading?.id === missionId ? reading : null;
  const now = mine?.now ?? null;
  const failed = mine?.failed ?? false;

  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tick = async () => {
      timer = null;
      try {
        const r = await api.missionNow(missionId);
        if (!live) return;
        // A body without a sessions array is not a reading; never render it as "no session".
        if (!Array.isArray(r?.sessions)) throw new Error("malformed");
        setReading({ id: missionId, now: r, failed: false });
      } catch {
        // A failed poll keeps the last reading rather than blanking the strip.
        if (live)
          setReading((prev) => ({
            id: missionId,
            now: prev?.id === missionId ? prev.now : null,
            failed: true,
          }));
      }
      if (live && !document.hidden) timer = setTimeout(tick, NOW_POLL_MS);
    };
    const onVis = () => {
      if (document.hidden) {
        if (timer) clearTimeout(timer);
        timer = null;
      } else if (!timer) {
        void tick();
      }
    };
    void tick();
    document.addEventListener("visibilitychange", onVis);
    return () => {
      live = false;
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [missionId]);

  if (!now) {
    return failed ? (
      <div className={styles.nowStrip} data-testid="mission-now" role="status">
        <span className={styles.nowHead}>Now</span>
        <span className={styles.nowMuted}>Live status unavailable.</span>
      </div>
    ) : null;
  }
  return (
    <div
      className={styles.nowStrip}
      data-testid="mission-now"
      role="status"
      aria-live="off"
    >
      <span className={styles.nowHead}>Now</span>
      {now.sessions.length === 0 ? (
        <span className={styles.nowMuted}>
          No session is held by this mission.
        </span>
      ) : (
        <ul className={styles.nowList}>
          {now.sessions.map((s) => {
            const recap = recapPhrase(s);
            return (
              <li
                key={s.session_key}
                className={styles.nowRow}
                data-testid="mission-now-row"
                data-status={s.status}
              >
                <span
                  className={`${styles.nowDot} ${DOT[s.status] ?? styles.nowUnobserved}`}
                  aria-hidden
                />
                <span className={styles.nowWho}>
                  {sessionLabel(s.session_key)}
                </span>
                <span className={styles.nowWhat}>{nowPhrase(s)}</span>
                {recap ? (
                  <span
                    className={`${styles.nowMuted} ${s.recap_older_than_output ? styles.nowStale : ""}`}
                  >
                    {recap}
                  </span>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
      {failed ? (
        <span className={styles.nowMuted}>(last reading — refresh failed)</span>
      ) : null}
    </div>
  );
}
