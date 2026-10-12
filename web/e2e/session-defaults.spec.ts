import { expect, type Page, test } from "@playwright/test";
import { ROSTER } from "./roster";

// API new-session defaults (#1407) in a real browser: save a model + level on the opencode — API
// agent page, then the new-session screen preselects them and the create carries them. What
// breaks here is the wiring across two routes and the config refresh between them, plus layout
// (44px targets, no overflow on a phone) — none of which jsdom can show.

const ENGINE = "opencode-api";
const ID = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
const MODELS = {
  status: "ok",
  reason: null,
  models: [
    { id: "local/coder", label: "Coder", description: null, efforts: ["low", "high"], is_default: false },
    { id: "openai/plain", label: "Plain", description: null, efforts: [], is_default: false },
  ],
};

async function mockApp(page: Page) {
  const engines = ROSTER.engines.map((e) =>
    e.runtime === "api" ? { ...e, present: true, supports_new: true } : e,
  );
  const ids = engines.map((e) => e.id);
  const state = {
    sessionDefaults: {} as Record<string, unknown>,
    prefsPosts: [] as unknown[],
    creates: [] as Record<string, unknown>[],
  };
  const source = engines.find((e) => e.id === ENGINE);
  const detail = {
    id: ENGINE,
    label: source?.label ?? ENGINE,
    publisher: "battlelab",
    version: "1",
    contract: 1,
    source: "in-tree",
    kind: "agent",
    runtime: "api",
    status: "active",
    binary: null,
    provenance: { state: "absent", via: null, path: null, note: null },
    store: { root: "~/.local/share/x", resolved: "/home/operator/.local/share/x", layout: "battlelab-chat", read_only: false },
    launch: null,
    transcript: { kind: "battlelab-chat", strict: false },
    usage: { source: "tokens", kind: "none" },
    capabilities: source?.capabilities ?? {},
    models: [],
    maintenance: [],
  };
  await page.route("**/api/**", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, json: body });
    if (path === "/api/config") {
      return json({
        csrf: "x",
        new_session_engines: ids,
        terminal_backend: "ws",
        auth_mode: "none",
        default_project: "/d",
        agent_defaults: { default_engine: ENGINE, bypass: false, session_defaults: state.sessionDefaults },
      });
    }
    if (path === "/api/prefs" && req.method() === "POST") {
      const body = req.postDataJSON();
      state.prefsPosts.push(body);
      for (const [k, v] of Object.entries(body.agent_defaults?.session_defaults ?? {})) {
        if (v) state.sessionDefaults[k] = v;
        else delete state.sessionDefaults[k];
      }
      return json({ agent_defaults: { default_engine: ENGINE, bypass: false, session_defaults: state.sessionDefaults } });
    }
    if (path === "/api/engines") return json({ engines, problems: [] });
    if (path === `/api/engines/${ENGINE}`) return json(detail);
    if (path === `/api/structured/clients/${ENGINE}/models`) return json(MODELS);
    if (path === "/api/structured/sessions" && req.method() === "POST") {
      state.creates.push(req.postDataJSON());
      return json({ session_key: `${ENGINE}:${ID}` }, 201);
    }
    if (path === "/api/version") return json({ version: "test" });
    if (path === "/api/pulse/notifications") return json({ notifications: [], unread: 0, uncertain: 0, settled: [] });
    if (path.startsWith("/api/missions")) return json({ missions: [], total: 0, next_offset: null, facets: { projects: [], states: [] } });
    if (path.startsWith("/api/sessions")) return json({ sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } });
    if (path.startsWith("/api/projects")) return json({ projects: [] });
    if (path.startsWith("/api/folders")) return json({ folders: [] });
    if (path === "/api/agents/usage") return json({ budgets: { threshold_pct: 90, notify: true, engines: {} }, agents: [] });
    if (path === "/api/plugins") {
      return json({ feed: { state: "ready", error: null }, catalog: [], plugins: [], operations: [], roster_generation: 1, roster_revision: null });
    }
    return json({});
  });
  return state;
}

for (const theme of ["dark", "light"] as const) {
  test(`saved defaults preselect the new-session screen and reach the create · ${theme}`, async ({ page }) => {
    const state = await mockApp(page);
    await page.goto(`/settings/agents/${ENGINE}`);
    await page.evaluate((t) => (document.documentElement.dataset.theme = t), theme);

    const section = page.getByTestId("session-defaults");
    const model = section.getByTestId("session-defaults-model");
    const effort = section.getByTestId("session-defaults-effort");
    await expect(effort).toBeDisabled(); // a level needs a model
    await model.selectOption("local/coder");
    await effort.selectOption("high");
    const save = section.getByRole("button", { name: "Save defaults" });
    for (const target of [model, effort, save]) {
      const box = (await target.boundingBox())!;
      expect(box.height, "44px target (design.md §8)").toBeGreaterThanOrEqual(44);
    }
    await save.click();
    await expect(section.getByTestId("session-defaults-status")).toContainText("Saved");
    expect(state.prefsPosts).toEqual([
      { agent_defaults: { session_defaults: { [ENGINE]: { model: "local/coder", effort: "high" } } } },
    ]);
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - window.innerWidth,
    );
    expect(overflow, "no sideways scroll").toBeLessThanOrEqual(1);

    // The new-session screen preselects them — reached INSIDE the SPA (no reload), so it is the
    // refreshed config the app holds that carries them (#1407 PR review), not a fresh page load.
    await page.evaluate(() => {
      window.history.pushState({}, "", "/");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByRole("combobox", { name: "Model" })).toHaveValue("local/coder");
    await expect(page.getByTestId("new-session-effort")).toHaveValue("high");
    await expect(page.getByTestId("new-session-defaults")).toContainText("defaults");
    await page.getByRole("button", { name: /start session/i }).click();
    await expect.poll(() => state.creates.length).toBe(1);
    expect(state.creates[0]).toMatchObject({ engine: ENGINE, model: "local/coder", effort: "high" });
  });
}

test("a level changed for one session is sent, and Reset to defaults restores the saved one", async ({ page }) => {
  const state = await mockApp(page);
  state.sessionDefaults[ENGINE] = { model: "local/coder", effort: "high" };
  await page.goto("/");
  const effort = page.getByTestId("new-session-effort");
  await expect(effort).toHaveValue("high");
  await effort.selectOption("low");
  const reset = page.getByRole("button", { name: "Reset to defaults" });
  expect((await reset.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  await reset.click();
  await expect(effort).toHaveValue("high");
  await effort.selectOption("low");
  await page.getByRole("button", { name: /start session/i }).click();
  await expect.poll(() => state.creates.length).toBe(1);
  expect(state.creates[0]).toMatchObject({ model: "local/coder", effort: "low" });
});
