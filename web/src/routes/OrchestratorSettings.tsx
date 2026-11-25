import { PushDevices } from "../components/pulse/PushDevices";
import { useEffect, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useConfig, useConfigRefresh, useConfigReload } from "../app/config";
import { api, ApiError } from "../lib/api";
import type { AutomationPolicy, AutomationScope, OrchestratorConfig, OrchestratorTier } from "../types/api";
import styles from "./Settings.module.css";
import { promptPath } from "./settingsTabs";

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
  notify: "escalations",
  configured: false,
  default_nudge_template: "",
  auto_ai_directions: false,
  ai_direction_confidence_min: 0.9,
  ai_direction_confidence_floor: 0.9,
  ai_direction_confidence_max: 1,
  judge_confidence_min: 0.9,
  judge_confidence_floor: 0.9,
  judge_confidence_max: 1,
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
  off: "Off — no automatic actions",
  suggest: "Suggest — propose for approval",
  yolo: "YOLO — act above the confidence threshold",
};

const readable = (policy: AutomationPolicy | undefined) =>
  typeof policy?.revision === "string" && policy.revision !== "" && policy.revision !== "invalid";

/** HQ autonomy (#2037): one editor and one accepted revision per server scope. Only one write
 * may be in flight in an editor. A conflict reloads without replay; unreadable authority disables
 * editing. The legacy combined orchestrator endpoint is never a fallback. */
export function OrchestratorSettings({ scope = "session" }: { scope?: AutomationScope }) {
  const config = useConfig();
  const cfgBlock = config?.automation?.version === 1 ? config.automation[scope] : undefined;
  const available = readable(cfgBlock);
  const mission = scope === "mission";
  const title = mission ? "Mission orchestration" : "Session assistance";
  const refreshConfig = useConfigRefresh();
  const reloadConfig = useConfigReload();
  // Rides on the in-app prompt link so the #155 "Back to sessions" target survives the hop.
  const location = useLocation();
  const [block, setBlock] = useState<OrchestratorConfig & { revision: string }>(
    { ...FALLBACK, revision: "invalid", ...cfgBlock },
  );
  const [synced, setSynced] = useState(cfgBlock);
  if (cfgBlock !== synced) {
    setSynced(cfgBlock);
    setBlock({ ...FALLBACK, revision: "invalid", ...cfgBlock });
  }

  const [intervalDraft, setIntervalDraft] = useState(
    String(block.interval_minutes),
  );
  const [confDraft, setConfDraft] = useState(block.confidence_min);
  const [aiConfDraft, setAiConfDraft] = useState(block.ai_direction_confidence_min);
  /** The judgment threshold (#1088). An older server may not send it: the floor is the answer. */
  const judgeFloor = block.judge_confidence_floor ?? 0.9;
  const judgeMax = block.judge_confidence_max ?? 1;
  const judgeMin = block.judge_confidence_min ?? judgeFloor;
  const [judgeDraft, setJudgeDraft] = useState(judgeMin);
  const [nudgeDraft, setNudgeDraft] = useState(block.nudge_template);
  const [seeded, setSeeded] = useState(block);
  if (seeded !== block) {
    setSeeded(block);
    setIntervalDraft(String(block.interval_minutes));
    setConfDraft(block.confidence_min);
    setAiConfDraft(block.ai_direction_confidence_min);
    setJudgeDraft(block.judge_confidence_min ?? block.judge_confidence_floor ?? 0.9);
    setNudgeDraft(block.nudge_template);
  }

  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);
  const busy = useRef(false);
  const [readFailed, setReadFailed] = useState(false);
  const savedTimer = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined,
  );

  // #922: cancel an existing toast timer and fence async continuations after unmount.
  // Re-arm in the effect body for StrictMode's mount/unmount/mount cycle.
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      clearTimeout(savedTimer.current);
    };
  }, []);

  // Serialize writes so the next edit uses the revision the preceding response actually accepted.
  // The fieldset disables interaction; the ref also fences blur/pointer events in the same frame.
  const save = async (partial: Record<string, unknown>) => {
    if (busy.current || !available || readFailed) return;
    busy.current = true;
    setSaving(true);
    setSaved(false);
    setError(null);
    try {
      const r = await api.setAutomationPolicy(scope, block.revision, partial);
      if (r.version !== 1 || r.scope !== scope || !readable(r.policy)) {
        if (alive.current) setReadFailed(true);
        throw new Error("The accepted policy could not be read.");
      }
      if (alive.current) setBlock({ ...FALLBACK, ...r.policy });
      // Shared, and deliberately ungated: the save succeeded server-side and `ConfigCtx` is
      // owned by a provider above this panel. Gating it would reintroduce the stale-config
      // failure for the operator who navigated away. Only component-local work is gated.
      refreshConfig();
      if (!alive.current) return;
      clearTimeout(savedTimer.current);
      setSaved(true);
      savedTimer.current = setTimeout(() => setSaved(false), 1500);
    } catch (e) {
      if (!alive.current) return;
      setBlock((current) => ({ ...current })); // Reset any unsaved slider/text draft too.
      if (e instanceof ApiError && e.status === 409) {
        setReadFailed(true);
        const loaded = await reloadConfig().catch(() => false);
        if (!alive.current) return;
        setReadFailed(!loaded);
        setError(loaded
          ? "These settings changed elsewhere. Latest values loaded; review them before saving again."
          : "These settings changed elsewhere, but HQ could not read the latest values. Reload settings before editing.");
        return;
      }
      if (e instanceof ApiError && e.status === 503) setReadFailed(true);
      setError(
        e instanceof ApiError && e.status === 422
          ? e.message
          : e instanceof ApiError && e.status === 503
            ? "HQ could not read or update this policy. Reload settings before editing."
            : "Couldn’t save — please try again.",
      );
    } finally {
      busy.current = false;
      if (alive.current) setSaving(false);
    }
  };

  const reload = async () => {
    if (busy.current) return;
    busy.current = true;
    setSaving(true);
    const loaded = await reloadConfig().catch(() => false);
    busy.current = false;
    if (!alive.current) return;
    setSaving(false);
    setReadFailed(!loaded);
    setError(loaded ? null : "HQ could not read the latest settings. Try reloading again.");
  };

  const commitConf = () => {
    // Nothing to write when the drag ended where it started — a click on the track that lands
    // on the current value should not cost a round trip.
    if (confDraft === block.confidence_min) return;
    void save({ confidence_min: confDraft });
  };

  // Same drag-then-save rule as `commitConf` (#776): `onChange` fires per pixel, and a locked
  // read-modify-write of prefs.json per pixel is what made the panel feel stuck.
  const commitAiConf = () => {
    if (aiConfDraft === block.ai_direction_confidence_min) return;
    void save({ ai_direction_confidence_min: aiConfDraft });
  };

  // Same drag-then-save rule (#776): ONE write on release, never one per pixel.
  const commitJudge = () => {
    if (judgeDraft === judgeMin) return;
    void save({ judge_confidence_min: judgeDraft });
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

  return (
    <section className={styles.section} aria-labelledby={`${scope}-orch-h`}>
      <h2 id={`${scope}-orch-h`}>{title}</h2>
      <p className={styles.hint}>
        {mission
          ? "HQ’s mission supervision and proposed actions. This policy does not change session assistance."
          : "HQ’s assistance for sessions no active mission holds. This policy does not change mission orchestration."}
        {" "}Uses the AI endpoint from Endpoint &amp; model. Changes save automatically.
      </p>
      {!block.configured && (
        <p className={styles.hint}>
          The AI endpoint isn&rsquo;t configured yet, so the orchestrator
          can&rsquo;t run.
        </p>
      )}
      {(error || !available || readFailed) && (
        <div className={styles.err} role="alert">
          <p>{error || "HQ could not read this scope’s settings. Reload settings before editing."}</p>
          {(!available || readFailed) && (
            <button type="button" className={styles.secBtn} disabled={saving} onClick={() => void reload()}>
              Reload settings
            </button>
          )}
        </div>
      )}
      {saved && (
        <p className={styles.ok} role="status">
          Saved.
        </p>
      )}

      <fieldset className={styles.policyFields} disabled={saving || !available || readFailed}>
      <label className={styles.aiToggle}>
        <input
          type="checkbox"
          checked={block.enabled}
          onChange={(e) => void save({ enabled: e.currentTarget.checked })}
        />
        <span>{mission ? "Allow mission orchestration" : "Allow session assistance"}</span>
      </label>
      <p className={styles.hint}>
        {mission
          ? "Off stops mission decisions and delivery under this policy. Plan approval and mission-specific opt-ins remain required."
          : "Off stops scheduled assistance and delivery to unassigned sessions. Run now can still observe; it does not grant permission to send."}
      </p>

      {!mission && <div className={styles.aiField} data-testid="orchestrator-run">
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
          also how you retry after a failed pass — the badge on HQ points
          here. Review proposed actions in the session that raised them.
        </p>
        {passNote && (
          <p className={styles.hint} role="status">
            {passNote}
          </p>
        )}
      </div>}

      <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor={`${scope}-orch-tier`}>
          Autonomy
        </label>
        <select
          id={`${scope}-orch-tier`}
          className={styles.aiInput}
          value={block.autonomy}
          onChange={(e) => void save({ autonomy: e.target.value })}
        >
          {(Object.keys(TIER_LABELS) as OrchestratorTier[]).map((t) => (
            <option key={t} value={t}>
              {t === "off" && mission ? "Off — no mission decisions" : TIER_LABELS[t]}
            </option>
          ))}
        </select>
        {/* The tier alone doesn't tell the whole story, and implying it does would be the
            dangerous reading. Say plainly what YOLO can type on its own (#983): only operator
            text, the objective's direction or the default nudge, never words the AI wrote. */}
        <p className={`${styles.hint} ${styles.callout}`} data-testid="orchestrator-yolo-copy">
          Even on <strong>YOLO</strong>, HQ types text{" "}
          <strong>you wrote</strong>: {mission
            ? "an objective’s direction, filled with facts it checked itself, or your default nudge below."
            : "your default nudge below."} The AI decides when to send it. Picking an option
          waits for your approval{mission ? ", unless a mission lets HQ answer its sessions’ menus (set per mission, in Follow-through)" : ""}.
          Answering a question or starting a new session always waits for your approval.
          {/* …UNLESS THE OPERATOR TURNED ON THE ONE EXCEPTION (#983 P4). With the toggle off this
              paragraph is exactly P3's, and it is true; with it on, the sentence above would be a
              flat untruth, so the exception is stated here rather than only next to the toggle. */}
          {mission && block.auto_ai_directions ? (
            <>
              {" "}
              <strong>One exception, which you turned on:</strong> an AI-written
              direction the model rates{" "}
              {block.ai_direction_confidence_min.toFixed(2)} or higher is sent on
              its own, with nobody reading it first.
            </>
          ) : null}
        </p>
      </div>

      {mission && <div className={styles.aiField} data-testid="orchestrator-mission-bypass">
        <p className={styles.hint}>
          Mission agents never skip tool prompts. The agent default for sessions you start
          does not grant permission bypass to missions.
        </p>
      </div>}

      <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor={`${scope}-orch-conf`}>
          Act above confidence
        </label>
        <div className={styles.aiIntervalRow}>
          {/* Drag updates LOCAL state only; the save happens once, on release. `onChange` fires
              continuously while dragging, so saving there issued a full locked read-modify-write
              of prefs.json per pixel — 43 in one second, measured — which serialized into the
              "stuck and slow" and painted a false "Couldn't save" (#776). */}
          <input
            id={`${scope}-orch-conf`}
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
          Below this, HQ asks you instead of acting. Unsure means ask — never
          guess.
        </p>
      </div>

      {/* THE SUPERVISOR'S JUDGMENT FLOOR (#1088). What judging is, that the floor cannot be lowered,
          and that confidence is the model's own opinion — beside the control, never in a tooltip. */}
      {mission && <div className={styles.aiField} data-testid="orchestrator-judge">
        <label className={styles.aiFieldLabel} htmlFor={`${scope}-orch-judge-conf`}>
          Judge an objective met at or above
        </label>
        <div className={styles.aiIntervalRow}>
          <input
            id={`${scope}-orch-judge-conf`}
            type="range"
            min={judgeFloor}
            max={judgeMax}
            step={0.01}
            value={judgeDraft}
            onChange={(e) => setJudgeDraft(Number(e.target.value))}
            onPointerUp={commitJudge}
            onKeyUp={commitJudge}
            onBlur={commitJudge}
            data-testid="judge-threshold"
          />
          <span data-testid="judge-threshold-value">{judgeDraft.toFixed(2)}</span>
        </div>
        <p className={styles.hint}>
          For objectives the <strong>supervisor judges</strong> — like &ldquo;a finding is
          written down&rdquo; — rather than checks. An independent model call reads the
          session and must quote its evidence. At or above this, the objective counts as{" "}
          <strong>judged met</strong>; below it, it stays unmet.
        </p>
        <p className={styles.hint}>
          {judgeFloor.toFixed(2)} is the floor and cannot be lowered. Confidence is the
          model&rsquo;s own opinion, and a session&rsquo;s output can try to talk it round — so a
          judgment can at most move a mission to <strong>review</strong>. You still close it.
        </p>
      </div>}

      {/* AUTONOMOUS AI-WRITTEN DIRECTIONS (#983 P4). The one mode in which a model authors the
          bytes typed into a permission-bypassed agent. The honest-limits warning sits BESIDE the
          control, never in a tooltip: a caveat you have to hover to discover is not a disclosure. */}
      {mission && <div className={styles.aiField} data-testid="orchestrator-auto-ai">
        <label className={styles.aiToggle}>
          <input
            type="checkbox"
            checked={block.auto_ai_directions}
            disabled={block.autonomy !== "yolo"}
            onChange={(e) =>
              void save({ auto_ai_directions: e.currentTarget.checked })
            }
            data-testid="auto-ai-toggle"
          />
          <span>Let HQ send AI-written directions on its own</span>
        </label>
        <p className={`${styles.hint} ${styles.callout} ${styles.calloutWarn}`} data-testid="auto-ai-warning">
          <strong>
            This is the one thing the AI writes that gets typed into a session for you.
          </strong>{" "}
          Confidence is the model&rsquo;s own opinion of its own work, not a safety check. What a
          session contains — its output, the files and PR comments it read — is untrusted, and an
          instruction hidden in any of it can shape both the text and the confidence. Nobody reads
          the text before it reaches the agent. Every send is announced, even if you have
          notifications turned down. It is off unless you turn it on, YOLO only, and one send per
          objective attempt.
        </p>
        {block.autonomy !== "yolo" ? (
          <p className={styles.hint} data-testid="auto-ai-tier-note">
            Available on <strong>YOLO</strong> only. Leaving YOLO turns it off.
          </p>
        ) : null}
        {mission && block.auto_ai_directions ? (
          <>
            <label className={styles.aiFieldLabel} htmlFor={`${scope}-orch-ai-conf`}>
              Send an AI-written direction at or above
            </label>
            <div className={styles.aiIntervalRow}>
              <input
                id={`${scope}-orch-ai-conf`}
                type="range"
                min={block.ai_direction_confidence_floor}
                max={block.ai_direction_confidence_max}
                step={0.01}
                value={aiConfDraft}
                onChange={(e) => setAiConfDraft(Number(e.target.value))}
                onPointerUp={commitAiConf}
                onKeyUp={commitAiConf}
                onBlur={commitAiConf}
                data-testid="auto-ai-threshold"
              />
              <span data-testid="auto-ai-threshold-value">
                {aiConfDraft.toFixed(2)}
              </span>
            </div>
            <p className={styles.hint}>
              {block.ai_direction_confidence_floor.toFixed(2)} is the floor and cannot be
              lowered — it is the threshold this mode was approved for. Below it, a draft still
              waits for your tap.
            </p>
          </>
        ) : null}
      </div>}

      {!mission && <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor={`${scope}-orch-interval`}>
          Check every
        </label>
        <div className={styles.aiIntervalRow}>
          <input
            id={`${scope}-orch-interval`}
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
      </div>}

      <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor={`${scope}-orch-notify`}>
          Notify me about
        </label>
        <select
          id={`${scope}-orch-notify`}
          className={styles.aiInput}
          value={block.notify}
          onChange={(e) => void save({ notify: e.target.value })}
        >
          <option value="none">Nothing</option>
          <option value="escalations">Only things that need my decision</option>
          <option value="all">Everything it does</option>
        </select>
      </div>

      {!mission && <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor={`${scope}-orch-stale`}>
          Stop after a session has been idle for
        </label>
        <select
          id={`${scope}-orch-stale`}
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
          HQ and in the sidebar, it just goes quiet.
        </p>
      </div>}

      {!mission && <div className={styles.aiField}>
        <p className={styles.hint}>
          What the orchestrator is asked on each pass lives in{" "}
          <Link
            className={styles.nameLink}
            to={promptPath("orchestrator_pass")}
            state={location.state}
          >
            Prompts → Scheduled pass
          </Link>{" "}
          (with the chat’s router and instruct prompts beside it). The
          rule that session output is untrusted is appended by the server and is
          not editable there.
        </p>
      </div>}


      <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor={`${scope}-orch-nudge`}>
          {mission ? "Default nudge · sent when an objective has no direction" : "Default nudge"}
        </label>
        <textarea
          id={`${scope}-orch-nudge`}
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
      </fieldset>
      {!mission && <PushDevices />}
    </section>
  );
}
