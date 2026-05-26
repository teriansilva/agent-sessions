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
  /** Per-user UI theme id (royal|dark|light); applied at load. Absent on older servers. */
  theme?: string;
  /** Auth mode: "single-user" (cookie login) or "none" (no login — self-host on a
   * trusted network). Lets the SPA hide login/logout UI. Absent on older servers. */
  auth_mode?: "single-user" | "none" | string;
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

export interface SessionsQuery {
  limit?: number;
  offset?: number;
  archived?: boolean;
  q?: string;
  project?: string;
  engine?: string;
}
