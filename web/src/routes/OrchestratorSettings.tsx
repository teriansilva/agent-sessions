import { PushDevices } from "../components/pulse/PushDevices";
import { useEffect, useRef, useState } from "react";
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
  stale_hours: 24,
  nudge_template: "",
  prompt: "",
  notify: "escalations",
  configured: false,
  default_prompt: "",
  default_nudge_template: "",
};

/** Idle-window presets (#768). The useful values are few and the units need saying, so a
 *  select beats a free number field — but the stored value is not restricted to these. */
const STALE_PRESETS: [number, string][] = [
  [6, "6 hours"],
  [12, "12 hours"],
  [24, "1 day (recommended)"],
  [48, "2 days"],
  [168, "1 week"],
];

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
  const [confDraft, setConfDraft] = useState(block.confidence_min);
  const [nudgeDraft, setNudgeDraft] = useState(block.nudge_template);
  const [seeded, setSeeded] = useState(block);
  if (seeded !== block) {
    setSeeded(block);
    setIntervalDraft(String(block.interval_minutes));
    setConfDraft(block.confidence_min);
    setNudgeDraft(block.nudge_template);
  }

  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const savedTimer = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined,
  );

  // THE SAME LEAK AS `PulseSettings` (#922). Found by that issue's grep phase and fixed here
  // rather than noted, because it is the identical defect with the identical one-line fix: the
  // `clearTimeout` below covers a SECOND save restarting the window, and nothing covers the
  // component going away with a save still in flight. Leaving it would have reproduced the same
  // unhandled `ReferenceError` in `web-ci` from a sibling file.
  // …AND THE CONTINUATION AFTER THE AWAIT MUST KNOW IT LOST ITS COMPONENT (#922 review 1).
  //
  // Clearing on unmount only cancels a timer that ALREADY EXISTS, and `save()` awaits
  // `setPrefs` *before* creating one. Leave the panel mid-request and the cleanup runs against
  // an empty ref, then the response installs a fresh 1500 ms timeout on a component that is
  // gone. The `saveGen` fence below does NOT cover this: it orders responses against each
  // other, and an unmount never bumps a generation.
  //
  // `alive` is re-armed in the effect BODY so StrictMode's mount → unmount → mount does not
  // leave a live panel marked dead.
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      clearTimeout(savedTimer.current);
    };
  }, []);

  // Monotonic generation per save. Responses do not arrive in send order, so applying whichever
  // lands LAST is not applying the last WRITE — measured live at 43 saves in one second from a
  // single slider drag, leaving the panel showing 0.70 while the server held 0.85 (#776). The
  // same rule `Pulse.tsx` uses for overview writes.
  const saveGen = useRef(0);

  const save = async (partial: Record<string, unknown>) => {
    setError(null);
    const gen = ++saveGen.current;
    try {
      const r = (await api.setPrefs({ orchestrator: partial })) as {
        orchestrator?: OrchestratorConfig;
      };
      // A newer save is already in flight — its answer is the truth, not this one's.
      if (gen < saveGen.current) return;
      if (alive.current && r.orchestrator) setBlock(r.orchestrator);
      // Shared, and deliberately ungated: the save succeeded server-side and `ConfigCtx` is
      // owned by a provider above this panel. Gating it would reintroduce the stale-config
      // failure for the operator who navigated away. Only component-local work is gated.
      refreshConfig();
      if (!alive.current) return;
      clearTimeout(savedTimer.current);
      setSaved(true);
      savedTimer.current = setTimeout(() => setSaved(false), 1500);
    } catch (e) {
      // The fence applies to FAILURES too. Guarding only the success path left a stale
      // rejection able to paint "Couldn't save" over a newer save that had already succeeded —
      // the same false error this whole change exists to remove (#776 review). An older
      // response, of either kind, is not news about the current state.
      if (gen < saveGen.current || !alive.current) return;
      setError(
        e instanceof ApiError && e.status === 422
          ? e.message
          : "Couldn’t save — please try again.",
      );
    }
  };

  const commitConf = () => {
    // Nothing to write when the drag ended where it started — a click on the track that lands
    // on the current value should not cost a round trip.
    if (confDraft === block.confidence_min) return;
    void save({ confidence_min: confDraft });
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

  // The manual pass (#929). It used to live on the Pulse route inside the orchestrator panel,
  // which that issue removes — but it is the one control the degraded badge tells the operator
  // to reach for, so it moves here beside the configuration rather than going away with the
  // chrome. Without this the badge would name a recovery with nothing to press.
  const [running, setRunning] = useState(false);
  const [passNote, setPassNote] = useState<string | null>(null);
  const runNow = async () => {
    if (running) return;
    setRunning(true);
    setPassNote(null);
    try {
      const s = await api.orchestrate();
      // The assessment is the pass's own words; falling back to a fixed string would claim a
      // result the server never reported.
      setPassNote(s.assessment || "Pass complete.");
    } catch (e) {
      setPassNote(
        e instanceof ApiError ? e.message : "The pass failed — please try again.",
      );
    } finally {
      setRunning(false);
    }
  };

  const ceiling = block.auto_verbs_ceiling.join(", ");

  return (
    <section className={styles.section} aria-labelledby="orch-h">
      <h2 id="orch-h">Mission control orchestrator</h2>
      <p className={styles.hint}>
        Lets mission control <strong>act</strong> on what it sees: nudging a session that
        stopped mid-task, or raising one that needs your decision. It reuses the
        AI review endpoint above. Every session is managed by default — use{" "}
        <strong>Stop mission control managing this</strong> in a session&rsquo;s row
        menu
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
        <span>Let mission control watch my sessions on a schedule</span>
      </label>

      <div className={styles.aiField} data-testid="orchestrator-run">
        <div className={styles.aiActions}>
          <button
            type="button"
            className={`${styles.secBtn} shine`}
            onClick={() => void runNow()}
            disabled={running || !block.configured}
          >
            {running ? "Thinking…" : "Run now"}
          </button>
        </div>
        <p className={styles.hint}>
          Runs one pass immediately instead of waiting for the schedule. This is
          also how you retry after a failed pass — the badge on mission control points
          here. Anything it proposes appears on the mission that raised it.
        </p>
        {passNote && (
          <p className={styles.hint} role="status">
            {passNote}
          </p>
        )}
      </div>

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
          Even on <strong>YOLO</strong>, mission control only ever sends{" "}
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
          {/* Drag updates LOCAL state only; the save happens once, on release. `onChange` fires
              continuously while dragging, so saving there issued a full locked read-modify-write
              of prefs.json per pixel — 43 in one second, measured — which serialized into the
              "stuck and slow" and painted a false "Couldn't save" (#776). */}
          <input
            id="orch-conf"
            type="range"
            min={0.5}
            max={0.95}
            step={0.05}
            value={confDraft}
            onChange={(e) => setConfDraft(Number(e.target.value))}
            onPointerUp={commitConf}
            onKeyUp={commitConf}
            onBlur={commitConf}
          />
          <span>{confDraft.toFixed(2)}</span>
        </div>
        <p className={styles.hint}>
          Below this, mission control asks you instead of acting. Unsure means ask — never
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

      <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor="orch-stale">
          Stop after a session has been idle for
        </label>
        <select
          id="orch-stale"
          className={styles.aiInput}
          value={String(block.stale_hours)}
          onChange={(e) => void save({ stale_hours: Number(e.target.value) })}
        >
          {/* The server accepts 1–720; these presets are a convenience, not the schema. A
              stored value outside them (set by hand, or by a preset a later version drops)
              would otherwise make the select render its FIRST option — silently showing a
              window the operator never chose, and saving it the moment they touch anything
              else. Carry the real value as its own option instead. */}
          {!STALE_PRESETS.some(([h]) => h === block.stale_hours) && (
            <option value={String(block.stale_hours)}>
              {`${block.stale_hours} hours`}
            </option>
          )}
          {STALE_PRESETS.map(([h, label]) => (
            <option key={h} value={String(h)}>
              {label}
            </option>
          ))}
        </select>
        <p className={styles.hint}>
          Past this the orchestrator stops considering the session, so it stops
          notifying you about it. The session doesn’t go anywhere — it stays on
          mission control and in the sidebar, it just goes quiet.
        </p>
      </div>

      <div className={styles.aiField}>
        <p className={styles.hint}>
          What the orchestrator is asked on each pass lives in{" "}
          <a className={styles.nameLink} href="#prompt-orchestrator_pass">
            Prompts → Scheduled pass
          </a>{" "}
          below (and the chat’s router and instruct prompts alongside it). The
          rule that session output is untrusted is appended by the server and is
          not editable there.
        </p>
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
