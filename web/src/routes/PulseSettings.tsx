import { useEffect, useRef, useState } from "react";
import { useConfig, useConfigRefresh } from "../app/config";
import { api, ApiError } from "../lib/api";
import {
  isRecentWindowDays,
  RECENT_WINDOW_DEFAULT,
  RECENT_WINDOW_MAX,
  RECENT_WINDOW_MIN,
} from "../lib/recentWindow";
import type { PulseConfig } from "../types/api";
import toast from "../components/ui/toast.module.css";
import styles from "./Settings.module.css";

const FALLBACK: PulseConfig = {
  window_days: RECENT_WINDOW_DEFAULT,
  configured: false,
};

/** The recent window (`pulse.window_days`, #1086): how far back the Needs you list, the work recap
 *  and HQ look. It is all that is left of the "Session scan" card (#2179): the background scan,
 *  its interval, its depth and Scan now were retired with the scan, so the card shrank in place.
 *  The `pulse-h` id stays — deep links and #2180's redirect map land on it.
 *
 *  The field saves on blur / Enter (no explicit Save button); a successful save flashes a "Saved."
 *  note and refreshes the shared config context — without the refresh, ConfigCtx keeps the value
 *  from app load, and remounting the panel (switching Settings tabs and back) shows the pre-save
 *  value as if the save had been lost. */
export function PulseSettings() {
  const cfgBlock = useConfig()?.pulse;
  const refreshConfig = useConfigRefresh();
  const [block, setBlock] = useState<PulseConfig>(cfgBlock ?? FALLBACK);
  const [synced, setSynced] = useState(cfgBlock);
  if (cfgBlock !== synced) {
    setSynced(cfgBlock);
    if (cfgBlock) setBlock(cfgBlock);
  }

  const [windowDraft, setWindowDraft] = useState(String(block.window_days));
  const [seeded, setSeeded] = useState(block);
  if (seeded !== block) {
    setSeeded(block);
    setWindowDraft(String(block.window_days));
  }

  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const savedTimer = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined,
  );

  // THE TOAST TIMER MUST NOT OUTLIVE THE COMPONENT (#922), and neither may the continuation after
  // the awaited save (#922 review 1): leaving the panel while a save is in flight would otherwise
  // install a fresh timeout on a component that is gone — an unhandled error in `web-ci` after
  // every test has passed. `alive` is re-armed in the effect BODY, so StrictMode's deliberate
  // mount → unmount → mount does not leave a live panel marked dead.
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      clearTimeout(savedTimer.current);
    };
  }, []);

  const save = async (partial: Record<string, unknown>) => {
    setError(null);
    try {
      const r = (await api.setPrefs({ pulse: partial })) as {
        pulse?: PulseConfig;
      };
      if (alive.current && r.pulse) setBlock(r.pulse);
      // THE SHARED REFRESH STILL RUNS, deliberately: the save succeeded on the server, and
      // `ConfigCtx` belongs to a provider above this panel that is still mounted (#667).
      refreshConfig();
      if (!alive.current) return;
      clearTimeout(savedTimer.current);
      setSaved(true);
      savedTimer.current = setTimeout(() => setSaved(false), 1500);
    } catch (e) {
      if (!alive.current) return;
      setError(
        e instanceof ApiError && e.status === 422
          ? "That value was rejected — check the window."
          : "Couldn’t save — please try again.",
      );
    }
  };

  // Bounds mirror the server (1–3 days, #1086); out of range reverts to the saved value, with a
  // message saying why instead of a silent snap-back.
  const commitWindow = () => {
    const n = Number(windowDraft);
    if (!isRecentWindowDays(n)) {
      setWindowDraft(String(block.window_days));
      setError(
        `The recent window must be a whole number between ${RECENT_WINDOW_MIN} and ${RECENT_WINDOW_MAX} days.`,
      );
      return;
    }
    setError(null);
    if (n !== block.window_days) void save({ window_days: n });
  };

  return (
    <section
      className={styles.section}
      aria-labelledby="pulse-h"
      data-testid="recent-window"
    >
      <h2 id="pulse-h">Recent window</h2>
      {error && <p className={styles.err}>{error}</p>}
      {saved && (
        <p className={`${toast.toast} ${toast.chip} ${toast.led} ${toast.ok}`} role="status">
          Saved.
        </p>
      )}
      <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor="pulse-window">
          How far back to look
        </label>
        <div className={styles.aiIntervalRow}>
          <input
            id="pulse-window"
            className={`${styles.aiInput} ${styles.aiIntervalInput}`}
            type="number"
            min={RECENT_WINDOW_MIN}
            max={RECENT_WINDOW_MAX}
            value={windowDraft}
            onChange={(e) => setWindowDraft(e.target.value)}
            onBlur={commitWindow}
            onKeyDown={(e) => {
              if (e.key === "Enter") e.currentTarget.blur();
            }}
          />
          <span>days</span>
        </div>
        <p className={styles.hint}>
          The Needs you list, the work recap and HQ look this many days back
          ({RECENT_WINDOW_MIN}–{RECENT_WINDOW_MAX}). Changes save automatically.
        </p>
      </div>
    </section>
  );
}
