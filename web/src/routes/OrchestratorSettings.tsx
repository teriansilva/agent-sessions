import { PushDevices } from "../components/pulse/PushDevices";
import { useRef, useState } from "react";
import { useConfig, useConfigRefresh } from "../app/config";
import { api, ApiError } from "../lib/api";
import type { OrchestratorConfig, OrchestratorTier } from "../types/api";
import styles from "./Settings.module.css";

const FALLBACK: OrchestratorConfig = {
  enabled: false,
  autonomy: "suggest",
  allowed_verbs: ["continue"],
  auto_verbs_ceiling: ["continue"],
  confidence_min: 0.75,
  interval_minutes: 10,
  max_actions_per_pass: 4,
  proposal_ttl_minutes: 30,
  nudge_template: "",
  prompt: "",
  notify: "escalations",
  configured: false,
  default_prompt: "",
  default_nudge_template: "",
};

const TIER_LABELS: Record<OrchestratorTier, string> = {
  off: "Off — watch and propose, never send anything",
  suggest: "Suggest — every action waits for your approval (recommended)",
  yolo: "YOLO — act without asking above the confidence threshold",
};

/** Pulse orchestrator settings (#726 Phase 1). Mirrors PulseSettings/AutoSortSettings: opt-in,
 *  reuses the AI review endpoint above, saves on change/blur and refreshes the shared config
 *  context (without that refresh, remounting the panel shows pre-save values as if the save had
 *  been lost — the #667 failure mode). */
export function OrchestratorSettings() {
  const cfgBlock = useConfig()?.orchestrator;
  const refreshConfig = useConfigRefresh();
  const [block, setBlock] = useState<OrchestratorConfig>(cfgBlock ?? FALLBACK);
  const [synced, setSynced] = useState(cfgBlock);
  if (cfgBlock !== synced) {
    setSynced(cfgBlock);
    if (cfgBlock) setBlock(cfgBlock);
  }

  const [intervalDraft, setIntervalDraft] = useState(
    String(block.interval_minutes),
  );
  const [nudgeDraft, setNudgeDraft] = useState(block.nudge_template);
  const [seeded, setSeeded] = useState(block);
  if (seeded !== block) {
    setSeeded(block);
    setIntervalDraft(String(block.interval_minutes));
    setNudgeDraft(block.nudge_template);
  }

  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const savedTimer = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined,
  );

  const save = async (partial: Record<string, unknown>) => {
    setError(null);
    try {
      const r = (await api.setPrefs({ orchestrator: partial })) as {
        orchestrator?: OrchestratorConfig;
      };
      if (r.orchestrator) setBlock(r.orchestrator);
      refreshConfig();
      clearTimeout(savedTimer.current);
      setSaved(true);
      savedTimer.current = setTimeout(() => setSaved(false), 1500);
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 422
          ? e.message
          : "Couldn’t save — please try again.",
      );
    }
  };

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

  const commitNudge = () => {
    if (nudgeDraft !== block.nudge_template)
      void save({ nudge_template: nudgeDraft });
  };

  const blurOnEnter = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") e.currentTarget.blur();
  };

  const ceiling = block.auto_verbs_ceiling.join(", ");

  return (
    <section className={styles.section} aria-labelledby="orch-h">
      <h2 id="orch-h">Pulse orchestrator</h2>
      <p className={styles.hint}>
        Lets Pulse <strong>act</strong> on what it sees: nudging a session that
        stopped mid-task, or raising one that needs your decision. It reuses the
        AI review endpoint above. Every session is managed by default — use{" "}
        <strong>Stop Pulse managing this</strong> in a session&rsquo;s row menu
        to withdraw one. Changes save automatically.
      </p>
      {!block.configured && (
        <p className={styles.hint}>
          The AI endpoint isn&rsquo;t configured yet, so the orchestrator
          can&rsquo;t run.
        </p>
      )}
      {error && <p className={styles.err}>{error}</p>}
      {saved && (
        <p className={styles.ok} role="status">
          Saved.
        </p>
      )}

      <label className={styles.aiToggle}>
        <input
          type="checkbox"
          checked={block.enabled}
          onChange={(e) => void save({ enabled: e.currentTarget.checked })}
        />
        <span>Let Pulse watch my sessions on a schedule</span>
      </label>

      <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor="orch-tier">
          Autonomy
        </label>
        <select
          id="orch-tier"
          className={styles.aiInput}
          value={block.autonomy}
          onChange={(e) => void save({ autonomy: e.target.value })}
        >
          {(Object.keys(TIER_LABELS) as OrchestratorTier[]).map((t) => (
            <option key={t} value={t}>
              {TIER_LABELS[t]}
            </option>
          ))}
        </select>
        {/* The tier alone doesn't tell the whole story, and implying it does would be the
            dangerous reading. Say plainly which verbs YOLO can actually deliver. */}
        <p className={styles.hint}>
          Even on <strong>YOLO</strong>, Pulse only ever sends{" "}
          <strong>{ceiling}</strong> on its own — the fixed nudge you write
          below, which the AI cannot alter. Picking an option, answering a
          question, or starting a new session always waits for your approval.
        </p>
      </div>

      <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor="orch-conf">
          Act above confidence
        </label>
        <div className={styles.aiIntervalRow}>
          <input
            id="orch-conf"
            type="range"
            min={0.5}
            max={0.95}
            step={0.05}
            value={block.confidence_min}
            onChange={(e) =>
              void save({ confidence_min: Number(e.target.value) })
            }
          />
          <span>{block.confidence_min.toFixed(2)}</span>
        </div>
        <p className={styles.hint}>
          Below this, Pulse asks you instead of acting. Unsure means ask — never
          guess.
        </p>
      </div>

      <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor="orch-interval">
          Check every
        </label>
        <div className={styles.aiIntervalRow}>
          <input
            id="orch-interval"
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
          A pass is skipped entirely when nothing about your sessions changed
          (5–1440).
        </p>
      </div>

      <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor="orch-notify">
          Notify me about
        </label>
        <select
          id="orch-notify"
          className={styles.aiInput}
          value={block.notify}
          onChange={(e) => void save({ notify: e.target.value })}
        >
          <option value="none">Nothing</option>
          <option value="escalations">Only things that need my decision</option>
          <option value="all">Everything it does</option>
        </select>
      </div>

      <PushDevices />

      <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor="orch-nudge">
          Nudge text
        </label>
        <textarea
          id="orch-nudge"
          className={`${styles.aiInput} ${styles.aiPrompt}`}
          rows={2}
          maxLength={2000}
          value={nudgeDraft}
          onChange={(e) => setNudgeDraft(e.target.value)}
          onBlur={commitNudge}
        />
        <p className={styles.hint}>
          The exact text sent to a stalled session. Written by you, never by the
          AI — that is what makes this the one action safe to automate.
        </p>
        <div className={styles.aiActions}>
          <button
            type="button"
            className={`${styles.secBtn} shine`}
            onClick={() => {
              setNudgeDraft(block.default_nudge_template);
              void save({ nudge_template: block.default_nudge_template });
            }}
          >
            Reset to default
          </button>
        </div>
      </div>
    </section>
  );
}
