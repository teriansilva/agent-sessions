import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
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
  request_timeout: null,
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

/** Endpoint save/validation lifecycle (#394). `ok` = the /models probe succeeded against
 *  the PERSISTED config ("validated"); `error` carries the gateway's error text verbatim
 *  (#382); `incomplete` = saved but base URL or key still missing. */
type EndpointState =
  | { kind: "idle" }
  | { kind: "saving" }
  | { kind: "validating" }
  | { kind: "ok"; count: number }
  | { kind: "incomplete" }
  | { kind: "error"; message: string };

/** AI Review settings (#356 Phase 1, replaces the #357 placeholder): OpenAI-compatible
 *  endpoint config (write-only API key), model dropdown loaded through the server-side
 *  /models proxy (free-text fallback + refresh), review interval, the fully-exposed
 *  prompt with reset-to-default, and the excluded-sessions list.
 *
 *  Save semantics (#394): the endpoint section (base URL + API key) persists ONLY via
 *  the explicit "Save & validate" button — never on blur — and the save immediately
 *  validates by probing the /models proxy: success shows a confirmed state + populates
 *  the dropdown, failure shows the gateway's error verbatim (#382). The model choice
 *  auto-saves on change; interval/timeout/prompt keep their commit-on-blur behavior. */
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
  const [timeoutDraft, setTimeoutDraft] = useState(
    block.request_timeout == null ? "" : String(block.request_timeout),
  );
  const [promptDraft, setPromptDraft] = useState(block.prompt);
  const [seeded, setSeeded] = useState<AiReviewConfig | null>(null);
  if (seeded !== block) {
    // Unrelated save echoes (model, interval, timeout, prompt, enable toggle) must not
    // clobber an unsaved endpoint edit (Hermes on #396): a URL draft that is dirty
    // against BOTH the outgoing and the incoming block keeps the user's text. The
    // endpoint's own Save & validate echoes the draft back as `base_url`, so that path
    // (like the initial config load, where the draft is clean) still reseeds. The key
    // draft never reseeds — it's write-only and cleared explicitly by saveEndpoint.
    const urlDirty =
      seeded !== null &&
      urlDraft.trim() !== seeded.base_url &&
      urlDraft.trim() !== block.base_url;
    setSeeded(block);
    if (!urlDirty) setUrlDraft(block.base_url);
    setIntervalDraft(String(block.interval_minutes));
    setTimeoutDraft(block.request_timeout == null ? "" : String(block.request_timeout));
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
  // The /models probe doubles as the endpoint VALIDATION (#394/#382): success = the
  // saved base URL + key work (confirmed state, dropdown populated); failure shows the
  // gateway's error verbatim. The model field still falls back to free-text entry —
  // a listing failure never blocks configuration (#356).
  const [models, setModels] = useState<ModelsState>({ kind: "idle" });
  const [endpoint, setEndpoint] = useState<EndpointState>({ kind: "idle" });
  const probe = useCallback(async (refresh = false) => {
    setModels({ kind: "loading" });
    setEndpoint({ kind: "validating" });
    try {
      const d = await api.aiReviewModels(refresh ? { refresh: true } : undefined);
      if (d.models.length > 0) {
        setModels({ kind: "ok", models: d.models });
        setEndpoint({ kind: "ok", count: d.models.length });
      } else {
        setModels({ kind: "unsupported" });
        setEndpoint({ kind: "ok", count: 0 });
      }
    } catch (e) {
      setModels({ kind: "unsupported" });
      setEndpoint({
        kind: "error",
        message:
          e instanceof ApiError && e.message ? e.message : "Endpoint validation failed.",
      });
    }
  }, []);
  // Probe once when the panel opens with a stored, complete config (it can land after
  // mount). Saves run their own explicit probe — `probedOnce` keeps the two paths from
  // double-fetching when `configured` flips on a save echo.
  const probedOnce = useRef(false);
  useEffect(() => {
    if (!block.configured || probedOnce.current) return;
    probedOnce.current = true;
    void probe();
  }, [block.configured, probe]);

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

  // --- explicit endpoint save (#394): blur NEVER persists the URL or the key ---
  const keyEdit = keyDraft.trim();
  const endpointDirty =
    urlDraft.trim() !== block.base_url || (keyEdit !== "" && keyEdit !== KEY_MASK);
  const busy = endpoint.kind === "saving" || endpoint.kind === "validating";
  /** Persist base URL + key together, then validate immediately via the /models probe.
   *  The mask/blank key is the "unchanged" sentinel and is never sent (#356). */
  const saveEndpoint = async () => {
    if (busy) return;
    setError(null);
    const patch: Record<string, unknown> = { base_url: urlDraft.trim() };
    if (keyEdit && keyEdit !== KEY_MASK) patch.api_key = keyEdit;
    setEndpoint({ kind: "saving" });
    let next: AiReviewConfig | undefined;
    try {
      const r = (await api.setPrefs({ ai_review: patch })) as { ai_review?: AiReviewConfig };
      next = r.ai_review;
    } catch (e) {
      setEndpoint({
        kind: "error",
        message:
          e instanceof ApiError && e.status === 422 && e.message
            ? e.message
            : "Couldn’t save — please try again.",
      });
      return;
    }
    setKeyDraft(""); // write-only: the field clears once the key is stored
    probedOnce.current = true; // this save owns the probe — don't double-fetch
    if (next) {
      setBlock(next);
      if (next.configured !== ctxConfigured) refreshConfig();
    }
    if (!next?.configured) {
      setEndpoint({ kind: "incomplete" });
      setModels({ kind: "idle" });
      return;
    }
    await probe(true);
  };
  /** Explicit clear (Hermes #367): `api_key: null` is the backend's "remove the stored
   *  secret" contract — the blank field means "unchanged", so removal needs its own
   *  action. The echo flips `api_key_set` (and `configured`) to false. */
  const removeKey = () => {
    setKeyDraft("");
    setEndpoint({ kind: "idle" });
    setModels({ kind: "idle" });
    probedOnce.current = false;
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
  /** Review timeout (#391 follow-up): empty = unset (server falls back to the env var /
   *  120s default); otherwise 10–600 seconds — out-of-range reverts to the saved value,
   *  mirroring commitInterval. */
  const commitTimeout = () => {
    const v = timeoutDraft.trim();
    if (v === "") {
      if (block.request_timeout !== null) void save({ request_timeout: null });
      return;
    }
    const n = Number(v);
    if (!Number.isFinite(n) || n < 10 || n > 600) {
      setTimeoutDraft(block.request_timeout == null ? "" : String(block.request_timeout));
      return;
    }
    if (n !== block.request_timeout) void save({ request_timeout: n });
  };

  // While unsaved endpoint edits exist and no VALIDATED config backs the list, the
  // model control stays locked (#394) — the dropdown would be showing models from a
  // config the user is about to replace.
  const modelLocked = endpointDirty && endpoint.kind !== "ok";
  const modelSelect =
    models.kind === "ok" ? (
      <select
        className={styles.aiInput}
        aria-label="Model"
        value={block.model}
        disabled={modelLocked}
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
        disabled={modelLocked}
        placeholder={models.kind === "loading" ? "loading model list…" : "model id"}
        defaultValue={block.model}
        onBlur={(e) => {
          const v = e.target.value.trim();
          if (v !== block.model) void save({ model: v });
        }}
      />
    );

  // One status line under the Save button: the in-flight save/validation wins, then
  // dirty edits (any prior result describes values the user is replacing), then the
  // last validation outcome.
  const endpointNote = endpoint.kind === "saving" ? (
    <p className={styles.hint}>Saving…</p>
  ) : endpoint.kind === "validating" ? (
    <p className={styles.hint}>Validating endpoint…</p>
  ) : endpointDirty ? (
    <p className={styles.warn}>● Unsaved changes — Save applies and validates them.</p>
  ) : endpoint.kind === "ok" ? (
    <p className={styles.ok}>
      {endpoint.count > 0
        ? `✓ Endpoint validated — ${endpoint.count} model${endpoint.count === 1 ? "" : "s"} available.`
        : "✓ Endpoint saved — it doesn’t list models; enter the model id manually."}
    </p>
  ) : endpoint.kind === "error" ? (
    <p className={styles.err}>✗ {endpoint.message}</p>
  ) : endpoint.kind === "incomplete" ? (
    <p className={styles.hint}>Saved. Set both the base URL and an API key to validate.</p>
  ) : null;

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
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void saveEndpoint();
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
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void saveEndpoint();
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
            Write-only: the stored key is never shown. It is saved only by “Save &amp;
            validate” — never on blur. Use “Remove key” to delete the stored secret.
          </p>
        </div>

        <div className={styles.aiActions}>
          <button
            type="button"
            className={`${styles.secBtn} shine`}
            disabled={!endpointDirty || busy}
            onClick={() => void saveEndpoint()}
          >
            Save &amp; validate
          </button>
          {endpointNote}
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
              disabled={!block.configured || models.kind === "loading" || modelLocked}
              onClick={() => void probe(true)}
            >
              <RefreshCw size={14} />
            </button>
          </div>
          <p className={styles.hint}>
            {block.configured
              ? models.kind === "unsupported"
                ? "The endpoint doesn’t list models — enter the model id manually."
                : "Loaded from the endpoint’s /models — picking a model saves it immediately."
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

        <div className={styles.aiField}>
          <label className={styles.aiFieldLabel} htmlFor="ai-timeout">
            Review timeout
          </label>
          <div className={styles.aiIntervalRow}>
            <input
              id="ai-timeout"
              className={`${styles.aiInput} ${styles.aiIntervalInput}`}
              type="number"
              min={10}
              max={600}
              placeholder="120"
              value={timeoutDraft}
              onChange={(e) => setTimeoutDraft(e.target.value)}
              onBlur={commitTimeout}
            />
            <span>seconds</span>
          </div>
          <p className={styles.hint}>
            Hard timeout per review request (10–600). Slow local models often need
            60–180s. Leave empty to use the server default.
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
