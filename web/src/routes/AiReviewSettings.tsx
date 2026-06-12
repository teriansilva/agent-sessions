import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useConfig, useConfigRefresh } from "../app/config";
import { api, ApiError } from "../lib/api";
import type { AiReviewConfig, Session } from "../types/api";
import styles from "./Settings.module.css";

/** What the user sees in the API-key field while a key is stored. Round-tripping it back
 *  to the server means "unchanged" (the masked-sentinel contract, #356) — but we never
 *  send it: the field is cleared after a save and an empty value is simply not sent. */
const KEY_MASK = "********";

const FALLBACK: AiReviewConfig = {
  enabled: false,
  base_url: "",
  model: "",
  interval_minutes: 5,
  prompt: "",
  max_input_chars: 24000,
  api_key_set: false,
  configured: false,
  default_prompt: "",
};

type ModelsState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ok"; models: string[] }
  /** The endpoint can't list models (no /models, error, timeout) → free-text entry. */
  | { kind: "unsupported" };

/** AI Review settings (#356 Phase 1, replaces the #357 placeholder): OpenAI-compatible
 *  endpoint config (write-only API key), model dropdown loaded through the server-side
 *  /models proxy (free-text fallback + refresh), review interval, the fully-exposed
 *  prompt with reset-to-default, and the excluded-sessions list. Fields persist on
 *  commit (blur/change) via /api/prefs `ai_review` — matching the rest of Settings. */
export function AiReviewSettings() {
  const cfgBlock = useConfig()?.ai_review;
  const [block, setBlock] = useState<AiReviewConfig>(cfgBlock ?? FALLBACK);
  // Reflect the config load (it can land after mount) exactly once per change.
  const [synced, setSynced] = useState(cfgBlock);
  if (cfgBlock !== synced) {
    setSynced(cfgBlock);
    if (cfgBlock) setBlock(cfgBlock);
  }

  // Drafts for commit-on-blur fields (typing must not spam the server).
  const [urlDraft, setUrlDraft] = useState(block.base_url);
  const [keyDraft, setKeyDraft] = useState("");
  const [intervalDraft, setIntervalDraft] = useState(String(block.interval_minutes));
  const [promptDraft, setPromptDraft] = useState(block.prompt);
  const [seeded, setSeeded] = useState<AiReviewConfig | null>(null);
  if (seeded !== block) {
    setSeeded(block);
    setUrlDraft(block.base_url);
    setIntervalDraft(String(block.interval_minutes));
    setPromptDraft(block.prompt);
  }

  const [error, setError] = useState<string | null>(null);
  const [savedNote, setSavedNote] = useState(false);

  // The sidebar's Review now/exclude gating reads the shared /api/config context, which
  // is fetched once on mount (Hermes #367). When a save flips `configured` (endpoint+key
  // completed, or the key removed), refetch that context so the manual review controls
  // appear/disappear without a reload.
  const refreshConfig = useConfigRefresh();
  const ctxConfigured = cfgBlock?.configured ?? false;

  /** Persist a partial ai_review block; the echo is the server's public view. */
  const save = useCallback(
    async (partial: Record<string, unknown>) => {
      setError(null);
      try {
        const r = (await api.setPrefs({ ai_review: partial })) as { ai_review?: AiReviewConfig };
        if (r.ai_review) {
          setBlock(r.ai_review);
          if (r.ai_review.configured !== ctxConfigured) refreshConfig();
        }
        setSavedNote(true);
        setTimeout(() => setSavedNote(false), 1500);
        return true;
      } catch (e) {
        setError(
          e instanceof ApiError && e.status === 422
            ? "That value was rejected — check the endpoint URL and numbers."
            : "Couldn’t save — please try again.",
        );
        return false;
      }
    },
    [ctxConfigured, refreshConfig],
  );

  // --- model listing through the server-side proxy (key never in the browser) ---
  const [models, setModels] = useState<ModelsState>({ kind: "idle" });
  const loadModels = useCallback(
    async (refresh = false) => {
      if (!block.configured) return;
      setModels({ kind: "loading" });
      try {
        const d = await api.aiReviewModels(refresh ? { refresh: true } : undefined);
        setModels(
          d.models.length > 0 ? { kind: "ok", models: d.models } : { kind: "unsupported" },
        );
      } catch {
        // 400 (not configured) / 502 (endpoint can't list) → free-text fallback; a
        // listing failure never blocks configuration (#356).
        setModels({ kind: "unsupported" });
      }
    },
    [block.configured],
  );
  useEffect(() => {
    // Load once the endpoint becomes configured (same pattern as useSessionsList's
    // filter-change reload).
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadModels();
  }, [loadModels]);

  // --- excluded sessions (#356): row-menu opt-outs surface here for re-inclusion ---
  const [excluded, setExcluded] = useState<Session[] | null>(null);
  useEffect(() => {
    let alive = true;
    api
      .sessions({ limit: 200 })
      .then((d) => alive && setExcluded(d.sessions.filter((s) => s.review_excluded)))
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

  const commitUrl = () => {
    const v = urlDraft.trim();
    if (v !== block.base_url) void save({ base_url: v });
  };
  const commitKey = () => {
    const v = keyDraft.trim();
    if (!v || v === KEY_MASK) return; // blank/mask = unchanged — never sent
    void save({ api_key: v }).then((ok) => {
      if (ok) setKeyDraft("");
    });
  };
  /** Explicit clear (Hermes #367): `api_key: null` is the backend's "remove the stored
   *  secret" contract — the blank field means "unchanged", so removal needs its own
   *  action. The echo flips `api_key_set` (and `configured`) to false. */
  const removeKey = () => {
    setKeyDraft("");
    void save({ api_key: null });
  };
  const commitInterval = () => {
    const n = Number(intervalDraft);
    if (!Number.isInteger(n) || n < 1) {
      setIntervalDraft(String(block.interval_minutes));
      return;
    }
    if (n !== block.interval_minutes) void save({ interval_minutes: n });
  };

  const modelSelect =
    models.kind === "ok" ? (
      <select
        className={styles.aiInput}
        aria-label="Model"
        value={block.model}
        onChange={(e) => void save({ model: e.target.value })}
      >
        {!block.model && <option value="">— pick a model —</option>}
        {block.model && !models.models.includes(block.model) && (
          <option value={block.model}>{block.model}</option>
        )}
        {models.models.map((m) => (
          <option key={m} value={m}>
            {m}
          </option>
        ))}
      </select>
    ) : (
      <input
        className={styles.aiInput}
        aria-label="Model"
        type="text"
        spellCheck={false}
        placeholder={models.kind === "loading" ? "loading model list…" : "model id"}
        defaultValue={block.model}
        onBlur={(e) => {
          const v = e.target.value.trim();
          if (v !== block.model) void save({ model: v });
        }}
      />
    );

  return (
    <>
      <section className={styles.section} aria-labelledby="ai-review-h">
        <h2 id="ai-review-h">AI session review</h2>
        <p className={styles.hint}>
          An OpenAI-compatible endpoint reviews your sessions and produces a one-line
          summary, a title, and an intervention flag per session. The API key is stored
          server-side and never sent to the browser.
        </p>
        {error && <p className={styles.err}>{error}</p>}

        <label className={styles.aiToggle}>
          <input
            type="checkbox"
            checked={block.enabled}
            onChange={(e) => void save({ enabled: e.currentTarget.checked })}
          />
          <span>Enable periodic reviews</span>
        </label>
        <p className={styles.hint}>
          The background loop ships in the next phase — manual “Review now” works as soon
          as the endpoint below is configured.
        </p>

        <div className={styles.aiField}>
          <label className={styles.aiFieldLabel} htmlFor="ai-base-url">
            Endpoint base URL (OpenAI-compatible)
          </label>
          <input
            id="ai-base-url"
            className={styles.aiInput}
            type="url"
            spellCheck={false}
            placeholder="https://ai.example.io/v1"
            value={urlDraft}
            onChange={(e) => setUrlDraft(e.target.value)}
            onBlur={commitUrl}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                commitUrl();
              }
            }}
          />
        </div>

        <div className={styles.aiField}>
          <label className={styles.aiFieldLabel} htmlFor="ai-api-key">
            API key
            {block.api_key_set && <span className={styles.aiKeyBadge}>set</span>}
          </label>
          <div className={styles.aiModelRow}>
            <input
              id="ai-api-key"
              className={styles.aiInput}
              type="password"
              autoComplete="off"
              spellCheck={false}
              placeholder={block.api_key_set ? `${KEY_MASK} (write-only)` : "sk-…"}
              value={keyDraft}
              onChange={(e) => setKeyDraft(e.target.value)}
              onBlur={commitKey}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  commitKey();
                }
              }}
            />
            {block.api_key_set && (
              <button
                type="button"
                className={styles.secBtnGhost}
                onClick={removeKey}
                title="Remove the stored API key"
              >
                Remove key
              </button>
            )}
          </div>
          <p className={styles.hint}>
            Write-only: the stored key is never shown. Enter a new value to replace it, or
            use “Remove key” to delete the stored secret.
          </p>
        </div>

        <div className={styles.aiField}>
          <label className={styles.aiFieldLabel} htmlFor="ai-model">
            Model
          </label>
          <div className={styles.aiModelRow} id="ai-model">
            {modelSelect}
            <button
              type="button"
              className={styles.secBtnGhost}
              aria-label="Refresh model list"
              title="Refresh model list"
              disabled={!block.configured || models.kind === "loading"}
              onClick={() => void loadModels(true)}
            >
              <RefreshCw size={14} />
            </button>
          </div>
          <p className={styles.hint}>
            {block.configured
              ? models.kind === "unsupported"
                ? "The endpoint doesn’t list models — enter the model id manually."
                : "Loaded from the endpoint’s /models once the base URL + key validate."
              : "Set the base URL and API key first to load the model list."}
          </p>
        </div>

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
            Only sessions with new activity since their last review are sent. One bounded
            request per session — no streaming.
          </p>
        </div>
        {savedNote && <p className={styles.hint}>Saved.</p>}
      </section>

      <section className={styles.section} aria-labelledby="ai-prompt-h">
        <h2 id="ai-prompt-h">Review prompt</h2>
        <textarea
          className={`${styles.aiInput} ${styles.aiPrompt}`}
          aria-label="Review prompt"
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
      </section>

      <section className={styles.section} aria-labelledby="ai-excluded-h">
        <h2 id="ai-excluded-h">Excluded sessions</h2>
        <p className={styles.hint}>
          Exclude a session from review via its row actions in the sidebar. Currently
          excluded:
        </p>
        {excluded === null ? (
          <p className={styles.hint}>…</p>
        ) : excluded.length === 0 ? (
          <p className={styles.hint}>No sessions are excluded.</p>
        ) : (
          <ul className={styles.aiExcludedList} aria-label="Excluded sessions">
            {excluded.map((s) => (
              <li key={s.id} className={styles.aiExcludedRow}>
                <span className={styles.aiExcludedTitle}>{s.title || "(untitled)"}</span>
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
      </section>
    </>
  );
}
