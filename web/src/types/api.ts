// Types mirroring the existing FastAPI `/api/*` contract (backend is unchanged).

export type EngineId = "claude" | "opencode" | "codex" | "gemini" | "antigravity";

export interface Session {
  /** engine-qualified identity, e.g. "claude:<uuid>" — the URL + socket + lock key */
  id: string;
  engine: EngineId | string;
  uuid: string;
  short_uuid: string;
  cwd: string;
  /** Resolved project ref (#361): entity or implicit folder group. `cwd` stays the
   *  launch location; with zero entities this is always `{kind:"folder", id: cwd}`. */
  project: ProjectRef;
  last_mtime: number;
  /** Wall-clock of the last byte the agent emitted that we observed (#156). null when
   * the server hasn't seen output for this session in this process (no WS attached). */
  last_output_at?: number | null;
  /** True when ``now - last_output_at`` is inside the working window (#156 v1).
   * Browser-attached-only; a headless session not yet reconnected to reports false. */
  working?: boolean;
  first_user_message: string;
  title: string;
  sticky: boolean;
  sort_key: number;
  archived: boolean;
  /** AI review (#356): one-line summary from the last successful review. */
  ai_summary?: string;
  /** AI-generated title. Display precedence is resolved SERVER-side into `title`
   *  (user title → ai_title → first message); this field is informational. */
  ai_title?: string;
  /** Advisory "needs a human" flag from the last review + its short reason. */
  intervention_required?: boolean;
  intervention_reason?: string;
  /** Wall-clock (s) of the last SUCCESSFUL review — the stale-age source: a failed
   *  review never bumps it, so an old result is visibly old. null/absent = never. */
  reviewed_at?: number | null;
  /** Per-session opt-out from AI review. */
  review_excluded?: boolean;
}

/** AI session review config (#356) — the PUBLIC view from /api/config. The API key is
 *  write-only: only `api_key_set` ever crosses the wire. */
export interface AiReviewConfig {
  enabled: boolean;
  base_url: string;
  model: string;
  interval_minutes: number;
  prompt: string;
  max_input_chars: number;
  /** Per-request review timeout in seconds (10–600); null = unset → server falls back
   *  to the AGENT_SESSIONS_AI_REVIEW_TIMEOUT env var, then 120s (#391 follow-up). */
  request_timeout: number | null;
  /** A key is stored server-side (its value is never echoed). */
  api_key_set: boolean;
  /** Base URL + key present — the /models proxy + Review now are usable. */
  configured: boolean;
  /** Server's default prompt, for the reset-to-default control. */
  default_prompt: string;
}

/** AI auto-sort config (#424 Phase 6) — the PUBLIC view from /api/config. Opt-in; reuses the
 *  ai_review endpoint, so it holds no secret of its own. */
export interface AutoSortConfig {
  enabled: boolean;
  interval_minutes: number;
  /** The reused ai_review endpoint is usable (base URL + key present). Mirrors
   *  `ai_review.configured` — auto-sort can't run without it. */
  configured: boolean;
}

/** Report from POST /api/projects/auto-sort (#424 Phase 6): one bounded on-demand pass. */
export interface AutoSortReport {
  candidates: number;
  scanned: number;
  assigned: { id: string; project_id: string; confidence: number }[];
  low_confidence: number;
  errors: number;
  /** Present when the pass did nothing (e.g. "no projects" / "not configured"). */
  skipped?: string;
}

export interface SessionsPage {
  sessions: Session[];
  next_offset: number | null;
  total: number;
  facets: { projects: ProjectRef[]; engines: string[] };
}

/** What a session BELONGS to (#361): a project entity, or the implicit folder group
 *  (pre-#361 behaviour — id is the cwd). Resolved server-side by the shared resolver. */
export interface ProjectRef {
  kind: "project" | "folder";
  /** Entity id (`p-…`) or the cwd for a folder ref — also the `project` filter value. */
  id: string;
  name: string;
  /** Entity color (#285 spends it); absent on folder refs. */
  color?: string;
  /** Scoped rows resolving to this ref — set on FACET refs only (#361 Phase 3),
   *  never on a session row's `project`. */
  count?: number;
}

/** A project entity from GET /api/projects (#361): what the Settings manager edits.
 *  `session_count` is the resolver's member count at read time (GET only — the
 *  create/patch responses omit it). */
export interface ProjectEntity {
  id: string;
  name: string;
  color: string;
  folders: string[];
  /** Default launch folder (#448): where new sessions in this project start unless overridden.
   *  Always one of `folders` (auto-adopted); "" for legacy folderless projects with none set. */
  default_folder: string;
  archived: boolean;
  created_at: number;
  session_count: number;
}

/** A directory from GET /api/fs/dirs — the folder picker's tree node (#448), bounded to ~/. */
export interface FsDir {
  name: string;
  path: string;
}

/** Bulk archive/unarchive report from POST /api/projects/{id}/(un)archive (#361 Phase 2).
 *  Idempotent + blindly retryable: re-calling after a partial failure retries only the
 *  failed members (the rest report `already_*`). Result keys mirror the direction:
 *  archived/already_archived/failed or unarchived/already_unarchived/failed. */
export interface ProjectArchiveReport {
  id: string;
  archived: boolean;
  sessions: { id: string; result: string; reason?: string }[];
  counts: Record<string, number>;
}

/** A launch-location folder from GET /api/folders — the pre-#361 "project" picker row.
 *  Folders stay where sessions LAUNCH; project entities are what sessions BELONG to. */
export interface Folder {
  cwd: string;
  label: string;
}

export interface AppConfig {
  /** CSRF token bound to the session cookie; sent as X-CSRF-Token on mutations. */
  csrf: string;
  /** Engines that are installed AND can start a new session (drives the picker). */
  new_session_engines: string[];
  terminal_backend: "ttyd" | "ws" | string;
  /** First-run forced password change pending — the SPA routes to /change-password. */
  must_change_password?: boolean;
  /** Per-user UI theme id (dark|light); applied at load. Absent on older servers. */
  theme?: string;
  /** Per-user brand accent (#rrggbb) driving --accent + the xterm cursor (#211 Phase 2);
   *  applied at load. Absent on older servers (→ client default phosphor-amber). */
  accent?: string;
  /** Compose box default on load: "auto" (device heuristic) | "open" | "collapsed". */
  compose_default?: "auto" | "open" | "collapsed" | string;
  /** Overview (#144): expanded cluster cwds (default collapsed). */
  overview_expanded?: string[];
  /** Cwds hidden globally from the UI (#174): sidebar list, project filter, new-session
   *  picker, and the overview map. The legacy `overview_excluded` alias is retired
   *  (#357 Phase 2) — the server migrates old on-disk values into this key. */
  projects_hidden?: string[];
  /** Project-visibility mode (#335): "all" (legacy denylist, default) or "included" (curated
   *  allowlist — only `projects_included` cwds show; new dirs never auto-appear). Mode-exclusive:
   *  in "included" mode `projects_hidden` is ignored and `projects_included` is authoritative. */
  projects_mode?: "all" | "included" | string;
  /** The "included"-mode allowlist of visible project cwds (#335). Ignored in "all" mode. */
  projects_included?: string[];
  /** Preferred new-session start directory (#335 Phase 2). The picker pre-selects it when it's
   *  still a pickable project, else falls back to the first option. "" / absent = no preference. */
  default_project?: string;
  /** Base dirs under which the UI may create a new project folder (#335 Phase 3). Empty/absent ⇒
   *  the "New folder" affordance is hidden and the mkdir endpoint is disabled. */
  project_roots?: string[];
  /** Per-cwd custom project display names (#148). */
  project_names?: Record<string, string>;
  /** Auth mode: "single-user" (cookie login) or "none" (no login — self-host on a
   * trusted network). Lets the SPA hide login/logout UI. Absent on older servers. */
  auth_mode?: "single-user" | "none" | string;
  /** Optional TOTP 2FA on/off (#116) — drives the Settings security section. Just the
   *  bit; the secret/recovery codes are never exposed here. Absent on older servers. */
  two_factor_enabled?: boolean;
  /** Faithful real-frame scroll-up via the VT sidecar (#329; garble-proof since #298). The
   *  effective on/off bit (pref override, else env default); drives the Settings →
   *  Appearance toggle (promoted out of "Experimental" in #357 Phase 2). */
  vt_scrollback?: boolean;
  /** AI session review (#356): public config block (write-only key → `api_key_set`). */
  ai_review?: AiReviewConfig;
  /** AI auto-sort (#424 Phase 6): opt-in; reuses the ai_review endpoint (no secret). */
  auto_sort?: AutoSortConfig;
}

/** TOTP enrollment payload (#116): shown once. The secret + recovery codes are never
 *  returned again after this response. */
export interface TwoFactorEnrollment {
  /** base32 TOTP secret (also encoded in otpauth_uri) for manual entry. */
  secret: string;
  /** otpauth://totp/... URI to render as a QR for the authenticator app. */
  otpauth_uri: string;
  /** One-time recovery codes — display once, never persisted by the SPA. */
  recovery_codes: string[];
}

/** One engine provider's discovery status (Settings → Connected agents). */
export interface EngineInfo {
  id: EngineId | string;
  present: boolean;
  supports_new: boolean;
  bin: string | null;
}

export interface EnginesResponse {
  engines: EngineInfo[];
}

/** Host/system info (Settings → System). Every field is fail-soft server-side, so any
 *  of them may be absent depending on the platform / permissions. */
export interface SystemInfo {
  os?: string;
  platform?: string;
  arch?: string;
  python?: string;
  version?: string;
  hostname?: string;
  cpus?: number;
  load?: { "1": number; "5": number; "15": number };
  mem_total?: number;
  mem_available?: number;
  disk_total?: number;
  disk_free?: number;
  uptime_seconds?: number;
}

export interface UpdateInfo {
  current: string;
  channel: string;
  /** The channel's latest ref (highest v* tag on stable, main HEAD short SHA on main),
   *  or null when git/network is unavailable or no release tag exists yet. */
  latest: string | null;
  update_available: boolean;
}

/** One page of older transcript history for scroll-up lazy-load (#348 Phase 3).
 *  `cursor` is a stable per-engine TURN index (never a rendered-line offset): pass it
 *  back as `before` to fetch the next-older page. `null` cursor + `has_more=false`
 *  means the oldest turn was reached (or the engine has no transcript at all). */
export interface HistoryPage {
  ansi: string;
  cursor: number | null;
  has_more: boolean;
}

export interface SessionsQuery {
  limit?: number;
  offset?: number;
  archived?: boolean;
  q?: string;
  project?: string;
  engine?: string;
}
