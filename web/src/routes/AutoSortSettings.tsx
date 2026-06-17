import { Sparkles } from "lucide-react";
import { useEffect, useState } from "react";
import { useConfig } from "../app/config";
import { api, ApiError } from "../lib/api";
import type { AutoSortConfig, AutoSortReport } from "../types/api";
import styles from "./Settings.module.css";

const FALLBACK: AutoSortConfig = {
  enabled: false,
  interval_minutes: 30,
  confidence_min: 0.7,
  max_per_pass: 8,
  prompt: "",
  configured: false,
  default_prompt: "",
};

// Mirror the server bounds (prefs.AUTO_SORT_*) so an out-of-range entry reverts client-side.
const CONFIDENCE_LO = 0.5;
const CONFIDENCE_HI = 0.95;
const PER_PASS_LO = 1;
const PER_PASS_HI = 50;

/** AI auto-sort settings (#424 Phase 6; tunables #459). Opt-in: when enabled, a background
 *  loop (and the on-demand button here) assigns UNASSIGNED sessions to existing projects,
 *  reusing the AI endpoint configured above. The confidence floor, classifier prompt, and
 *  per-run cap are operator-settable here; a run that assigns nothing reports its near-misses
 *  so the threshold is an informed choice. Off by default; never touches a manually-assigned
 *  session; only assigns when the classifier clears the floor. */
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
  const [confidenceDraft, setConfidenceDraft] = useState(block.confidence_min.toFixed(2));
  const [perPassDraft, setPerPassDraft] = useState(String(block.max_per_pass));
  const [promptDraft, setPromptDraft] = useState(block.prompt);
  const [seeded, setSeeded] = useState(block);
  if (seeded !== block) {
    setSeeded(block);
    setIntervalDraft(String(block.interval_minutes));
    setConfidenceDraft(block.confidence_min.toFixed(2));
    setPerPassDraft(String(block.max_per_pass));
    setPromptDraft(block.prompt);
  }

  // Project id → name, so a near-miss reads "superstatus 0.62", not a raw entity id. Loaded
  // once; falls back to the id if a project isn't in the map (e.g. archived between runs).
  const [projNames, setProjNames] = useState<Record<string, string>>({});
  useEffect(() => {
    let alive = true;
    api
      .projectEntities()
      .then((d) => {
        if (alive) setProjNames(Object.fromEntries(d.projects.map((p) => [p.id, p.name])));
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [nearMisses, setNearMisses] = useState<NonNullable<AutoSortReport["near_misses"]>>([]);

  const save = async (partial: Record<string, unknown>) => {
    setError(null);
    try {
      const r = (await api.setPrefs({ auto_sort: partial })) as { auto_sort?: AutoSortConfig };
      if (r.auto_sort) setBlock(r.auto_sort);
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 422
          ? "That value was rejected — check the numbers."
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

  // Confidence floor 0.50–0.95 (2 decimals); out-of-range reverts to the saved value.
  const commitConfidence = () => {
    const n = Number(confidenceDraft);
    if (!Number.isFinite(n) || n < CONFIDENCE_LO || n > CONFIDENCE_HI) {
      setConfidenceDraft(block.confidence_min.toFixed(2));
      return;
    }
    const v = Math.round(n * 100) / 100; // avoid float noise from the 0.05 step
    if (v !== block.confidence_min) void save({ confidence_min: v });
    setConfidenceDraft(v.toFixed(2));
  };

  // Sessions per run 1–50; out-of-range reverts to the saved value.
  const commitPerPass = () => {
    const n = Number(perPassDraft);
    if (!Number.isInteger(n) || n < PER_PASS_LO || n > PER_PASS_HI) {
      setPerPassDraft(String(block.max_per_pass));
      return;
    }
    if (n !== block.max_per_pass) void save({ max_per_pass: n });
  };

  const runNow = async () => {
    if (running) return;
    setRunning(true);
    setError(null);
    setResult(null);
    setNearMisses([]);
    try {
      const rep = await api.autoSortNow();
      setNearMisses(rep.near_misses ?? []);
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
        existing projects and assigned automatically — reusing the AI endpoint above. It runs in
        the background and only assigns when it clears the confidence floor; ambiguous sessions
        are left alone, and a session you’ve assigned yourself is never changed.
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
          Configure the AI endpoint above first — auto-sort reuses it and can’t run until it’s
          set.
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

      <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor="auto-sort-confidence">
          Confidence threshold
        </label>
        <div className={styles.aiIntervalRow}>
          <input
            id="auto-sort-confidence"
            className={`${styles.aiInput} ${styles.aiIntervalInput}`}
            type="number"
            step={0.05}
            min={CONFIDENCE_LO}
            max={CONFIDENCE_HI}
            value={confidenceDraft}
            onChange={(e) => setConfidenceDraft(e.target.value)}
            onBlur={commitConfidence}
          />
          <span>0 – 1 (lower = more matches)</span>
        </div>
        <p className={styles.hint}>
          A session is assigned only when the classifier is at least this confident (0.50–0.95).
          Lower it if confident matches are too rare; raise it to be stricter.
        </p>
      </div>

      <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor="auto-sort-per-pass">
          Sessions per run
        </label>
        <div className={styles.aiIntervalRow}>
          <input
            id="auto-sort-per-pass"
            className={`${styles.aiInput} ${styles.aiIntervalInput}`}
            type="number"
            min={PER_PASS_LO}
            max={PER_PASS_HI}
            value={perPassDraft}
            onChange={(e) => setPerPassDraft(e.target.value)}
            onBlur={commitPerPass}
          />
          <span>per pass (1–50)</span>
        </div>
        <p className={styles.hint}>
          How many unassigned sessions each run classifies — the background loop and the button
          below. A larger backlog is cleared across several runs; raise this to sort more at once.
        </p>
      </div>

      <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor="auto-sort-prompt">
          Auto-sort prompt
        </label>
        <textarea
          id="auto-sort-prompt"
          className={`${styles.aiInput} ${styles.aiPrompt}`}
          aria-label="Auto-sort prompt"
          value={promptDraft}
          onChange={(e) => setPromptDraft(e.target.value)}
        />
        <div className={styles.aiActions}>
          <button
            type="button"
            className={`${styles.secBtn} shine`}
            disabled={promptDraft === block.prompt}
            onClick={() => void save({ prompt: promptDraft })}
          >
            Save
          </button>
          <button
            type="button"
            className={styles.secBtnGhost}
            onClick={() => {
              setPromptDraft(block.default_prompt);
              void save({ prompt: block.default_prompt });
            }}
          >
            Reset to default
          </button>
        </div>
        <p className={styles.hint}>
          The classifier instruction. Leave it empty to reset to the default. Make it less
          conservative if too many sessions come back as “no confident match”.
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
              ? "Configure the AI endpoint first"
              : !block.enabled
                ? "Enable auto-sort first"
                : "Sort unassigned sessions now"
          }
        >
          <Sparkles size={14} /> {running ? "Sorting…" : "Auto-sort now"}
        </button>
        {result && <p className={styles.ok}>{result}</p>}
      </div>
      {/* Near-misses are the report's known-project picks that fell below the floor (the
          backend bounds + sorts them), so each resolves to a real project name. */}
      {nearMisses.length > 0 && (
        <p className={styles.hint}>
          <strong>Closest:</strong>{" "}
          {nearMisses.map((n, i) => (
            <span key={n.id}>
              {i > 0 ? " · " : ""}
              {projNames[n.project_id] ?? n.project_id} {n.confidence.toFixed(2)}
            </span>
          ))}{" "}
          — lower the threshold to assign them.
        </p>
      )}
    </section>
  );
}
