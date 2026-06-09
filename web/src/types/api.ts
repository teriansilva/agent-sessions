// Types mirroring the existing FastAPI `/api/*` contract (backend is unchanged).

export type EngineId = "claude" | "opencode" | "codex" | "gemini";

export interface Session {
  /** engine-qualified identity, e.g. "claude:<uuid>" — the URL + socket + lock key */
  id: string;
  engine: EngineId | string;
  uuid: string;
  short_uuid: string;
  cwd: string;
  project: string;
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
}

export interface SessionsPage {
  sessions: Session[];
  next_offset: number | null;
  total: number;
  facets: { projects: string[]; engines: string[] };
}

export interface Project {
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
  /** Per-user sidebar body: "list" (session list) or "overview" (squeezed map, #139). */
  sidebar_view?: "list" | "overview" | string;
  /** Compose box default on load: "auto" (device heuristic) | "open" | "collapsed". */
  compose_default?: "auto" | "open" | "collapsed" | string;
  /** Overview (#144): expanded cluster cwds (default collapsed) + cwds hidden globally. */
  overview_expanded?: string[];
  /** @deprecated Use `projects_hidden` (#174 — same data; broader scope). Emitted in
   *  parallel during the transition window so an old client tab still reads its hides. */
  overview_excluded?: string[];
  /** Cwds hidden globally from the UI (#174). Wins over `overview_excluded` when both
   *  are present. The hide affects the sidebar list, the project filter, the new-session
   *  picker, and the overview map. */
  projects_hidden?: string[];
  /** Project-visibility mode (#335): "all" (legacy denylist, default) or "included" (curated
   *  allowlist — only `projects_included` cwds show; new dirs never auto-appear). Mode-exclusive:
   *  in "included" mode `projects_hidden` is ignored and `projects_included` is authoritative. */
  projects_mode?: "all" | "included" | string;
  /** The "included"-mode allowlist of visible project cwds (#335). Ignored in "all" mode. */
  projects_included?: string[];
  /** Per-cwd custom project display names (#148). */
  project_names?: Record<string, string>;
  /** Auth mode: "single-user" (cookie login) or "none" (no login — self-host on a
   * trusted network). Lets the SPA hide login/logout UI. Absent on older servers. */
  auth_mode?: "single-user" | "none" | string;
  /** Optional TOTP 2FA on/off (#116) — drives the Settings security section. Just the
   *  bit; the secret/recovery codes are never exposed here. Absent on older servers. */
  two_factor_enabled?: boolean;
  /** Experimental (#329): faithful real-frame scroll-up via the VT sidecar. The effective
   *  on/off bit (pref override, else env default); drives the Settings → Experimental toggle. */
  vt_scrollback?: boolean;
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

export interface SessionsQuery {
  limit?: number;
  offset?: number;
  archived?: boolean;
  q?: string;
  project?: string;
  engine?: string;
}
