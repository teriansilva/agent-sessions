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
import type { McpCheckRow, McpOperation, McpReviewFacts, McpServerDetail, McpServerSummary, McpSetting } from "../src/types/mcp";
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
export type FakeOp = McpOperation & { polls: number; reviewId?: string };

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
  };
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
});

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
  if (op.state !== "running" || --op.polls > 0) return;
  const rec = f.servers.find((s) => s.id === op.server);
  if (!rec) Object.assign(op, { state: "failed", error: "no such MCP server" });
  else if (op.kind === "review") Object.assign(op, { state: "done", result: reviewFacts(rec, `rv_${op.id.slice(0, 8)}`) });
  else if (f.installFails) {
    Object.assign(op, { state: "failed", error: f.installFails });
    f.installFails = "";
  } else {
    rec.installed = { installed_at: NOW, integrity: "sha512-AbC" };
    Object.assign(rec.installed, { source: rec.source, review_id: op.reviewId });
    rec.updated_at += 1;
    Object.assign(op, { state: "done", result: summary(f, rec) });
  }
  op.updated_at = NOW;
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
    if (sub === "" && method === "GET") return json(r, detail(f, rec));
    if (sub === "" && method === "PATCH") {
      const fields = { ...body };
      delete fields.expected_updated_at;
      if (fields.settings) fields.settings = settingsIn(fields.settings, rec.transport);
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
  await page.route(new RegExp(`${MCP_OPERATIONS_API}/[^/?]+$`), async (r) => {
    const id = decodeURIComponent(new URL(r.request().url()).pathname.slice(MCP_OPERATIONS_API.length + 1));
    f.calls.push({ method: "GET", path: new URL(r.request().url()).pathname, body: null });
    const op = f.ops[id];
    if (!op) return json(r, { detail: "no such operation (it may predate an app restart; start again)" }, 404);
    readOp(f, op);
    return json(r, opView(op));
  });
}
