/** Library → MCP servers (#2163): the shapes `routes/mcp.py` answers. A server record never holds a
 *  credential value — a credential setting names a Variables secret — and nothing here ever does. */

export type McpSource =
  | { kind: "remote"; url: string }
  | { kind: "npm" | "pypi"; package: string; version: string; entrypoint?: string }
  /** A Go module built from a repository link's pinned commit (#2427): `package` is the module path.
   *  Never typed in: only a link's resolution sets it. */
  | { kind: "go"; package: string; version: string; entrypoint?: string };

export type McpPackageSource = Exclude<McpSource, { kind: "remote" }>;

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

/** Where a package server was added from (#2353): display only, set by the server from a resolved
 *  repository link, cleared when the source changes. */
export interface McpOrigin {
  url: string;
  commit: string;
}

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
  installed: { installed_at?: number; integrity?: string; source?: McpSource } | null;
  origin?: McpOrigin;
  created_at: number;
  updated_at: number;
  tool_count: number;
  enabled_projects: string[];
  project_count: number;
  state: { kind: McpStateKind; projects?: string[] };
  /** Only on a project page's read (`GET /api/mcp/servers?project=<id>`): that project's row. */
  project?: McpProjectStatus;
  /** "Newer version available" (#2431), from the cache only. `null` for a remote server or one with
   *  nothing installed for its current source. Older answers may lack it. */
  update?: McpUpdateHint | null;
}

/** `mcp_updates.view` (#2431): what the registry last said about the INSTALLED version. `unknown`
 *  is no successful answer yet, or one older than 3 days: never "up to date". `latest` only when
 *  `available`; `checked_at` is the last success, `error` / `tried_at` the last failed attempt after
 *  it (epoch seconds); `checking` while a read runs in the background. */
export interface McpUpdateHint {
  state: "available" | "current" | "unknown";
  installed: string;
  latest: string | null;
  checked_at: number | null;
  error: string | null;
  tried_at: number | null;
  checking: boolean;
}

/** `POST /api/mcp/servers/{id}/updates/check`: `started` (202) when a read began in the background,
 *  `false` (200) when the server was read in the last 10 minutes. */
export interface McpUpdateCheck {
  started: boolean;
  update: McpUpdateHint | null;
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
  /** A Go module's review only (#2427): the main package it builds (an import path), the Go
   *  version its go.mod asks for, and the toolchain that will build it (pinned: Install refuses
   *  another). `integrity` is then the module hash (`h1:…`), `dependencies` the modules in the build
   *  list, each authenticated against sum.golang.org. */
  command?: string;
  go?: string;
  toolchain?: McpToolchain;
}

export interface McpToolchain {
  version: string;
  sha256: string;
  platform: string;
}

/** A review or install running on the server (#2163 P1b): `POST …/review` and `POST …/install`
 *  answer 202 with one, and `GET /api/mcp/operations/{id}` reads it until it is no longer
 *  `running`. `done` carries what the route used to answer; `failed` carries the reason in words. */
export interface McpOperation<T = unknown> {
  id: string;
  server: string | null;
  kind: "review" | "install" | "resolve";
  state: "running" | "done" | "failed";
  created_at: number;
  updated_at: number;
  result: T | null;
  error: string | null;
  /** A link's resolution only (#2353): what has been read so far. */
  progress?: McpResolveProgress;
  /** A refused resolution only: what was read before the refusal. */
  details?: McpLinkFacts | null;
}

/** The manifest a repository root declares (#2353). */
export interface McpDeclared {
  manifest: "package.json" | "pyproject.toml" | "go.mod";
  kind: "npm" | "pypi" | "go";
  package: string;
  version: string;
  dynamic?: boolean;
}

export interface McpResolveProgress {
  step?: "commit" | "manifest" | "release" | "repository" | "review";
  commit?: string;
  manifest?: string;
  kind?: "npm" | "pypi" | "go";
  package?: string;
  version?: string;
}

/** What reading a repository link found (`mcp_gitsource.resolve`'s facts). */
export interface McpLinkFacts {
  repository: string;
  url?: string;
  commit: string;
  found?: McpDeclared[];
  declared?: McpDeclared;
  release?: McpPackageRelease | McpGoRelease;
}

/** The npm / PyPI release a link's manifest names, and the registry record that names the link. */
export interface McpPackageRelease {
  registry: string;
  record_field: string;
  names?: string;
  license?: string;
  direct_dependencies?: number;
  install_scripts?: string[];
}

/** The Go module at the link's pinned commit, from the Go module proxy (#2427): its canonical
 *  version, its home repository and whether the link is a mirror of it (the proxy's commit equals
 *  the link's: `same_commit`), the go directive, the version's time and the ref it was tagged at. */
export interface McpGoRelease {
  module: string;
  version: string;
  home: string;
  mirror: boolean;
  same_commit: boolean;
  go: string;
  time: string;
  ref: string;
}

/** `POST /api/mcp/servers/resolve-link`'s result: the draft source, the origin, the facts, and
 *  P1b's review of that release. Registered by `create` with `resolution_id`. */
export interface McpResolution {
  resolution_id: string;
  source: McpSource;
  origin: McpOrigin;
  facts: McpLinkFacts;
  review: McpReviewFacts;
  disclaimer: string;
}

/** What the server was when its link was read again (Update from source, #2427). */
export interface McpPrevious {
  source: McpSource;
  origin: McpOrigin;
}

/** `POST /api/mcp/servers/resolve-link {server_id, request_id}`'s result (Update from source,
 *  #2427): the server's own link read again. Up to date → nothing to apply; otherwise a resolution
 *  `PATCH …/{id} {resolution_id}` applies (source and origin together), then P1b's install of
 *  `review.review_id`. */
export type McpUpdateResolution =
  | {
      up_to_date: true;
      resolution_id?: string;
      source: McpSource;
      origin: McpOrigin;
      previous: McpPrevious;
      facts: McpLinkFacts;
      disclaimer: string;
    }
  | (McpResolution & { up_to_date: false; previous: McpPrevious });

export interface McpServerInput {
  name: string;
  label?: string;
  source: McpSource;
  args?: string[];
  settings?: McpSettingInput[];
  probe?: Omit<McpProbe, "confirmed_write"> & { confirmed_write?: boolean } | null;
  projects?: "all" | string[];
  /** A resolved repository link (#2353): the server binds its source and origin. */
  resolution_id?: string;
}

export type McpServerUpdate = Partial<Omit<McpServerInput, "resolution_id">> & { enabled?: boolean };
