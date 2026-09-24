import { useCallback, useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useConfig, useConfigRefresh } from "../app/config";
import { api, ApiError } from "../lib/api";
import type { AiReviewConfig, Session, SessionReviewConfig } from "../types/api";
import styles from "./Settings.module.css";
import { promptPath } from "./settingsTabs";

const FALLBACK: AiReviewConfig = {
  enabled: false,
  base_url: "",
  model: "",
  interval_minutes: 5,
  max_input_chars: 24000,
  request_timeout: null,
  api_key_set: false,
  configured: false,
};

/** Session review (#356) — its own Settings page since #956. The periodic-review toggle, the
 *  interval, where the review prompt lives, and the excluded-sessions list.
 *
 *  The endpoint, key, model and timeout used to share this component; they moved to
 *  `AiEndpointSetup` (Settings → AI → Endpoint & model), which every AI feature uses. These
 *  fields keep their commit-on-change / commit-on-blur behaviour. */
const DEPTH_FALLBACK: SessionReviewConfig = {
  recognise_prompts: true,
  decision_context: "standard",
};

/** How deeply a session is read before a decision (#1086 Phase 2): the `session_review` block.
 *
 *  Its own block and its own save, never folded into `ai_review` — that block carries the
 *  endpoint key and its host binding, and nothing here should share that write path. Same
 *  commit-on-change shape as the rest of this page, and the same refresh of the shared config
 *  context after a save, so a remount never shows the pre-save value (#667). */
export function SessionReviewDepth() {
  const cfgBlock = useConfig()?.session_review;
  const refreshConfig = useConfigRefresh();
  const [block, setBlock] = useState<SessionReviewConfig>(cfgBlock ?? DEPTH_FALLBACK);
  const [synced, setSynced] = useState(cfgBlock);
  if (cfgBlock !== synced) {
    setSynced(cfgBlock);
    if (cfgBlock) setBlock(cfgBlock);
  }
  const [error, setError] = useState<string | null>(null);
  // ONE save at a time (review 5180): with overlapping saves, an older request that failed would
  // restore ITS snapshot over a newer request that succeeded. The controls are disabled while a
  // save is in flight, so there is never a second request whose answer could be overtaken.
  const [saving, setSaving] = useState(false);

  const save = async (partial: Partial<SessionReviewConfig>) => {
    if (saving) return;
    setError(null);
    setSaving(true);
    const before = block;
    setBlock({ ...block, ...partial });
    try {
      const out = await api.setPrefs({ session_review: partial });
      const echoed = out?.session_review as SessionReviewConfig | undefined;
      if (echoed) setBlock(echoed);
    } catch (e) {
      // A refused save must not LOOK saved: show the last known value now, and re-read the
      // server's authoritative block below rather than trusting the local snapshot alone.
      setBlock(before);
      setError(e instanceof ApiError ? e.message : "Couldn’t save — please try again.");
    } finally {
      setSaving(false);
      refreshConfig();
    }
  };

  return (
    <div className={styles.aiField} data-testid="session-review-depth">
      <span className={styles.aiFieldLabel}>Reading sessions</span>
      {error && (
        <p className={styles.err} role="alert">
          {error}
        </p>
      )}
      <label className={styles.aiToggle}>
        <input
          type="checkbox"
          checked={block.recognise_prompts}
          disabled={saving}
          onChange={(e) => void save({ recognise_prompts: e.currentTarget.checked })}
        />
        <span>Recognise questions and choices</span>
      </label>
      <p className={styles.hint}>
        Reads the live screen for numbered options, yes/no prompts and permission
        requests, so a session that needs you says what kind of answer it is waiting
        for, and the orchestrator can see the options.
      </p>
      <span className={styles.aiFieldLabel} id="decision-context-label">
        Decision context
      </span>
      <div
        className={styles.modeRow}
        role="radiogroup"
        aria-labelledby="decision-context-label"
      >
        <label className={styles.modeOpt}>
          <input
            type="radio"
            name="decision-context"
            checked={block.decision_context === "standard"}
            disabled={saving}
          onChange={() => void save({ decision_context: "standard" })}
          />
          Standard
        </label>
        <label className={styles.modeOpt}>
          <input
            type="radio"
            name="decision-context"
            checked={block.decision_context === "deep"}
            disabled={saving}
          onChange={() => void save({ decision_context: "deep" })}
          />
          Deep
        </label>
      </div>
      <p className={styles.hint}>
        {block.decision_context === "deep"
          ? "Deep: before it suggests an answer, the orchestrator also reads the end of the conversation and what was already done or refused — for the few sessions a decision is about, not the whole list."
          : "Standard: the orchestrator reads where each session stands now, why it needs you, and what its screen is waiting on."}
      </p>
    </div>
  );
}

export function AiReviewSettings() {
  const cfgBlock = useConfig()?.ai_review;
  // Rides on the in-app prompt link so the #155 "Back to sessions" target survives the hop.
  const location = useLocation();
  const [block, setBlock] = useState<AiReviewConfig>(cfgBlock ?? FALLBACK);
  // Reflect the config load (it can land after mount) exactly once per change.
  const [synced, setSynced] = useState(cfgBlock);
  if (cfgBlock !== synced) {
    setSynced(cfgBlock);
    if (cfgBlock) setBlock(cfgBlock);
  }

  // Draft for the commit-on-blur interval (typing must not spam the server).
  const [intervalDraft, setIntervalDraft] = useState(
    String(block.interval_minutes),
  );
  const [seededInterval, setSeededInterval] = useState(block.interval_minutes);
  if (seededInterval !== block.interval_minutes) {
    setSeededInterval(block.interval_minutes);
    setIntervalDraft(String(block.interval_minutes));
  }

  const [error, setError] = useState<string | null>(null);
  const [savedNote, setSavedNote] = useState(false);

  // Refetch the shared /api/config context after EVERY successful save (#956): with one page per
  // section a remount on navigation is routine, and a remount on a stale context shows pre-save
  // values as if the save had been lost — the #667 failure mode.
  const refreshConfig = useConfigRefresh();

  /** Persist a partial ai_review block; the echo is the server's public view. */
  const save = useCallback(
    async (partial: Record<string, unknown>) => {
      setError(null);
      try {
        const r = (await api.setPrefs({ ai_review: partial })) as {
          ai_review?: AiReviewConfig;
        };
        if (r.ai_review) {
          setBlock(r.ai_review);
          refreshConfig();
        }
        setSavedNote(true);
        setTimeout(() => setSavedNote(false), 1500);
      } catch (e) {
        setError(
          e instanceof ApiError && e.status === 422
            ? // The server names the field and the bound it broke (#834).
              e.message || "That value was rejected — check the number."
            : "Couldn’t save — please try again.",
        );
      }
    },
    [refreshConfig],
  );

  // --- excluded sessions (#356): row-menu opt-outs surface here for re-inclusion ---
  const [excluded, setExcluded] = useState<Session[] | null>(null);
  useEffect(() => {
    let alive = true;
    api
      .sessions({ limit: 200 })
      .then(
        (d) =>
          alive && setExcluded(d.sessions.filter((s) => s.review_excluded)),
      )
      .catch(() => alive && setExcluded([]));
    return () => {
      alive = false;
    };
  }, []);
  const include = async (id: string) => {
    try {
      await api.reviewExclude(id, false);
      setExcluded((prev) => (prev ?? []).filter((s) => s.id !== id));
    } catch {
      /* keep the row — the next visit re-fetches the truth */
    }
  };

  const commitInterval = () => {
    const n = Number(intervalDraft);
    if (!Number.isInteger(n) || n < 1) {
      setIntervalDraft(String(block.interval_minutes));
      return;
    }
    if (n !== block.interval_minutes) void save({ interval_minutes: n });
  };

  return (
    <section className={styles.section} aria-labelledby="ai-review-h">
      <h2 id="ai-review-h">Session review</h2>
      <p className={styles.hint}>
        Periodically reviews sessions with new activity and produces a one-line
        summary, a title, and an intervention flag per session — using the AI
        endpoint from Endpoint &amp; model.
      </p>
      {error && (
        <p className={styles.err} role="alert">
          {error}
        </p>
      )}

      <label className={styles.aiToggle}>
        <input
          type="checkbox"
          checked={block.enabled}
          onChange={(e) => void save({ enabled: e.currentTarget.checked })}
        />
        <span>Enable periodic reviews</span>
      </label>
      <p className={styles.hint}>
        Reviews run in the background at the interval below. “Review now” on a
        session works whenever the endpoint is configured, even with this off.
      </p>

      <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor="ai-interval">
          Review every
        </label>
        <div className={styles.aiIntervalRow}>
          <input
            id="ai-interval"
            className={`${styles.aiInput} ${styles.aiIntervalInput}`}
            type="number"
            min={1}
            value={intervalDraft}
            onChange={(e) => setIntervalDraft(e.target.value)}
            onBlur={commitInterval}
          />
          <span>minutes</span>
        </div>
        <p className={styles.hint}>
          Only sessions with new activity since their last review are sent. One
          bounded request per session — no streaming.
        </p>
      </div>

      <SessionReviewDepth />

      <div className={styles.aiField}>
        <p className={styles.hint}>
          The review prompt lives in{" "}
          <Link
            className={styles.nameLink}
            to={promptPath("tail_review")}
            state={location.state}
          >
            Prompts → Tail review
          </Link>
          , alongside every other prompt this app sends.
        </p>
      </div>

      <div className={styles.aiField}>
        <span className={styles.aiFieldLabel}>Excluded sessions</span>
        <p className={styles.hint}>
          Exclude a session from review via its row actions in the sidebar.
          Currently excluded:
        </p>
        {excluded === null ? (
          <p className={styles.hint}>…</p>
        ) : excluded.length === 0 ? (
          <p className={styles.hint}>No sessions are excluded.</p>
        ) : (
          <ul className={styles.aiExcludedList} aria-label="Excluded sessions">
            {excluded.map((s) => (
              <li key={s.id} className={styles.aiExcludedRow}>
                <span className={styles.aiExcludedTitle}>
                  {s.title || "(untitled)"}
                </span>
                <button
                  type="button"
                  className={styles.secBtnGhost}
                  onClick={() => void include(s.id)}
                >
                  Include
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
      {savedNote && <p className={styles.hint}>Saved.</p>}
    </section>
  );
}
