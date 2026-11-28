/** A stand-in for `routes/mcp.py` and the slice of `/api/variables` the MCP pages use (#2163 P1c).
 *
 *  Why a stand-in and not the real app: a check of a REMOTE server needs an MCP server the app can
 *  reach over https with a verified certificate, and the registry admits plain `http://` to a
 *  loopback fixture only through a test-only module flag that the running app never sets. So the
 *  wizard → check → enable flow is driven against this fake, which keeps the backend's own rules
 *  where the pages depend on them: credentials resolve own > bound shared > missing, a check passes
 *  per scope and a passing shared check counts for every project using the shared values, `PUT
 *  …/projects` refuses a project without credentials or a passing check (422 with words), and no
 *  answer ever carries a credential value. `appmode-mcp-servers.spec.ts` covers what the real app
 *  can do without a reachable server. */
import type { Page, Route } from "@playwright/test";

import { MCP_OPERATIONS_API } from "../src/lib/apiPaths";
import type {
  McpCheckRow,
  McpLinkFacts,
  McpOperation,
  McpOrigin,
  McpResolution,
  McpResolveProgress,
  McpReviewFacts,
  McpServerDetail,
  McpServerSummary,
  McpSetting,
  McpSource,
  McpUpdateHint,
} from "../src/types/mcp";
import { commonMocks } from "./mission-directions";

export const PROJECTS = [
  { id: "p_atlas", name: "Atlas", color: "#e8792b" },
  { id: "p_beta", name: "Beta", color: "#8b5cf6" },
  { id: "p_cedar", name: "Cedar", color: "#14b8a6" },
];

export const NOW = Math.floor(Date.now() / 1000);

type Rec = Omit<McpServerSummary, "tool_count" | "enabled_projects" | "project_count" | "state">;

export interface FakeVar {
  id: string;
  name: string;
  kind: "secret";
  projects: string[];
  updated_at: number;
}

/** An operation as the fake keeps it: the view, plus how many reads it has left and (install)
 *  the review it installs. */
export type FakeOp = McpOperation & {
  polls: number;
  reviewId?: string;
  link?: string;
  reserved?: string;
  consumed?: boolean;
  /** Update from source (#2427): the server this reading may update, and what it was when read. */
  updateOf?: string;
  previous?: { source: McpSource; origin: McpOrigin | undefined };
  installedNow?: boolean;
};

/** What reading a repository link answers (#2353): its resolution, or a refusal in words with what
 *  was read before it. A link the fake does not know is "no git repository". */
export type FakeLink = { resolution: Omit<McpResolution, "resolution_id"> } | { error: string; details?: McpLinkFacts };

export interface Fake {
  servers: Rec[];
  checks: (McpCheckRow & { server: string })[];
  variables: FakeVar[];
  /** `{server id: {project id: [variable, …]}}` — a project's own values. */
  own: Record<string, Record<string, string[]>>;
  /** Scopes whose next check fails at the handshake. */
  failing: Set<string>;
  /** Everything the pages sent, in order (method, path, body). */
  calls: { method: string; path: string; body: unknown }[];
  /** The list read answers 500 this many more times. */
  listErrors: number;
  /** Bumped per `server:project` whenever a project's own values are written. */
  ownRev: Record<string, number>;
  /** Review and install operations by request_id (`mcp_jobs`): `polls` is how many more reads
   *  still answer `running`; `Infinity` holds it running until a test settles it. */
  ops: Record<string, FakeOp>;
  /** How many reads a new operation stays `running` for. */
  opPolls: number;
  /** The next install fails with these words (`InstallError`), then clears. */
  installFails: string;
  /** Repository links by their link as typed (`mcp_gitsource.resolve`'s stand-in, #2353). */
  links: Record<string, FakeLink>;
  /** A reading's progress stops advancing at this step (and does not settle) until cleared: lets a
   *  spec see one step running, e.g. a Go link's first toolchain download (#2427). */
  resolveHold?: McpResolveProgress["step"];
  /** "Newer version available" (#2431): `mcp_updates.view` per server id; absent → `update: null`
   *  (a remote server, or nothing installed). */
  updates: Record<string, McpUpdateHint>;
  /** What a Check for updates settles to, per server id (merged into its view; `checking` off). */
  updateResult: Record<string, Partial<McpUpdateHint>>;
  /** How many reads of the server a started check stays `checking` for (`Infinity`: until a spec
   *  clears it). */
  updatePolls: number;
  /** Running checks: server id → reads left. */
  updateRunning: Record<string, number>;
  /** Duplicate (#2430): `request_id` → the original it copied and the copy it made (a retry of the
   *  same request returns that copy, `mcp_registry.duplicate`). */
  dupRequests: Record<string, { from: string; copy: string }>;
  /** The next this-many duplicates are made and their answer is lost (the connection drops). */
  dupLoseResponses: number;
}

let seq = 0;
export const newId = () => (0x1000000000000000 + ++seq * 0x1111).toString(16).slice(0, 16);

export function record(over: Partial<Rec> & Pick<Rec, "name">): Rec {
  return {
    id: newId(),
    label: "",
    source: { kind: "remote", url: "https://mcp.example.com/mcp" },
    transport: "http",
    args: [],
    settings: [],
    project_settings: {},
    projects: [],
    probe: null,
    enabled: true,
    rev: 1,
    installed: null,
    created_at: NOW - 86_400,
    updated_at: NOW - 3_600,
    ...over,
  };
}

export const TOOLS = ["list_issues", "get_issue", "search_docs", "create_issue"];

export function passedRow(server: string, scope: string, rev: number, at = NOW - 600): McpCheckRow & { server: string } {
  return {
    server,
    scope,
    rev,
    outcome: "passed",
    at,
    steps: [
      { step: "settings", ok: true, detail: "1 credential resolved" },
      { step: "started", ok: true, detail: "connected to mcp.example.com" },
      { step: "handshake", ok: true, detail: "protocol 2025-06-18 · example-mcp 1.4.2" },
      { step: "tools", ok: true, detail: `${TOOLS.length} tools` },
    ],
    tools: TOOLS,
  };
}

function failedRow(server: string, scope: string, rev: number): McpCheckRow & { server: string } {
  return {
    server,
    scope,
    rev,
    outcome: "failed",
    at: NOW,
    steps: [
      { step: "settings", ok: true, detail: "1 credential resolved" },
      { step: "started", ok: true, detail: "connected to mcp.example.com" },
      { step: "handshake", ok: false, detail: "401 Unauthorized: the server refused the credential" },
    ],
    tools: [],
  };
}

const credVars = (rec: Rec) =>
  rec.settings.flatMap((s) => (s.kind === "credential" ? [s.variable] : []));

function sharedMissing(f: Fake, rec: Rec) {
  return credVars(rec).filter((v) => !f.variables.some((x) => x.name === v));
}

/** The resolved set a scope's check is good for — the fake's `Resolved.digest`: the shared values,
 *  or one project's own values at their current revision. A passing check counts for every scope
 *  with the same digest (`mcp_registry._passing`), so a passing SHARED check covers a project
 *  that uses the shared values, never one with its own. */
export function digestFor(f: Fake, rec: Rec, scope: string): string {
  const own = scope === "shared" ? [] : (f.own[rec.id]?.[scope] ?? []);
  return own.length ? `own:${scope}:${f.ownRev[`${rec.id}:${scope}`] ?? 0}` : "shared";
}

/** Record a check row as of now (stamps the scope's current digest). */
export function addCheck(f: Fake, rec: Rec, row: McpCheckRow & { server: string }) {
  f.checks.push({ ...row, digest: digestFor(f, rec, row.scope) });
}

function projectRow(f: Fake, rec: Rec, pid: string) {
  const vars = credVars(rec);
  const own = f.own[rec.id]?.[pid] ?? [];
  const missing = vars.filter(
    (v) => !own.includes(v) && !f.variables.some((x) => x.name === v && x.projects.includes(pid)),
  );
  const credentials = !vars.length ? "none_needed" : missing.length ? "missing" : own.length ? "own" : "shared";
  const digest = digestFor(f, rec, pid);
  const mine = f.checks.filter((c) => c.server === rec.id && c.rev === rec.rev);
  const passing = [...mine].reverse().find((c) => c.outcome === "passed" && c.digest === digest);
  const newest = [...mine].reverse().find((c) => c.scope === pid);
  const check = missing.length
    ? "needs_credentials"
    : passing
      ? "passed"
      : newest?.outcome === "failed" && newest.digest === digest
        ? "failed"
        : newest
          ? "stale"
          : "not_run";
  const enabled = rec.projects === "all" || rec.projects.includes(pid);
  return {
    project_id: pid,
    enabled,
    credentials,
    missing,
    check,
    checked_at: (passing ?? newest)?.at ?? null,
    delivered: enabled && check === "passed",
  } as McpServerDetail["status"]["projects"][number];
}

export function summary(f: Fake, rec: Rec): McpServerSummary {
  const rows = PROJECTS.map((p) => projectRow(f, rec, p.id));
  const enabled = rows.filter((r) => r.enabled);
  const passing = [...f.checks].reverse().find((c) => c.server === rec.id && c.rev === rec.rev && c.outcome === "passed");
  let state: McpServerSummary["state"];
  if (!rec.enabled) state = { kind: "disabled" };
  else if (enabled.some((r) => r.check === "failed"))
    state = { kind: "failing", projects: enabled.filter((r) => r.check === "failed").map((r) => r.project_id) };
  else if (enabled.some((r) => r.credentials === "missing"))
    state = { kind: "needs_credentials", projects: enabled.filter((r) => r.credentials === "missing").map((r) => r.project_id) };
  else if (enabled.some((r) => r.check === "stale" || r.check === "not_run"))
    state = {
      kind: "needs_check",
      projects: enabled.filter((r) => r.check === "stale" || r.check === "not_run").map((r) => r.project_id),
    };
  else state = { kind: enabled.length ? "ready" : "not_enabled" };
  return {
    ...rec,
    tool_count: passing ? passing.tools.length : 0,
    enabled_projects: enabled.map((r) => r.project_id),
    project_count: PROJECTS.length,
    state,
    update: f.updates[rec.id] ?? null,
  };
}

/** `mcp_updates.THROTTLE_S`: a server read (or tried) in the last 10 minutes is not read again. */
const UPDATE_THROTTLE_S = 600;

/** `POST …/updates/check` (#2431): 422 without an installed version; 200 `{started: false}` inside
 *  the throttle; else 202 `{started: true}` with `checking`, settling after `updatePolls` reads. */
function startUpdateCheck(r: Route, f: Fake, rec: Rec) {
  const u = f.updates[rec.id];
  if (!u) return json(r, { detail: "this server has no installed package version to compare" }, 422);
  const last = Math.max(u.checked_at ?? 0, u.tried_at ?? 0);
  if (u.checking || Date.now() / 1000 - last < UPDATE_THROTTLE_S) return json(r, { started: false, update: u });
  u.checking = true;
  f.updateRunning[rec.id] = f.updatePolls;
  return json(r, { started: true, update: u }, 202);
}

/** One read of a server while its check runs: it settles once its reads run out. */
function readUpdate(f: Fake, rec: Rec) {
  if (!(rec.id in f.updateRunning)) return;
  if (--f.updateRunning[rec.id] > 0) return;
  delete f.updateRunning[rec.id];
  Object.assign(f.updates[rec.id], f.updateResult[rec.id] ?? {}, { checking: false });
}

function detail(f: Fake, rec: Rec): McpServerDetail {
  const missing = sharedMissing(f, rec);
  const sharedPass = f.checks.some((c) => c.server === rec.id && c.rev === rec.rev && c.outcome === "passed" && c.digest === "shared");
  return {
    server: summary(f, rec),
    status: {
      projects: PROJECTS.map((p) => projectRow(f, rec, p.id)),
      shared: { missing, check: sharedPass ? "passed" : "not_run" },
    },
    checks: f.checks.filter((c) => c.server === rec.id).reverse(),
  };
}

function settingsIn(raw: unknown, transport: "http" | "stdio"): McpSetting[] {
  return ((raw as Record<string, string>[]) ?? []).map((s) =>
    s.kind === "plain"
      ? { key: s.key, kind: "plain", value: s.value, deliver: transport === "http" ? "header" : "env" }
      : { key: s.key, kind: "credential", variable: s.variable, prefix: s.prefix ?? "", deliver: transport === "http" ? "header" : "env" },
  );
}

export function emptyFake(): Fake {
  return {
    servers: [],
    checks: [],
    variables: [],
    own: {},
    failing: new Set(),
    calls: [],
    listErrors: 0,
    ownRev: {},
    ops: {},
    opPolls: 1,
    installFails: "",
    links: {},
    updates: {},
    updateResult: {},
    updatePolls: 2,
    updateRunning: {},
    dupRequests: {},
    dupLoseResponses: 0,
  };
}

/** Three servers in the index's three most telling states. */
export function seededFake(): Fake {
  const f = emptyFake();
  const auth: McpSetting = { key: "Authorization", kind: "credential", variable: "tracker_token", prefix: "Bearer ", deliver: "header" };
  const ready = record({ name: "tracker", label: "Tracker", settings: [auth], projects: "all" });
  const needs = record({
    name: "files",
    label: "Files",
    source: { kind: "npm", package: "files-mcp-server", version: "0.14.2" },
    transport: "stdio",
    settings: [{ key: "FILES_TOKEN", kind: "credential", variable: "files_token", prefix: "", deliver: "env" }],
    projects: ["p_atlas", "p_beta", "p_cedar"],
    installed: { installed_at: NOW - 7200, integrity: "sha512-abc" },
  });
  (needs.installed as Record<string, unknown>).source = needs.source;
  const failing = record({
    name: "errors",
    label: "Errors",
    source: { kind: "remote", url: "https://mcp.errors.example.org/mcp" },
    settings: [{ ...auth, variable: "errors_token" }],
    projects: ["p_atlas", "p_beta"],
  });
  f.servers.push(ready, needs, failing);
  f.variables.push(
    { id: "v_tracker", name: "tracker_token", kind: "secret", projects: ["p_atlas", "p_beta", "p_cedar"], updated_at: NOW - 999 },
    { id: "v_files", name: "files_token", kind: "secret", projects: ["p_atlas"], updated_at: NOW - 999 },
    { id: "v_errors", name: "errors_token", kind: "secret", projects: ["p_atlas", "p_beta"], updated_at: NOW - 999 },
  );
  f.own[needs.id] = { p_beta: ["files_token"] };
  // Beta's OWN value for the third one is wrong: its check failed (the shared one passed).
  f.own[failing.id] = { p_beta: ["errors_token"] };
  addCheck(f, ready, passedRow(ready.id, "shared", 1, NOW - 4000));
  addCheck(f, needs, passedRow(needs.id, "shared", 1, NOW - 3000));
  addCheck(f, needs, passedRow(needs.id, "p_atlas", 1, NOW - 2500));
  addCheck(f, needs, passedRow(needs.id, "p_beta", 1, NOW - 2000));
  addCheck(f, failing, passedRow(failing.id, "shared", 1, NOW - 90_000));
  addCheck(f, failing, failedRow(failing.id, "p_beta", 1));
  return f;
}

const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, json: body });

const UUID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;

/** What `mcp_install.review` answers for a package server. */
export function reviewFacts(rec: Rec, reviewId: string): McpReviewFacts {
  return {
    review_id: reviewId,
    source: rec.source,
    reviewed_at: NOW,
    lock_digest: "sha256:0f0f",
    lock_file: "package-lock.json",
    integrity: "sha512-AbC",
    registry: "registry.npmjs.org",
    dependencies: 12,
    install_scripts_declared: ["postinstall"],
    entrypoints: ["files-mcp"],
    entrypoint: "files-mcp",
    license: "MIT",
    runs: "node …/files-mcp",
  };
}

/** A view of an operation, as `Operation.view()` answers it. */
const opView = (op: FakeOp): McpOperation => ({
  id: op.id,
  server: op.server,
  kind: op.kind,
  state: op.state,
  created_at: op.created_at,
  updated_at: op.updated_at,
  result: op.result,
  error: op.error,
  ...(op.kind === "resolve" ? { progress: op.progress, details: op.details ?? null } : {}),
});

export const LINK = "https://github.com/owner/repo";
export const LINK_SHA = "3f2a9c1e5b7d40a8c6f2e19d3b4a7c58e0f1d26b";

/** A link that resolves to `@acme/mcp-server 1.4.0`, whose npm record names the repository. */
export function linkResolution(): Omit<McpResolution, "resolution_id"> {
  const source = { kind: "npm" as const, package: "@acme/mcp-server", version: "1.4.0" };
  const declared = { manifest: "package.json" as const, kind: "npm" as const, package: source.package, version: source.version };
  return {
    source,
    origin: { url: LINK, commit: LINK_SHA },
    facts: {
      repository: "github.com/owner/repo",
      url: LINK,
      commit: LINK_SHA,
      found: [declared],
      declared,
      release: { registry: "registry.npmjs.org", record_field: "repository", names: "github.com/owner/repo", license: "MIT" },
    },
    review: { ...reviewFacts(record({ name: "x", source }), "a1b2c3d4e5f60718"), entrypoints: ["acme-mcp"], entrypoint: "acme-mcp", runs: "node …/acme-mcp" },
    disclaimer: "Installs the published release; not built from this commit.",
  };
}

export const GO_LINK = "https://codeberg.org/goern/forgejo-mcp";
export const GO_SHA = "9c3fa38c6708a24918bd2e9ae2f171f211202155";
export const GO_NEW_SHA = "4b1d2e7f0a93c5e8d61b27a4f9c03e5d8b7a6c21";
export const GO_MODULE = "git.b4mad.industries/agentic-forges/forgejo-mcp/v3";
export const GO_HOME = "git.b4mad.industries/agentic-forges/forgejo-mcp";
export const GO_LINE =
  "Built from this commit with the Go toolchain. No install scripts run; every download is verified against sum.golang.org.";
export const GO_SUM = "h1:a7TU1kvcAEAtlPIzQwErTy0123456789abcdefghij=";
export const GO_NEW_SUM = "h1:Qx9mLp2RtVw8kZ4nB6yC1dF3gH5jK7lM9oP0qS2uX4=";

/** What reading the Codeberg forgejo-mcp link answers (#2427): a mirror of the module's home, at
 *  the commit v3.3.0 is tagged at; the review a real run produced (46 modules in its build). */
export function goResolution(
  over: { version?: string; commit?: string; sum?: string; reviewId?: string } = {},
): Omit<McpResolution, "resolution_id"> {
  const version = over.version ?? "v3.3.0";
  const commit = over.commit ?? GO_SHA;
  const source = { kind: "go" as const, package: GO_MODULE, version };
  const declared = { manifest: "go.mod" as const, kind: "go" as const, package: GO_MODULE, version: "" };
  return {
    source,
    origin: { url: GO_LINK, commit },
    facts: {
      repository: "codeberg.org/goern/forgejo-mcp",
      url: GO_LINK,
      commit,
      found: [declared],
      declared,
      release: {
        module: GO_MODULE,
        version,
        home: GO_HOME,
        mirror: true,
        same_commit: true,
        go: "1.26.0",
        time: "2026-11-20T10:00:00Z",
        ref: `refs/tags/${version}`,
      },
    },
    review: {
      review_id: over.reviewId ?? "b0c1d2e3f4a5b6c7",
      source,
      reviewed_at: NOW,
      lock_digest: "sha256:9a9a",
      lock_file: "go.lock.json",
      integrity: over.sum ?? GO_SUM,
      registry: "proxy.golang.org",
      dependencies: 46,
      install_scripts_declared: [],
      entrypoints: ["forgejo-mcp"],
      entrypoint: "forgejo-mcp",
      license: "",
      runs: "forgejo-mcp (stdio), built with go1.27.4",
      command: GO_MODULE,
      go: "1.26.0",
      toolchain: { version: "1.27.4", sha256: "ab".repeat(32), platform: "linux/amd64" },
    },
    disclaimer: GO_LINE,
  };
}

/** A Go server added from the Codeberg link at v3.3.0, installed (and checked when `checked`). */
export function goServer(f: Fake, checked = true): Rec {
  const source = { kind: "go" as const, package: GO_MODULE, version: "v3.3.0" };
  const rec = record({
    name: "forgejo-mcp",
    label: "Forgejo MCP",
    source,
    transport: "stdio",
    origin: { url: GO_LINK, commit: GO_SHA },
    installed: { installed_at: NOW - 86_400 * 3, integrity: GO_SUM, source } as Rec["installed"],
  });
  f.servers.push(rec);
  if (checked) addCheck(f, rec, passedRow(rec.id, "shared", rec.rev, NOW - 4000));
  return rec;
}

const RESOLVE_STEPS = ["commit", "manifest", "release", "repository", "review"] as const;

/** `mcp_jobs.start_resolve`: no server; the same id with another link is a 409. */
function startResolve(r: Route, f: Fake, body: Record<string, unknown>) {
  if (body && Object.keys(body).sort().join() === "request_id,server_id") return startUpdateRead(r, f, body);
  if (!body || Object.keys(body).sort().join() !== "request_id,url" || typeof body.url !== "string")
    return json(r, { detail: "send the repository link (url) and a request_id (a UUID)" }, 422);
  const id = body.request_id;
  if (typeof id !== "string" || !UUID.test(id)) return json(r, { detail: "request_id must be a UUID" }, 422);
  const existing = f.ops[id];
  if (existing) {
    if (existing.kind !== "resolve" || existing.link !== body.url.trim())
      return json(r, { detail: "that request_id belongs to another operation or link; resolve with a new one" }, 409);
    return json(r, opView(existing), 202);
  }
  const op: FakeOp = {
    id,
    server: null,
    kind: "resolve",
    state: "running",
    created_at: NOW,
    updated_at: NOW,
    result: null,
    error: null,
    progress: { step: "commit" },
    details: null,
    polls: f.opPolls,
    link: body.url.trim(),
    reserved: newId(),
  };
  f.ops[id] = op;
  return json(r, opView(op), 202);
}

/** Update from source (#2427): the server's OWN link is read (never one from the client); a server
 *  without an origin is a 422; one read per server at a time (409). */
function startUpdateRead(r: Route, f: Fake, body: Record<string, unknown>) {
  const id = body.request_id;
  if (typeof id !== "string" || !UUID.test(id)) return json(r, { detail: "request_id must be a UUID" }, 422);
  const rec = f.servers.find((s) => s.id === body.server_id);
  if (!rec) return json(r, { detail: "No such MCP server." }, 404);
  if (!rec.origin)
    return json(r, { detail: "this server was not added from a repository link; update it by version" }, 422);
  const existing = f.ops[id];
  if (existing) {
    if (existing.kind !== "resolve" || existing.updateOf !== rec.id)
      return json(r, { detail: "that request_id belongs to another operation or link; resolve with a new one" }, 409);
    return json(r, opView(existing), 202);
  }
  if (Object.values(f.ops).some((o) => o.updateOf === rec.id && o.state === "running"))
    return json(r, { detail: "this server's link is already being read" }, 409);
  const inst = rec.installed as { source?: McpSource } | null;
  const op: FakeOp = {
    id,
    server: null,
    kind: "resolve",
    state: "running",
    created_at: NOW,
    updated_at: NOW,
    result: null,
    error: null,
    progress: { step: "commit" },
    details: null,
    polls: f.opPolls,
    link: rec.origin.url,
    reserved: rec.id,
    updateOf: rec.id,
    previous: { source: rec.source, origin: rec.origin },
    installedNow: JSON.stringify(inst?.source) === JSON.stringify(rec.source),
  };
  f.ops[id] = op;
  return json(r, opView(op), 202);
}

/** A read of a running resolution: it moves through its steps, then settles. */
function readResolve(f: Fake, op: FakeOp) {
  const known = f.links[op.link ?? ""];
  if (f.resolveHold && op.progress?.step === f.resolveHold) return;
  const at = RESOLVE_STEPS.indexOf(op.progress?.step ?? "commit");
  const next = RESOLVE_STEPS[Math.min(at + 1, RESOLVE_STEPS.length - 1)];
  const res = known && "resolution" in known ? known.resolution : null;
  op.progress = {
    step: next,
    commit: res?.origin.commit ?? LINK_SHA,
    ...(res?.facts.declared && at >= 1 ? { ...res.facts.declared } : {}),
    ...(res?.source.kind === "go" && at >= 2 ? { version: res.source.version } : {}),
  };
  if (f.resolveHold && op.progress.step === f.resolveHold) return;
  if (--op.polls > 0) return;
  if (!known) Object.assign(op, { state: "failed", error: "there is no git repository at that link" });
  else if ("error" in known) Object.assign(op, { state: "failed", error: known.error, details: known.details ?? null });
  else if (op.updateOf) {
    const res = known.resolution;
    const same = JSON.stringify(res.source) === JSON.stringify(op.previous?.source);
    const result =
      same && op.installedNow
        ? { resolution_id: op.id, up_to_date: true, source: res.source, origin: res.origin, previous: op.previous, facts: res.facts, disclaimer: res.disclaimer }
        : { ...res, resolution_id: op.id, up_to_date: false, previous: op.previous };
    Object.assign(op, { state: "done", result });
  } else Object.assign(op, { state: "done", result: { ...known.resolution, resolution_id: op.id } });
  op.updated_at = NOW;
}

/** `POST /api/mcp/servers` with a `resolution_id`: the server-held source and origin, once. */
function createFromResolution(r: Route, f: Fake, body: Record<string, unknown>) {
  if ("origin" in body) return json(r, { detail: "origin comes only from a resolved link: send its resolution_id" }, 422);
  const op = f.ops[body.resolution_id as string];
  if (!op || op.kind !== "resolve" || op.state !== "done" || !op.result)
    return json(r, { detail: "this link's resolution is no longer held (it expired, or BattleLab restarted); resolve the link again" }, 404);
  if (op.consumed) return json(r, { detail: "a server was already registered from this resolution; resolve again" }, 409);
  if (op.updateOf) return json(r, { detail: "that resolution updates an existing server; it registers nothing new" }, 409);
  const res = op.result as McpResolution;
  if (body.source && JSON.stringify(body.source) !== JSON.stringify(res.source))
    return json(r, { detail: "the source differs from the package the link resolved to; resolve the link again" }, 409);
  op.consumed = true;
  const rec = record({
    id: op.reserved,
    name: body.name as string,
    label: (body.label as string) ?? "",
    source: res.source,
    transport: "stdio",
    origin: res.origin,
    updated_at: NOW,
  });
  f.servers.push(rec);
  return json(r, summary(f, rec), 201);
}

/** `PATCH …/{id} {resolution_id, expected_updated_at}` (Update from source, #2427): source and
 *  origin replaced together, fenced to what the link was read against (`mcp_registry.update`). */
function applyUpdate(r: Route, f: Fake, rec: Rec, body: Record<string, unknown>) {
  if (Object.keys(body).sort().join() !== "expected_updated_at,resolution_id")
    return json(r, { detail: "an update from source takes its source from the link, not from a field" }, 422);
  const op = f.ops[body.resolution_id as string];
  if (!op || op.kind !== "resolve" || op.state !== "done" || !op.result)
    return json(r, { detail: "this link's resolution is no longer held (it expired, or BattleLab restarted); resolve the link again" }, 404);
  if ((op.result as { up_to_date?: boolean }).up_to_date)
    return json(r, { detail: "the server is already up to date with its link; there is nothing to apply" }, 409);
  if (op.consumed) return json(r, { detail: "a server was already registered from this resolution; resolve again" }, 409);
  if (rec.updated_at !== body.expected_updated_at) return json(r, { detail: "the server changed since you loaded it" }, 409);
  if (op.updateOf !== rec.id) return json(r, { detail: "that resolution is not this server's update; read its link again" }, 409);
  if (JSON.stringify([rec.source, rec.origin]) !== JSON.stringify([op.previous?.source, op.previous?.origin]))
    return json(r, { detail: "the server changed since its link was read; update it again" }, 409);
  const res = op.result as McpResolution;
  op.consumed = true;
  Object.assign(rec, { source: res.source, origin: res.origin, rev: rec.rev + 1, updated_at: rec.updated_at + 1 });
  return json(r, summary(f, rec));
}

/** `mcp_jobs.start`: the same id returns the same operation; another id while one runs for this
 *  server is a 409; anything else starts one, answered 202. */
function startOp(r: Route, f: Fake, rec: Rec, kind: "review" | "install", body: Record<string, unknown>) {
  const keys = kind === "review" ? ["request_id"] : ["request_id", "review_id"];
  if (!body || Object.keys(body).sort().join() !== keys.join())
    return json(r, { detail: kind === "review" ? "send a request_id (a UUID)" : "send the review_id of a review and a request_id (a UUID)" }, 422);
  const id = body.request_id;
  if (typeof id !== "string" || !UUID.test(id)) return json(r, { detail: "request_id must be a UUID" }, 422);
  const existing = f.ops[id];
  if (existing) {
    if (existing.server !== rec.id || existing.kind !== kind)
      return json(r, { detail: "that request_id belongs to another operation" }, 409);
    return json(r, opView(existing), 202);
  }
  if (Object.values(f.ops).some((o) => o.server === rec.id && o.state === "running"))
    return json(r, { detail: "this server is being reviewed or installed" }, 409);
  const op: FakeOp = {
    id,
    server: rec.id,
    kind,
    state: "running",
    created_at: NOW,
    updated_at: NOW,
    result: null,
    error: null,
    polls: f.opPolls,
    reviewId: body.review_id as string | undefined,
  };
  f.ops[id] = op;
  return json(r, opView(op), 202);
}

/** One read of an operation: it settles once its `polls` run out. */
function readOp(f: Fake, op: FakeOp) {
  if (op.state === "running" && op.kind === "resolve") return readResolve(f, op);
  if (op.state !== "running" || --op.polls > 0) return;
  const rec = f.servers.find((s) => s.id === op.server);
  if (!rec) Object.assign(op, { state: "failed", error: "no such MCP server" });
  else if (op.kind === "review") Object.assign(op, { state: "done", result: reviewFacts(rec, `rv_${op.id.slice(0, 8)}`) });
  else if (f.installFails) {
    Object.assign(op, { state: "failed", error: f.installFails });
    f.installFails = "";
  } else {
    const fromLink = Object.values(f.ops).find(
      (o) => o.kind === "resolve" && (o.result as McpResolution | null)?.review?.review_id === op.reviewId,
    );
    rec.installed = { installed_at: NOW, integrity: (fromLink?.result as McpResolution | undefined)?.review.integrity ?? "sha512-AbC" };
    Object.assign(rec.installed, { source: rec.source, review_id: op.reviewId });
    rec.updated_at += 1;
    Object.assign(op, { state: "done", result: summary(f, rec) });
  }
  op.updated_at = NOW;
}

const NAME_RE = /^[a-z][a-z0-9_-]{1,31}$/;

/** `POST …/{id}/duplicate` (#2430), `mcp_registry.duplicate`'s rules: a retried `request_id`
 *  returns the copy it made; a stale revision is a 409; the name follows the server-name rule and
 *  is unique; `variables` remaps only the original's credential variables, each to an existing
 *  library SECRET. The copy keeps source, origin, install, args and probe; never projects,
 *  overrides or checks. */
async function duplicate(r: Route, f: Fake, rec: Rec, body: Record<string, unknown>) {
  const unknown = Object.keys(body ?? {}).filter((k) => !["name", "label", "variables", "request_id", "expected_updated_at"].includes(k));
  if (unknown.length) return json(r, { detail: "unknown duplicate field" }, 422);
  const rid = body.request_id as string | undefined;
  if (rid !== undefined && (typeof rid !== "string" || !UUID.test(rid))) return json(r, { detail: "request_id must be a UUID" }, 422);
  if (rid && f.dupRequests[rid]) {
    const made = f.dupRequests[rid];
    if (made.from !== rec.id) return json(r, { detail: "that request_id made a copy of another server" }, 409);
    return json(r, summary(f, f.servers.find((s) => s.id === made.copy)!), 201);
  }
  if (rec.updated_at !== body.expected_updated_at) return json(r, { detail: "the server changed since you loaded it" }, 409);
  const variables = (body.variables ?? {}) as Record<string, string>;
  const named = new Set(credVars(rec));
  const foreign = Object.keys(variables).filter((v) => !named.has(v)).sort();
  if (foreign.length) return json(r, { detail: `this server names no credential variable ${foreign[0]}` }, 422);
  for (const target of Object.values(variables))
    if (!f.variables.some((v) => v.name === target)) return json(r, { detail: `no secret named ${target} in Variables` }, 422);
  const name = body.name;
  if (typeof name !== "string" || !NAME_RE.test(name))
    return json(r, { detail: "a name is lowercase letters, digits, - and _ (2–32, letter first)" }, 422);
  if (f.servers.some((s) => s.name === name)) return json(r, { detail: `a server named ${name} exists` }, 422);
  const copy = record({
    name,
    label: (body.label as string) ?? "",
    source: { ...rec.source },
    transport: rec.transport,
    args: [...rec.args],
    settings: rec.settings.map((s) => (s.kind === "credential" ? { ...s, variable: variables[s.variable] ?? s.variable } : { ...s })),
    probe: rec.probe,
    installed: rec.installed ? { ...rec.installed } : null,
    ...(rec.origin ? { origin: { ...rec.origin } } : {}),
    created_at: NOW,
    updated_at: NOW,
  });
  f.servers.push(copy);
  if (rid) f.dupRequests[rid] = { from: rec.id, copy: copy.id };
  if (f.dupLoseResponses > 0) {
    f.dupLoseResponses -= 1;
    return r.abort("connectionreset");
  }
  return json(r, summary(f, copy), 201);
}

/** Install the fake on `page` (after the shell's common reads). */
export async function mockMcp(page: Page, f: Fake): Promise<void> {
  await commonMocks(page);
  await page.route("**/api/pulse/orchestrator", (r) => r.fulfill({ status: 500, json: { detail: "off" } }));
  await page.route(/\/api\/projects(\?.*)?$/, (r) =>
    json(r, { projects: PROJECTS.map((p) => ({ ...p, archived: false, created_at: NOW, updated_at: NOW })) }),
  );
  await page.route(/\/api\/variables(\/[^?]*)?(\?.*)?$/, async (r) => {
    const req = r.request();
    const path = new URL(req.url()).pathname;
    const body = req.postData() ? req.postDataJSON() : null;
    if (req.method() !== "GET") f.calls.push({ method: req.method(), path, body });
    if (path === "/api/variables" && req.method() === "GET")
      return json(r, {
        variables: f.variables.map((v) => ({ ...v, definition: { value_type: "text" }, set: true, created_at: v.updated_at })),
        collections: [],
        migration_required: false,
        limits: { variables_max: 200, collections_max: 100, secret_min: 8, value_max: 10000, name_max: 64 },
      });
    if (path === "/api/variables" && req.method() === "POST") {
      // The store is name-unique: an existing name is refused, never replaced (`template_vars`).
      if (f.variables.some((x) => x.name === body.name))
        return json(r, { detail: `a variable named ${body.name} already exists` }, 422);
      const v: FakeVar = { id: `v_${body.name}`, name: body.name, kind: "secret", projects: [], updated_at: NOW };
      f.variables.push(v);
      return json(r, { ...v, definition: { value_type: "text" }, set: true, created_at: NOW }, 201);
    }
    const m = path.match(/^\/api\/variables\/([^/]+)\/projects$/);
    if (m && req.method() === "PUT") {
      const v = f.variables.find((x) => x.id === m[1]);
      if (!v) return json(r, { detail: "no such variable" }, 404);
      v.projects = body.project_ids;
      v.updated_at += 1;
      return json(r, { ...v, definition: { value_type: "text" }, set: true, created_at: NOW });
    }
    return json(r, { detail: "not in the fake" }, 404);
  });
  await page.route(/\/api\/mcp\/servers(\/.*)?(\?.*)?$/, async (r) => {
    const req = r.request();
    const url = new URL(req.url());
    const path = url.pathname;
    const method = req.method();
    const body = req.postData() ? req.postDataJSON() : null;
    if (method !== "GET") f.calls.push({ method, path, body });
    if (path === "/api/mcp/servers/resolve-link" && method === "POST") return startResolve(r, f, body);
    if (path === "/api/mcp/servers" && method === "POST" && body && "resolution_id" in body)
      return createFromResolution(r, f, body);
    if (path === "/api/mcp/servers") {
      if (method === "GET") {
        if (f.listErrors > 0) {
          f.listErrors -= 1;
          return json(r, { detail: "the MCP server registry cannot be read" }, 500);
        }
        return json(r, { servers: f.servers.map((s) => summary(f, s)) });
      }
      const transport = body.source.kind === "remote" ? "http" : "stdio";
      const rec = record({
        name: body.name,
        label: body.label ?? "",
        source: body.source,
        transport,
        settings: settingsIn(body.settings, transport),
        updated_at: NOW,
      });
      f.servers.push(rec);
      return json(r, summary(f, rec), 201);
    }
    const m = path.match(/^\/api\/mcp\/servers\/([0-9a-f]{16})(\/.*)?$/);
    const rec = m && f.servers.find((s) => s.id === m[1]);
    if (!m || !rec) return json(r, { detail: "no such MCP server" }, 404);
    const sub = m[2] ?? "";
    if (sub === "" && method === "GET") {
      readUpdate(f, rec);
      return json(r, detail(f, rec));
    }
    if (sub === "/updates/check" && method === "POST") return startUpdateCheck(r, f, rec);
    if (sub === "/duplicate" && method === "POST") return duplicate(r, f, rec, body);
    if (sub === "" && method === "PATCH" && body && "resolution_id" in body) return applyUpdate(r, f, rec, body);
    if (sub === "" && method === "PATCH") {
      const fields = { ...body };
      delete fields.expected_updated_at;
      if (fields.settings) fields.settings = settingsIn(fields.settings, rec.transport);
      // A changed source clears the display-only origin (#2353).
      if (fields.source && JSON.stringify(fields.source) !== JSON.stringify(rec.source)) delete rec.origin;
      const bumps = ["settings", "source", "args", "probe"].some((k) => k in fields);
      Object.assign(rec, fields, { updated_at: rec.updated_at + 1, rev: rec.rev + (bumps ? 1 : 0) });
      return json(r, summary(f, rec));
    }
    if (sub === "" && method === "DELETE") {
      f.servers = f.servers.filter((s) => s !== rec);
      return r.fulfill({ status: 204, body: "" });
    }
    if (sub === "/check") {
      const row = f.failing.has(body.scope) ? failedRow(rec.id, body.scope, rec.rev) : passedRow(rec.id, body.scope, rec.rev, NOW);
      addCheck(f, rec, row);
      return json(r, row);
    }
    if ((sub === "/review" || sub === "/install") && method === "POST") {
      if (sub === "/review" && rec.transport !== "stdio") return json(r, { detail: "only a package server is reviewed" }, 422);
      return startOp(r, f, rec, sub === "/review" ? "review" : "install", body);
    }
    if (sub === "/projects") {
      const list = body.projects === "all" ? PROJECTS.map((p) => p.id) : body.projects;
      for (const pid of list) {
        const row = projectRow(f, rec, pid);
        if (row.credentials === "missing" || row.check !== "passed")
          if (body.projects !== "all")
            return json(r, { detail: `${pid} has no credentials or no passing check for them` }, 422);
      }
      rec.projects = body.projects;
      rec.updated_at += 1;
      return json(r, summary(f, rec));
    }
    const own = sub.match(/^\/projects\/([^/]+)\/credentials$/);
    if (own) {
      (f.own[rec.id] ??= {})[own[1]] = Object.keys(body.values);
      const k = `${rec.id}:${own[1]}`;
      f.ownRev[k] = (f.ownRev[k] ?? 0) + 1;
      return json(r, detail(f, rec));
    }
    return json(r, { detail: "not in the fake" }, 404);
  });
  await page.route(new RegExp(`${MCP_OPERATIONS_API}/[^/?]+/stop$`), async (r) => {
    const path = new URL(r.request().url()).pathname;
    f.calls.push({ method: "POST", path, body: null });
    const op = f.ops[decodeURIComponent(path.split("/").slice(-2)[0])];
    if (!op) return json(r, { detail: "no such operation" }, 404);
    if (op.state === "running") Object.assign(op, { state: "failed", error: "stopped: the link was not read" });
    return json(r, opView(op));
  });
  await page.route(new RegExp(`${MCP_OPERATIONS_API}/[^/?]+$`), async (r) => {
    const id = decodeURIComponent(new URL(r.request().url()).pathname.slice(MCP_OPERATIONS_API.length + 1));
    f.calls.push({ method: "GET", path: new URL(r.request().url()).pathname, body: null });
    const op = f.ops[id];
    if (!op) return json(r, { detail: "no such operation (it may predate an app restart; start again)" }, 404);
    readOp(f, op);
    return json(r, opView(op));
  });
}
