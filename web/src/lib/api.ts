// Typed client for the FastAPI `/api/*` surface. Same-origin; cookie session auth.
// Mutations (later) attach the CSRF token + are origin-checked server-side.
import type { AuthoringSchema, Files } from "../components/playbooks/playbookDraft";
import type { PluginCatalog, PluginReview, PluginOperation, PluginAction } from "../types/plugins";
import type { PlaybookDetail, PlaybookFleet, PlaybookFleetResult, PlaybookFleetReview, PlaybookList, PlaybookVerify, PlaybookWriteResult } from "../types/playbooks";
import {
  AUTOMATION_ORIGINS_API,
  AUTOMATIONS_API,
  DIRECTION_PREVIEW_PATH,
} from "./apiPaths";
import type {
  Automation,
  AutomationConfig,
  AutomationDeleted,
  AutomationList,
  AutomationOrigin,
  AutomationRun,
} from "../types/automations";
import { uploadStoredName } from "./templateMessage";
import type {
  AgentEndpoint,
  AgentEndpointPatch,
  ChatSession,
  Containment,
  StructuredEventPage,
  StructuredModelList,
  StructuredSnapshot,
  ChatTurn,
  DashboardSessions,
  AgentBudgets,
  AgentDefaults,
  AgentUsageResponse,
  AiActivity,
  AnalyticsState,
  AppConfig,
  AutoSortReport,
  DirectionPreview,
  DraftAttachment,
  EngineDetail,
  EnginesResponse,
  Evidence,
  EvidenceKind,
  FileCapabilities,
  FileContent,
  FileListing,
  FileWriteResult,
  Folder,
  FsDir,
  GitBranches,
  GitDiff,
  GitLog,
  GitPushTarget,
  GitStatus,
  GitWriteResult,
  HandoffCommitted,
  HandoffMode,
  HandoffPrepared,
  HistoryPage,
  Mission,
  MissionContext,
  MissionNow,
  MissionList,
  MissionObjective,
  MissionPlan,
  MissionTurn,
  MissionTurnEvent,
  NotificationList,
  OrchestratorAction,
  NeedsYouDetails,
  NeedsYouPayload,
  RecentWorkPayload,
  OrchestratorState_,
  ProjectArchiveReport,
  ProjectEntity,
  PromptEntry,
  PulseAskEvent,
  PulseAskResult,
  PulseDepth,
  PulseOverview,
  PushSubscriptionInfo,
  Session,
  SessionDraft,
  SessionsPage,
  SessionsQuery,
  SystemInfo,
  Template,
  TemplateInput,
  TemplatesResponse,
  TemplateSendResult,
  TemplateSuggestionsResponse,
  TemplateDraft,
  TemplateSuggestionsResult,
  TemplateVariable,
  TemplateVariablesResponse,
  TwoFactorEnrollment,
  UpdateInfo,
  UpdateProgress,
  UpdateSettings,
  UploadBatch,
  UploadResult,
} from "../types/api";
import { clearSent } from "./sentHistory";
import { announceActionResolved } from "./actionEvents";

/** OpenCode maintenance keeps unknown measurements distinct from zero. */
export interface CompactMeasurement {
  available: boolean;
  db_bytes: number | null;
  wal_bytes: number | null;
  reclaimable_bytes: number | null;
  holders: { pids: number[]; unknown: boolean } | null;
  disk: {
    shared_filesystem: boolean;
    database_required: number;
    database_free: number;
    temp_required: number;
    temp_free: number;
  } | null;
  blockers: { code: string; detail: string }[];
}
export interface CompactJob {
  id: string;
  /** The engine whose store this job compacts (#853 P4). Absent from an older server. */
  engine?: string;
  state:
    | "checking"
    | "vacuum"
    | "checkpoint"
    | "done"
    | "refused"
    | "interrupted"
    | "failed";
  started_at: number;
  finished_at: number | null;
  result: {
    vacuum: "done" | "rolled_back" | "not_started" | "unknown";
    checkpoint: "done" | "deferred" | "failed" | "not_started";
    checkpoint_result: number[] | null;
    bytes_freed: number | null;
    blockers: { code: string; detail: string }[];
  } | null;
}
export interface CompactInfo {
  compact: CompactMeasurement;
  /** Every engine whose store can be compacted (#853 P4). Absent from an older server. */
  targets?: string[];
  job: CompactJob | null;
  runner: { job: string; started_at: number | null } | null;
}

export class ApiError extends Error {
  readonly status: number;
  /** The parsed response body, when the server sent one.
   *
   *  A 409 from compare-and-execute carries the SETTLED record — the server has already moved
   *  the action to stale/expired. Reducing the response to a message threw that away, so the
   *  client kept showing the row as actionable and every retry 409'd again until a refresh. */
  readonly record?: unknown;
  constructor(status: number, message: string, record?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.record = record;
  }
}

// The fetch used for every `/api` call. Same-origin `globalThis.fetch` by default;
// Home Free's connect page (#579 P3) injects a mux-backed `tunnelFetch` so the SPA's
// private traffic rides the blind relay instead of hitting the network directly. The
// seam is behaviour-neutral when unset — the app can't tell it's tunneled.
export type ApiFetch = (input: string, init?: RequestInit) => Promise<Response>;
const defaultFetch: ApiFetch = (input, init) => fetch(input, init);
let apiFetch: ApiFetch = defaultFetch;
/** Route every `/api` call through `fn` (Home Free tunnel), or back to same-origin fetch with `null`. */
export function setApiFetch(fn: ApiFetch | null): void {
  apiFetch = fn ?? defaultFetch;
}

/** Where to send an unauthenticated user: the server login form, carrying the
 *  current location so it can bounce back after sign-in (server open-redirect guards). */
export function loginRedirectUrl(
  loc: { pathname: string; search: string } = location,
): string {
  return `/login?next=${encodeURIComponent(loc.pathname + loc.search)}`;
}

// One-shot guard: several /api calls can 401 at once (config + sessions on load); we
// only want a single navigation.
let redirecting = false;
function gotoLogin(): void {
  if (redirecting) return;
  redirecting = true;
  location.assign(loginRedirectUrl());
}

/** First-run forced password change → the server-rendered /change-password (the SPA has
 *  no change screen; this route is on the SW navigateFallbackDenylist). */
export function gotoChangePassword(): void {
  if (redirecting) return;
  redirecting = true;
  location.assign("/change-password");
}

/** Handle a 401/403 on an /api call (always throws). 401 = not signed in → /login.
 *  403 = bad CSRF/origin (surfaced) UNLESS the body says a password change is required,
 *  in which case route to /change-password (belt-and-suspenders alongside the config gate). */
async function authGate(r: Response): Promise<never> {
  if (r.status === 401) {
    gotoLogin();
    throw new ApiError(401, "unauthorized");
  }
  let detail = "";
  try {
    detail = ((await r.json()) as { detail?: string })?.detail ?? "";
  } catch {
    /* non-JSON body */
  }
  if (/password change required/i.test(detail)) {
    gotoChangePassword();
    throw new ApiError(403, "password change required");
  }
  throw new ApiError(403, "forbidden");
}

async function getJson<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await apiFetch(path, { credentials: "same-origin", ...init });
  if (r.status === 401 || r.status === 403) await authGate(r);
  if (!r.ok) throw new ApiError(r.status, `GET ${path} → ${r.status}`);
  return (await r.json()) as T;
}

/** A GET whose failures carry the server's `detail` string, the read-side twin of
 *  `mutateJson` (#834). Scoped to the endpoints whose error text IS the answer — the
 *  /models proxy relays the gateway's own message (#382), and "GET /api/ai-review/models
 *  → 502" in its place tells the operator nothing about why their endpoint was rejected.
 *  Plain `getJson` stays the default: most GETs have no useful `detail` to surface. */
async function getJsonWithDetail<T>(path: string): Promise<T> {
  const r = await apiFetch(path, { credentials: "same-origin" });
  if (r.status === 401 || r.status === 403) await authGate(r);
  if (!r.ok) {
    let detail = "";
    try {
      detail = ((await r.json()) as { detail?: string })?.detail ?? "";
    } catch {
      /* non-JSON body */
    }
    throw new ApiError(r.status, detail || `GET ${path} → ${r.status}`);
  }
  return (await r.json()) as T;
}

// CSRF token for mutations, fetched once via /api/config and cached. The browser
// adds the Origin header on same-origin POSTs; the server checks both.
let csrfToken = "";
export function setCsrfToken(token: string): void {
  csrfToken = token;
}

async function postJson<T>(path: string, body?: unknown): Promise<T> {
  const r = await apiFetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (r.status === 401 || r.status === 403) await authGate(r);
  if (!r.ok) throw new ApiError(r.status, `POST ${path} → ${r.status}`);
  return (await r.json()) as T;
}

/** A CSRF-guarded JSON mutation that surfaces the server's `detail` string in the thrown
 *  ApiError (#361): folder-adoption conflicts (409) carry an explanation the Projects
 *  manager shows inline — the generic "PATCH … → 409" would tell the user nothing. */
/** A CSRF-guarded multipart mutation.
 *
 *  `Content-Type` is deliberately NOT set: the browser derives it from the `FormData`, boundary
 *  included, and setting it by hand produces a header whose boundary does not match the body.
 *  Through the Home Free tunnel this still works because `tunnel.fetch` normalizes the body via a
 *  `Request` and sends `await req.arrayBuffer()`, which preserves that generated header. */
async function postForm<T>(path: string, form: FormData): Promise<T> {
  const r = await apiFetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrfToken },
    body: form,
  });
  if (r.status === 401 || r.status === 403) await authGate(r);
  if (!r.ok) {
    let detail = `POST ${path} → ${r.status}`;
    try {
      const body = (await r.json()) as { detail?: string };
      if (body?.detail) detail = body.detail;
    } catch {
      /* a non-JSON error body: keep the generic message */
    }
    throw new ApiError(r.status, detail);
  }
  return (await r.json()) as T;
}

async function mutateJson<T>(
  method: "POST" | "PUT" | "PATCH" | "DELETE",
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const r = await apiFetch(path, {
    method,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: body === undefined ? undefined : JSON.stringify(body),
    ...(signal ? { signal } : {}),
  });
  if (r.status === 401 || r.status === 403) await authGate(r);
  if (!r.ok) {
    let detail = "";
    let parsed: unknown;
    try {
      parsed = await r.json();
      detail = (parsed as { detail?: string })?.detail ?? "";
    } catch {
      /* non-JSON body */
    }
    // Carry the whole body, not just `detail`. A compare-and-execute 409 ships the SETTLED
    // record alongside its explanation, and a caller that can fold that back in place saves
    // the operator a refresh (#726).
    throw new ApiError(
      r.status,
      detail || `${method} ${path} → ${r.status}`,
      parsed,
    );
  }
  return (await r.json()) as T;
}

/** `POST /api/pulse/ask/stream`, read line by line (#1171). A fetch whose body cannot be read as
 *  a stream (the Home Free tunnel may buffer it) still works: the lines are simply all there at
 *  once, and the caller sees the same events, only later. */
async function streamAsk(
  query: string,
  history: { role: "user" | "assistant"; content: string }[],
  onEvent: (ev: PulseAskEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const path = "/api/pulse/ask/stream";
  const r = await apiFetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: JSON.stringify({ query, history }),
    ...(signal ? { signal } : {}),
  });
  if (r.status === 401 || r.status === 403) await authGate(r);
  if (!r.ok) {
    let parsed: unknown;
    try {
      parsed = await r.json();
    } catch {
      /* non-JSON body */
    }
    const detail = (parsed as { detail?: string } | undefined)?.detail ?? "";
    throw new ApiError(r.status, detail || `POST ${path} → ${r.status}`, parsed);
  }
  // A stream that ends without its final answer — the connection dropped, a proxy cut it — is a
  // failure, never a turn left spinning, and a half-written last line is the same failure rather
  // than a JSON parse error.
  let final = false;
  const cut = () =>
    new ApiError(502, "The answer was cut off before it finished — ask again.");
  const emit = (line: string) => {
    if (!line.trim()) return;
    let ev: PulseAskEvent;
    try {
      ev = JSON.parse(line) as PulseAskEvent;
    } catch {
      throw cut();
    }
    if (ev.type === "error") throw new ApiError(ev.status, ev.detail);
    if (ev.type === "answer" && ev.final) final = true;
    onEvent(ev);
  };
  await readLines(r, emit);
  if (!final) throw cut();
}

/** Hand each NDJSON line of `r` to `emit` as it arrives — or all at once, when the body cannot be
 *  read as a stream (the Home Free tunnel may buffer it). Shared by both streamed chats. */
async function readLines(r: Response, emit: (line: string) => void): Promise<void> {
  const reader = r.body?.getReader();
  if (!reader) {
    for (const line of (await r.text()).split("\n")) emit(line);
    return;
  }
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let nl = buf.indexOf("\n");
    while (nl >= 0) {
      emit(buf.slice(0, nl));
      buf = buf.slice(nl + 1);
      nl = buf.indexOf("\n");
    }
  }
  emit(buf + decoder.decode());
}

/** `POST /api/missions/{id}/message/stream`, read line by line (#1224). Resolves with the turn —
 *  the same `MissionTurn` `/message` answers — and hands every line before it to `onEvent`.
 *
 *  A refusal before the stream is an `ApiError` with its status, exactly as `missionMessage`'s;
 *  an `error` line is one too. A stream that ends WITHOUT its `turn` line (a dropped connection,
 *  a proxy cutting it) is a failure, never a turn left spinning — and not a statement about the
 *  turn: it is durable, and sending the same `turn_id` again finds it. */
async function streamMissionMessage(
  id: string,
  body: { message: string; turnId: string },
  onEvent: (ev: MissionTurnEvent) => void,
): Promise<MissionTurn> {
  const path = `/api/missions/${encodeURIComponent(id)}/message/stream`;
  const r = await apiFetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: JSON.stringify({ message: body.message, turn_id: body.turnId }),
  });
  if (r.status === 401 || r.status === 403) await authGate(r);
  if (!r.ok) {
    let parsed: unknown;
    try {
      parsed = await r.json();
    } catch {
      /* non-JSON body */
    }
    const detail = (parsed as { detail?: string } | undefined)?.detail ?? "";
    throw new ApiError(r.status, detail || `POST ${path} → ${r.status}`, parsed);
  }
  let turn: MissionTurn | null = null;
  const cut = () =>
    new ApiError(502, "The connection dropped before the turn finished.");
  await readLines(r, (line) => {
    if (!line.trim()) return;
    let ev: MissionTurnEvent;
    try {
      ev = JSON.parse(line) as MissionTurnEvent;
    } catch {
      throw cut();
    }
    if (ev.type === "error") throw new ApiError(ev.status, ev.detail);
    if (ev.type === "turn") {
      turn = ev.turn;
      return;
    }
    onEvent(ev);
  });
  if (!turn) throw cut();
  return turn;
}

const patchJson = <T>(path: string, body?: unknown): Promise<T> =>
  mutateJson<T>("PATCH", path, body);

const putJson = <T>(path: string, body?: unknown): Promise<T> =>
  mutateJson<T>("PUT", path, body);

const deleteJson = <T>(path: string): Promise<T> =>
  mutateJson<T>("DELETE", path);

/** DELETE a CSRF-guarded resource that answers 204 (no body). Like `mutateJson`, a failure
 *  carries the server's `detail` and the whole parsed body as `record` — a template DELETE
 *  409s with the `current` record, which the gallery folds back in (#905). */
async function deleteVoid(path: string): Promise<void> {
  const r = await apiFetch(path, {
    method: "DELETE",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrfToken },
  });
  if (r.status === 401 || r.status === 403) await authGate(r);
  if (!r.ok) {
    let detail = "";
    let parsed: unknown;
    try {
      parsed = await r.json();
      detail = (parsed as { detail?: string })?.detail ?? "";
    } catch {
      /* non-JSON body */
    }
    throw new ApiError(
      r.status,
      detail || `DELETE ${path} → ${r.status}`,
      parsed,
    );
  }
}

/** POST a CSRF-guarded mutation that returns 204 (no body) — e.g. confirm/disable 2FA. */
async function postVoid(path: string, body?: unknown): Promise<void> {
  const r = await apiFetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (r.status === 401 || r.status === 403) await authGate(r);
  if (!r.ok) throw new ApiError(r.status, `POST ${path} → ${r.status}`);
}

export function sessionsUrl(q: SessionsQuery = {}): string {
  const p = new URLSearchParams();
  p.set("limit", String(q.limit ?? 20));
  p.set("offset", String(q.offset ?? 0));
  p.set("archived", q.archived ? "1" : "0");
  if (q.q?.trim()) p.set("q", q.q.trim());
  if (q.project) p.set("project", q.project);
  if (q.engine) p.set("engine", q.engine);
  if (q.mission) p.set("mission", q.mission);
  if (q.snapshot) p.set("snapshot", q.snapshot);
  return `/api/sessions?${p.toString()}`;
}

const enc = encodeURIComponent;

/** The read-back URL for an upload path's image (#905): `GET /api/uploads/{stored}`, keyed by
 *  the STORED basename — `<stamp>-<safe>`, the last component of the path the server returned.
 *  Never by the response's `name`, which is the sanitized original and names no file. */
export function uploadUrl(path: string): string {
  return `/api/uploads/${enc(uploadStoredName(path))}`;
}

/** The bytes of an upload's image, through the injectable fetch seam (#905). In Home Free app
 *  mode private HTTP reaches the box only via the tunnel `setApiFetch` installs; a native-origin
 *  `<img src>` would hit the relay instead (a 404, and the stored name in its logs). Callers turn
 *  the blob into an object URL and revoke it (`components/templates/UploadImage.tsx`). */
async function uploadBlob(path: string, signal?: AbortSignal): Promise<Blob> {
  const r = await apiFetch(uploadUrl(path), {
    credentials: "same-origin",
    signal,
  });
  if (r.status === 401 || r.status === 403) await authGate(r);
  if (!r.ok) throw new ApiError(r.status, `upload read-back → ${r.status}`);
  return await r.blob();
}

/** Upload a file (image/context) → server saves it under ~/.agent-sessions/uploads/
 *  and returns a path the agent can read. Multipart, CSRF-guarded (not JSON). `stored` is the
 *  basename actually written (absent on servers before #905). */
async function upload(
  file: File,
): Promise<{ path: string; name: string; stored?: string }> {
  const fd = new FormData();
  fd.append("file", file, file.name || "pasted");
  const r = await apiFetch("/api/upload", {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrfToken },
    body: fd,
  });
  if (r.status === 401 || r.status === 403) await authGate(r);
  if (!r.ok) throw new ApiError(r.status, `upload → ${r.status}`);
  return (await r.json()) as { path: string; name: string; stored?: string };
}

/** A session key's URL segment (`engine:native`). */
const chatPath = (sid: string) => `/api/chat/${encodeURIComponent(sid)}`;
const structuredPath = (key: string) => `/api/structured/sessions/${encodeURIComponent(key)}`;

/** One automation's URL (#1201). */
const autoPath = (id: string) => `${AUTOMATIONS_API}/${encodeURIComponent(id)}`;

async function pluginCatalog(path: string, refresh = false): Promise<PluginCatalog> {
  const value = await (refresh ? pluginPost<PluginCatalog>(path, {}) : getJsonWithDetail<PluginCatalog>(path));
  if (!value?.feed || !Array.isArray(value.catalog) || !Array.isArray(value.plugins) || !Array.isArray(value.operations)) {
    throw new Error("The installer catalog is unavailable. Existing agents remain listed below.");
  }
  return value;
}

function pluginPost<T>(path: string, body: unknown): Promise<T> {
  return mutateJson<T>("POST", path, body);
}

export const api = {
  plugins: () => pluginCatalog("/api/plugins"),
  pluginRefresh: () => pluginCatalog("/api/plugins/feed/refresh", true),
  pluginReview: (body: { plugin_id?: string; local?: unknown; adopted_path?: string }) =>
    pluginPost<PluginReview>("/api/plugins/review", body),
  pluginInstall: (body: { request_id: string; review_id: string; digest: string; confirm_local: boolean; confirm_adopted: boolean }) =>
    pluginPost<PluginOperation>("/api/plugins/install", body),
  pluginOperation: (id: string) => getJsonWithDetail<PluginOperation>(`/api/plugins/operations/${enc(id)}`),
  pluginCancel: (id: string) => pluginPost<PluginOperation>(`/api/plugins/operations/${enc(id)}/cancel`, {}),
  pluginRecover: () => pluginPost<PluginCatalog>("/api/plugins/recover", {}),
  pluginSignin: (body: PluginAction) => pluginPost<PluginOperation>("/api/plugins/signin", body),
  pluginVerify: (body: PluginAction) => pluginPost<PluginOperation>("/api/plugins/verify", { ...body, confirm_effects: true }),
  pluginActivate: (body: PluginAction) => pluginPost<PluginOperation>("/api/plugins/activate", body),
  pluginDisable: (body: { request_id: string; plugin_id: string; expected_active: string | null; expected_revision: string | null }) => pluginPost<PluginOperation>("/api/plugins/disable", body),
  pluginRemove: (body: { request_id: string; plugin_id: string; expected_active: string | null; expected_revision: string | null }) => pluginPost<PluginOperation>("/api/plugins/remove", body),
  pluginReload: () => pluginPost<{ generation: number }>("/api/plugins/reload", {}),
  pluginEndpoint: (id: string, generation: string) => getJsonWithDetail<AgentEndpoint>(`/api/plugins/${enc(id)}/generations/${enc(generation)}/endpoint`),
  pluginEndpointSet: (id: string, generation: string, patch: AgentEndpointPatch) => mutateJson<AgentEndpoint>("PATCH", `/api/plugins/${enc(id)}/generations/${enc(generation)}/endpoint`, patch),
  pluginEndpointTest: (id: string, generation: string, draft: { base_url: string; api_key?: string }) => pluginPost<{ models: string[]; listing: "ok" | "unsupported" }>(`/api/plugins/${enc(id)}/generations/${enc(generation)}/endpoint/test`, draft),

  // ---- Structured sessions (#1311): native API clients ---------------------------------------------
  /** Create; an exact repeat of `operation_id` returns the same session (never a second one). */
  /** The models the client's own CLI reports (#1313). */
  structuredModels: (engine: string) =>
    getJsonWithDetail<StructuredModelList>(`/api/structured/clients/${enc(engine)}/models`),
  /** `model` omitted = the client's default; otherwise one its CLI lists (#1313). `bypass` (#1339)
   *  is fixed for the session; sent only when set, so a guarded create is the same request it
   *  always was. */
  structuredCreate: (
    engine: string,
    cwd: string,
    operation_id: string,
    model?: string,
    bypass = false,
  ) =>
    mutateJson<StructuredSnapshot>("POST", "/api/structured/sessions", {
      engine,
      cwd,
      operation_id,
      ...(model && model !== "default" ? { model } : {}),
      ...(bypass ? { bypass: true } : {}),
    }),
  structuredSnapshot: (key: string) =>
    getJsonWithDetail<StructuredSnapshot>(structuredPath(key)),
  structuredEvents: (key: string, after: number, limit = 100) =>
    getJsonWithDetail<StructuredEventPage>(
      `${structuredPath(key)}/events?after=${after}&limit=${limit}`,
    ),
  /** Idempotent on `operation_id`: a repeat observes the recorded turn, never a second write. */
  structuredSubmit: (
    key: string,
    operation_id: string,
    text: string,
    expected_revision: number,
    attachments: string[] = [],
  ) =>
    mutateJson<{ state?: string }>("POST", `${structuredPath(key)}/turns`, {
      operation_id,
      text,
      expected_revision,
      // Upload names (#1332 Phase 3); omitted when none, so a text turn's body is unchanged.
      ...(attachments.length ? { attachments } : {}),
    }),
  structuredDecide: (
    key: string,
    body: {
      decision_id: string;
      turn_id: string;
      request_id: string;
      decision: string;
      /** Only with `decision: "always"` (#1339): one offered grant id. */
      grant?: string;
    },
  ) => mutateJson<unknown>("POST", `${structuredPath(key)}/decisions`, body),
  structuredInterrupt: (key: string, operation_id: string, turn_id: string) =>
    mutateJson<unknown>("POST", `${structuredPath(key)}/interrupt`, { operation_id, turn_id }),
  /** Launch a skip-permissions creation the operator has seen succeed (#1339). Idempotent. */
  structuredStart: (key: string) =>
    mutateJson<StructuredSnapshot>("POST", `${structuredPath(key)}/start`),
  structuredStop: (key: string) =>
    mutateJson<{ containment: Containment }>("POST", `${structuredPath(key)}/stop`),
  structuredContainment: (key: string) =>
    getJsonWithDetail<{ containment: Containment }>(`${structuredPath(key)}/containment`),

  // ---- API agents (#1209) ------------------------------------------------------------------------
  /** Start a chat-runtime conversation; resolves to its session key (`engine:uuid`). */
  chatNew: (engine: string, cwd: string) =>
    mutateJson<{ id: string }>("POST", "/api/chat/new", { engine, cwd }),
  chatGet: (sid: string) => getJsonWithDetail<ChatSession>(chatPath(sid)),
  /** Idempotent on `turn_id` (client-minted): a repeat never appends. */
  chatSend: (sid: string, turn_id: string, text: string) =>
    mutateJson<{ turn: Partial<ChatTurn> & { turn_id: string; status: string } }>(
      "POST",
      `${chatPath(sid)}/messages`,
      { turn_id, text },
    ),
  chatRetry: (sid: string, turn_id: string) =>
    mutateJson<{ turn: { turn_id: string; status: string } }>(
      "POST",
      `${chatPath(sid)}/turns/${encodeURIComponent(turn_id)}/retry`,
    ),
  agentEndpoint: (engine: string) =>
    getJsonWithDetail<AgentEndpoint>(`/api/agents/${encodeURIComponent(engine)}/endpoint`),
  chatDecide: (sid: string, turnId: string, proposalId: string, decision: "approve" | "reject") =>
    mutateJson("POST", `${chatPath(sid)}/turns/${encodeURIComponent(turnId)}/proposals/${encodeURIComponent(proposalId)}/decide`, { decision }),
  setAgentEndpoint: (engine: string, patch: AgentEndpointPatch) =>
    mutateJson<AgentEndpoint>(
      "PATCH",
      `/api/agents/${encodeURIComponent(engine)}/endpoint`,
      patch,
    ),
  /** Checks a DRAFT; saves nothing. */
  testAgentEndpoint: (engine: string, draft: { base_url: string; api_key?: string }) =>
    mutateJson<{ models: string[]; listing: "ok" | "unsupported" }>(
      "POST",
      `/api/agents/${encodeURIComponent(engine)}/endpoint/test`,
      draft,
    ),

  config: () => getJson<AppConfig>("/api/config"),
  version: () => getJson<{ version: string }>("/api/version"),
  /** The dashboard's live + recent sessions (#1123), one scoped read with its own read health. */
  dashboardSessions: (liveLimit = 5, recentLimit = 6) =>
    getJson<DashboardSessions>(
      `/api/dashboard/sessions?live_limit=${liveLimit}&recent_limit=${recentLimit}`,
    ),
  /** The drill-down behind a live/working count: the session list filtered to running agents. */
  runningSessions: (which: "live" | "working", limit = 200, offset = 0) =>
    getJson<SessionsPage>(`/api/sessions?running=${which}&limit=${limit}&offset=${offset}`),
  /** Discovery: every known engine provider with presence / new-session / bin path. */
  engines: () => getJson<EnginesResponse>("/api/engines"),
  /** One engine's manifest detail (#853 P4) — the Agents page. A 404 means no such agent. */
  engineDetail: (id: string) =>
    getJson<EngineDetail>(`/api/engines/${encodeURIComponent(id)}`),
  /** Save a PARTIAL `agent_defaults` block (#1128). Send only the fields that changed: the
   *  server merges, so a bypass-only save leaves the stored default engine exactly as it is. */
  setAgentDefaults: (patch: Partial<AgentDefaults>) =>
    mutateJson<{ agent_defaults: AgentDefaults }>("POST", "/api/prefs", {
      agent_defaults: patch,
    }),

  /** Per-agent usage + budgets (#839). The GET never probes — it serves the last answers,
   *  each labelled with when it was taken. */
  agentUsage: () => getJson<AgentUsageResponse>("/api/agents/usage"),
  /** Ask the agents now. `mutateJson` so the 409 ("a refresh is already running") reaches
   *  the operator as itself rather than as a bare status code (#834). */
  agentUsageRefresh: () =>
    mutateJson<AgentUsageResponse>("POST", "/api/agents/usage/refresh"),
  setAgentBudgets: (patch: Partial<AgentBudgets>) =>
    patchJson<AgentUsageResponse>("/api/agents/budgets", patch),
  /** Cross-engine handoff prepare (#597): build the seed for a source session in `mode`
   *  and return {handle, preview, meta}. Side-effect-free — cancel by letting the
   *  short-TTL handle expire. An "ai" request degrades to the quick tail server-side when
   *  the endpoint is unconfigured/failing (`meta.notice` explains). 409 = empty source
   *  transcript; 422 = unsupported target/mode — `mutateJson` surfaces the server detail
   *  either way. CSRF-guarded. */
  prepareHandoff: (
    sourceId: string,
    targetEngine: string,
    mode: HandoffMode = "quick",
    includeSourceRef = false,
  ) =>
    mutateJson<HandoffPrepared>("POST", "/api/handoff/prepare", {
      source_id: sourceId,
      target_engine: targetEngine,
      mode,
      // #716: opt-in pointer to where the source transcript lives, so the target agent can
      // read past the seed cap. Default false — following it costs tokens.
      include_source_ref: includeSourceRef,
    }),
  /** Cross-engine handoff commit (#597): bind the prepared handle to a freshly minted
   *  target session id. `seed` carries the user's EDITED preview (Phase 2) — the server
   *  re-sanitizes it. The caller then navigates to /s/{engine}/{native} (fresh launch) and
   *  the server redeems the seed at spawn time — never through the URL. 404/409 = the
   *  handle expired or was already committed; the modal re-prepares. */
  commitHandoff: (handle: string, seed?: string) =>
    mutateJson<HandoffCommitted>(
      "POST",
      "/api/handoff",
      seed === undefined ? { handle } : { handle, seed },
    ),
  /** Host/system info for the Settings → System card (fail-soft fields). */
  system: () => getJson<SystemInfo>("/api/system"),
  /** Self-update: compare the running version to the channel's latest. */
  updateCheck: () => getJson<UpdateInfo>("/api/update/check"),
  /** Apply the channel's latest (re-runs the installer detached). CSRF-guarded; 202. */
  updateApply: () => postJson<{ status: string }>("/api/update/apply"),
  /** The running (or last) self-update's progress (#1085). */
  updateProgress: () => getJson<UpdateProgress>("/api/update/progress"),
  /** Cheap read (no remote hit) of the persisted update settings for the card mount (#538). */
  updateSettings: () => getJson<UpdateSettings>("/api/update/settings"),
  /** Persist the auto-update opt-in and/or release channel (#538). CSRF-guarded. */
  setUpdateSettings: (body: { auto_update?: boolean; channel?: string }) =>
    postJson<UpdateSettings>("/api/update/settings", body),
  /** The AI prompt catalog (#824). Off /api/config on purpose: ~11 prompts x (value +
   *  default) is boot-path weight for data only Settings reads. */
  prompts: () => getJson<{ prompts: PromptEntry[] }>("/api/prompts"),
  /** Save one prompt by id, or restore its shipped default. The server resolves the id to
   *  its storage binding; the client never sends one. CSRF-guarded. */
  savePrompt: (id: string, value: string) =>
    mutateJson<PromptEntry>("PATCH", `/api/prompts/${encodeURIComponent(id)}`, {
      value,
    }),
  resetPrompt: (id: string) =>
    mutateJson<PromptEntry>("PATCH", `/api/prompts/${encodeURIComponent(id)}`, {
      reset: true,
    }),
  /** Persist the UI theme server-side (per-user, across devices). CSRF-guarded. */
  setTheme: (theme: string) =>
    postJson<{ theme: string }>("/api/prefs", { theme }),
  /** Persist the brand accent (#rrggbb) server-side, per-user (#211 Phase 2). CSRF-guarded. */
  setAccent: (accent: string) =>
    postJson<{ accent: string }>("/api/prefs", { accent }),
  /** Persist the terminal font size in px (#859). CSRF-guarded. The server is STRICT here —
   *  a non-integer or out-of-range value is a 422, not a clamp — so callers send an
   *  already-coerced value from `coerceTermFontSize`. */
  setTermFontSize: (size: number) =>
    postJson<{ term_font_size: number }>("/api/prefs", {
      term_font_size: size,
    }),
  /** Persist the terminal font stack (#866). CSRF-guarded. Strict on the server — a stack with
   *  a forbidden character or an empty comma segment is a 422, not a silent fallback — so
   *  callers send an already-coerced value from `coerceTermFontFamily`. */
  setTermFontFamily: (family: string) =>
    postJson<{ term_font_family: string }>("/api/prefs", {
      term_font_family: family,
    }),
  /** Persist a partial set of UI preferences (e.g. overview lists, #144). CSRF-guarded. */
  /** `mutateJson`, not `postJson`, so a rejection carries the server's `detail` (#834):
   *  /api/prefs is the one mutation whose 422 text is a real answer — "ai_review.base_url
   *  must be an http(s) URL" tells the operator what to change, where "POST /api/prefs →
   *  422" tells them nothing. The AI endpoint form renders it verbatim. */
  setPrefs: (partial: Record<string, unknown>) =>
    mutateJson<Record<string, unknown>>("POST", "/api/prefs", partial),
  /** First-run onboarding (#463): mark the wizard complete (or skipped) so it never shows
   *  again. Persists `onboarded: true` via the prefs store. CSRF-guarded. */
  completeOnboarding: (whatsNewSeen?: string) =>
    postJson<Record<string, unknown>>(
      "/api/prefs",
      whatsNewSeen ? { onboarded: true, whats_new_seen: whatsNewSeen } : { onboarded: true },
    ),
  /** Usage analytics (#1009): record the operator's decision. The response is the stored state. */
  setAnalyticsConsent: (value: boolean) =>
    postJson<{ analytics: AnalyticsState }>("/api/prefs", {
      analytics_consent: value,
    }),
  /** What's new (#971): record the release whose notes were dismissed. `onboarded: true` rides along
   *  because the server only stores the key beside it; the dialog only ever shows once onboarding
   *  resolves true, so it states nothing new. The response carries the value the server KEPT. */
  dismissWhatsNew: (version: string) =>
    mutateJson<{ onboarded: boolean; whats_new_seen: string }>("POST", "/api/prefs", {
      onboarded: true,
      whats_new_seen: version,
    }),
  /** Optional TOTP 2FA (#116). All CSRF-guarded. */
  enroll2fa: () => postJson<TwoFactorEnrollment>("/api/2fa/enroll"),
  confirm2fa: (code: string) => postVoid("/api/2fa/confirm", { code }),
  /** Disable 2FA — needs a fresh proof (current code OR password). */
  disable2fa: (proof: { code?: string; password?: string }) =>
    postVoid("/api/2fa/disable", proof),
  /** Regenerate recovery codes — same fresh-proof requirement; returns the new set once. */
  regenerate2fa: (proof: { code?: string; password?: string }) =>
    postJson<{ recovery_codes: string[] }>("/api/2fa/recovery-codes", proof),
  upload,
  uploadBlob,
  /** Instruction templates (#905): the operator's library of reusable messages. Its own
   *  route, off /api/config. Reads need a session; every mutation is CSRF-guarded. A stale
   *  PATCH/DELETE (the record moved since `expectedUpdatedAt`) is a 409 whose `record.current`
   *  is what is stored now. */
  templates: () => getJsonWithDetail<TemplatesResponse>("/api/templates"),
  createTemplate: (input: TemplateInput) =>
    mutateJson<Template>("POST", "/api/templates", input),
  updateTemplate: (
    id: string,
    input: TemplateInput,
    expectedUpdatedAt: number,
  ) =>
    mutateJson<Template>("PATCH", `/api/templates/${enc(id)}`, {
      ...input,
      expected_updated_at: expectedUpdatedAt,
    }),
  deleteTemplate: (id: string, expectedUpdatedAt: number) =>
    deleteVoid(
      `/api/templates/${enc(id)}?expected_updated_at=${enc(String(expectedUpdatedAt))}`,
    ),
  /** A send happened — bumps the usage counters, never `updated_at`. */
  markTemplateUsed: (id: string) =>
    mutateJson<Template>("POST", `/api/templates/${enc(id)}/used`),
  /** Render + deliver a template SERVER-SIDE (#1090 Phase 2) — the only path for a template
   *  with a secret field. Only the masked text comes back; the server bumps the usage counter. */
  sendTemplate: (
    id: string,
    session: string,
    values: Record<string, string>,
    expectedUpdatedAt: number,
  ) =>
    mutateJson<TemplateSendResult>("POST", `/api/templates/${enc(id)}/send`, {
      session,
      values,
      expected_updated_at: expectedUpdatedAt,
    }),
  /** AI-suggested templates (#1090 Phase 3). Analysis runs ONLY on this request — never in the
   *  background. `signal` lets the page stop waiting; the server may still finish and store it. */
  templateSuggestions: () =>
    getJsonWithDetail<TemplateSuggestionsResponse>("/api/templates/suggestions"),
  suggestTemplates: (signal?: AbortSignal) =>
    mutateJson<TemplateSuggestionsResult>("POST", "/api/templates/suggest", undefined, signal),
  dismissSuggestion: (id: string) => postVoid(`/api/templates/suggestions/${enc(id)}/dismiss`),
  writeTemplate: (request: string, signal?: AbortSignal) =>
    mutateJson<{ template: TemplateDraft }>("POST", "/api/templates/write", { request }, signal),
  /** The template variables library (#1090): values a `source: "library"` field takes by name.
   *  Same fencing as a template; the name is the identity (no rename). A DELETE refused because
   *  templates still use the variable is a 409 whose `record.dependants` lists them. */
  templateVariables: () =>
    getJsonWithDetail<TemplateVariablesResponse>("/api/template-variables"),
  createTemplateVariable: (input: { name: string; value: string; kind?: "text" | "secret" }) =>
    mutateJson<TemplateVariable>("POST", "/api/template-variables", input),
  updateTemplateVariable: (name: string, value: string, expectedUpdatedAt: number) =>
    mutateJson<TemplateVariable>("PATCH", `/api/template-variables/${enc(name)}`, {
      value,
      expected_updated_at: expectedUpdatedAt,
    }),
  deleteTemplateVariable: (name: string, expectedUpdatedAt: number) =>
    deleteVoid(
      `/api/template-variables/${enc(name)}?expected_updated_at=${enc(String(expectedUpdatedAt))}`,
    ),
  /** Launch-folder list (#361: behaviour-preserving rename of the old /api/projects).
   *  `visible: true` applies the mode-aware visibility filter (#335) — the new-session
   *  picker uses it so the dropdown mirrors the curated sidebar; Settings omits it to
   *  get the full discovered set for curation. */
  folders: (opts?: { visible?: boolean }) =>
    getJson<{ folders: Folder[] }>(
      `/api/folders${opts?.visible ? "?visible=1" : ""}`,
    ),
  /** Create a new project directory under a configured base root (#335 Phase 3). CSRF-guarded;
   *  returns the new absolute cwd. 404 if the feature is disabled, 403/422 on a rejected
   *  root/name. */
  mkdir: (root: string, name: string) =>
    postJson<{ cwd: string }>("/api/folders/mkdir", { root, name }),
  /** Project ENTITIES (#361): what sessions BELONG to (folders above stay where they
   *  LAUNCH). Archived entities are hidden unless `includeArchived` (Settings opts in). */
  projectEntities: (opts?: { includeArchived?: boolean }) =>
    getJson<{ projects: ProjectEntity[] }>(
      `/api/projects${opts?.includeArchived ? "?include_archived=1" : ""}`,
    ),
  /** Create an entity (#361). "From a folder" is just `folders: [cwd]`; adopting a folder
   *  (or one nested under/above) already owned by another project is a 409 whose detail
   *  string names the conflict. CSRF-guarded. */
  createProject: (body: {
    name: string;
    color?: string;
    folders?: string[];
    default_folder?: string;
  }) =>
    mutateJson<Omit<ProjectEntity, "session_count">>(
      "POST",
      "/api/projects",
      body,
    ),
  /** On-demand AI auto-sort (#424 Phase 6): one bounded pass assigning unassigned sessions to
   *  existing projects. 409 unless auto_sort is enabled AND the reused ai_review endpoint is
   *  configured. CSRF-guarded. */
  autoSortNow: () =>
    mutateJson<AutoSortReport>("POST", "/api/projects/auto-sort"),
  /** Rename / recolor / adopt+release folders (#361). Omitted fields stay unchanged;
   *  `color: ""` clears. Archiving is NOT patchable — use archive/unarchive below. */
  patchProject: (
    id: string,
    body: {
      name?: string;
      color?: string;
      folders?: string[];
      default_folder?: string;
    },
  ) =>
    patchJson<Omit<ProjectEntity, "session_count">>(
      `/api/projects/${enc(id)}`,
      body,
    ),
  /** Folder picker (#448): immediate subdirectories of `path` (default ~), bounded to ~/.
   *  Returns the resolved path, the home root, and the child dirs. */
  fsDirs: (path?: string) =>
    getJson<{ path: string; home: string; dirs: FsDir[] }>(
      `/api/fs/dirs${path ? `?path=${encodeURIComponent(path)}` : ""}`,
    ),
  /** Create a folder under a browsed parent (#448), bounded to ~/. Idempotent; returns the path.
   *  A refusal carries the server's reason (bad name, outside home) — the New project wizard
   *  shows it verbatim (#1187). */
  fsMkdir: (parent: string, name: string) =>
    mutateJson<{ path: string }>("POST", "/api/fs/mkdir", { parent, name }),
  /** File panel (#783): one directory, bounded by an entry cap AND a wall-clock budget. */
  filesList: (path?: string, init?: RequestInit) =>
    getJson<FileListing>(
      `/api/files/list${path ? `?path=${encodeURIComponent(path)}` : ""}`,
      init,
    ),
  /** File panel (#783): one regular file, capped while reading. Binary returns metadata only. */
  filesRead: (path: string, init?: RequestInit) =>
    getJson<FileContent>(
      `/api/files/read?path=${encodeURIComponent(path)}`,
      init,
    ),
  /** File viewer (#950): save edited text, bound to the version it was loaded at. A refusal is a
   *  409 whose body (`FileWriteRefusal`) rides on `ApiError.record` — `mutateJson`, not
   *  `postJson`, precisely so that body survives. */
  filesWrite: (path: string, content: string, expect: string) =>
    mutateJson<FileWriteResult>("POST", "/api/files/write", { path, content, expect }),
  /** File panel (#783): platform support for the containment contract. Fails closed. */
  filesCapabilities: () => getJson<FileCapabilities>("/api/files/capabilities"),
  /** GIT tab (#784): repository state for the panel's current root. */
  gitStatus: (path?: string, init?: RequestInit) =>
    getJson<GitStatus>(
      `/api/git/status${path ? `?path=${encodeURIComponent(path)}` : ""}`,
      init,
    ),
  /** GIT tab (#784): a unified diff for one path, assembled server-side from blobs. */
  gitDiff: (path: string, staged: boolean, init?: RequestInit) =>
    getJson<GitDiff>(
      `/api/git/diff?path=${encodeURIComponent(path)}&staged=${staged ? 1 : 0}`,
      init,
    ),
  /** GIT tab (#806): local + remote-tracking refs for the branch menu. Read-only. */
  gitBranches: (path?: string, init?: RequestInit) =>
    getJson<GitBranches>(
      `/api/git/branches${path ? `?path=${encodeURIComponent(path)}` : ""}`,
      init,
    ),
  /** GIT tab (#806): which remote a push WOULD go to, resolved server-side. A read — it resolves
   *  and reports, and changes nothing, so the control can render `PUSH -> origin` truthfully. */
  /** RECENT COMMITS (#950): the branch's first-parent history. Read-only. */
  gitLog: (path: string, init?: RequestInit) =>
    getJson<GitLog>(`/api/git/log?path=${encodeURIComponent(path)}`, init),
  gitPushTarget: (path: string, remote?: string, init?: RequestInit) =>
    getJson<GitPushTarget>(
      `/api/git/push-target?path=${encodeURIComponent(path)}${remote ? `&remote=${encodeURIComponent(remote)}` : ""}`,
      init,
    ),
  // --- the write side (#806). Every one is POST + CSRF + Origin-checked; none has a force
  // variant, and each surfaces the server's own refusal text, because the refusal IS the feature:
  // "409 dirty tree" and "423 the agent is running git" are different facts and read as such.
  gitFetch: (path: string, remote?: string) =>
    mutateJson<GitWriteResult>("POST", "/api/git/fetch", { path, remote }),
  /** Fast-forward only. A diverged branch comes back 409 with the numbers, never a merge. */
  gitPull: (path: string) =>
    mutateJson<GitWriteResult>("POST", "/api/git/pull", { path }),
  /** Refuses a dirty tree — `git switch` silently carries uncommitted work across (measured). */
  gitSwitch: (
    path: string,
    branch: string,
    create = false,
    start?: string,
    expect?: string,
  ) =>
    // `expect` is the `dirty_fp` the panel last displayed. `switch` CARRIES uncommitted work
    // across, so a tree that went dirty after the menu opened would drag those edits onto the
    // other branch; the server re-checks inside its lock and refuses.
    mutateJson<GitWriteResult>("POST", "/api/git/switch", {
      path,
      branch,
      create,
      start,
      expect,
    }),
  /** `git branch -d` only: an unmerged branch is refused, and no force variant exists. */
  gitBranchDelete: (path: string, branch: string) =>
    mutateJson<GitWriteResult>("POST", "/api/git/branch/delete", {
      path,
      branch,
    }),
  /** Whole files, either direction. Never by hunk. */
  gitStage: (
    path: string,
    paths: string[],
    staged: boolean,
    expect?: Record<string, string>,
  ) =>
    mutateJson<GitWriteResult>("POST", "/api/git/stage", {
      path,
      paths,
      staged,
      expect,
    }),
  /** The one destructive call. `expect` carries the `fp` of each row the confirmation showed,
   *  and the server re-reads inside its lock: a file the session agent edited while the dialog
   *  was open no longer matches, so the confirmed bytes are what gets discarded — or nothing is.
   *  Binding to the pathname alone meant "discard whatever is there when the command runs". */
  gitDiscard: (
    path: string,
    paths: string[],
    expect?: Record<string, string>,
    /** #950: "head" is REVERT FILE — index and worktree back to `head`. Default restores from the
     *  index, as before. */
    from?: "index" | "head",
    head?: string,
  ) =>
    mutateJson<GitWriteResult>("POST", "/api/git/discard", {
      path,
      paths,
      expect,
      from,
      head,
    }),
  /** `expect` is the whole staged SET (`staged_fp`), not the listed rows: `git commit` records
   *  the index, so a file staged after the panel read it would otherwise ride along unseen. */
  gitCommit: (path: string, message: string, expect?: string) =>
    mutateJson<GitWriteResult>("POST", "/api/git/commit", {
      path,
      message,
      expect,
    }),
  /** COMMIT SELECTED / ALL (#950): exactly `paths` as they are now, bound to their row
   *  fingerprints and to the commit the panel showed. The rest of the index is left alone. */
  gitCommitPaths: (
    path: string,
    message: string,
    paths: string[],
    expect: Record<string, string>,
    head: string,
  ) =>
    mutateJson<GitWriteResult>("POST", "/api/git/commit-paths", {
      path,
      message,
      paths,
      expect,
      head,
    }),
  /** SETTLE (#950): bring the index up to `commit` (which must still be HEAD) where it still holds
   *  the parent's entries. Idempotent; never consumes newer staging. */
  gitSettle: (path: string, commit: string) =>
    mutateJson<GitWriteResult>("POST", "/api/git/settle", { path, commit }),
  /** REVERT A COMMIT (#950): a new commit undoing `commit`, bound to the `head` the panel showed.
   *  Never `git revert`: computed without touching the tree, published by compare-and-swap. */
  gitRevert: (path: string, commit: string, head: string, branch: string) =>
    mutateJson<GitWriteResult>("POST", "/api/git/revert", { path, commit, head, branch }),
  /** Current branch to a server-resolved target; never --force, never a client refspec. */
  gitPush: (path: string, remote?: string, expect?: string) =>
    // `expect` is the target the panel DISPLAYED. The server refuses if it has since resolved
    // elsewhere, so a config change between the preflight and the click cannot silently redirect
    // the push somewhere the operator was never shown.
    mutateJson<GitWriteResult>("POST", "/api/git/push", {
      path,
      remote,
      expect,
    }),
  /** FILES panel (#807): mint a batch reservation from a manifest, so an over-budget folder drop
   *  fails before a single byte moves. */
  filesUploadBatch: (files: { relpath: string; size: number }[]) =>
    // `mutateJson`, not `postJson`: the server names the limit it refused on ("that drop is
    // 310 MB — the limit is 250 MB") and the generic wrapper replaced it with
    // `POST /api/files/upload/batch → 413`, which tells the operator nothing.
    mutateJson<UploadBatch>("POST", "/api/files/upload/batch", { files }),
  /** FILES panel (#807): the operator chose Skip on a collision. Terminal, so the server can
   *  settle that manifest entry — going quiet left the batch holding its slot for the whole TTL. */
  filesUploadSkip: (batchId: string, relpath: string) =>
    mutateJson<{ skipped: boolean }>("POST", "/api/files/upload/skip", {
      batch_id: batchId,
      relpath,
    }),
  /** FILES panel (#807): one file into the browsed directory. One file per REQUEST — the relay
   *  buffers a whole body in browser and agent memory, so a folder is many bounded requests. */
  filesUpload: (
    dir: string,
    relpath: string,
    file: File,
    opts: {
      onCollision?: "fail" | "keep_both" | "replace";
      batchId?: string;
    } = {},
  ) => {
    const form = new FormData();
    // Order matters and is part of the route's contract: the server opens the destination when
    // the FILE part starts, so `dir`/`relpath` must already have been parsed by then.
    form.append("dir", dir);
    form.append("relpath", relpath);
    form.append("on_collision", opts.onCollision ?? "fail");
    if (opts.batchId) form.append("batch_id", opts.batchId);
    form.append("file", file, relpath.split("/").pop() || "upload");
    return postForm<UploadResult>("/api/files/upload", form);
  },
  /** Remove the ENTITY only (#361): members revert to folder grouping on the next
   *  resolve — session files are never touched. CSRF-guarded. */
  deleteProject: (id: string) =>
    deleteJson<{ deleted: boolean; id: string }>(`/api/projects/${enc(id)}`),
  /** Bulk archive/unarchive every member session (#361 Phase 2). Idempotent + blindly
   *  retryable — after a partial failure, re-calling retries only the failed set. */
  archiveProject: (id: string) =>
    mutateJson<ProjectArchiveReport>(
      "POST",
      `/api/projects/${enc(id)}/archive`,
    ),
  unarchiveProject: (id: string) =>
    mutateJson<ProjectArchiveReport>(
      "POST",
      `/api/projects/${enc(id)}/unarchive`,
    ),
  /** Session → project assignment (#361): one sidecar metadata write. `null`/"" clears;
   *  an unknown project id is a 422. Engine stores stay read-only. */
  setSessionProject: (sid: string, projectId: string | null) =>
    patchJson<{ id: string; project_id: string }>(
      `/api/sessions/${enc(sid)}/metadata`,
      {
        project_id: projectId,
      },
    ),
  /** One page of the session listing. `init.signal` cancels the request (#1007 Phase 2: the map
   *  aborts its paging sequence when the route unmounts), the same seam `filesRead`/`gitDiff` use.
   *  It reaches whichever fetch is bound AT CALL TIME — `getJson` reads the `apiFetch` binding per
   *  request, so a `setApiFetch` swap to the Home Free tunnel is honoured. Omitted, the request is
   *  exactly what it was before: the sidebar's `useSessionsList` passes nothing. An abort stops the
   *  browser waiting; it cannot stop a server scan already running in a worker thread. */
  sessions: (q?: SessionsQuery, init?: RequestInit) =>
    getJson<SessionsPage>(sessionsUrl(q), init),
  /** ONE session row by id (#867) — what the session pane reads when the sidebar's page
   *  doesn't hold it. Unlike `sessions()` this is a lookup, not a listing: it ignores the
   *  list's pagination, filters, archived tab and visibility scope, so a deep-linked or
   *  hidden session can still name its project and folder. 404 when nothing has that id. */
  session: (id: string) => getJson<Session>(`/api/sessions/${enc(id)}`),
  rename: (id: string, title: string) =>
    mutateJson<{ id: string; title: string }>("POST", `/api/sessions/${enc(id)}/rename`, {
      title,
    }),
  /** Set (or clear, with "") a session's custom tag (#551): a short label shown before the
   *  AI summary in the sidebar row. Trimmed + length-capped server-side. */
  setTag: (id: string, tag: string) =>
    mutateJson<{ id: string; tag: string }>("POST", `/api/sessions/${enc(id)}/tag`, {
      tag,
    }),
  /** Favorite/unfavorite a session (#122): flips the sidecar `sticky` flag so the row
   *  pins to the top of the sidebar. Engine-agnostic; CSRF-guarded. Returns `{id, sticky}`. */
  favorite: (id: string) =>
    mutateJson<{ id: string; sticky: boolean }>("POST",
      `/api/sessions/${enc(id)}/favorite`,
    ),
  unfavorite: (id: string) =>
    mutateJson<{ id: string; sticky: boolean }>("POST",
      `/api/sessions/${enc(id)}/unfavorite`,
    ),
  /** Compose draft (#477): fetch the saved draft (text + attachment pills) to restore the
   *  box when a session is reopened. Returns an empty draft when there is none. */
  getDraft: (id: string) =>
    getJson<SessionDraft>(`/api/sessions/${enc(id)}/draft`),
  /** Save (or clear) the compose draft for a session (#477). Empty text + no attachments
   *  clears it. CSRF-guarded sidecar write; returns whether a draft now exists (the dot). */
  saveDraft: (
    id: string,
    draft: { text: string; attachments: DraftAttachment[] },
  ) =>
    putJson<{ id: string; has_draft: boolean }>(
      `/api/sessions/${enc(id)}/draft`,
      draft,
    ),
  archive: (id: string) =>
    mutateJson<{ id: string; archived: boolean }>("POST",
      `/api/sessions/${enc(id)}/archive`,
    ),
  unarchive: (id: string) =>
    mutateJson<{ id: string; archived: boolean }>("POST",
      `/api/sessions/${enc(id)}/unarchive`,
    ),
  /** Bulk-archive every non-archived session older than `hours` (#142). CSRF-guarded. */
  archiveOlder: (hours: number) =>
    postJson<{ archived: number; skipped: number }>(
      "/api/sessions/archive-older",
      { hours },
    ),
  /** One page of older transcript history for scroll-up lazy-load (#348 Phase 3). GET —
   *  no CSRF. `before` is the exact turn boundary: seeded from the attach's {"t":"hist"}
   *  frame for the first page, then the returned `cursor` for each next-older page.
   *  Omitting it (no hist frame received) gets the server's width-independent
   *  APPROXIMATE fallback — everything older than the newest page-sized turn window. */
  history: (
    id: string,
    q: { before?: number; lines?: number; cols?: number } = {},
  ) => {
    const p = new URLSearchParams();
    if (q.before !== undefined) p.set("before", String(q.before));
    if (q.lines !== undefined) p.set("lines", String(q.lines));
    if (q.cols !== undefined) p.set("cols", String(q.cols));
    const qs = p.toString();
    return getJson<HistoryPage>(
      `/api/sessions/${enc(id)}/history${qs ? `?${qs}` : ""}`,
    );
  },
  /** AI review (#356): server-proxied model listing from the configured endpoint — the
   *  API key never reaches the browser. 400 = not configured, 502 = endpoint can't list
   *  (the Settings dropdown falls back to free-text entry). */
  /** The endpoint-validation probe (#394): `getJsonWithDetail` so a 502 arrives as the
   *  gateway's own message — the panel renders it verbatim (#382, #834). */
  aiReviewModels: (opts?: { refresh?: boolean }) =>
    getJsonWithDetail<{ models: string[] }>(
      `/api/ai-review/models${opts?.refresh ? "?refresh=1" : ""}`,
    ),
  /** Check a DRAFT AI connection without saving it (#956). CSRF-guarded. The stored key is used
   *  only when `base_url` is on the origin it was saved for; any other origin needs `api_key`
   *  (422 otherwise, with no request made). `listing: "unsupported"` = the endpoint answered but
   *  can't list models; a 502 carries the gateway's own (key-redacted) reason. */
  testAiEndpoint: (body: { base_url: string; api_key?: string }) =>
    mutateJson<{ models: string[]; listing: "ok" | "unsupported" }>(
      "POST",
      "/api/ai-review/endpoint/test",
      body,
    ),
  /** AI review (#356): manual "Review now" for one session. CSRF-guarded. 409 when the
   *  endpoint isn't configured; 502 when the review failed (last good result stays).
   *  `mutateJson` so the server's error `detail` (gateway timeout, endpoint HTTP status)
   *  reaches the outcome toast (#392) instead of a generic "POST … → 502". */
  reviewNow: (id: string) =>
    mutateJson<{
      id: string;
      title: string;
      ai_summary: string;
      ai_title: string;
      intervention_required: boolean;
      intervention_reason: string;
      reviewed_at: number | null;
      review_excluded: boolean;
      /** #481: chronological whole-session recap, refreshed by this review. */
      ai_recap: string;
      recap_fingerprint: string;
    }>("POST", `/api/sessions/${enc(id)}/review`),
  /** AI review (#356): set (or toggle, when `excluded` is omitted) the per-session
   *  exclude-from-review flag. CSRF-guarded. */
  reviewExclude: (id: string, excluded?: boolean) =>
    mutateJson<{ id: string; review_excluded: boolean }>("POST",
      `/api/sessions/${enc(id)}/review-exclude`,
      excluded === undefined ? undefined : { excluded },
    ),
  /** Pulse recent-work overview (#441): the cached artifact, served instantly (never scans).
   *  `generated_at` null = never scanned (the empty overview). */
  pulse: () => getJson<PulseOverview>("/api/pulse"),
  /** Pulse "Scan now" (#441): run one scan and return the fresh artifact. Uses the configured
   *  window/depth; `depth`/`window_days` override per-request (the page's depth control). The
   *  only 409 is "a Pulse scan is already running" — its body carries the AI-activity snapshot
   *  (`mutateJson` surfaces the `detail`). An unconfigured endpoint never 409s: depth ≥ medium
   *  returns 200 with `synthesis_skipped`. CSRF-guarded. */
  pulseScan: (opts?: { depth?: PulseDepth; window_days?: number }) =>
    mutateJson<PulseOverview>("POST", "/api/pulse/scan", opts ?? {}),
  /** Pulse "Ask" (#522): one natural-language question over past sessions. `history` is
   *  the replayed conversation tail (the server clamps it again). 409 = endpoint
   *  unconfigured or a question already running; 502 = endpoint failure — `mutateJson`
   *  surfaces the server `detail` either way. CSRF-guarded. */
  pulseAsk: (
    query: string,
    history: { role: "user" | "assistant"; content: string }[],
  ) => mutateJson<PulseAskResult>("POST", "/api/pulse/ask", { query, history }),
  /** The same ask, streamed (#1171): `onEvent` sees each NDJSON event as the server sends it —
   *  the step it is on, the Stage-1 answer (`final: false`) the moment it exists, then the final
   *  one. Resolves after the last event. A refusal before the stream (422 / 409) throws like
   *  `pulseAsk`; a failure after it started arrives as `{type: "error"}` and throws the same way,
   *  so a caller has one error path either way. Aborting `signal` closes the connection, which
   *  ends the server's run and frees its one-question gate. */
  pulseAskStream: (
    query: string,
    history: { role: "user" | "assistant"; content: string }[],
    onEvent: (ev: PulseAskEvent) => void,
    signal?: AbortSignal,
  ) => streamAsk(query, history, onEvent, signal),
  /** Pulse orchestrator state (#726): config + pending actions + the activity feed. Like
   *  `pulse()` this is CACHE-ONLY — it never runs a pass. */
  orchestrator: () => getJson<OrchestratorState_>("/api/pulse/orchestrator"),
  /** Run one orchestrator pass now (#726). 409 = unconfigured endpoint or a pass already
   *  running; 502 = endpoint failure. Deliberately unlike `pulseScan`, which degrades to a
   *  200: a decision has no useful non-LLM fallback, so it says so rather than returning an
   *  empty action list that reads as "nothing needs you". CSRF-guarded. */
  orchestrate: () =>
    mutateJson<OrchestratorState_ & { assessment: string }>(
      "POST",
      "/api/pulse/orchestrate",
      {},
    ),
  /** Approve one orchestrator action and deliver it (#726 Phase 2). Compare-and-execute: the
   *  server re-verifies the screen immediately before writing, so a session that moved on comes
   *  back 409 rather than receiving input meant for a different prompt. CSRF-guarded. */
  approveAction: (id: string, text?: string) =>
    mutateJson<OrchestratorAction>(
      "POST",
      `/api/pulse/actions/${enc(id)}/approve`,
      // #1086: the operator's EDITED text for a standalone answer/continue. The server turns it
      // into an operator-authored `relay`; omitted, the approve is exactly what it always was.
      text === undefined ? {} : { text },
    ).then(announceActionResolved),
  /** The Ask page's NEEDS YOU list (#1086): sessions no mission holds that need the operator. */
  needsYou: (params: { window_days?: number; engine?: string; project?: string } = {}) => {
    const q = new URLSearchParams();
    if (params.window_days !== undefined) q.set("window_days", String(params.window_days));
    if (params.engine) q.set("engine", params.engine);
    if (params.project) q.set("project", params.project);
    const qs = q.toString();
    return getJsonWithDetail<NeedsYouPayload>(`/api/pulse/needs-you${qs ? `?${qs}` : ""}`);
  },
  /** Details for one NEEDS YOU row — read-only, and no viewer is attached (#1086). */
  needsYouDetails: (sessionId: string) =>
    getJsonWithDetail<NeedsYouDetails>(`/api/pulse/needs-you/${enc(sessionId)}/details`),
  /** Hide a row until its screen changes; reject `actionId` too when given (#1086). */
  needsYouDismiss: (sessionId: string, actionId?: string) =>
    mutateJson<{ dismissed: boolean; rejected: boolean }>(
      "POST",
      `/api/pulse/needs-you/${enc(sessionId)}/dismiss`,
      actionId ? { action_id: actionId } : {},
    ),
  /** RECENT WORK above Ask (#1086). Never calls the model. */
  recentWork: (windowDays?: number) =>
    getJsonWithDetail<RecentWorkPayload>(
      `/api/pulse/recap${windowDays !== undefined ? `?window_days=${windowDays}` : ""}`,
    ),
  /** Write a fresh RECENT WORK summary (one completion; a no-op when nothing changed). */
  refreshRecentWork: (windowDays?: number) =>
    mutateJson<RecentWorkPayload>(
      "POST",
      "/api/pulse/recap",
      windowDays !== undefined ? { window_days: windowDays } : {},
    ),
  /** Answer an escalated menu with option `n` (#1060 Phase 3). `label` is the text the card showed
   *  for it; the server refuses unless both still match the live screen. Returns the settled
   *  escalation, with the delivered choice under `choice`. */
  chooseAction: (id: string, option: number, label: string) =>
    mutateJson<OrchestratorAction>(
      "POST",
      `/api/pulse/actions/${enc(id)}/choose`,
      { option, label },
    ).then(announceActionResolved),
  /** Decline an action (#726 Phase 2). Terminal — the ledger keeps it as history. */
  rejectAction: (id: string) =>
    mutateJson<OrchestratorAction>(
      "POST",
      `/api/pulse/actions/${enc(id)}/reject`,
      {},
    ).then(announceActionResolved),
  /** In-app notifications (#726 Phase 3) — the channel that always works, regardless of push
   *  permission or platform. */
  notifications: () => getJson<NotificationList>("/api/pulse/notifications"),
  /** Mark notifications read; omit `ids` to mark all. CSRF-guarded. */
  markNotificationsRead: (ids?: string[]) =>
    mutateJson<NotificationList & { marked: number }>(
      "POST",
      "/api/pulse/notifications/read",
      ids ? { ids } : {},
    ),
  /** Remove notifications: the given ids, or every one with `all`. CSRF-guarded.
   *
   *  Fails CLOSED server-side, unlike `/read` — a malformed body is a 422 rather than being
   *  read as "everything" (#752). So the all-scope has to be asked for in as many words; there
   *  is deliberately no `dismissNotifications()` shorthand that clears the bell by accident. */
  dismissNotifications: (arg: { ids: string[] } | { all: true }) =>
    mutateJson<NotificationList & { dismissed: number }>(
      "POST",
      "/api/pulse/notifications/dismiss",
      arg,
    ),
  /** The VAPID PUBLIC key + registered devices. The private half never leaves the server, and
   *  a device is identified by an opaque id + origin — never its endpoint URL, which is a
   *  capability anyone holding it could push with. */
  pushKey: () =>
    getJson<{ public_key: string; subscriptions: PushSubscriptionInfo[] }>(
      "/api/pulse/push/key",
    ),
  pushSubscribe: (subscription: unknown) =>
    mutateJson<PushSubscriptionInfo>("POST", "/api/pulse/push/subscribe", {
      subscription,
    }),
  pushUnsubscribe: (id: string) =>
    mutateJson<{ removed: boolean; subscriptions: PushSubscriptionInfo[] }>(
      "POST",
      "/api/pulse/push/unsubscribe",
      { id },
    ),
  /** Server-pulled evidence for one session (#726). Fetched at render time, never cached and
   *  never stored in the ledger, so the operator always reads the CURRENT screen. */
  evidence: (sessionId: string, kind: EvidenceKind) =>
    getJson<Evidence>(
      `/api/pulse/evidence/${enc(sessionId)}?kind=${encodeURIComponent(kind)}`,
      {
        // "Live" must mean live: never let the HTTP cache answer this one.
        cache: "no-store",
      },
    ),
  /** The live screen of a session a MISSION holds (#903 review 3, finding 1).
   *
   *  Not `evidence(key, "screen")`, and the difference is the whole point: that route is
   *  mission-agnostic, so an already-open block for mission A goes on rendering the same key
   *  after the session has been detached and adopted into B — B's live output, under A's
   *  heading. Membership is a row another tab can change, so it is checked SERVER-SIDE at
   *  request time and a session the mission no longer holds is a 409. */
  missionScreen: (missionId: string, sessionKey: string) =>
    getJson<Evidence>(
      `/api/missions/${enc(missionId)}/screen/${enc(sessionKey)}`,
      { cache: "no-store" },
    ),

  /** Per-session Pulse-orchestration opt-out (#726). Managed-by-default; this withdraws (or
   *  restores) agency for ONE session without touching its AI review. CSRF-guarded. */
  setOrchestratorExcluded: (id: string, excluded?: boolean) =>
    mutateJson<{ id: string; orchestrator_excluded: boolean }>(
      "POST",
      `/api/sessions/${enc(id)}/orchestrator-exclude`,
      excluded === undefined ? undefined : { excluded },
    ),
  /** Shared AI-activity surface (#441): AI tasks running now + the last run per kind. The
   *  Settings panel polls it; read-only, no CSRF. */
  aiActivity: () => getJson<AiActivity>("/api/ai/activity"),
  /** Persisted-scrollback cache size, for the Settings cache panel (#206). */
  scrollbackInfo: () =>
    getJson<{ bytes: number; files: number }>("/api/scrollback"),
  /** Clear the persisted-scrollback cache — scope "all" or "archived" (#206). CSRF-guarded. */
  clearScrollback: (scope: "all" | "archived") =>
    postJson<{ scope: string; removed: number; bytes_freed: number }>(
      "/api/scrollback/clear",
      {
        scope,
      },
    ),
  // Settings → Maintenance (#993). Dry runs are plain GETs; mutations are CSRF-guarded
  // and share one server-side runner, so a submission while any maintenance job runs is a 409
  // (`ApiError.status`) whose body names the running job — refused, not queued.
  /** Poll a known job, or discover the latest. Unknown/replaced/restarted ids return 404. */
  // `engine` names WHICH store to compact (#853 P3/P4: every engine whose manifest selects the
  // sqlite-vacuum kind is a target); omitted, the server uses its default target.
  compactInfo: (jobId?: string, engine?: string) => {
    const q = new URLSearchParams();
    if (jobId) q.set("job_id", jobId);
    if (engine) q.set("engine", engine);
    const qs = q.toString();
    return getJson<CompactInfo>(`/api/maintenance/compact${qs ? `?${qs}` : ""}`);
  },
  compact: (engine?: string) =>
    mutateJson<Pick<CompactInfo, "job" | "runner">>(
      "POST",
      "/api/maintenance/compact",
      engine ? { confirm: true, engine } : { confirm: true },
    ),
  /** What a cache prune would remove now, per category, plus the runner's busy state. */
  pruneInfo: () =>
    getJson<{
      categories: Record<
        "stale_sockets" | "archived_scrollback",
        { items: number; bytes: number; error?: string }
      >;
      runner: { job: string; started_at: number | null } | null;
    }>("/api/maintenance/prune"),
  /** Remove the selected cache categories. Session history is never touched. */
  prune: (categories: ("stale_sockets" | "archived_scrollback")[]) =>
    mutateJson<{
      removed: number;
      bytes_freed: number;
      skipped: { category: string; reason: string; count: number }[];
      failed: { category: string; item: string; reason: string }[];
      failed_total: number;
    }>("POST", "/api/maintenance/prune", { categories }),
  /** What "Archive old missions" would do for missions older than `days`. */
  archiveOldMissionsInfo: (days: number) =>
    getJson<{
      eligible: number;
      sessions: number;
      live_sessions: number;
      unresolved: string[];
      runner: { job: string; started_at: number | null } | null;
    }>(`/api/missions/archive-older?older_than_days=${encodeURIComponent(String(days))}`),
  /** Archive every finished mission older than `days` — never abandons a live one. */
  archiveOldMissions: (days: number) =>
    mutateJson<{
      archived: number;
      sessions_archived: number;
      terminals_stopped: number;
      skipped: { mission_id: string; reason: string }[];
      failed: { mission_id: string; session_key: string | null; reason: string }[];
    }>("POST", "/api/missions/archive-older", { older_than_days: days }),
  // -------------------------------------------------------------------------------------------
  // Missions (#846 / #862). Every route is `logged_in`; the mutating ones are CSRF-guarded.
  // -------------------------------------------------------------------------------------------

  /** The rail's list. `archived` scopes it; `q` / `project` / `state` filter the FULL set before
   *  the page window, so `total` and the facets describe the filtered result (#840). */
  missions: (opts?: {
    q?: string;
    project?: string;
    state?: string;
    archived?: boolean;
    limit?: number;
    offset?: number;
  }) => {
    const p = new URLSearchParams();
    if (opts?.q) p.set("q", opts.q);
    if (opts?.project) p.set("project", opts.project);
    if (opts?.state) p.set("state", opts.state);
    if (opts?.archived) p.set("archived", "1");
    if (opts?.limit != null) p.set("limit", String(opts.limit));
    if (opts?.offset != null) p.set("offset", String(opts.offset));
    const q = p.toString();
    return getJson<MissionList>(`/api/missions${q ? `?${q}` : ""}`);
  },

  /** One mission with a page of its timeline. `before` is the cursor from a previous page's
   *  `events_next_seq` — older events, never a page offset, so a new event arriving between
   *  pages cannot shift the window and duplicate or skip a row. */
  mission: (
    id: string,
    opts?: { eventsLimit?: number; before?: number | null },
  ) => {
    const p = new URLSearchParams();
    if (opts?.eventsLimit != null)
      p.set("events_limit", String(opts.eventsLimit));
    if (opts?.before != null) p.set("events_before_seq", String(opts.before));
    const q = p.toString();
    return getJson<Mission>(
      `/api/missions/${encodeURIComponent(id)}${q ? `?${q}` : ""}`,
    );
  },

  /** Folder, git summary and session roster. Sends NO path — the server resolves the mission's
   *  own cwd, which is the property that makes it traversal-proof. */
  missionContext: (id: string) =>
    getJson<MissionContext>(`/api/missions/${encodeURIComponent(id)}/context`),

  /** What each held session is doing right now (#1064): derived status only, no screen text. */
  missionNow: (id: string) =>
    getJson<MissionNow>(`/api/missions/${encodeURIComponent(id)}/now`),

  missionObjectives: (id: string) =>
    getJson<{ objectives: MissionObjective[] }>(
      `/api/missions/${encodeURIComponent(id)}/objectives`,
    ),

  /** Operator edits only — add / drop / retitle / waive / reorder. `state` and `met_at` are not
   *  writable on this route at all, so an edit can never retroactively mark an objective met. */
  patchMissionObjectives: (id: string, ops: Record<string, unknown>[]) =>
    patchJson<{ objectives: MissionObjective[] }>(
      `/api/missions/${encodeURIComponent(id)}/objectives`,
      { ops },
    ),

  /** The three DIRECTION ops (#983), one objective each, on the same guarded objectives route. Only
   *  the operator writes a direction; an unknown placeholder is a 422 in the server's words. */
  setObjectiveDirection: (id: string, key: string, direction: string) =>
    patchJson<{ objectives: MissionObjective[] }>(
      `/api/missions/${encodeURIComponent(id)}/objectives`,
      { ops: [{ op: "set_direction", key, direction }] },
    ),
  /** Copy the playbook's CURRENT direction for this objective again. A copy, never a link. */
  resetObjectiveDirection: (id: string, key: string) =>
    patchJson<{ objectives: MissionObjective[] }>(
      `/api/missions/${encodeURIComponent(id)}/objectives`,
      { ops: [{ op: "reset_direction", key }] },
    ),
  /** No direction: a nudge for this objective types the default nudge. */
  clearObjectiveDirection: (id: string, key: string) =>
    patchJson<{ objectives: MissionObjective[] }>(
      `/api/missions/${encodeURIComponent(id)}/objectives`,
      { ops: [{ op: "clear_direction", key }] },
    ),

  /** What a direction would type for a `probe` objective, filled with EXAMPLE facts by the server's
   *  one renderer (#983 P2). Read-only; refuses an unknown placeholder with the save's 422 words. */
  previewDirection: (direction: string, probe: string) =>
    mutateJson<DirectionPreview>("POST", DIRECTION_PREVIEW_PATH, { direction, probe }),

  /** Type the operator's OWN words into one of a mission's sessions (#894).
   *
   *  Not a second path to a PTY: the server builds a ledger action and hands it to the actuator,
   *  so the write takes the single-writer lock, the liveness check at the write boundary, the
   *  viewer-busy precondition and the mission fence — the same door every other write uses.
   *
   *  `mutateJson` because the refusals are the interesting part and each has a different fix:
   *  "this mission does not hold that session", "session is not live", "a viewer is attached".
   */
  relayToSession: (
    id: string,
    sessionKey: string,
    text: string,
    /** #983 P3: the AI-drafted direction this relay replaces. The server closes the draft BEFORE
     *  it records the relay, and a draft that is no longer waiting is a 409 with nothing sent. */
    replacesDraft?: string,
  ) =>
    mutateJson<{
      action_id: string;
      state: string;
      detail?: string;
      session_key: string;
      /** Present whenever `replacesDraft` was sent and the server got as far as the draft. */
      draft_replaced?: boolean;
      replaced_draft?: string;
    }>("POST", `/api/missions/${encodeURIComponent(id)}/relay`, {
      session_key: sessionKey,
      text,
      ...(replacesDraft ? { replaces_draft: replacesDraft } : {}),
    }),
  /** One durable operator turn on a mission (#871, wired in #890).
   *
   *  **`turnId` is minted once per send and REUSED on every retry**, which is what makes the
   *  route's idempotency real rather than decorative: one model execution per id, across crashes
   *  and retries, with each produced action delivered at most once. A fresh id per attempt would
   *  make every retry a new execution — precisely the double-instruct the route was built to
   *  prevent — so it is the caller's job to keep it stable, and the caller here is the composer.
   *
   *  `mutateJson` so the server's own `detail` survives: "turn_id was already used for a
   *  different message" and "a question is already running" are different problems with
   *  different fixes, and a flattened failure tells the operator neither (#834). */
  missionMessage: (id: string, body: { message: string; turnId: string }) =>
    mutateJson<MissionTurn>(
      "POST",
      `/api/missions/${encodeURIComponent(id)}/message`,
      { message: body.message, turn_id: body.turnId },
    ),

  /** `missionMessage`, streamed (#1224) — what the composer sends. The steps and a find turn's
   *  provisional answer arrive through `onEvent` while it runs; resolves with the same
   *  `MissionTurn`. `missionMessage` stays the plain route's client. */
  missionMessageStream: streamMissionMessage,

  /** Dismiss an AMBIGUOUS turn. Only `indeterminate` is dismissible — the server cannot resolve
   *  it, so "I have seen this" has to be durable or the banner returns on every reload (#890). */
  ackMissionTurn: (id: string, turnId: string) =>
    mutateJson<{ turn_id: string; acked: boolean }>(
      "POST",
      `/api/missions/${encodeURIComponent(id)}/turns/${encodeURIComponent(turnId)}/ack`,
      {},
    ),
  /** Answer the mission's open question (#892).
   *
   *  **The INDEX, never the label.** The option's label is display text the model wrote; the
   *  action it maps to lives in the server's closed set and is looked up there. Sending the
   *  label back would be the one thing the whole design refuses — model text deciding what runs.
   *
   *  `seq` names WHICH question is being answered, so a stale console answering a superseded
   *  question is a 409 rather than a second application of an action. `mutateJson` so the
   *  server's `detail` — "that question is no longer the open one" — reaches the operator (#834).
   */
  answerMissionQuestion: (
    id: string,
    seq: number,
    answer: { optionIndex?: number; text?: string },
  ) =>
    mutateJson<{
      action: string;
      objective: string;
      answer: string;
      applied: string;
      /** Whether the chosen EFFECT happened. Not derivable from `applied`, which is prose. */
      applied_ok: boolean;
    }>("POST", `/api/missions/${encodeURIComponent(id)}/answer`, {
      seq,
      ...(answer.optionIndex != null
        ? { option_index: answer.optionIndex }
        : {}),
      ...(answer.text ? { text: answer.text } : {}),
    }),

  /** Take a live session into a mission. Membership is exclusive: a session already held by
   *  another mission comes back 409 NAMING the holder, so the operator is told where it went
   *  rather than being told "no". `mutateJson` so that detail survives (#834). */
  /** Produce a dispatch PROPOSAL. Launches nothing — that is a separate, explicit call (#893).
   *  `mutateJson` because a refusal carries a `detail` the operator needs (no AI endpoint
   *  configured, the mission is already running). */
  planMission: (id: string) =>
    mutateJson<MissionPlan>(
      "POST",
      `/api/missions/${encodeURIComponent(id)}/plan`,
      {},
    ),

  /** The operator's own edit of the proposal. Sends a PROJECT ID, never a path — the server
   *  resolves the working directory. Every edit comes back with a NEW `plan_id`.
   *
   *  **`planId` is the proposal being edited, and it is a comparand, not a hint.** This route
   *  replaces the whole row, so two tabs editing different fields of one plan would both be told
   *  the save worked while the later write restored its own stale copy of the other's field. The
   *  server compares it inside the transaction and answers 409 (#904 review 6). */
  editMissionPlan: (
    id: string,
    planId: string,
    body: {
      project_id?: string | null;
      engine?: string | null;
      brief?: string;
    },
  ) =>
    mutateJson<MissionPlan>(
      "PATCH",
      `/api/missions/${encodeURIComponent(id)}/plan`,
      { plan_id: planId, ...body },
    ),

  /** PLAN MANUALLY: the FIRST plan of a mission whose planning was `skipped` (no AI endpoint) or
   *  `failed` (#967 P2b). The same authenticated, CSRF-guarded `PATCH /plan` as an edit, and
   *  deliberately WITHOUT `plan_id`: there is no plan to name, and the server accepts this shape only
   *  while no plan exists and planning is not running, so it cannot overwrite one. A project ID and
   *  an engine ID, never a path. `mutateJson`, so a 422 carries the server's own `detail`. */
  firstMissionPlan: (
    id: string,
    body: { project_id: string; engine: string; brief: string },
  ) =>
    mutateJson<MissionPlan>(
      "PATCH",
      `/api/missions/${encodeURIComponent(id)}/plan`,
      {
        project_id: body.project_id,
        engine: body.engine,
        brief: body.brief,
      },
    ),

  /** RUN the proposal. The highest-privilege call the client can make: it starts an agent with
   *  nobody watching it, so it names the plan it is dispatching and the server refuses any other
   *  (`claim_plan`'s compare-and-set). A stale id is a 409 that says to read the plan again. */
  /** Start a bounded, approval-gated SUB-AGENT alongside one of this mission's sessions (#894).
   *
   *  `engine` is a REQUEST, not a choice: the server checks it against the same capability
   *  allowlist the plan picker is built from, so `shell` and the mint-own-id engines are refused
   *  there rather than trusted here. There is no `cwd` argument at all — the server resolves it
   *  from the project entity, which is why a client cannot say where an agent runs. */
  /** `expectCwd` is the directory the panel SHOWED, asserted back so the approval binds to it.
   *  It is a comparand the server compares and discards — never a launch argument, and never a
   *  path this client gets to choose. A mismatch is a 409, not a silent relocation. */
  spawnSubAgent: (
    id: string,
    parentKey: string,
    brief: string,
    engine: string,
    expectCwd: string,
  ) =>
    mutateJson<{
      state: string;
      /** The ATTEMPT's verdict — `started` / `failed` / `refused`. The mission's `state` answers a
       *  different question and is deliberately `running` even when the child failed. */
      outcome?: "started" | "failed" | "refused";
      reason: string;
      session_key: string | null;
    }>(
      "POST",
      `/api/missions/${encodeURIComponent(id)}/spawn`,
      { parent_key: parentKey, brief, engine, expect_cwd: expectCwd },
    ),

  dispatchMission: (
    id: string,
    planId: string,
    expectCwd: string,
    expectObjectives: string,
  ) =>
    mutateJson<{ state: string; reason: string; session_key: string | null }>(
      "POST",
      `/api/missions/${encodeURIComponent(id)}/dispatch`,
      // `expect_cwd` is a COMPARAND, never a launch argument: the server resolves the path from
      // the project entity and refuses if what it resolves is not what the operator confirmed
      // (#904 review 2, finding 6). The client still cannot choose where an agent runs.
      // …and `expect_objectives` is the same kind of assertion about WHAT DONE MEANS: the
      // checklist the card showed, digested, so "settled" and "the one you read" are one check
      // (#904 review 3, finding 5).
      {
        plan_id: planId,
        expect_cwd: expectCwd,
        expect_objectives: expectObjectives,
      },
    ),

  adoptMissionSession: (id: string, sessionKey: string) =>
    mutateJson<Mission>(
      "POST",
      `/api/missions/${encodeURIComponent(id)}/adopt`,
      {
        session_key: sessionKey,
      },
    ),

  /** Start a mission from an instruction. #889.
   *
   *  **`cwd` is never sent and the route refuses it outright (422).** The server resolves the
   *  working directory from `project_id`, which is what keeps a mission's cwd out of the
   *  client's hands — a field the server must author cannot also be one the client may supply.
   *
   *  Returns 201 immediately: the objective list is produced by a background task, so the
   *  response carries `objectives_state: "pending"` and the pane says so rather than rendering
   *  an empty checklist as "no objectives". */
  createMission: (body: {
    instruction: string;
    title?: string;
    project_id?: string | null;
    playbook_id?: string | null;
  }) => mutateJson<Mission>("POST", "/api/missions", body),

  /** The mission's opt-in to autonomous menu answers (#1060 Phase 4). Operator-only on the server;
   *  turning it off always succeeds and withdraws any answer still in flight. */
  setMissionAutoChoose: (id: string, on: boolean) =>
    mutateJson<{ id: string; auto_choose: boolean }>(
      "PATCH",
      `/api/missions/${encodeURIComponent(id)}/autonomy`,
      { auto_choose: on },
    ),

  /** Compare-and-set the lifecycle state. #889.
   *
   *  `from` is REQUIRED and is the state the client believes the mission is in — the server
   *  compares it rather than re-reading, because a state read before an await cannot be trusted
   *  after it. A lost race is a **409**, never a silent retry, and the caller's job is to re-read
   *  and render what the mission actually is rather than what was asked for. */
  setMissionState: (
    id: string,
    body: { from: string; to: string; outcome?: string; detail?: string },
  ) =>
    mutateJson<Mission>(
      "POST",
      `/api/missions/${encodeURIComponent(id)}/state`,
      body,
    ),

  /** Archive the mission AND tear down its sessions' runtime. #889.
   *
   *  Terminal-state-only: a live mission is a 409, not a prompt. `abandon: true` is the explicit
   *  two-transition path and is only sent after the operator has been told plainly that it stops
   *  the agents. A real boolean, because the route type-checks it — `"false"` is truthy and this
   *  is the flag that authorises terminating agents. */
  archiveMission: (id: string, opts?: { abandon?: boolean }) =>
    mutateJson<{ mission: Mission; sessions?: unknown[] }>(
      "POST",
      `/api/missions/${encodeURIComponent(id)}/archive`,
      { abandon: opts?.abandon === true },
    ),

  /** Reverse an archive. `sessions: false` restores the mission row without unarchiving its
   *  sessions — the default is to bring them back with it. */
  unarchiveMission: (id: string, opts?: { sessions?: boolean }) =>
    mutateJson<{ mission: Mission; sessions?: unknown[] }>(
      "POST",
      `/api/missions/${encodeURIComponent(id)}/unarchive`,
      { sessions: opts?.sessions !== false },
    ),

  /** Release one session from a mission. Fenced server-side: an in-flight nudge either lands
   *  first or sees the new epoch, so a detached session can never still be typed into. */
  detachMissionSession: (id: string, sessionKey: string) =>
    mutateJson<Mission>(
      "POST",
      `/api/missions/${encodeURIComponent(id)}/detach`,
      {
        session_key: sessionKey,
      },
    ),

  /** "Stop telling me about this one", for the EPISODE the operator was looking at (#885).
   *
   *  The episode is required and is the one the board was RENDERED at — not "whatever is current".
   *  If the objective has moved since, the tap is about a situation that no longer exists, and the
   *  server answers 409 with the current episode so the console can re-render instead of silently
   *  silencing a report nobody has seen.
   *
   *  It silences; it does not settle. The objective stays visibly unmet. */
  standDownObjective: (id: string, objectiveKey: string, episode: number) =>
    mutateJson<{ episode: number; stood_down: boolean }>(
      "POST",
      `/api/missions/${encodeURIComponent(id)}/objectives/${encodeURIComponent(objectiveKey)}/stand-down`,
      { episode },
    ),

  // Bundles (#1192). Every mutation uses the loaded revision and the shared CSRF transport.
  playbookAuthoring: () => getJsonWithDetail<AuthoringSchema>("/api/playbooks/authoring/schema"),
  createPlaybook: (files: Files) => mutateJson<PlaybookDetail & PlaybookWriteResult>("POST", "/api/playbooks", { files }),
  savePlaybook: (id: string, revision: string, files: Files) => mutateJson<PlaybookDetail & PlaybookWriteResult>("PUT", `/api/playbooks/${encodeURIComponent(id)}`, { revision, files }),
  copyPlaybookDraft: (files: Files) => mutateJson<PlaybookDetail & PlaybookWriteResult>("POST", "/api/playbooks/authoring/copy", { files }),
  playbooks: () => getJsonWithDetail<PlaybookList>("/api/playbooks"),
  playbook: (id: string) => getJsonWithDetail<PlaybookDetail>(`/api/playbooks/${encodeURIComponent(id)}`),
  duplicatePlaybook: (id: string, revision: string) =>
    mutateJson<PlaybookDetail & PlaybookWriteResult>("POST", `/api/playbooks/${encodeURIComponent(id)}/duplicate`, { revision }),
  deletePlaybook: (id: string, revision: string) =>
    mutateJson<PlaybookWriteResult>("DELETE", `/api/playbooks/${encodeURIComponent(id)}?revision=${encodeURIComponent(revision)}`),
  setDefaultPlaybook: (id: string, revision: string, expectDefault: string | null) =>
    mutateJson<PlaybookWriteResult>("PUT", `/api/playbooks/${encodeURIComponent(id)}/default`, { revision, expect_default: expectDefault }),
  clearDefaultPlaybook: (id: string) =>
    mutateJson<PlaybookWriteResult>("DELETE", `/api/playbooks/${encodeURIComponent(id)}/default`),
  playbookProjects: (id: string) => getJsonWithDetail<PlaybookFleet>(`/api/playbooks/${encodeURIComponent(id)}/projects`),
  verifyPlaybook: (project: string) => getJsonWithDetail<PlaybookVerify>(`/api/projects/${encodeURIComponent(project)}/playbook/verify`),
  reviewPlaybookFleet: (id: string) => mutateJson<PlaybookFleetReview>("POST", `/api/playbooks/${encodeURIComponent(id)}/fleet/review`, {}),
  updatePlaybookFleet: (id: string, digest: string, operationId: string) =>
    mutateJson<PlaybookFleetResult>("POST", `/api/playbooks/${encodeURIComponent(id)}/fleet/update`, { digest, operation_id: operationId }),
  playbookFleetOperation: (id: string, operationId: string) =>
    getJsonWithDetail<PlaybookFleetResult>(`/api/playbooks/${encodeURIComponent(id)}/fleet/${encodeURIComponent(operationId)}`),

  // ---- automations (#1201) -----------------------------------------------------------------------
  automations: () => getJsonWithDetail<AutomationList>(AUTOMATIONS_API),
  automation: (id: string) => getJsonWithDetail<Automation>(autoPath(id)),
  /** Always created OFF: enabling is a separate, consented step. */
  createAutomation: (config: AutomationConfig) =>
    mutateJson<Automation>("POST", AUTOMATIONS_API, config),
  /** Fenced by `revision`. A widening of an enabled automation is a 422 whose body
   *  (`ApiError.record`) is a `ConsentRequired`; resend with `consent` + that `scope_digest`. */
  patchAutomation: (
    id: string,
    body: Partial<AutomationConfig> & {
      revision: number;
      consent?: boolean;
      scope_digest?: string;
    },
  ) => mutateJson<Automation>("PATCH", autoPath(id), body),
  deleteAutomation: (id: string, revision: number) =>
    mutateJson<AutomationDeleted>(
      "DELETE",
      `${autoPath(id)}?revision=${encodeURIComponent(String(revision))}`,
    ),
  enableAutomation: (
    id: string,
    body: { revision: number; consent: boolean; scope_digest?: string },
  ) => mutateJson<Automation>("POST", `${autoPath(id)}/enable`, body),
  automationVerb: (id: string, verb: "disable" | "pause" | "resume") =>
    mutateJson<Automation>("POST", `${autoPath(id)}/${verb}`),
  /** Run now. 202 with the run; a 409 `detail` says why it was refused. */
  runAutomation: (id: string) =>
    mutateJson<AutomationRun>("POST", `${autoPath(id)}/run`),
  automationRuns: (id: string, limit = 50, offset = 0) =>
    getJsonWithDetail<{ runs: AutomationRun[]; total: number }>(
      `${autoPath(id)}/runs?limit=${limit}&offset=${offset}`,
    ),
  automationRun: (runId: string) =>
    getJsonWithDetail<AutomationRun>(
      `${AUTOMATIONS_API}/runs/${encodeURIComponent(runId)}`,
    ),
  automationOrigins: () =>
    getJson<{ origins: Record<string, AutomationOrigin> }>(AUTOMATION_ORIGINS_API),

  /** Hide the settled rows the client DISPLAYED — never "the current window" (#862). Passing
   *  what was on screen is what stops a decision that settled between the render and the click
   *  from being hidden without ever being seen. */
  clearSettledNotifications: (ids: string[]) =>
    postJson<{ cleared: number }>("/api/pulse/notifications/clear-settled", {
      ids,
    }),

  /** Sign out: clear the session server-side, then hard-navigate to the login page (#141). */
  logout,
};

async function logout(): Promise<void> {
  await postVoid("/logout"); // CSRF POST; the server clears the cookie + 303s to /login
  clearSent(); // #619: sent prompt text must not outlive the session on a shared device
  window.location.assign("/login");
}
