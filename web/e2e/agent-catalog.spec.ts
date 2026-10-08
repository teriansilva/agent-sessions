import { expect, test, type Page } from "@playwright/test";
import { mockRoster } from "./roster";
import type { PluginCatalog } from "../src/types/plugins";

async function catalogFixture(page: Page, state: "bundled" | "expired" | "corrupt" = "bundled") {
  const unavailable = state === "corrupt";
  const data: PluginCatalog = {
    feed: {
      state: unavailable ? "unavailable" : "ready", source: unavailable ? "unavailable" : state === "expired" ? "remote" : "bundled",
      error: unavailable ? "Saved catalog trust evidence is unavailable. Restore it before installing." : null,
      stale: state === "expired", sequence: state === "expired" ? 3 : null,
      digest: "a".repeat(64), bundled_digest: "b".repeat(64), release_version: "0.20.0",
      definitions_url: "https://github.com/teriansilva/agent-sessions/tree/main/release/recipes/linux-x64",
      history_url: "https://github.com/teriansilva/agent-sessions/commits/main/release/recipes/linux-x64",
      updates_url: "https://github.com/teriansilva/agent-sessions/releases",
      refresh: { automatic: true, last_attempt: null, last_success: null, error: null },
    },
    catalog: [
      { manifest: { identity: { id: "sample-agent", label: "Sample agent", version: "1.2.3", publisher: "BattleLab" } }, digest: "c".repeat(64), installable: !unavailable, source: unavailable ? null : "bundled", reason: unavailable ? "Restore catalog evidence before installing." : null },
      { manifest: { identity: { id: "sample-api", label: "Sample API", version: "1", publisher: "BattleLab" }, runtime: { kind: "api" }, api: { kind: "native", source: "sample-agent" } }, digest: "d".repeat(64), installable: false, included: true, reason: "Included with BattleLab; configure its source agent." },
      { manifest: { identity: { id: "removed-agent", label: "Removed agent", version: "1", publisher: "BattleLab" } }, digest: "e".repeat(64), installable: false, reason: "This agent is not offered by the last accepted remote catalog." },
    ],
    plugins: [], operations: [], roster_generation: 1, roster_revision: null,
  };
  const writes: string[] = [];
  await page.route("**/api/**", route => route.fulfill({ json: {} }));
  await page.route("**/api/config", route => route.fulfill({ json: { csrf: "test", new_session_engines: [], terminal_backend: "ws", auth_mode: "none", agent_defaults: { default_engine: null, bypass: false } } }));
  await mockRoster(page);
  await page.route("**/api/sessions**", route => route.fulfill({ json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } } }));
  await page.route(/\/api\/projects(\?.*)?$/, route => route.fulfill({ json: { projects: [] } }));
  await page.route(/\/api\/folders(\?.*)?$/, route => route.fulfill({ json: { folders: [] } }));
  await page.route("**/api/missions**", route => route.fulfill({ json: { missions: [], total: 0, next_offset: null, facets: { projects: [], states: [] } } }));
  await page.route("**/api/pulse/notifications**", route => route.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }));
  await page.route("**/api/agents/usage", route => route.fulfill({ json: { budgets: { threshold_pct: 90, notify: true, engines: {} }, agents: [] } }));
  await page.route("**/api/plugins", route => route.fulfill({ json: data }));
  await page.route("**/api/agents/catalog/preferences", route => {
    writes.push("preferences"); data.feed.refresh!.automatic = route.request().postDataJSON().automatic;
    return route.fulfill({ json: data });
  });
  await page.route("**/api/plugins/feed/refresh", route => {
    writes.push("refresh");
    data.feed.refresh!.last_attempt = 1_800_000_000;
    data.feed.refresh!.error = "The update could not be verified or fetched. Previous catalog retained.";
    return route.fulfill({ status: 409, json: { detail: "GitHub unavailable" } });
  });
  return { data, writes };
}

for (const theme of ["dark", "light"] as const) {
  test(`bundled catalog, public provenance and persistent opt-out · ${theme}`, async ({ page }, info) => {
    const { writes } = await catalogFixture(page);
    await page.goto("/settings/agents");
    await page.evaluate(value => document.documentElement.dataset.theme = value, theme);
    const panel = page.getByRole("region", { name: "Public agent catalog" });
    await expect(panel).toContainText("Bundled with this release");
    await expect(panel.getByText("Not checked yet", { exact: true })).toHaveCount(2);
    await expect(panel.getByRole("link", { name: "View definitions" })).toHaveAttribute("href", /github.com/);
    const toggle = panel.getByRole("checkbox", { name: "Automatically check daily" });
    await toggle.click();
    await expect(toggle).not.toBeChecked();
    await page.reload();
    await page.evaluate(value => document.documentElement.dataset.theme = value, theme);
    await expect(toggle).not.toBeChecked();
    await panel.getByRole("button", { name: "Refresh catalog" }).click();
    await expect(panel).toContainText("Previous catalog retained");
    await expect(page.getByText("GitHub unavailable", { exact: false })).toBeVisible();
    await expect(panel.getByText("Not checked yet", { exact: true })).toHaveCount(1);
    await expect(page.locator('[data-plugin-id="sample-agent"]').getByRole("link", { name: "Set up agent" })).toBeVisible();
    await expect(page.locator('[data-plugin-id="sample-api"]')).toContainText("Included with BattleLab");
    await expect(page.locator('[data-plugin-id="sample-api"]').getByRole("link", { name: "Set up agent" })).toHaveCount(0);
    expect(writes).toEqual(["preferences", "refresh"]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    for (const control of await panel.locator("button, a, label").all()) {
      expect((await control.boundingBox())!.height).toBeGreaterThanOrEqual(44);
    }
    await panel.screenshot({ path: info.outputPath(`agent-catalog-panel-${theme}.png`) });
    await page.evaluate(() => document.querySelectorAll("*").forEach(element => { if (element.scrollTop) element.scrollTop = 0; }));
    await page.screenshot({ path: info.outputPath(`agent-catalog-${theme}.png`), fullPage: true });
  });
}

for (const state of ["expired", "corrupt"] as const) {
  test(`${state} evidence controls actions in cards and direct setup links`, async ({ page }, info) => {
    await catalogFixture(page, state);
    await page.goto("/settings/agents");
    await expect(page.getByRole("region", { name: "Public agent catalog" })).toContainText(state === "expired" ? "has expired" : "Trust records unavailable");
    const sample = page.locator('[data-plugin-id="sample-agent"]');
    await expect(sample).toBeVisible();
    await expect(sample.getByRole("link", { name: "Set up agent" })).toHaveCount(state === "expired" ? 1 : 0);
    await expect(page.locator('[data-plugin-id="removed-agent"]').getByRole("link", { name: "Set up agent" })).toHaveCount(0);
    await page.screenshot({ path: info.outputPath(`agent-catalog-${state}.png`), fullPage: true });
    await page.goto("/settings/agents/setup/new?plugin=removed-agent");
    await expect(page.getByRole("button", { name: "Review installation" })).toBeDisabled();
    await expect(page.getByRole("combobox", { name: "Agent" }).locator('option[value="sample-api"]')).toHaveJSProperty("disabled", true);
    await expect(page.getByText("This agent is not offered by the last accepted remote catalog.", { exact: true })).toBeVisible();
  });
}
