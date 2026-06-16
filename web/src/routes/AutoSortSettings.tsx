import { Sparkles } from "lucide-react";
import { useState } from "react";
import { useConfig } from "../app/config";
import { api, ApiError } from "../lib/api";
import type { AutoSortConfig } from "../types/api";
import styles from "./Settings.module.css";

const FALLBACK: AutoSortConfig = { enabled: false, interval_minutes: 30, configured: false };

/** AI auto-sort settings (#424 Phase 6). Opt-in: when enabled, a background loop (and the
 *  on-demand button here) assigns UNASSIGNED sessions to existing projects, reusing the AI
 *  review endpoint configured above. Off by default; never touches a manually-assigned
 *  session; only assigns when the classifier is confident. */
export function AutoSortSettings() {
  const cfgBlock = useConfig()?.auto_sort;
  const [block, setBlock] = useState<AutoSortConfig>(cfgBlock ?? FALLBACK);
  // Reflect the config load (it can land after mount) exactly once per change.
  const [synced, setSynced] = useState(cfgBlock);
  if (cfgBlock !== synced) {
    setSynced(cfgBlock);
    if (cfgBlock) setBlock(cfgBlock);
  }

  const [intervalDraft, setIntervalDraft] = useState(String(block.interval_minutes));
  const [seeded, setSeeded] = useState(block);
  if (seeded !== block) {
    setSeeded(block);
    setIntervalDraft(String(block.interval_minutes));
  }

  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  const save = async (partial: Record<string, unknown>) => {
    setError(null);
    try {
      const r = (await api.setPrefs({ auto_sort: partial })) as { auto_sort?: AutoSortConfig };
      if (r.auto_sort) setBlock(r.auto_sort);
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 422
          ? "That value was rejected — check the interval."
          : "Couldn’t save — please try again.",
      );
    }
  };

  // Interval bounds mirror the server (5–1440 minutes); out-of-range reverts to the saved value.
  const commitInterval = () => {
    const n = Number(intervalDraft);
    if (!Number.isInteger(n) || n < 5 || n > 1440) {
      setIntervalDraft(String(block.interval_minutes));
      return;
    }
    if (n !== block.interval_minutes) void save({ interval_minutes: n });
  };

  const runNow = async () => {
    if (running) return;
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const rep = await api.autoSortNow();
      if (rep.skipped) {
        setResult(`Nothing to sort (${rep.skipped}).`);
      } else if (rep.assigned.length > 0) {
        const n = rep.assigned.length;
        setResult(`Assigned ${n} session${n === 1 ? "" : "s"} to projects.`);
      } else {
        const c = rep.candidates;
        setResult(
          c === 0
            ? "No unassigned sessions to sort."
            : `No confident matches among ${c} unassigned session${c === 1 ? "" : "s"}.`,
        );
      }
    } catch (e) {
      setError(
        e instanceof ApiError && e.message ? e.message : "Couldn’t run auto-sort — please try again.",
      );
    } finally {
      setRunning(false);
    }
  };

  return (
    <section className={styles.section} aria-labelledby="auto-sort-h">
      <h2 id="auto-sort-h">Auto-sort projects</h2>
      <p className={styles.hint}>
        When enabled, sessions that aren’t assigned to any project are classified against your
        existing projects and assigned automatically — reusing the AI review endpoint above. It
        runs in the background and only assigns when it’s confident; ambiguous sessions are left
        alone, and a session you’ve assigned yourself is never changed.
      </p>
      {error && <p className={styles.err}>{error}</p>}

      <label className={styles.aiToggle}>
        <input
          type="checkbox"
          checked={block.enabled}
          onChange={(e) => void save({ enabled: e.currentTarget.checked })}
        />
        <span>Enable auto-sort</span>
      </label>
      {!block.configured && (
        <p className={styles.hint}>
          Configure the AI review endpoint above first — auto-sort reuses it and can’t run until
          it’s set.
        </p>
      )}

      <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor="auto-sort-interval">
          Sort every
        </label>
        <div className={styles.aiIntervalRow}>
          <input
            id="auto-sort-interval"
            className={`${styles.aiInput} ${styles.aiIntervalInput}`}
            type="number"
            min={5}
            max={1440}
            value={intervalDraft}
            onChange={(e) => setIntervalDraft(e.target.value)}
            onBlur={commitInterval}
          />
          <span>minutes</span>
        </div>
        <p className={styles.hint}>
          How often the background loop checks for unassigned sessions (5–1440). Each run is
          bounded — a large backlog is sorted across several runs.
        </p>
      </div>

      <div className={styles.aiActions}>
        <button
          type="button"
          className={`${styles.secBtn} shine`}
          disabled={!block.enabled || !block.configured || running}
          onClick={() => void runNow()}
          title={
            !block.configured
              ? "Configure the AI review endpoint first"
              : !block.enabled
                ? "Enable auto-sort first"
                : "Sort unassigned sessions now"
          }
        >
          <Sparkles size={14} /> {running ? "Sorting…" : "Auto-sort now"}
        </button>
        {result && <p className={styles.ok}>{result}</p>}
      </div>
    </section>
  );
}
