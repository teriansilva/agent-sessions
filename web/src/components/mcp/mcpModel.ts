/** Library → MCP servers (#2163 P1c): the pages' pure logic — what a state, a source, a check and a
 *  project row SAY, the settings editor's rows ↔ the API's settings, and what Enable does per
 *  project. No React and no fetch here, so `mcpModel.test.ts` pins every word and every decision. */
import type {
  McpCheckRow,
  McpCheckState,
  McpCredentialSource,
  McpProjectStatus,
  McpServerDetail,
  McpServerSummary,
  McpSetting,
  McpSettingInput,
  McpSource,
} from "../../types/mcp";

/** An LED tone. Status colours mean state only (design.md): `up` ready / passed, `att` it needs
 *  the operator (amber), `down` only for a check that really failed, `idle` a choice or nothing yet. */
export type Tone = "up" | "att" | "down" | "idle";

/** `mcp_registry.NAME_RE`, the reserved prefix, and the variables store's name rule. */
export const NAME_RE = /^[a-z][a-z0-9_-]{1,31}$/;
export const RESERVED_PREFIX = "battlelab_";
export const VARIABLE_RE = /^[a-z][a-z0-9_]{0,31}$/;
export const ENV_KEY_RE = /^[A-Z_][A-Z0-9_]{0,63}$/;
export const HEADER_KEY_RE = /^[A-Za-z0-9][A-Za-z0-9-]{0,63}$/;
/** `template_secrets.SECRET_MIN`. */
export const SECRET_MIN = 8;

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

export function urlHost(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

/** The index's source column: what it is, then how it runs. */
export function sourceLine(server: Pick<McpServerSummary, "source" | "settings">): {
  kind: string;
  main: string;
  sub: string;
} {
  const s = server.source;
  if (s.kind === "remote") {
    const headers = server.settings
      .filter((x) => x.kind === "credential")
      .map((x) => `${x.key} header`);
    return {
      kind: "remote",
      main: urlHost(s.url),
      sub: ["HTTP", ...headers].join(" · "),
    };
  }
  return { kind: s.kind, main: `${s.package} ${s.version}`, sub: "stdio · started per session" };
}

/** The short source chip on a server page (`NPM · 0.14.2`, `REMOTE · mcp.example.com`). */
export function sourceChip(source: McpSource): string {
  return source.kind === "remote"
    ? `remote · ${urlHost(source.url)}`
    : `${source.kind} · ${source.version}`;
}

/** The index's and the server page's status: an LED tone and its words. */
export function stateView(
  state: McpServerSummary["state"],
  projectName: (id: string) => string = (id) => id,
): { tone: Tone; words: string } {
  const ps = state.projects ?? [];
  const where = (lead: string) =>
    ps.length === 1 ? `${lead} in ${projectName(ps[0])}` : `${lead} in ${plural(ps.length, "project")}`;
  switch (state.kind) {
    case "ready":
      return { tone: "up", words: "Ready" };
    case "needs_credentials":
      return { tone: "att", words: `Needs credentials in ${plural(ps.length, "project")}` };
    case "needs_check":
      return { tone: "att", words: where("Needs a check") };
    case "failing":
      return { tone: "down", words: where("Check failing") };
    case "disabled":
      return { tone: "idle", words: "Disabled · no new session gets its tools" };
    case "not_enabled":
    default:
      return { tone: "idle", words: "Not enabled in any project" };
  }
}

/** `3 of 4 projects` / `All projects` / `No projects`. */
export function projectsSummary(
  server: Pick<McpServerSummary, "projects" | "enabled_projects" | "project_count">,
): string {
  if (server.projects === "all") return "All projects";
  const n = server.enabled_projects.length;
  if (n === 0) return "No projects";
  return `${n} of ${plural(server.project_count, "project")}`;
}

export function credentialWords(c: McpCredentialSource): string {
  return { own: "Own", shared: "Shared", missing: "Missing", none_needed: "None needed" }[c];
}

/** `today 14:02`, `yesterday 18:40`, `3 Nov` — local time, from epoch SECONDS. */
export function when(at: number | null | undefined, now: Date = new Date()): string {
  if (!at) return "";
  const d = new Date(at * 1000);
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  const day = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(now) - day(d)) / 86_400_000);
  if (diff === 0) return `today ${hm}`;
  if (diff === 1) return `yesterday ${hm}`;
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

/** A project row's check column. `stale` means the resolved set (a credential, a setting, the
 *  source) changed since its last check: amber, it needs the operator, nothing failed. */
export function checkView(
  check: McpCheckState,
  checkedAt: number | null,
  now?: Date,
): { tone: Tone; words: string } {
  const t = when(checkedAt, now);
  switch (check) {
    case "passed":
      return { tone: "up", words: t ? `Passed · ${t}` : "Passed" };
    case "failed":
      return { tone: "down", words: t ? `Check failed · ${t}` : "Check failed" };
    case "stale":
      return { tone: "att", words: "Credentials changed — check again" };
    case "needs_credentials":
      return { tone: "idle", words: "Not run" };
    case "not_run":
    default:
      return { tone: "idle", words: "Not run" };
  }
}

const STEP_LABELS: Record<string, string> = {
  settings: "Settings",
  installed: "Installed",
  started: "Started",
  handshake: "Handshake",
  tools: "Tools listed",
  probe: "Probe call",
  timeout: "Timed out",
  stderr: "Server output",
};
export const stepLabel = (step: string) => STEP_LABELS[step] ?? step;

/** A check row's verdict: the headline and which step stopped it. */
export function verdict(row: McpCheckRow): { tone: Tone; words: string; sub: string } {
  if (row.outcome === "passed") return { tone: "up", words: "Passed", sub: "" };
  if (row.outcome === "superseded")
    return {
      tone: "att",
      words: "Superseded",
      sub: "A setting or credential changed while it ran. Run it again.",
    };
  const steps = row.steps.filter((s) => s.step !== "stderr");
  const at = steps.findIndex((s) => !s.ok);
  return {
    tone: "down",
    words: "Check failed",
    sub: at >= 0 ? `Step ${at + 1} of ${steps.length}: ${stepLabel(steps[at].step)}.` : "",
  };
}

/** The tools of the newest PASSING check at the server's current revision (Overview, the probe
 *  picker). Empty when none passed yet. */
export function currentTools(checks: McpCheckRow[], rev: number): string[] {
  const row = checks.find((c) => c.outcome === "passed" && c.rev === rev);
  return row ? row.tools : [];
}

/** A variable name for a new credential: `<server>_<key>`, folded to the store's rule. */
export function variableNameFor(serverName: string, key: string): string {
  let n = `${serverName}_${key}`
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^[^a-z]+/, "")
    .replace(/_+$/, "");
  if (!n) n = "mcp_credential";
  return n.slice(0, 32).replace(/_+$/, "");
}

/** A name for the server from what the operator typed as its source. */
export function suggestName(source: McpSource): string {
  let host = "";
  if (source.kind === "remote") {
    try {
      host = new URL(source.url).hostname;
    } catch {
      return "";
    }
  }
  const raw =
    source.kind === "remote"
      ? (host.split(".").find((p) => p && p !== "mcp" && p !== "www") ?? "")
      : (source.package.split("/").pop() ?? "").replace(/[-_.]?mcp[-_.]?(server)?$/i, "").replace(/^mcp[-_.]/i, "");
  const n = raw
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^[^a-z]+/, "")
    .slice(0, 32)
    .replace(/[-_]+$/, "");
  return NAME_RE.test(n) && !n.startsWith(RESERVED_PREFIX) ? n : "";
}

export function nameError(name: string): string {
  if (!name) return "Give it a name.";
  if (!NAME_RE.test(name)) return "Lowercase letters, digits, - and _ (2–32, a letter first).";
  if (name.startsWith(RESERVED_PREFIX)) return `Names starting with ${RESERVED_PREFIX} are BattleLab's own.`;
  return "";
}

// ---- the settings editor -------------------------------------------------------------------------

/** One editor row. A credential is either a NEW secret (its value is typed here, write-only, and
 *  sent once to Variables) or an EXISTING Variables secret picked by name. */
export interface SettingRow {
  rid: string;
  key: string;
  kind: "plain" | "credential";
  value: string;
  credMode: "new" | "existing";
  variable: string;
  /** New credentials only: the value, never read back. */
  secret: string;
  prefix: string;
}

let ridSeq = 0;
export const newRid = () => `r${++ridSeq}`;

export function blankRow(kind: SettingRow["kind"] = "plain"): SettingRow {
  return { rid: newRid(), key: "", kind, value: "", credMode: "new", variable: "", secret: "", prefix: "" };
}

/** The server's stored settings as editor rows: credentials name an existing variable. */
export function rowsFromSettings(settings: McpSetting[]): SettingRow[] {
  return settings.map((s) =>
    s.kind === "plain"
      ? { ...blankRow("plain"), key: s.key, value: s.value }
      : {
          ...blankRow("credential"),
          key: s.key,
          credMode: "existing",
          variable: s.variable,
          prefix: s.prefix,
        },
  );
}

export interface RowErrors {
  key?: string;
  value?: string;
  variable?: string;
}

/** Validate the rows for a transport; `errors` is keyed by row id, empty when all is well.
 *  `existingSecrets` are the Variables secrets' names (a NEW credential must not reuse one). */
export function validateRows(
  rows: SettingRow[],
  transport: "stdio" | "http",
  existingSecrets: ReadonlySet<string>,
): Record<string, RowErrors> {
  const out: Record<string, RowErrors> = {};
  const seen = new Set<string>();
  const newNames = new Set<string>();
  for (const r of rows) {
    const e: RowErrors = {};
    const key = r.key.trim();
    if (transport === "stdio") {
      if (!ENV_KEY_RE.test(key)) e.key = "An environment variable: A–Z, 0–9 and _.";
    } else if (!HEADER_KEY_RE.test(key)) e.key = "A header name: letters, digits and -.";
    const folded = transport === "http" ? key.toLowerCase() : key;
    if (!e.key && seen.has(folded)) e.key = "Each setting appears once.";
    seen.add(folded);
    if (r.kind === "credential") {
      if (!VARIABLE_RE.test(r.variable)) e.variable = "Lowercase letters, digits and _ (a letter first).";
      else if (r.credMode === "new" && existingSecrets.has(r.variable))
        e.variable = "A variable of that name exists: pick it under Existing variable.";
      else if (r.credMode === "new" && newNames.has(r.variable)) e.variable = "Two new credentials share this name.";
      if (r.credMode === "new") {
        newNames.add(r.variable);
        if (r.secret.length < SECRET_MIN) e.value = `At least ${SECRET_MIN} characters.`;
        else if (r.secret !== r.secret.trim()) e.value = "No spaces at the start or end.";
      }
      if (r.credMode === "existing" && !e.variable && !existingSecrets.has(r.variable))
        e.variable = "No secret of that name in Variables.";
    }
    if (Object.keys(e).length) out[r.rid] = e;
  }
  return out;
}

/** The rows as the API's settings (after any new credential has been stored in Variables). */
export function settingsFromRows(rows: SettingRow[], transport: "stdio" | "http"): McpSettingInput[] {
  return rows.map((r) =>
    r.kind === "plain"
      ? { key: r.key.trim(), kind: "plain", value: r.value }
      : {
          key: r.key.trim(),
          kind: "credential",
          variable: r.variable,
          ...(transport === "http" && r.prefix ? { prefix: r.prefix } : {}),
        },
  );
}

/** How a row reaches the server, for the "Delivered as" column. */
export function deliveredAs(row: Pick<SettingRow, "key" | "kind" | "prefix">, transport: "stdio" | "http") {
  const key = row.key.trim() || "…";
  return transport === "stdio"
    ? { how: "env variable", what: key }
    : { how: "HTTP header", what: `${key}: ${row.kind === "credential" && row.prefix ? row.prefix : ""}…` };
}

/** Arguments: one per line, blank lines dropped. */
export const argsFromText = (text: string) =>
  text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);

// ---- enabling ------------------------------------------------------------------------------------

/** What Enable does for one project row:
 *  - `ready`: a check passed for its current set — turn it on;
 *  - `check`: it has credentials (its own, or none are needed) but no passing check — run its check
 *    first, and turn it on only if that passes;
 *  - `bind`: it has none but the shared credentials exist — bind them to it (Variables), then as
 *    `check` (a passing shared check already counts: the set is the same);
 *  - `blocked`: no credentials and nothing shared to use — "Add credentials first". */
export type EnableAction = "ready" | "check" | "bind" | "blocked";

export function enableAction(row: McpProjectStatus, sharedAvailable: boolean): EnableAction {
  if (row.credentials === "missing") return sharedAvailable ? "bind" : "blocked";
  return row.check === "passed" ? "ready" : "check";
}

/** Whether the library (shared) values resolve: every credential the server names is set. */
export const sharedAvailable = (detail: Pick<McpServerDetail, "status">) =>
  detail.status.shared.missing.length === 0;

/** The variables each `bind` project needs bound, grouped per variable: `{api_key: [pid, …]}`. */
export function bindPlan(rows: McpProjectStatus[], chosen: ReadonlySet<string>): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const r of rows) {
    if (!chosen.has(r.project_id) || r.credentials !== "missing") continue;
    for (const v of r.missing) (out[v] ??= []).push(r.project_id);
  }
  return out;
}

/** The project list to PUT after Enable: in Selected mode, exactly the chosen projects that ended
 *  up with a passing check. */
export const finalProjects = (chosen: readonly string[], passed: ReadonlySet<string>) =>
  chosen.filter((p) => passed.has(p));

/** The project list after one switch flips on the server page. `all` turned off for one project
 *  becomes every OTHER project, explicitly. */
export function toggledProjects(
  current: "all" | string[],
  every: readonly string[],
  pid: string,
  on: boolean,
): "all" | string[] {
  if (current === "all" && on) return "all";
  const list = current === "all" ? [...every] : [...current];
  if (on) return list.includes(pid) ? list : [...list, pid];
  return list.filter((p) => p !== pid);
}
