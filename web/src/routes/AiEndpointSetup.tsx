import { Lock, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useConfig, useConfigRefresh } from "../app/config";
import {
  ENDPOINT_LED_CLASS,
  ENDPOINT_LED_LABEL,
  endpointOrigin,
  recordEndpointCheck,
  useEndpointLed,
} from "../lib/aiEndpointStatus";
import {
  ackCovers,
  CUSTOM_ID,
  CUSTOM_ROW,
  groups,
  isHosted,
  isLoopback,
  needsConsent,
  recipient,
} from "../lib/aiProviders";
import { api, ApiError } from "../lib/api";
import type {
  AiModelMode,
  AiProvider,
  AiReviewConfig,
  AiThinkingLevel,
} from "../types/api";
import styles from "./Settings.module.css";

/** What the key field shows while a key is stored. Blank and the mask mean "keep the stored
 *  key" server-side (#356), so neither is ever sent as a key. */
const KEY_MASK = "********";

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

/** The provider table (#1657) comes from the server — `GET /api/ai-review/providers` — and is
 *  never copied here. Cached for the page's life: it is static per build. A failed load leaves
 *  Custom alone, which is exactly the pre-#1657 form. */
let catalogCache: AiProvider[] | null = null;

const THINKING_LEVELS: readonly { id: AiThinkingLevel; label: string }[] = [
  { id: "auto", label: "Auto" },
  { id: "off", label: "Off" },
  { id: "low", label: "Low" },
  { id: "medium", label: "Medium" },
  { id: "high", label: "High" },
];

const TIMEOUT_MIN = 10;
const TIMEOUT_MAX = 600;

/** The connection exactly as it was when Test or Save was pressed. `ack` = the operator ticked
 *  the egress acknowledgement for THIS draft's recipient (#1671). */
type Snapshot = {
  base_url: string;
  key: string;
  provider: string;
  ack: boolean;
};

type Outcome =
  | { kind: "idle" }
  | { kind: "testing" }
  | { kind: "saving" }
  /** The endpoint answered with a model list. `saved` = this was a Save, not a Test. */
  | { kind: "listed"; count: number; saved: boolean }
  /** The endpoint answered but can't list models — the URL and key can still be right. */
  | { kind: "unlisted"; saved: boolean }
  | { kind: "untested" }
  | {
      kind: "failed";
      message: string;
      during: "test" | "save";
      /** A gateway that is down right now may be saved on purpose. A request the SERVER refused
       *  (a malformed URL, a key for another host) may not — it would be refused again. */
      canSaveAnyway: boolean;
    };

type Models =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ok"; models: string[] }
  | { kind: "unlisted" }
  | { kind: "error"; message: string };

type ModelSave =
  | { kind: "idle" }
  | { kind: "saving" }
  | { kind: "saved" }
  | { kind: "error"; message: string };

const isNewKey = (v: string) => v.trim() !== "" && v.trim() !== KEY_MASK;
const timeoutText = (t: number | null) => (t == null ? "" : String(t));

/** `null` = empty (server default); `undefined` = not a valid timeout. */
function parseTimeout(v: string): number | null | undefined {
  const s = v.trim();
  if (s === "") return null;
  const n = Number(s);
  return Number.isFinite(n) && n >= TIMEOUT_MIN && n <= TIMEOUT_MAX
    ? n
    : undefined;
}

function hostOf(url: string): string {
  try {
    return new URL(url.trim()).host;
  } catch {
    return "";
  }
}

function reason(e: unknown, fallback: string): string {
  return e instanceof ApiError && e.message ? e.message : fallback;
}

function failure(e: unknown, during: "test" | "save"): Outcome {
  return {
    kind: "failed",
    message: reason(e, "The endpoint couldn’t be checked."),
    during,
    canSaveAnyway: !(e instanceof ApiError && e.status === 422),
  };
}

/** Endpoint & model (#956): one OpenAI-compatible connection for every AI feature, set up in
 *  two steps with two saves, each at the foot of the card it saves.
 *
 *  **01 Connection** — base URL and key. *Save connection* checks the DRAFT through the server
 *  first and stores it only if the endpoint answers; *Save without testing* stays available when
 *  the check failed because the gateway is unreachable. Moving to another host asks for that
 *  host's key: the stored key is only ever sent to the origin it was saved for (enforced by the
 *  server on both the test route and the save, this page only mirrors it).
 *
 *  **02 Model** — model and request timeout, saved by *Save model*. Locked, and says why, while
 *  the connection has unsaved edits: its list belongs to the saved endpoint.
 *
 *  **Stale responses.** Every check and save carries a generation; editing the URL or key,
 *  starting another action, or unmounting bumps it, and a response for an older generation is
 *  dropped — it cannot start a save, clean newer edits, publish a model list or write the LED.
 *  A save the server already ACCEPTED is different: it stays saved, and its echo is adopted as
 *  the saved state while newer edits stay dirty.
 *
 *  **Draft ownership.** A field follows the saved value only while the operator has not edited
 *  it. That is tracked by an edit revision, never by comparing values: an edit back to the
 *  previous saved value while a save is in flight is still the operator's newest intent, and an
 *  equality check would let the accepted echo overwrite it and mark the form clean (Hermes on
 *  #960).
 *
 *  The key field keeps the #834 reveal-to-edit design: with a key stored, a plain visit renders
 *  a readout, not an input, so a password manager has nothing to fill. */
export function AiEndpointSetup({
  variant = "settings",
}: {
  variant?: "settings" | "wizard";
}) {
  const idp = useId();
  const cfgBlock = useConfig()?.ai_review;
  const refreshConfig = useConfigRefresh();
  const [block, setBlock] = useState<AiReviewConfig>(cfgBlock ?? FALLBACK);
  const [synced, setSynced] = useState(cfgBlock);
  if (cfgBlock !== synced) {
    setSynced(cfgBlock);
    if (cfgBlock) setBlock(cfgBlock);
  }
  const savedOrigin = endpointOrigin(block.base_url);
  const led = useEndpointLed(block);

  // ---- 01 Connection -------------------------------------------------------------------
  const [urlDraft, setUrlDraft] = useState(block.base_url);
  const [keyDraft, setKeyDraft] = useState("");
  const [keyEditing, setKeyEditing] = useState(false);
  // A newly SAVED url replaces the draft only while the operator has not edited it: an echo must
  // never overwrite an edit in progress (Hermes on #396), including one back to the old value.
  const urlRev = useRef(0);
  const [urlEdited, setUrlEdited] = useState(false);
  const [seededUrl, setSeededUrl] = useState(block.base_url);
  if (seededUrl !== block.base_url) {
    if (!urlEdited) setUrlDraft(block.base_url);
    setSeededUrl(block.base_url);
  }
  // ---- provider (#1657) -----------------------------------------------------------------
  const [catalog, setCatalog] = useState<AiProvider[]>(
    catalogCache ?? [CUSTOM_ROW],
  );
  useEffect(() => {
    if (catalogCache) return;
    let live = true;
    // Any failure, even a synchronous one, leaves Custom alone: today's form.
    Promise.resolve()
      .then(() => api.aiProviders())
      .then((d) => {
        if (!live || !Array.isArray(d?.providers) || d.providers.length === 0)
          return;
        catalogCache = d.providers;
        setCatalog(d.providers);
      })
      .catch(() => {
        /* Custom alone: today's form */
      });
    return () => {
      live = false;
    };
  }, []);
  const savedProvider = block.provider ?? CUSTOM_ID;
  const [providerDraft, setProviderDraft] = useState(savedProvider);
  const providerRev = useRef(0);
  const [providerEdited, setProviderEdited] = useState(false);
  const [seededProvider, setSeededProvider] = useState(savedProvider);
  if (seededProvider !== savedProvider) {
    if (!providerEdited) setProviderDraft(savedProvider);
    setSeededProvider(savedProvider);
  }
  const row: AiProvider = catalog.find((r) => r.id === providerDraft) ?? {
    ...CUSTOM_ROW,
    id: providerDraft,
  };
  const hosted = isHosted(row);

  // ---- egress acknowledgement (#1671) -----------------------------------------------------
  // Feedback only: the server binds consent to the lock-current provider + origin on every save,
  // test and send. A stored ack that names this draft's recipient covers it (a `legacy` one too);
  // any change of provider or URL unticks an operator tick, so consent never carries over.
  const [ackTicked, setAckTicked] = useState(false);
  const draftNeedsConsent = needsConsent(urlDraft);
  const storedAckCovers = ackCovers(block.egress_ack, providerDraft, urlDraft);
  const ackOk = !draftNeedsConsent || storedAckCovers || ackTicked;

  /** The live drafts, readable from an async continuation (#836). */
  const draftRef = useRef<Snapshot>({
    base_url: urlDraft,
    key: keyDraft,
    provider: providerDraft,
    ack: ackTicked,
  });
  useEffect(() => {
    draftRef.current = {
      base_url: urlDraft,
      key: keyDraft,
      provider: providerDraft,
      ack: ackTicked,
    };
  }, [urlDraft, keyDraft, providerDraft, ackTicked]);

  const hostChanged =
    block.api_key_set &&
    savedOrigin !== null &&
    urlDraft.trim() !== "" &&
    endpointOrigin(urlDraft) !== savedOrigin;
  // A key draft keeps its field on screen until it is saved, cancelled or removed (Hermes on
  // #960): an accepted save that flips `api_key_set` must not hide a NEWER key typed while it
  // was in flight — hiding it would also drop it from `keyEdit`, marking the form clean.
  const keyInputShown =
    !block.api_key_set || keyEditing || hostChanged || keyDraft !== "";
  const keyEdit = keyInputShown ? keyDraft.trim() : "";
  // A tick for a recipient no stored ack covers is a change to save: the saved endpoint whose
  // acknowledgement could not be recorded (#1671) is fixed by ticking and saving it once.
  const connectionDirty =
    urlDraft.trim() !== block.base_url ||
    isNewKey(keyEdit) ||
    providerDraft !== savedProvider ||
    (ackTicked && !storedAckCovers && draftNeedsConsent);
  const keyMissing = hostChanged && !isNewKey(keyEdit);
  // Test, model discovery, Save and Save without testing all wait for the acknowledgement.
  const canCheck = urlDraft.trim() !== "" && !keyMissing && ackOk;

  const [outcome, setOutcome] = useState<Outcome>({ kind: "idle" });
  const busy = outcome.kind === "testing" || outcome.kind === "saving";
  /** Write ownership, separate from draft feedback (Hermes on #960). Every mutation on this page —
   *  save connection, save without testing, remove key, save model — holds it for its whole
   *  request, and no other write may start meanwhile. Edits stay possible (they only retire a
   *  verdict), but two writes can never overlap, so their responses can never arrive reversed
   *  and let an older echo replace a newer save. The ref closes the same-tick double-click gap
   *  the state alone would leave. */
  const [writing, setWriting] = useState(false);
  const writingRef = useRef(false);
  const beginWrite = () => {
    if (writingRef.current) return false;
    writingRef.current = true;
    setWriting(true);
    return true;
  };
  const endWrite = () => {
    writingRef.current = false;
    setWriting(false);
  };
  const gen = useRef(0);
  useEffect(() => {
    const g = gen;
    return () => {
      g.current += 1; // unmounting drops every response still in flight
    };
  }, []);
  /** An edit retires any verdict — it described the values being replaced — and orphans any
   *  response still in flight. */
  const onDraftEdit = () => {
    gen.current += 1;
    setOutcome((o) => (o.kind === "idle" ? o : { kind: "idle" }));
  };

  // ---- 02 Model ------------------------------------------------------------------------
  const [models, setModels] = useState<Models>({ kind: "idle" });
  const modelsGen = useRef(0);
  const listedFor = useRef<string | null>(null);
  const [modelDraft, setModelDraft] = useState(block.model);
  const [timeoutDraft, setTimeoutDraft] = useState(
    timeoutText(block.request_timeout),
  );
  const [seededModel, setSeededModel] = useState({
    model: block.model,
    timeout: block.request_timeout,
  });
  const modelRev = useRef(0);
  const timeoutRev = useRef(0);
  const [modelEdited, setModelEdited] = useState(false);
  const [timeoutEdited, setTimeoutEdited] = useState(false);
  if (
    seededModel.model !== block.model ||
    seededModel.timeout !== block.request_timeout
  ) {
    if (!modelEdited) setModelDraft(block.model);
    if (!timeoutEdited) setTimeoutDraft(timeoutText(block.request_timeout));
    setSeededModel({ model: block.model, timeout: block.request_timeout });
  }
  // Model "Auto" and the thinking level (#1657), for the SAVED provider: this card configures the
  // saved connection, like the model list it shows.
  const savedRow: AiProvider = catalog.find((r) => r.id === savedProvider) ?? {
    ...CUSTOM_ROW,
    id: savedProvider,
  };
  const savedHosted = isHosted(savedRow);
  const savedMode: AiModelMode = block.model_mode ?? "specific";
  const savedThinking: AiThinkingLevel = block.thinking ?? "auto";
  const [modeDraft, setModeDraft] = useState<AiModelMode>(savedMode);
  const [thinkingDraft, setThinkingDraft] =
    useState<AiThinkingLevel>(savedThinking);
  const [seededChoice, setSeededChoice] = useState({
    mode: savedMode,
    thinking: savedThinking,
  });
  const choiceRev = useRef(0);
  const [choiceEdited, setChoiceEdited] = useState(false);
  if (
    seededChoice.mode !== savedMode ||
    seededChoice.thinking !== savedThinking
  ) {
    if (!choiceEdited) {
      setModeDraft(savedMode);
      setThinkingDraft(savedThinking);
    }
    setSeededChoice({ mode: savedMode, thinking: savedThinking });
  }
  const autoModel = savedHosted && modeDraft === "auto";
  /** Specific with nothing picked would send every request without a model (Hermes on #1672);
   *  the server refuses it too. */
  const specificMissing = savedHosted && modeDraft === "specific" && !modelDraft.trim();
  const parsedTimeout = parseTimeout(timeoutDraft);
  const modelDirty =
    (!autoModel && modelDraft.trim() !== block.model) ||
    (savedHosted && modeDraft !== savedMode) ||
    thinkingDraft !== savedThinking ||
    (parsedTimeout !== undefined && parsedTimeout !== block.request_timeout) ||
    parsedTimeout === undefined;
  // The model list is an outbound call too: locked while the SAVED endpoint is not allowed (#1671).
  const savedAllowed = block.egress_allowed !== false;
  const modelLocked = !block.configured || connectionDirty || !savedAllowed;
  const [modelSave, setModelSave] = useState<ModelSave>({ kind: "idle" });

  const loadModels = useCallback(
    async (refresh: boolean) => {
      const g = ++modelsGen.current;
      const origin = endpointOrigin(block.base_url);
      const keySet = block.api_key_set;
      setModels({ kind: "loading" });
      try {
        const d = await api.aiReviewModels(
          refresh ? { refresh: true } : undefined,
        );
        if (g !== modelsGen.current) return;
        setModels(
          d.models.length > 0
            ? { kind: "ok", models: d.models }
            : { kind: "unlisted" },
        );
        recordEndpointCheck(
          origin,
          keySet,
          d.models.length > 0 ? "up" : "degraded",
        );
      } catch (e) {
        if (g !== modelsGen.current) return;
        setModels({
          kind: "error",
          message: reason(e, "The saved endpoint couldn’t be checked."),
        });
        recordEndpointCheck(origin, keySet, "down");
      }
    },
    [block.base_url, block.api_key_set],
  );
  useEffect(() => {
    const g = modelsGen;
    return () => {
      g.current += 1;
    };
  }, []);

  // List the SAVED connection once per connection, quietly, whenever it is clean — that listing
  // is also what the LED reports on a plain visit.
  const connectionKey = `${block.base_url}|${block.api_key_set}`;
  useEffect(() => {
    if (
      !block.configured ||
      connectionDirty ||
      !savedAllowed ||
      listedFor.current === connectionKey
    )
      return;
    listedFor.current = connectionKey;
    void Promise.resolve().then(() => loadModels(false));
  }, [block.configured, connectionDirty, savedAllowed, connectionKey, loadModels]);

  // ---- actions -------------------------------------------------------------------------
  const snapshot = (): Snapshot => ({
    base_url: urlDraft.trim(),
    key: isNewKey(keyEdit) ? keyEdit : "",
    provider: providerDraft,
    ack: ackTicked && !storedAckCovers,
  });

  const check = (snap: Snapshot) =>
    api.testAiEndpoint({
      base_url: snap.base_url,
      ...(snap.key ? { api_key: snap.key } : {}),
      // Custom is the route's default: a Custom draft's request stays exactly what it was.
      ...(snap.provider !== CUSTOM_ID ? { provider: snap.provider } : {}),
      ...(snap.ack ? { egress_acknowledged: true } : {}),
    });

  const test = async () => {
    if (busy || !canCheck) return;
    const snap = snapshot();
    const g = ++gen.current;
    setOutcome({ kind: "testing" });
    try {
      const r = await check(snap);
      if (g !== gen.current) return;
      setOutcome(
        r.listing === "ok" && r.models.length > 0
          ? { kind: "listed", count: r.models.length, saved: false }
          : { kind: "unlisted", saved: false },
      );
    } catch (e) {
      if (g !== gen.current) return;
      setOutcome(failure(e, "test"));
    }
  };

  /** Store `snap`. Whatever happened since it was taken, an ACCEPTED save is adopted as the
   *  saved state; only a still-current generation may clean the form and publish models. */
  const persist = async (
    snap: Snapshot,
    g: number,
    verdict:
      | { kind: "listed"; count: number; saved: true }
      | { kind: "unlisted"; saved: true }
      | { kind: "untested" },
    listed: string[],
  ) => {
    const patch: Record<string, unknown> = { base_url: snap.base_url };
    // Only a CHANGED provider is sent, so an unchanged Custom save is exactly today's patch.
    if (snap.provider !== savedProvider) patch.provider = snap.provider;
    if (snap.key) patch.api_key = snap.key;
    if (snap.ack) patch.egress_acknowledged = true;
    const rev = urlRev.current;
    const pRev = providerRev.current;
    if (!beginWrite()) {
      if (g === gen.current) setOutcome({ kind: "idle" });
      return;
    }
    let next: AiReviewConfig | undefined;
    try {
      const r = (await api.setPrefs({ ai_review: patch })) as {
        ai_review?: AiReviewConfig;
      };
      next = r.ai_review;
    } catch (e) {
      if (g === gen.current) setOutcome(failure(e, "save"));
      return;
    } finally {
      endWrite();
    }
    if (next) {
      if (urlRev.current === rev) {
        // Not edited since it was submitted: the draft IS what was saved, so it follows the echo.
        setUrlDraft(next.base_url);
        setUrlEdited(false);
      }
      if (providerRev.current === pRev) {
        setProviderDraft(next.provider ?? CUSTOM_ID);
        setProviderEdited(false);
        setAckTicked(false); // the stored ack now speaks for this recipient
      }
      setBlock(next);
      refreshConfig();
      recordEndpointCheck(
        endpointOrigin(next.base_url),
        next.api_key_set,
        verdict.kind === "listed" ? "up" : "degraded",
      );
    }
    if (g !== gen.current) return; // newer edits stay dirty; they need their own Save
    const d = draftRef.current;
    if (d.base_url.trim() === snap.base_url && d.key.trim() === snap.key) {
      setKeyDraft("");
      setKeyEditing(false);
    }
    setOutcome(verdict);
    if (next) {
      modelsGen.current += 1; // a listing of the previous connection must not land now
      if (verdict.kind === "listed") {
        setModels({ kind: "ok", models: listed });
        listedFor.current = `${next.base_url}|${next.api_key_set}`;
      } else if (verdict.kind === "unlisted") {
        setModels({ kind: "unlisted" });
        listedFor.current = `${next.base_url}|${next.api_key_set}`;
      } else {
        setModels({ kind: "idle" });
        listedFor.current = null; // untested: the effect lists the saved connection
      }
    }
  };

  const saveConnection = async () => {
    if (busy || writingRef.current || !canCheck || !connectionDirty) return;
    const snap = snapshot();
    const g = ++gen.current;
    setOutcome({ kind: "saving" });
    let r: { models: string[]; listing: "ok" | "unsupported" };
    try {
      r = await check(snap);
    } catch (e) {
      if (g === gen.current) setOutcome(failure(e, "save"));
      return;
    }
    // Edited, re-pressed or unmounted while the check ran: this snapshot is no longer what the
    // operator is saving, so nothing is saved.
    if (g !== gen.current) return;
    await persist(
      snap,
      g,
      r.listing === "ok" && r.models.length > 0
        ? { kind: "listed", count: r.models.length, saved: true }
        : { kind: "unlisted", saved: true },
      r.models,
    );
  };

  const saveWithoutTesting = async () => {
    if (busy || writingRef.current || !canCheck) return;
    const snap = snapshot();
    const g = ++gen.current;
    setOutcome({ kind: "saving" });
    await persist(snap, g, { kind: "untested" }, []);
  };

  /** `api_key: null` is the backend's "remove the stored secret" — blank means "unchanged". */
  const removeKey = async () => {
    if (!beginWrite()) return;
    gen.current += 1;
    setKeyDraft("");
    setKeyEditing(false);
    setOutcome({ kind: "idle" });
    try {
      const r = (await api.setPrefs({ ai_review: { api_key: null } })) as {
        ai_review?: AiReviewConfig;
      };
      if (r.ai_review) {
        setBlock(r.ai_review);
        refreshConfig();
      }
      modelsGen.current += 1;
      setModels({ kind: "idle" });
      listedFor.current = null;
    } catch (e) {
      setOutcome(failure(e, "save"));
    } finally {
      endWrite();
    }
  };

  const saveModel = async () => {
    if (modelLocked || writingRef.current) return;
    if (parsedTimeout === undefined) {
      setModelSave({
        kind: "error",
        message: `Request timeout must be ${TIMEOUT_MIN}–${TIMEOUT_MAX} seconds, or empty for the server default.`,
      });
      return;
    }
    if (!beginWrite()) return;
    setModelSave({ kind: "saving" });
    const revs = { model: modelRev.current, timeout: timeoutRev.current };
    try {
      const choice = choiceRev.current;
      const r = (await api.setPrefs({
        ai_review: {
          // Auto sends no model: the server resolves the provider's recommended one.
          model: autoModel ? "" : modelDraft.trim(),
          request_timeout: parsedTimeout,
          // Sent only when changed: an untouched Save model is exactly today's patch.
          ...(savedHosted && modeDraft !== savedMode
            ? { model_mode: modeDraft }
            : {}),
          ...(thinkingDraft !== savedThinking
            ? { thinking: thinkingDraft }
            : {}),
        },
      })) as { ai_review?: AiReviewConfig };
      const next = r.ai_review;
      if (next) {
        // A field edited while the save was in flight keeps its draft, even when that edit went
        // back to the value this save replaced; an untouched one follows the echo.
        if (modelRev.current === revs.model) {
          setModelDraft(next.model);
          setModelEdited(false);
        }
        if (timeoutRev.current === revs.timeout) {
          setTimeoutDraft(timeoutText(next.request_timeout));
          setTimeoutEdited(false);
        }
        if (choiceRev.current === choice) {
          setModeDraft(next.model_mode ?? "specific");
          setThinkingDraft(next.thinking ?? "auto");
          setChoiceEdited(false);
        }
        setBlock(next);
        refreshConfig();
      }
      setModelSave({ kind: "saved" });
    } catch (e) {
      setModelSave({
        kind: "error",
        message: reason(e, "Couldn’t save — please try again."),
      });
    } finally {
      endWrite();
    }
  };

  // ---- provider + URL edits --------------------------------------------------------------
  /** Any change of recipient unticks the operator's acknowledgement (#1671). */
  const editUrl = (v: string) => {
    setUrlDraft(v);
    urlRev.current += 1;
    setUrlEdited(true);
    setAckTicked(false);
    onDraftEdit();
  };
  const chooseProvider = (r: AiProvider) => {
    if (r.id === providerDraft) return;
    setProviderDraft(r.id);
    providerRev.current += 1;
    setProviderEdited(true);
    // A hosted row and a preset bring their own URL; Custom keeps a typed one, but never a
    // hosted vendor's (its rules would not follow it).
    const nextUrl = r.base_url || (hosted ? "" : urlDraft);
    if (nextUrl !== urlDraft) editUrl(nextUrl);
    else {
      setAckTicked(false);
      onDraftEdit();
    }
  };
  const who = recipient(row, urlDraft);

  // ---- copy ----------------------------------------------------------------------------
  const draftHost = hostOf(urlDraft) || urlDraft.trim();
  const connectionNote: { cls: string; text: string } | null =
    outcome.kind === "testing"
      ? {
          cls: styles.hint,
          text: `Testing ${draftHost}… nothing is saved yet.`,
        }
      : outcome.kind === "saving"
        ? { cls: styles.hint, text: `Testing ${draftHost}, then saving…` }
        : outcome.kind === "failed"
          ? {
              cls: styles.err,
              text:
                outcome.during === "save"
                  ? `✗ Not saved — ${outcome.message}`
                  : `✗ ${outcome.message}`,
            }
          : outcome.kind === "listed"
            ? {
                cls: styles.ok,
                text: outcome.saved
                  ? `✓ Connected — ${outcome.count} model${outcome.count === 1 ? "" : "s"} available.`
                  : `✓ The endpoint answered — ${outcome.count} model${outcome.count === 1 ? "" : "s"} available. Not saved yet.`,
              }
            : outcome.kind === "unlisted"
              ? {
                  cls: styles.ok,
                  text: outcome.saved
                    ? "✓ Saved — this endpoint doesn’t list models; type the model id below."
                    : "✓ The endpoint answered, but doesn’t list models. Not saved yet.",
                }
              : outcome.kind === "untested"
                ? { cls: styles.warn, text: "Saved without testing." }
                : !ackOk
                  ? {
                      cls: styles.warn,
                      text: "Tick the acknowledgement to test or save. Nothing is sent before.",
                    }
                  : keyMissing
                    ? {
                        cls: styles.warn,
                        text: `Enter the API key for ${draftHost} to test or save.`,
                      }
                    : connectionDirty
                      ? {
                          cls: styles.warn,
                          text: "● Unsaved — Save connection tests it, then stores it.",
                        }
                      : !block.configured
                        ? {
                            cls: styles.hint,
                            text: "Enter the base URL and an API key, then Save connection.",
                          }
                        : models.kind === "loading"
                          ? {
                              cls: styles.hint,
                              text: "Checking the saved connection…",
                            }
                          : models.kind === "error"
                            ? { cls: styles.err, text: `✗ ${models.message}` }
                            : models.kind === "ok"
                              ? {
                                  cls: styles.ok,
                                  text: `✓ Connected — ${models.models.length} model${models.models.length === 1 ? "" : "s"} available.`,
                                }
                              : models.kind === "unlisted"
                                ? {
                                    cls: styles.ok,
                                    text: "✓ Saved — this endpoint doesn’t list models.",
                                  }
                                : null;

  const active = block.model || "no active model yet";
  const timeoutLabel =
    block.request_timeout == null
      ? "server-default timeout"
      : `${block.request_timeout} s timeout`;
  const modelNote: { cls: string; text: string } =
    modelSave.kind === "error"
      ? { cls: styles.err, text: `✗ ${modelSave.message}` }
      : modelSave.kind === "saving" || (writing && modelSave.kind !== "saved")
        ? { cls: styles.hint, text: "Saving…" }
        : !block.configured
          ? { cls: styles.hint, text: "Save a connection first." }
          : specificMissing && !modelLocked
            ? { cls: styles.warn, text: "Pick a model, or switch back to Auto." }
          : modelLocked
            ? { cls: styles.hint, text: `Active: ${active}` }
            : parsedTimeout === undefined
              ? {
                  cls: styles.warn,
                  text: `Request timeout must be ${TIMEOUT_MIN}–${TIMEOUT_MAX} seconds, or empty.`,
                }
              : modelDirty
                ? {
                    cls: styles.warn,
                    text: `● Unsaved — active is still ${active}.`,
                  }
                : modelSave.kind === "saved"
                  ? {
                      cls: styles.ok,
                      text: `✓ Model saved — active: ${active} · ${timeoutLabel}. The next AI request uses it.`,
                    }
                  : {
                      cls: styles.hint,
                      text: `Active: ${active} · ${timeoutLabel}`,
                    };

  const cardClass = `${variant === "settings" ? styles.section : styles.endpointPlain} ${styles.endpointCard}`;
  const enterSaves = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault();
      void saveConnection();
    }
  };

  return (
    <>
      {variant === "settings" && (
        <div className={styles.endpointStrip} data-testid="endpoint-status">
          <span>
            Endpoint <b>{hostOf(block.base_url) || "not set"}</b>
          </span>
          <span>
            Key <b>{block.api_key_set ? "stored" : "none"}</b>
          </span>
          <span>
            Model <b>{block.model || "none"}</b>
          </span>
          <span className={styles.endpointStripState}>
            <span
              className={`hud-led ${ENDPOINT_LED_CLASS[led]}`}
              aria-hidden="true"
            />
            {ENDPOINT_LED_LABEL[led]}
          </span>
        </div>
      )}

      <section className={cardClass} aria-labelledby={`${idp}-connection-h`}>
        <h2 id={`${idp}-connection-h`}>
          <span className={styles.stepNum} aria-hidden="true">
            01
          </span>{" "}
          Connection
        </h2>
        <p className={styles.hint}>
          Where AI requests go, and the key they carry. One provider powers
          every AI feature: HQ, session review and recaps, handoff briefs,
          auto-sort, the mission-control scan, the orchestrator and mission
          turns. The key is stored server-side and never sent to the browser.
        </p>

        <div className={styles.aiField}>
          <span className={styles.aiFieldLabel} id={`${idp}-provider-h`}>
            Provider
          </span>
          {groups(catalog).map((g) => (
            <div
              key={g.title}
              className={styles.aiProviderGroup}
              role="radiogroup"
              aria-label={g.title}
            >
              <span className={styles.aiProviderGroupTitle} aria-hidden="true">
                {g.title}
              </span>
              <div className={styles.aiPresets}>
                {g.rows.map((r) => (
                  <button
                    key={r.id}
                    type="button"
                    role="radio"
                    aria-checked={r.id === providerDraft}
                    className={styles.secBtnGhost}
                    data-provider={r.id}
                    onClick={() => chooseProvider(r)}
                  >
                    {r.label}
                  </button>
                ))}
              </div>
            </div>
          ))}
          {variant === "wizard" && !hosted && (
            <p className={styles.hint}>
              <code>localhost</code> is the machine BattleLab runs on, not this
              browser&apos;s device. A preset fills in the server&apos;s usual
              address — check the port, then Save connection. A local server
              that doesn&apos;t check keys still needs one here: type any text,
              like <code>local</code>.
            </p>
          )}
        </div>

        {hosted ? (
          <div className={styles.aiField}>
            <span className={styles.aiFieldLabel}>Endpoint</span>
            <p
              className={styles.aiFixedEndpoint}
              data-testid="ai-fixed-endpoint"
            >
              {hostOf(row.base_url)} · fixed by BattleLab
            </p>
          </div>
        ) : (
          <div className={styles.aiField}>
            <label className={styles.aiFieldLabel} htmlFor={`${idp}-base-url`}>
              Base URL (OpenAI-compatible)
            </label>
            <input
              id={`${idp}-base-url`}
              className={styles.aiInput}
              type="url"
              spellCheck={false}
              placeholder="https://ai.example.io/v1"
              value={urlDraft}
              onChange={(e) => editUrl(e.target.value)}
              onKeyDown={enterSaves}
            />
          </div>
        )}

        <div className={styles.aiField}>
          <label
            className={styles.aiFieldLabel}
            {...(keyInputShown ? { htmlFor: `${idp}-api-key` } : {})}
          >
            API key
            {block.api_key_set && !hostChanged && (
              <span className={styles.aiKeyBadge}>set</span>
            )}
          </label>
          <div className={styles.aiKeyRow}>
            {keyInputShown ? (
              <input
                id={`${idp}-api-key`}
                className={styles.aiInput}
                type="password"
                // The vendor opt-outs 1Password / LastPass / Bitwarden honour (#834/#543). They
                // only matter while this field exists; a plain visit with a key renders none.
                autoComplete="new-password"
                data-1p-ignore="true"
                data-lpignore="true"
                data-bwignore="true"
                data-form-type="other"
                spellCheck={false}
                placeholder={
                  hostChanged
                    ? `key for ${draftHost}${row.key_hint ? ` (${row.key_hint})` : ""}`
                    : block.api_key_set
                      ? "new key"
                      : row.key_hint || "sk-…"
                }
                value={keyDraft}
                onChange={(e) => {
                  setKeyDraft(e.target.value);
                  onDraftEdit();
                }}
                onKeyDown={enterSaves}
              />
            ) : (
              <span className={styles.aiSecretReadout}>{KEY_MASK} stored</span>
            )}
            {block.api_key_set &&
              !hostChanged &&
              (keyInputShown ? (
                <button
                  type="button"
                  className={styles.secBtnGhost}
                  onClick={() => {
                    setKeyDraft("");
                    setKeyEditing(false);
                    onDraftEdit();
                  }}
                  title="Keep the stored API key"
                >
                  Cancel
                </button>
              ) : (
                <button
                  type="button"
                  className={styles.secBtnGhost}
                  onClick={() => setKeyEditing(true)}
                  title="Enter a new API key"
                >
                  Replace key
                </button>
              ))}
            {block.api_key_set && (
              <button
                type="button"
                className={styles.secBtnGhost}
                disabled={writing}
                onClick={() => void removeKey()}
                title="Remove the stored API key"
              >
                Remove key
              </button>
            )}
          </div>
          <p className={hostChanged ? styles.warn : styles.hint}>
            {hostChanged
              ? `New host — enter its API key. The stored key is only ever sent to the host it was saved for.`
              : "Write-only: the stored key is never shown."}
          </p>
        </div>

        {urlDraft.trim() !== "" &&
          (draftNeedsConsent ? (
            <div
              className={styles.aiEgress}
              role="group"
              aria-labelledby={`${idp}-egress-h`}
              data-testid="ai-egress"
            >
              <p className={styles.aiEgressHead} id={`${idp}-egress-h`}>
                <span className="hud-led attention" aria-hidden="true" />
                Session data leaves this host
              </p>
              {hosted ? (
                <p className={styles.aiEgressText}>
                  With <b>{who}</b> every AI feature sends what it reads to{" "}
                  <b>{who}</b>: session transcripts, terminal output, unsent
                  drafts, mission and HQ conversations. Stored secrets are still
                  redacted first.
                </p>
              ) : (
                <p className={styles.aiEgressText}>
                  <b>{who}</b> is not on this host. Every AI feature sends what
                  it reads there: session transcripts, terminal output, unsent
                  drafts, mission and HQ conversations. Stored secrets are still
                  redacted first.
                </p>
              )}
              <ul className={styles.aiEgressList}>
                {hosted ? (
                  <>
                    <li>
                      {who}&apos;s terms, retention and data location apply, not
                      BattleLab&apos;s.
                    </li>
                    <li>Usage is billed to your {who} account.</li>
                  </>
                ) : (
                  <li>
                    Whoever runs {who} decides what is kept and for how long.
                    Check that host&apos;s terms and retention policy, not
                    BattleLab&apos;s.
                  </li>
                )}
              </ul>
              <label className={styles.aiAck}>
                <input
                  type="checkbox"
                  checked={ackOk}
                  // A stored acknowledgement for this exact recipient stays: withdrawing it
                  // while keeping the provider is refused server-side.
                  disabled={storedAckCovers}
                  onChange={(e) => {
                    setAckTicked(e.target.checked);
                    onDraftEdit();
                  }}
                />
                <span>I understand session content is sent to {who}.</span>
              </label>
              {storedAckCovers && block.egress_ack?.source === "legacy" && (
                <p className={styles.hint} data-testid="ai-egress-legacy">
                  Carried over from before this version: you configured this
                  endpoint earlier, so it kept working on upgrade. Change the
                  URL or provider and you are asked again.
                </p>
              )}
            </div>
          ) : (
            isLoopback(urlDraft) && (
              <p className={styles.hint} data-testid="ai-egress-local">
                <b>{who}</b> is this host (loopback): no session content leaves
                it.
              </p>
            )
          ))}

        <div className={styles.cardFoot}>
          {connectionNote ? (
            <p className={connectionNote.cls} role="status">
              {connectionNote.text}
            </p>
          ) : (
            <p className={styles.hint} role="status" />
          )}
          <div className={styles.cardActions}>
            {outcome.kind === "failed" && outcome.canSaveAnyway && (
              <button
                type="button"
                className={styles.secBtnGhost}
                disabled={busy || writing || !canCheck}
                onClick={() => void saveWithoutTesting()}
              >
                Save without testing
              </button>
            )}
            <button
              type="button"
              className={styles.secBtnGhost}
              disabled={busy || !canCheck}
              onClick={() => void test()}
            >
              Test
            </button>
            <button
              type="button"
              className={`${styles.secBtn} shine`}
              disabled={busy || writing || !canCheck || !connectionDirty}
              onClick={() => void saveConnection()}
            >
              Save connection
            </button>
          </div>
        </div>
      </section>

      <section className={cardClass} aria-labelledby={`${idp}-model-h`}>
        <h2 id={`${idp}-model-h`}>
          <span className={styles.stepNum} aria-hidden="true">
            02
          </span>{" "}
          Model
        </h2>
        <p className={styles.hint}>
          Which model answers, and how long one request may take.
        </p>
        {block.configured && connectionDirty && (
          <p className={styles.lockBand}>
            <Lock size={13} aria-hidden="true" />
            Save the connection first — this list belongs to the saved endpoint,
            and the active model keeps serving until then.
          </p>
        )}

        {savedHosted && (
          <div className={styles.aiField}>
            <span className={styles.aiFieldLabel} id={`${idp}-mode-h`}>
              Model
            </span>
            <div
              className={styles.aiSeg}
              role="radiogroup"
              aria-labelledby={`${idp}-mode-h`}
            >
              {(["auto", "specific"] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  role="radio"
                  aria-checked={modeDraft === m}
                  disabled={modelLocked}
                  onClick={() => {
                    setModeDraft(m);
                    choiceRev.current += 1;
                    setChoiceEdited(true);
                    setModelSave({ kind: "idle" });
                  }}
                >
                  {m === "auto" ? "Auto" : "Specific"}
                </button>
              ))}
            </div>
            <p className={styles.hint}>
              {modeDraft === "auto"
                ? `Auto (default) uses ${savedRow.label}'s recommended model${
                    savedRow.auto_model ? ` (${savedRow.auto_model} today)` : ""
                  } and follows it when that model is retired.`
                : `Specific: pick one from ${savedRow.label}'s own list.`}
            </p>
          </div>
        )}

        <div className={styles.aiField} hidden={autoModel}>
          <label className={styles.aiFieldLabel} htmlFor={`${idp}-model`}>
            {savedHosted ? "Specific model" : "Model"}
          </label>
          <div className={styles.aiModelRow}>
            {models.kind === "ok" ? (
              <select
                id={`${idp}-model`}
                className={styles.aiInput}
                value={modelDraft}
                disabled={modelLocked}
                onChange={(e) => {
                  setModelDraft(e.target.value);
                  modelRev.current += 1;
                  setModelEdited(true);
                  setModelSave({ kind: "idle" });
                }}
              >
                {!modelDraft && <option value="">— pick a model —</option>}
                {modelDraft && !models.models.includes(modelDraft) && (
                  <option value={modelDraft}>{modelDraft}</option>
                )}
                {models.models.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            ) : (
              <input
                id={`${idp}-model`}
                className={styles.aiInput}
                type="text"
                spellCheck={false}
                disabled={modelLocked}
                placeholder={
                  models.kind === "loading" ? "loading model list…" : "model id"
                }
                value={modelDraft}
                onChange={(e) => {
                  setModelDraft(e.target.value);
                  modelRev.current += 1;
                  setModelEdited(true);
                  setModelSave({ kind: "idle" });
                }}
              />
            )}
            <button
              type="button"
              className={styles.secBtnGhost}
              aria-label="Refresh model list"
              title="Refresh model list"
              disabled={modelLocked || models.kind === "loading"}
              onClick={() => void loadModels(true)}
            >
              <RefreshCw size={14} />
            </button>
          </div>
          {models.kind === "unlisted" && !modelLocked && (
            <p className={styles.hint}>
              This endpoint doesn’t list models — type the model id.
            </p>
          )}
        </div>

        <div className={styles.aiField}>
          <span className={styles.aiFieldLabel} id={`${idp}-thinking-h`}>
            Thinking
          </span>
          <div
            className={styles.aiSeg}
            role="radiogroup"
            aria-labelledby={`${idp}-thinking-h`}
          >
            {THINKING_LEVELS.map((t) => (
              <button
                key={t.id}
                type="button"
                role="radio"
                aria-checked={thinkingDraft === t.id}
                disabled={modelLocked || !savedRow.thinking_supported}
                onClick={() => {
                  setThinkingDraft(t.id);
                  choiceRev.current += 1;
                  setChoiceEdited(true);
                  setModelSave({ kind: "idle" });
                }}
              >
                {t.label}
              </button>
            ))}
          </div>
          <p className={styles.hint} data-testid="ai-thinking-hint">
            {!savedRow.thinking_supported
              ? `${savedRow.label} has no thinking setting. Requests are sent without one.`
              : "Auto (default): background passes (reviews, recaps, sorting) run without thinking so they stay fast; HQ and chat use the provider's default. A level applies everywhere and makes answers slower and costlier."}
          </p>
        </div>

        <div className={styles.aiField}>
          <label className={styles.aiFieldLabel} htmlFor={`${idp}-timeout`}>
            Request timeout
          </label>
          <div className={styles.aiIntervalRow}>
            <input
              id={`${idp}-timeout`}
              className={`${styles.aiInput} ${styles.aiIntervalInput}`}
              type="number"
              min={TIMEOUT_MIN}
              max={TIMEOUT_MAX}
              placeholder="120"
              disabled={modelLocked}
              value={timeoutDraft}
              onChange={(e) => {
                setTimeoutDraft(e.target.value);
                timeoutRev.current += 1;
                setTimeoutEdited(true);
                setModelSave({ kind: "idle" });
              }}
            />
            <span>seconds</span>
          </div>
          <p className={styles.hint}>
            Hard timeout per AI request ({TIMEOUT_MIN}–{TIMEOUT_MAX}), for every
            feature that uses this endpoint. Slow local models often need
            60–180s. Leave empty to use the server default.
          </p>
        </div>

        <div className={styles.cardFoot}>
          <p className={modelNote.cls} role="status">
            {modelNote.text}
          </p>
          <div className={styles.cardActions}>
            <button
              type="button"
              className={`${styles.secBtn} shine`}
              disabled={modelLocked || !modelDirty || writing || specificMissing}
              onClick={() => void saveModel()}
            >
              Save model
            </button>
          </div>
        </div>
      </section>
    </>
  );
}
