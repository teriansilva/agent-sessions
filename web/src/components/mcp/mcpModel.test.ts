import { describe, expect, test } from "vitest";

import type { McpCheckRow, McpProjectStatus, McpServerSummary } from "../../types/mcp";
import {
  bindPlan,
  blankRow,
  checkView,
  currentTools,
  deliveredAs,
  enableAction,
  finalProjects,
  argsFromText,
  nameError,
  projectsSummary,
  rowsFromSettings,
  settingsFromRows,
  sourceLine,
  stateView,
  suggestName,
  toggledProjects,
  validateRows,
  variableNameFor,
  verdict,
  when,
  type SettingRow,
} from "./mcpModel";

const row = (p: Partial<McpProjectStatus> & { project_id: string }): McpProjectStatus => ({
  enabled: false,
  credentials: "shared",
  missing: [],
  check: "not_run",
  checked_at: null,
  delivered: false,
  ...p,
});

describe("what the index says", () => {
  test("a package source and a remote one", () => {
    expect(
      sourceLine({ source: { kind: "npm", package: "nextcloud-mcp-server", version: "0.14.2" }, settings: [] }),
    ).toEqual({ kind: "npm", main: "nextcloud-mcp-server 0.14.2", sub: "stdio · started per session" });
    expect(
      sourceLine({
        source: { kind: "remote", url: "https://mcp.example.com/mcp" },
        settings: [{ key: "Authorization", deliver: "header", kind: "credential", variable: "k", prefix: "Bearer " }],
      }),
    ).toEqual({ kind: "remote", main: "mcp.example.com", sub: "HTTP · Authorization header" });
  });

  test("state words and tones: red only for a failing check, amber when it needs the operator", () => {
    const name = (id: string) => ({ p1: "orbital" })[id] ?? id;
    expect(stateView({ kind: "ready" })).toEqual({ tone: "up", words: "Ready" });
    expect(stateView({ kind: "needs_credentials", projects: ["p1"] }, name)).toEqual({
      tone: "att",
      words: "Needs credentials in 1 project",
    });
    expect(stateView({ kind: "failing", projects: ["p1"] }, name)).toEqual({
      tone: "down",
      words: "Check failing in orbital",
    });
    expect(stateView({ kind: "failing", projects: ["p1", "p2"] }, name).words).toBe("Check failing in 2 projects");
    expect(stateView({ kind: "needs_check", projects: ["p1"] }, name)).toEqual({
      tone: "att",
      words: "Needs a check in orbital",
    });
    expect(stateView({ kind: "disabled" }).tone).toBe("idle");
    expect(stateView({ kind: "not_enabled" })).toEqual({ tone: "idle", words: "Not enabled in any project" });
  });

  test("the projects summary", () => {
    const s = (projects: McpServerSummary["projects"], enabled: string[]) =>
      projectsSummary({ projects, enabled_projects: enabled, project_count: 4 });
    expect(s("all", ["a", "b"])).toBe("All projects");
    expect(s(["a", "b", "c"], ["a", "b", "c"])).toBe("3 of 4 projects");
    expect(s([], [])).toBe("No projects");
  });
});

describe("checks", () => {
  const now = new Date(2026, 10, 19, 15, 0);
  const at = (d: Date) => d.getTime() / 1000;
  test("when: today, yesterday, a date", () => {
    expect(when(at(new Date(2026, 10, 19, 14, 2)), now)).toBe("today 14:02");
    expect(when(at(new Date(2026, 10, 18, 18, 40)), now)).toBe("yesterday 18:40");
    expect(when(at(new Date(2026, 10, 3, 9, 0)), now)).toBe("3 Nov");
    expect(when(null, now)).toBe("");
  });

  test("a stale check is amber and says what to do", () => {
    expect(checkView("stale", null)).toEqual({ tone: "att", words: "Credentials changed — check again" });
    expect(checkView("passed", at(new Date(2026, 10, 19, 14, 2)), now)).toEqual({
      tone: "up",
      words: "Passed · today 14:02",
    });
    expect(checkView("failed", null).tone).toBe("down");
    expect(checkView("not_run", null)).toEqual({ tone: "idle", words: "Not run" });
  });

  test("the verdict names the step that stopped it; stderr is not a step", () => {
    const failed: McpCheckRow = {
      scope: "shared",
      rev: 1,
      outcome: "failed",
      at: 0,
      tools: [],
      steps: [
        { step: "settings", ok: true, detail: "resolved" },
        { step: "started", ok: true, detail: "40 ms" },
        { step: "handshake", ok: false, detail: "401" },
        { step: "stderr", ok: false, detail: "…" },
      ],
    };
    expect(verdict(failed)).toEqual({ tone: "down", words: "Check failed", sub: "Step 3 of 3: Handshake." });
    expect(verdict({ ...failed, outcome: "superseded" }).tone).toBe("att");
    expect(verdict({ ...failed, outcome: "passed" }).words).toBe("Passed");
  });

  test("tools come from the newest passing check at the current revision only", () => {
    const c = (outcome: McpCheckRow["outcome"], rev: number, tools: string[]): McpCheckRow => ({
      scope: "shared",
      rev,
      outcome,
      at: 0,
      steps: [],
      tools,
    });
    // newest first, as the API answers
    const checks = [c("failed", 2, []), c("passed", 1, ["old"]), c("passed", 2, ["a", "b"])];
    expect(currentTools(checks, 2)).toEqual(["a", "b"]);
    expect(currentTools(checks, 3)).toEqual([]);
  });
});

describe("names", () => {
  test("a variable name for a new credential follows the store's rule", () => {
    expect(variableNameFor("nextcloud", "NEXTCLOUD_APP_PASSWORD")).toBe("nextcloud_nextcloud_app_password");
    expect(variableNameFor("linear", "Authorization")).toBe("linear_authorization");
    expect(variableNameFor("9x", "A")).toBe("x_a");
    expect(variableNameFor("a".repeat(30), "KEY")).toMatch(/^[a-z][a-z0-9_]{0,31}$/);
  });

  test("a server name is suggested from the source, and checked", () => {
    expect(suggestName({ kind: "npm", package: "nextcloud-mcp-server", version: "1.0.0" })).toBe("nextcloud");
    expect(suggestName({ kind: "npm", package: "@acme/mcp-linear", version: "1.0.0" })).toBe("linear");
    expect(suggestName({ kind: "remote", url: "https://mcp.sentry.dev/mcp" })).toBe("sentry");
    expect(suggestName({ kind: "remote", url: "not a url" })).toBe("");
    expect(nameError("nextcloud")).toBe("");
    expect(nameError("Next")).not.toBe("");
    expect(nameError("battlelab_x")).toMatch(/BattleLab's own/);
    expect(nameError("")).toBe("Give it a name.");
  });
});

describe("the settings editor", () => {
  const r = (p: Partial<SettingRow>): SettingRow => ({ ...blankRow(), ...p });

  test("rows validate per transport; a new credential is write-only and never reuses a name", () => {
    const rows = [
      r({ rid: "a", key: "NEXTCLOUD_HOST", value: "https://cloud.example.com" }),
      r({ rid: "b", key: "lower", value: "x" }),
      r({ rid: "c", key: "TOKEN", kind: "credential", credMode: "new", variable: "taken", secret: "longenough" }),
      r({ rid: "d", key: "PASS", kind: "credential", credMode: "new", variable: "fresh", secret: "short" }),
      r({ rid: "e", key: "NEXTCLOUD_HOST", value: "dup" }),
      r({ rid: "f", key: "OTHER", kind: "credential", credMode: "existing", variable: "gone" }),
    ];
    const errs = validateRows(rows, "stdio", new Set(["taken"]));
    expect(Object.keys(errs).sort()).toEqual(["b", "c", "d", "e", "f"]);
    expect(errs.c.variable).toMatch(/Existing variable/);
    expect(errs.d.value).toMatch(/At least 8/);
    expect(errs.e.key).toMatch(/once/);
    expect(errs.f.variable).toMatch(/No secret/);
    expect(validateRows([r({ rid: "h", key: "Authorization" })], "http", new Set())).toEqual({});
    expect(validateRows([r({ rid: "h", key: "AUTH" })], "stdio", new Set())).toEqual({});
  });

  test("rows become the API's settings; the value of a new credential never does", () => {
    const rows = [
      r({ key: "HOST", value: "h" }),
      r({ key: "Authorization", kind: "credential", variable: "k", secret: "s3cret-value", prefix: "Bearer " }),
    ];
    const http = settingsFromRows(rows, "http");
    expect(http).toEqual([
      { key: "HOST", kind: "plain", value: "h" },
      { key: "Authorization", kind: "credential", variable: "k", prefix: "Bearer " },
    ]);
    expect(JSON.stringify(http)).not.toContain("s3cret-value");
    expect(settingsFromRows(rows, "stdio")[1]).toEqual({ key: "Authorization", kind: "credential", variable: "k" });
  });

  test("stored settings come back as rows naming existing variables", () => {
    const back = rowsFromSettings([
      { key: "H", deliver: "env", kind: "plain", value: "v" },
      { key: "K", deliver: "env", kind: "credential", variable: "k", prefix: "" },
    ]);
    expect(back.map((x) => [x.key, x.kind, x.credMode, x.variable, x.value])).toEqual([
      ["H", "plain", "new", "", "v"],
      ["K", "credential", "existing", "k", ""],
    ]);
  });

  test("delivered as, and arguments", () => {
    expect(deliveredAs({ key: "TOKEN", kind: "credential", prefix: "" }, "stdio")).toEqual({
      how: "env variable",
      what: "TOKEN",
    });
    expect(deliveredAs({ key: "Authorization", kind: "credential", prefix: "Bearer " }, "http")).toEqual({
      how: "HTTP header",
      what: "Authorization: Bearer …",
    });
    expect(argsFromText(" --read-only \n\n stdio")).toEqual(["--read-only", "stdio"]);
  });
});

describe("enabling", () => {
  test("each row's action follows its credentials and its check", () => {
    expect(enableAction(row({ project_id: "a", check: "passed" }), true)).toBe("ready");
    expect(enableAction(row({ project_id: "a", credentials: "own", check: "not_run" }), false)).toBe("check");
    expect(enableAction(row({ project_id: "a", credentials: "own", check: "stale" }), false)).toBe("check");
    expect(enableAction(row({ project_id: "a", credentials: "none_needed", check: "not_run" }), false)).toBe("check");
    expect(enableAction(row({ project_id: "a", credentials: "missing", missing: ["k"] }), true)).toBe("bind");
    expect(enableAction(row({ project_id: "a", credentials: "missing", missing: ["k"] }), false)).toBe("blocked");
  });

  test("binding is planned per variable, only for chosen projects that miss it", () => {
    const rows = [
      row({ project_id: "a", credentials: "missing", missing: ["k", "u"] }),
      row({ project_id: "b", credentials: "missing", missing: ["k"] }),
      row({ project_id: "c", credentials: "missing", missing: ["k"] }),
      row({ project_id: "d", credentials: "own" }),
    ];
    expect(bindPlan(rows, new Set(["a", "b", "d"]))).toEqual({ k: ["a", "b"], u: ["a"] });
  });

  test("only chosen projects whose check passed are put; a failed one stays off", () => {
    expect(finalProjects(["a", "b", "c"], new Set(["a", "c", "z"]))).toEqual(["a", "c"]);
  });

  test("one switch on the server page", () => {
    expect(toggledProjects(["a"], ["a", "b", "c"], "b", true)).toEqual(["a", "b"]);
    expect(toggledProjects(["a", "b"], ["a", "b", "c"], "a", false)).toEqual(["b"]);
    expect(toggledProjects("all", ["a", "b", "c"], "b", false)).toEqual(["a", "c"]);
    expect(toggledProjects("all", ["a", "b"], "b", true)).toBe("all");
  });
});
