/** Settings → Agents → Add agent's picker (#2245), on the catalog an operator's host really had: a
 *  signed cut listing six agents, two bundled agents it does not list, and five included API
 *  agents. The picker offers only what this host can install; what it can't stays findable in a
 *  disabled group and is explained beside the picker (a disabled option can't be picked to read its
 *  reason); the API agents are named, never offered, and one whose source agent is nowhere says so. */
import { expect, test, type Page } from "@playwright/test";
import { mockRoster } from "./roster";
import type { PluginCatalog, PluginManifest } from "../src/types/plugins";

const MEMBERSHIP = "Not in the signed agent catalog this host accepted; a catalog update that lists it makes it installable.";
const INCLUDED = "Included with BattleLab; configure its source agent.";

const agent = (id: string, label: string, version: string): PluginManifest =>
  ({ identity: { id, label, version, publisher: "battlelab" } });
const client = (id: string, label: string, source: string): PluginManifest =>
  ({ identity: { id, label, version: "1", publisher: "battlelab" }, runtime: { kind: "api" }, api: { kind: "acp", source } });

const SIGNED = [
  agent("antigravity", "Antigravity", "1.2.14"), agent("claude", "Claude Code", "2.1.317"),
  agent("codex", "Codex", "0.169.0"), agent("gemini", "Gemini CLI", "0.68.0"),
  agent("kimi", "Kimi Code", "2.6.2"), agent("opencode", "opencode", "1.19.5"),
];
const UNLISTED = [agent("copilot", "GitHub Copilot CLI", "1.0.107"), agent("opencode2", "OpenCode 2", "2.2.7")];
const API = [
  client("claude-api", "Claude — API", "claude"), client("codex-api", "Codex — API", "codex"),
  client("copilot-api", "Copilot — API", "copilot"), client("opencode-api", "opencode — API", "opencode"),
  client("opencode2-api", "OpenCode 2 — API", "opencode2"),
];

async function fixture(page: Page, present: Record<string, boolean>, opts: { edit?: (data: PluginCatalog) => void; rosterFails?: boolean } = {}) {
  const data: PluginCatalog = {
    feed: { state: "ready", source: "remote", sequence: 3, error: null, stale: false, digest: "a".repeat(64), bundled_digest: "b".repeat(64),
      release_version: "0.20.0", refresh: { automatic: true, last_attempt: null, last_success: null, error: null } },
    catalog: [
      ...SIGNED.map((manifest, i) => ({ manifest, digest: String(i).repeat(64), source: "signed", installable: true, included: false, reason: null })),
      ...UNLISTED.map((manifest, i) => ({ manifest, digest: String(i + 6).repeat(64), source: null, installable: false, included: false, reason: MEMBERSHIP })),
      ...API.map((manifest, i) => ({ manifest, digest: "f".repeat(63) + i, source: null, installable: false, included: true, reason: INCLUDED })),
    ],
    plugins: [], operations: [], roster_generation: 1, roster_revision: null,
  };
  opts.edit?.(data);
  await page.route("**/api/**", r => r.fulfill({ json: {} }));
  await page.route("**/api/config", r => r.fulfill({ json: { csrf: "test", new_session_engines: [], terminal_backend: "ws", auth_mode: "none", agent_defaults: { default_engine: null, bypass: false } } }));
  await mockRoster(page, { overrides: Object.fromEntries(Object.entries(present).map(([id, value]) => [id, { present: value }])) });
  await page.route("**/api/sessions**", r => r.fulfill({ json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } } }));
  await page.route(/\/api\/projects(\?.*)?$/, r => r.fulfill({ json: { projects: [] } }));
  await page.route(/\/api\/folders(\?.*)?$/, r => r.fulfill({ json: { folders: [] } }));
  await page.route("**/api/missions**", r => r.fulfill({ json: { missions: [], total: 0, next_offset: null, facets: { projects: [], states: [] } } }));
  await page.route("**/api/pulse/notifications**", r => r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }));
  await page.route("**/api/agents/usage", r => r.fulfill({ json: { budgets: { threshold_pct: 90, notify: true, engines: {} }, agents: [] } }));
  await page.route("**/api/plugins", r => r.fulfill({ json: data }));
  if (opts.rosterFails) await page.route("**/api/engines", r => r.fulfill({ status: 500, json: { detail: "roster unavailable" } }));
}

for (const theme of ["dark", "light"] as const) {
  test(`the picker offers what this host can install and explains the rest · ${theme}`, async ({ page }, info) => {
    await fixture(page, { copilot: false, opencode2: false });
    await page.goto("/settings/agents/setup/new");
    await page.evaluate(value => document.documentElement.dataset.theme = value, theme);
    const select = page.getByRole("combobox", { name: "Agent", exact: true });
    await expect(select.locator("option")).toHaveCount(1 + SIGNED.length + UNLISTED.length);

    // Every installable agent is an enabled option, and nothing else is.
    for (const m of SIGNED) await expect(select.locator(`option[value="${m.identity.id}"]`)).toHaveJSProperty("disabled", false);
    // An included API agent is never installed here: it is not an option at all.
    for (const m of API) await expect(select.locator(`option[value="${m.identity.id}"]`)).toHaveCount(0);
    // What can't be installed stays findable, in one disabled group.
    const group = select.locator("optgroup");
    await expect(group).toHaveAttribute("label", "Can’t be installed right now");
    await expect(group).toHaveJSProperty("disabled", true);
    await expect(group.locator("option")).toHaveText(UNLISTED.map(m => `${m.identity.label} · ${m.identity.version}`));

    // …and each one's reason is on the page, without picking it.
    const blocked = page.getByTestId("agents-blocked");
    for (const m of UNLISTED) await expect(blocked.getByRole("listitem").filter({ hasText: m.identity.label })).toContainText(MEMBERSHIP);

    // The API agents are named; one whose source agent is nowhere says which agent it needs.
    const included = page.getByTestId("agents-included");
    for (const m of API) await expect(included).toContainText(m.identity.label);
    await expect(included).toContainText("An API agent isn’t installed on its own: it uses its source agent’s installation.");
    await expect(included).toContainText("OpenCode 2 — API needs OpenCode 2 installed first.");
    await expect(included).toContainText("Copilot — API needs GitHub Copilot CLI installed first.");
    await expect(included).not.toContainText("Claude — API needs");
    await expect(included.getByRole("link", { name: "Agents" })).toHaveAttribute("href", "/settings/agents");

    await select.selectOption("claude");
    await expect(page.getByRole("button", { name: "Review installation" })).toBeEnabled();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath(`agent-picker-${theme}.png`), fullPage: true });
  });
}

test("an API agent whose source agent is on this host needs nothing first", async ({ page }) => {
  // The source can't be installed from the catalog, but a host binary is in the roster.
  await fixture(page, { copilot: false, opencode2: true });
  await page.goto("/settings/agents/setup/new");
  const included = page.getByTestId("agents-included");
  await expect(included).toContainText("Copilot — API needs GitHub Copilot CLI installed first.");
  await expect(included).not.toContainText("OpenCode 2 — API needs");
});

for (const id of ["opencode2", "opencode2-api"]) test(`a deep link to ${id}, which can't be installed here, can't be reviewed and says why`, async ({ page }) => {
  await fixture(page, { copilot: false, opencode2: false });
  await page.goto(`/settings/agents/setup/new?plugin=${id}`);
  await expect(page.getByTestId("agents-included")).toContainText("OpenCode 2 — API needs OpenCode 2 installed first.");
  await expect(page.getByTestId("agents-blocked").getByRole("listitem").filter({ hasText: "OpenCode 2" })).toContainText(MEMBERSHIP);
  await expect(page.getByRole("button", { name: "Review installation" })).toBeDisabled();
});

test("with nothing installable the picker offers nothing and every agent says why", async ({ page }) => {
  await fixture(page, { copilot: false, opencode2: false }, { edit: data => {
    for (const c of data.catalog) if (!c.included) Object.assign(c, { installable: false, source: null, reason: MEMBERSHIP });
  } });
  await page.goto("/settings/agents/setup/new");
  const select = page.getByRole("combobox", { name: "Agent", exact: true });
  await expect(select.locator("optgroup option")).toHaveCount(SIGNED.length + UNLISTED.length);
  await expect(select.locator("option:not(optgroup option)")).toHaveText(["Select an agent"]);
  await expect(page.getByTestId("agents-blocked").getByRole("listitem")).toHaveCount(SIGNED.length + UNLISTED.length);
  // Every source is now uninstallable, but all but two are on this host: only those two need one.
  await expect(page.getByTestId("agents-included").getByRole("listitem")).toHaveText([
    "Copilot — API needs GitHub Copilot CLI installed first.", "OpenCode 2 — API needs OpenCode 2 installed first.",
  ]);
  await expect(page.getByRole("button", { name: "Review installation" })).toBeDisabled();
});

test("a feed error is said once, not repeated as every agent's reason", async ({ page }) => {
  const error = "Saved catalog trust evidence is unavailable. Restore it before installing.";
  await fixture(page, { copilot: false, opencode2: false }, { edit: data => {
    data.feed = { ...data.feed, state: "unavailable", source: "unavailable", error };
    for (const c of data.catalog) if (!c.included) Object.assign(c, { installable: false, source: null, reason: error });
  } });
  await page.goto("/settings/agents/setup/new");
  await expect(page.getByRole("alert").filter({ hasText: error }).first()).toBeVisible();
  const blocked = page.getByTestId("agents-blocked");
  await expect(blocked.getByRole("listitem")).toHaveCount(SIGNED.length + UNLISTED.length);
  await expect(blocked).not.toContainText(error);
});

test("before the roster has ever loaded, no source agent is called missing", async ({ page }) => {
  await fixture(page, { copilot: false, opencode2: false }, { rosterFails: true });
  await page.goto("/settings/agents/setup/new");
  const included = page.getByTestId("agents-included");
  await expect(included).toContainText("OpenCode 2 — API");
  await expect(included.getByRole("listitem")).toHaveCount(0);
  await expect(included).not.toContainText("installed first");
});
