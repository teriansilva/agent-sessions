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

export interface SessionsQuery {
  limit?: number;
  offset?: number;
  archived?: boolean;
  q?: string;
  project?: string;
  engine?: string;
}
