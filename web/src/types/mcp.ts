/** Library → MCP servers (#2163): the shapes `routes/mcp.py` answers. A server record never holds a
 *  credential value — a credential setting names a Variables secret — and nothing here ever does. */

export type McpSource =
  | { kind: "remote"; url: string }
  | { kind: "npm" | "pypi"; package: string; version: string; entrypoint?: string };

export type McpSetting =
  | { key: string; deliver: "env" | "header"; kind: "plain"; value: string }
  | { key: string; deliver: "env" | "header"; kind: "credential"; variable: string; prefix: string };

/** What a setting is sent as on create / update (`deliver` defaults from the transport). */
export type McpSettingInput =
  | { key: string; kind: "plain"; value: string }
  | { key: string; kind: "credential"; variable: string; prefix?: string };

export interface McpProbe {
  tool: string;
  arguments: Record<string, unknown>;
  confirmed_write: boolean;
}

export type McpStateKind =
  | "ready"
  | "needs_credentials"
  | "needs_check"
  | "failing"
  | "disabled"
  | "not_enabled";

export interface McpServerSummary {
  id: string;
  name: string;
  label: string;
  source: McpSource;
  transport: "stdio" | "http";
  args: string[];
  settings: McpSetting[];
  project_settings: Record<string, Record<string, string>>;
  projects: "all" | string[];
  probe: McpProbe | null;
  enabled: boolean;
  rev: number;
  installed: { installed_at?: number; integrity?: string } | null;
  created_at: number;
  updated_at: number;
  tool_count: number;
  enabled_projects: string[];
  project_count: number;
  state: { kind: McpStateKind; projects?: string[] };
}

export type McpCredentialSource = "none_needed" | "own" | "shared" | "missing";
export type McpCheckState = "passed" | "failed" | "stale" | "not_run" | "needs_credentials";

export interface McpProjectStatus {
  project_id: string;
  enabled: boolean;
  credentials: McpCredentialSource;
  missing: string[];
  check: McpCheckState;
  checked_at: number | null;
  delivered: boolean;
}

export interface McpCheckStep {
  step: string;
  ok: boolean;
  detail: string;
}

export interface McpCheckRow {
  server?: string;
  scope: string;
  rev: number;
  digest?: string;
  outcome: "passed" | "failed" | "superseded";
  at: number;
  steps: McpCheckStep[];
  tools: string[];
}

export interface McpServerDetail {
  server: McpServerSummary;
  status: {
    projects: McpProjectStatus[];
    shared: { missing: string[]; check: "passed" | "not_run" };
  };
  checks: McpCheckRow[];
}

/** `POST …/review`: what would run, before anything is installed (P1b). */
export interface McpReviewFacts {
  review_id: string;
  source: McpSource;
  reviewed_at: number;
  lock_digest: string;
  lock_file: string;
  integrity: string;
  registry: string;
  dependencies: number;
  install_scripts_declared: string[];
  entrypoints: string[];
  entrypoint: string;
  license: string;
  runs: string;
}

/** A review or install running on the server (#2163 P1b): `POST …/review` and `POST …/install`
 *  answer 202 with one, and `GET /api/mcp/operations/{id}` reads it until it is no longer
 *  `running`. `done` carries what the route used to answer; `failed` carries the reason in words. */
export interface McpOperation<T = unknown> {
  id: string;
  server: string;
  kind: "review" | "install";
  state: "running" | "done" | "failed";
  created_at: number;
  updated_at: number;
  result: T | null;
  error: string | null;
}

export interface McpServerInput {
  name: string;
  label?: string;
  source: McpSource;
  args?: string[];
  settings?: McpSettingInput[];
  probe?: Omit<McpProbe, "confirmed_write"> & { confirmed_write?: boolean } | null;
  projects?: "all" | string[];
}

export type McpServerUpdate = Partial<McpServerInput> & { enabled?: boolean };
