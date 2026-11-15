import { expect, test, type Page } from "@playwright/test";
import { mockShell } from "./callFixture";
import { TOUCH_FLOOR } from "./touchTarget";

async function setup(page: Page) {
  const calls = await mockShell(page);
  const config = {
    csrf: "csrf-x", auth_mode: "none", terminal_backend: "ws", new_session_engines: [],
    onboarded: true, hq_contact: "off", ai_review: { model: "endpoint-model", thinking: "medium" },
    hq_route: { enabled: true, model: "", thinking: "off" },
    hq_models: { typed: { model: "", thinking: "hq" }, mission: { model: "", thinking: "hq" } },
  };
  const writes: unknown[] = [];
  const state = { catalog: true, save: true };
  await page.route("**/api/config", (r) => r.fulfill({ json: config }));
  await page.route("**/api/ai-review/models**", (r) => state.catalog
    ? r.fulfill({ json: { models: ["endpoint-model", "fast-model"] } })
    : r.fulfill({ status: 503, json: { detail: "catalog unavailable" } }));
  await page.route("**/api/prefs", async (r) => {
    const body = r.request().postDataJSON();
    writes.push(body);
    if (!state.save) return r.fulfill({ status: 503, json: { detail: "save refused" } });
    if (body.hq_models) {
      for (const key of ["typed", "mission"] as const) {
        config.hq_models[key] = { ...config.hq_models[key], ...body.hq_models[key] };
      }
    }
    if (body.hq_route) config.hq_route = { ...config.hq_route, ...body.hq_route };
    return r.fulfill({ json: { hq_models: config.hq_models, hq_route: config.hq_route } });
  });
  return { calls, config, writes, state };
}

for (const theme of ["dark", "light"]) {
  test(`HQ models: four independent rows, persistence and inheritance · ${theme}`, async ({ page }, info) => {
    const s = await setup(page);
    await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
    await page.goto("/settings/ai-hq#hq-models-h");
    const group = page.getByRole("region", { name: "HQ models", exact: true });
    await expect(group).toBeVisible();
    for (const row of ["Typed turns", "Question routing", "Calls", "Mission conversations"]) {
      const fields = group.getByRole("group", { name: row, exact: true });
      await expect(fields.getByRole("combobox")).toHaveCount(2);
      if (info.project.name === "mobile") {
        for (const field of await fields.getByRole("combobox").all()) {
          expect((await field.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
        }
      }
    }
    await group.getByLabel("Model for typed turns", { exact: true }).selectOption("fast-model");
    await expect.poll(() => s.config.hq_models.typed.model).toBe("fast-model");
    await group.getByLabel("Thinking for question routing", { exact: true }).selectOption("hq");
    await expect.poll(() => s.config.hq_route.thinking).toBe("hq");
    await group.getByLabel("Thinking for calls", { exact: true }).selectOption("auto");
    await expect.poll(() => s.calls.settings.thinking).toBe("auto");
    await group.getByLabel("Model for mission conversations", { exact: true }).selectOption("endpoint-model");
    await expect.poll(() => s.config.hq_models.mission.model).toBe("endpoint-model");
    await page.reload();
    await expect(group.getByLabel("Model for typed turns", { exact: true })).toHaveValue("fast-model");
    await expect(group.getByLabel("Thinking for calls", { exact: true })).toHaveValue("auto");
    await expect(group.getByRole("group", { name: "Question routing", exact: true })).toContainText("enclosing call");
    await expect(group.getByRole("group", { name: "Mission conversations", exact: true })).toContainText("Own choice");
    expect(s.writes).toEqual([
      { hq_models: { typed: { model: "fast-model" } } },
      { hq_route: { thinking: "hq" } },
      { hq_models: { mission: { model: "endpoint-model" } } },
    ]);
    expect(s.calls.saves).toEqual([{ thinking: "auto" }]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
    await group.screenshot({ path: info.outputPath(`hq-models-${theme}.png`) });
  });
}

test("HQ models preserves confirmed choices across catalog and save failures", async ({ page }) => {
  const s = await setup(page);
  s.config.hq_models.typed.model = "saved-unlisted";
  s.state.catalog = false;
  await page.goto("/settings/ai-hq#hq-models-h");
  const group = page.getByRole("region", { name: "HQ models", exact: true });
  const typed = group.getByLabel("Model for typed turns", { exact: true });
  await expect(typed).toHaveValue("saved-unlisted");
  await expect(group).toContainText("Model list unavailable");
  s.state.catalog = true;
  await group.getByRole("button", { name: "Reload model list" }).click();
  await expect(typed.getByRole("option", { name: "fast-model", exact: true })).toBeAttached();
  s.state.save = false;
  await typed.selectOption("fast-model");
  await expect(group.getByRole("alert")).toContainText("save refused");
  await expect(typed).toHaveValue("saved-unlisted");
  await page.reload();
  await expect(typed).toHaveValue("saved-unlisted");
});
