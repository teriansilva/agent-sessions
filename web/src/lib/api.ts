// Typed client for the FastAPI `/api/*` surface. Same-origin; cookie session auth.
// Mutations (later) attach the CSRF token + are origin-checked server-side.
import type {
  AiActivity,
  AppConfig,
  AutoSortReport,
  EnginesResponse,
  FileCapabilities,
  FileContent,
  FileListing,
  Folder,
  FsDir,
  GitDiff,
  GitStatus,
  HandoffCommitted,
  HandoffMode,
  HandoffPrepared,
  HistoryPage,
  DraftAttachment,
  ProjectArchiveReport,
  ProjectEntity,
  Evidence,
  EvidenceKind,
  OrchestratorAction,
  NotificationList,
  OrchestratorState_,
  PushSubscriptionInfo,
  PulseAskResult,
  PulseDepth,
  PulseOverview,
  SessionDraft,
  SessionsPage,
  SessionsQuery,
  SystemInfo,
  TwoFactorEnrollment,
  UpdateInfo,
  UpdateSettings,
} from "../types/api";
import { clearSent } from "./sentHistory";
import { announceActionResolved } from "./actionEvents";

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
async function mutateJson<T>(
  method: "POST" | "PUT" | "PATCH" | "DELETE",
  path: string,
  body?: unknown,
): Promise<T> {
  const r = await apiFetch(path, {
    method,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: body === undefined ? undefined : JSON.stringify(body),
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

const patchJson = <T>(path: string, body?: unknown): Promise<T> =>
  mutateJson<T>("PATCH", path, body);

const putJson = <T>(path: string, body?: unknown): Promise<T> =>
  mutateJson<T>("PUT", path, body);

const deleteJson = <T>(path: string): Promise<T> =>
  mutateJson<T>("DELETE", path);

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
  return `/api/sessions?${p.toString()}`;
}

const enc = encodeURIComponent;

/** Upload a file (image/context) → server saves it under ~/.agent-sessions/uploads/
 *  and returns a path the agent can read. Multipart, CSRF-guarded (not JSON). */
async function upload(file: File): Promise<{ path: string; name: string }> {
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
  return (await r.json()) as { path: string; name: string };
}

export const api = {
  config: () => getJson<AppConfig>("/api/config"),
  version: () => getJson<{ version: string }>("/api/version"),
  /** Discovery: every known engine provider with presence / new-session / bin path. */
  engines: () => getJson<EnginesResponse>("/api/engines"),
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
  /** Cheap read (no remote hit) of the persisted update settings for the card mount (#538). */
  updateSettings: () => getJson<UpdateSettings>("/api/update/settings"),
  /** Persist the auto-update opt-in and/or release channel (#538). CSRF-guarded. */
  setUpdateSettings: (body: { auto_update?: boolean; channel?: string }) =>
    postJson<UpdateSettings>("/api/update/settings", body),
  /** Persist the UI theme server-side (per-user, across devices). CSRF-guarded. */
  setTheme: (theme: string) =>
    postJson<{ theme: string }>("/api/prefs", { theme }),
  /** Persist the brand accent (#rrggbb) server-side, per-user (#211 Phase 2). CSRF-guarded. */
  setAccent: (accent: string) =>
    postJson<{ accent: string }>("/api/prefs", { accent }),
  /** Persist a partial set of UI preferences (e.g. overview lists, #144). CSRF-guarded. */
  setPrefs: (partial: Record<string, unknown>) =>
    postJson<Record<string, unknown>>("/api/prefs", partial),
  /** First-run onboarding (#463): mark the wizard complete (or skipped) so it never shows
   *  again. Persists `onboarded: true` via the prefs store. CSRF-guarded. */
  completeOnboarding: () =>
    postJson<Record<string, unknown>>("/api/prefs", { onboarded: true }),
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
  /** Create a folder under a browsed parent (#448), bounded to ~/. Idempotent; returns the path. */
  fsMkdir: (parent: string, name: string) =>
    postJson<{ path: string }>("/api/fs/mkdir", { parent, name }),
  /** File panel (#783): one directory, bounded by an entry cap AND a wall-clock budget. */
  filesList: (path?: string, init?: RequestInit) =>
    getJson<FileListing>(
      `/api/files/list${path ? `?path=${encodeURIComponent(path)}` : ""}`,
      init,
    ),
  /** File panel (#783): one regular file, capped while reading. Binary returns metadata only. */
  filesRead: (path: string, init?: RequestInit) =>
    getJson<FileContent>(`/api/files/read?path=${encodeURIComponent(path)}`, init),
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
  sessions: (q?: SessionsQuery) => getJson<SessionsPage>(sessionsUrl(q)),
  rename: (id: string, title: string) =>
    postJson<{ id: string; title: string }>(`/api/sessions/${enc(id)}/rename`, {
      title,
    }),
  /** Set (or clear, with "") a session's custom tag (#551): a short label shown before the
   *  AI summary in the sidebar row. Trimmed + length-capped server-side. */
  setTag: (id: string, tag: string) =>
    postJson<{ id: string; tag: string }>(`/api/sessions/${enc(id)}/tag`, {
      tag,
    }),
  /** Favorite/unfavorite a session (#122): flips the sidecar `sticky` flag so the row
   *  pins to the top of the sidebar. Engine-agnostic; CSRF-guarded. Returns `{id, sticky}`. */
  favorite: (id: string) =>
    postJson<{ id: string; sticky: boolean }>(
      `/api/sessions/${enc(id)}/favorite`,
    ),
  unfavorite: (id: string) =>
    postJson<{ id: string; sticky: boolean }>(
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
    postJson<{ id: string; archived: boolean }>(
      `/api/sessions/${enc(id)}/archive`,
    ),
  unarchive: (id: string) =>
    postJson<{ id: string; archived: boolean }>(
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
  aiReviewModels: (opts?: { refresh?: boolean }) =>
    getJson<{ models: string[] }>(
      `/api/ai-review/models${opts?.refresh ? "?refresh=1" : ""}`,
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
    postJson<{ id: string; review_excluded: boolean }>(
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
  approveAction: (id: string) =>
    mutateJson<OrchestratorAction>(
      "POST",
      `/api/pulse/actions/${enc(id)}/approve`,
      {},
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
  /** Sign out: clear the session server-side, then hard-navigate to the login page (#141). */
  logout,
};

async function logout(): Promise<void> {
  await postVoid("/logout"); // CSRF POST; the server clears the cookie + 303s to /login
  clearSent(); // #619: sent prompt text must not outlive the session on a shared device
  window.location.assign("/login");
}
