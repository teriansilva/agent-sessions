import { Activity } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useConfig, useConfigRefresh } from "../app/config";
import { api, ApiError } from "../lib/api";
import type { PulseConfig, PulseDepth } from "../types/api";
import styles from "./Settings.module.css";

const FALLBACK: PulseConfig = {
  auto_enabled: false,
  interval_minutes: 30,
  window_days: 3,
  scan_depth: "fast",
  configured: false,
};

// `medium` is gone (#956): all it added was a banner that nothing rendered.
const DEPTH_LABELS: Record<PulseDepth, string> = {
  fast: "Fast — curation only, no AI calls",
  slow: "Slow — adds a one-line state per session",
};

/** Pulse settings (#441 Phase 6). Mirrors AutoSortSettings: opt-in background scan + the
 *  window/depth the manual ("Scan now") and background scans use. Synthesis (depth ≥ medium)
 *  reuses the AI review endpoint above; fast is always free.
 *
 *  Every control saves on change/blur (no explicit Save button); a successful save flashes a
 *  "Saved." note and refreshes the shared config context — without the refresh, ConfigCtx keeps
 *  the values from app load, and remounting the panel (switching Settings tabs and back) shows
 *  the pre-save values as if the save had been lost. */
export function PulseSettings() {
  const cfgBlock = useConfig()?.pulse;
  const refreshConfig = useConfigRefresh();
  const [block, setBlock] = useState<PulseConfig>(cfgBlock ?? FALLBACK);
  const [synced, setSynced] = useState(cfgBlock);
  if (cfgBlock !== synced) {
    setSynced(cfgBlock);
    if (cfgBlock) setBlock(cfgBlock);
  }

  const [intervalDraft, setIntervalDraft] = useState(
    String(block.interval_minutes),
  );
  const [windowDraft, setWindowDraft] = useState(String(block.window_days));
  const [seeded, setSeeded] = useState(block);
  if (seeded !== block) {
    setSeeded(block);
    setIntervalDraft(String(block.interval_minutes));
    setWindowDraft(String(block.window_days));
  }

  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const savedTimer = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined,
  );
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  // THE TOAST TIMER MUST NOT OUTLIVE THE COMPONENT (#922).
  //
  // `save` already clears this ref before starting a new one, which covers a SECOND save
  // restarting the window — and that is exactly why the gap was easy to miss: a ref plus a
  // `clearTimeout` at the call site reads as complete timer hygiene. It only covers re-entry.
  // Unmount is the other exit, the one a test harness takes on every single case and a human
  // almost never does, and nothing here handled it.
  //
  // Left alone, the pending `setSaved(false)` fires against a torn-down environment. In `web-ci`
  // that is an UNHANDLED error, so the job fails after every test has passed — 1468 passed,
  // 1 error — and it is load-sensitive, so the identical tree passes on a quieter run. That
  // makes it a tax on unrelated PRs rather than an honest red.
  // …AND THE CONTINUATION AFTER THE AWAIT MUST KNOW IT LOST ITS COMPONENT (#922 review 1).
  //
  // Clearing on unmount only cancels a timer that ALREADY EXISTS, and `save()` awaits
  // `setPrefs` *before* creating one. Leave the panel while that request is in flight and the
  // cleanup runs against an empty ref; the response then resolves and installs a fresh 1500 ms
  // timeout on a component that is gone. The leak survived its own fix.
  //
  // `alive` is re-armed in the effect BODY, not just released in the teardown, so StrictMode's
  // deliberate mount → unmount → mount does not leave a live panel marked dead.
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
      // THE SHARED REFRESH STILL RUNS, deliberately. The save SUCCEEDED on the server, and
      // `ConfigCtx` belongs to a provider ABOVE this panel that is still mounted — skipping it
      // because this panel went away would reintroduce #667 (a remount showing the pre-save
      // value) for exactly the operator who navigated off. Only component-local work is gated.
      refreshConfig();
      if (!alive.current) return;
      clearTimeout(savedTimer.current);
      setSaved(true);
      savedTimer.current = setTimeout(() => setSaved(false), 1500);
    } catch (e) {
      if (!alive.current) return;
      setError(
        e instanceof ApiError && e.status === 422
          ? "That value was rejected — check the interval / window."
          : "Couldn’t save — please try again.",
      );
    }
  };

  // Bounds mirror the server (interval 5–1440 min, window 1–30 days); out-of-range reverts
  // to the saved value, with a message saying why instead of a silent snap-back.
  const commitInterval = () => {
    const n = Number(intervalDraft);
    if (!Number.isInteger(n) || n < 5 || n > 1440) {
      setIntervalDraft(String(block.interval_minutes));
      setError(
        "The interval must be a whole number between 5 and 1440 minutes.",
      );
      return;
    }
    setError(null);
    if (n !== block.interval_minutes) void save({ interval_minutes: n });
  };
  const commitWindow = () => {
    const n = Number(windowDraft);
    if (!Number.isInteger(n) || n < 1 || n > 30) {
      setWindowDraft(String(block.window_days));
      setError(
        "The recent window must be a whole number between 1 and 30 days.",
      );
      return;
    }
    setError(null);
    if (n !== block.window_days) void save({ window_days: n });
  };

  // Enter commits a number field the same way leaving it does.
  const blurOnEnter = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") e.currentTarget.blur();
  };

  const scanNow = async () => {
    if (running) return;
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const art = await api.pulseScan({ depth: block.scan_depth });
      const n = art.cards.length;
      const base =
        n === 0
          ? "No recent work to surface."
          : `Curated ${n} session${n === 1 ? "" : "s"}.`;
      setResult(
        art.synthesis_skipped
          ? `${base} Synthesis skipped (endpoint not configured).`
          : base,
      );
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 409
          ? "A scan is already running."
          : "Couldn’t scan — please try again.",
      );
    } finally {
      setRunning(false);
    }
  };

  return (
    <section className={styles.section} aria-labelledby="pulse-h">
      <h2 id="pulse-h">Session scan</h2>
      <p className={styles.hint}>
        Keeps the <strong>Sessions without a mission</strong> list on mission
        control current. A scan curates your recent sessions, on demand or on a
        background loop, and caches the result so the page loads instantly.{" "}
        <strong>Fast</strong> depth is local and free; <strong>Slow</strong> adds
        a one-line state per session using the AI endpoint. Changes save
        automatically.
      </p>
      {error && <p className={styles.err}>{error}</p>}
      {saved && (
        <p className={styles.ok} role="status">
          Saved.
        </p>
      )}

      <label className={styles.aiToggle}>
        <input
          type="checkbox"
          checked={block.auto_enabled}
          onChange={(e) => void save({ auto_enabled: e.currentTarget.checked })}
        />
        <span>Scan automatically in the background</span>
      </label>
      {!block.auto_enabled && (
        <p className={styles.hint}>
          With this off, the list only changes when you press Scan now.
        </p>
      )}

      <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor="pulse-interval">
          Scan every
        </label>
        <div className={styles.aiIntervalRow}>
          <input
            id="pulse-interval"
            className={`${styles.aiInput} ${styles.aiIntervalInput}`}
            type="number"
            min={5}
            max={1440}
            value={intervalDraft}
            onChange={(e) => setIntervalDraft(e.target.value)}
            onBlur={commitInterval}
            onKeyDown={blurOnEnter}
          />
          <span>minutes</span>
        </div>
        <p className={styles.hint}>
          How often the background loop refreshes (5–1440). A scan is skipped
          when nothing changed.
        </p>
      </div>

      <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor="pulse-window">
          Recent window
        </label>
        <div className={styles.aiIntervalRow}>
          <input
            id="pulse-window"
            className={`${styles.aiInput} ${styles.aiIntervalInput}`}
            type="number"
            min={1}
            max={30}
            value={windowDraft}
            onChange={(e) => setWindowDraft(e.target.value)}
            onBlur={commitWindow}
            onKeyDown={blurOnEnter}
          />
          <span>days</span>
        </div>
      </div>

      <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor="pulse-depth">
          Scan depth
        </label>
        <select
          id="pulse-depth"
          className={styles.aiInput}
          value={block.scan_depth}
          onChange={(e) => void save({ scan_depth: e.currentTarget.value })}
        >
          {(Object.keys(DEPTH_LABELS) as PulseDepth[]).map((d) => (
            <option key={d} value={d}>
              {DEPTH_LABELS[d]}
            </option>
          ))}
        </select>
        {!block.configured && block.scan_depth !== "fast" && (
          <p className={styles.hint}>
            The AI endpoint isn’t configured — Slow scans degrade to Fast
            curation until it’s set.
          </p>
        )}
      </div>

      <div className={styles.aiActions}>
        <button
          type="button"
          className={`${styles.secBtn} shine`}
          disabled={running}
          onClick={() => void scanNow()}
        >
          <Activity size={14} /> {running ? "Scanning…" : "Scan now"}
        </button>
        {result && <p className={styles.ok}>{result}</p>}
      </div>
    </section>
  );
}
