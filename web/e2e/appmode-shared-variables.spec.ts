import { expect, test, type Page } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { PY_STACK_AVAILABLE, startStack, type Stack } from "./appmode";
import { HOOK_TIMEOUT_MS } from "./harness";
import { TOUCH_FLOOR } from "./touchTarget";

/** Shared Library acceptance uses the real scoped/encrypted store in a temporary home. */
test.describe.configure({ mode: "serial" });
test.setTimeout(90_000);
test.skip(!PY_STACK_AVAILABLE, "needs the Python stack (uv)");
let stack: Stack;
test.beforeAll(async () => {
  test.setTimeout(HOOK_TIMEOUT_MS);
  stack = await startStack();
  const prefs = join(stack.home, ".config", "agent-sessions");
  mkdirSync(prefs, { recursive: true });
  writeFileSync(join(prefs, "prefs.json"), JSON.stringify({ onboarded: true, whats_new_seen: "0.20.0" }));
});
test.afterAll(async () => { await stack?.stop(); });

test("Variables has its own Library destination and old links redirect", async ({ page }) => {
  await page.goto(`http://127.0.0.1:${stack.appPort}/library/variables`);
  await expect(page.getByRole("heading", { name: "Variables & collections", exact: true })).toBeVisible();
  await page.goto(`http://127.0.0.1:${stack.appPort}/templates?tab=variables`);
  await expect(page).toHaveURL(/\/library\/variables$/);
  await expect(page.getByRole("heading", { name: "Variables & collections", exact: true })).toBeVisible();
});

async function library(page: Page) {
  const response = await page.request.get(`http://127.0.0.1:${stack.appPort}/api/variables`);
  expect(response.status()).toBe(200);
  expect(response.headers()["cache-control"]).toContain("no-store");
  return response.json();
}

test("definitions, write-only secrets and overlapping collections survive real saves", async ({ page }) => {
  await page.goto(`http://127.0.0.1:${stack.appPort}/library/variables`);
  await page.getByRole("button", { name: "New variable", exact: true }).click();
  await page.getByLabel("Name", { exact: true }).fill("attempts");
  await page.getByLabel("Value type", { exact: true }).selectOption("int");
  await page.getByLabel("Store a global value").uncheck();
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByRole("article", { name: "attempts" })).toContainText("No global value");
  let data = await library(page);
  const id = data.variables.find((v: { name: string }) => v.name === "attempts").id;
  expect(id).toBeTruthy();
  expect(data.variables[0]).toMatchObject({ set: false, definition: { value_type: "int" } });

  await page.getByRole("button", { name: "Edit attempts", exact: true }).click();
  await page.getByLabel("Store a global value").check();
  await page.getByLabel("Global value", { exact: true }).fill("4");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByRole("article", { name: "attempts" })).toContainText("4");
  expect((await library(page)).variables[0]).toMatchObject({ id, set: true, value: 4 });

  const secret = "test-only-library-private-value";
  await page.getByRole("button", { name: "New secret", exact: true }).click();
  await page.getByLabel("Name", { exact: true }).fill("token");
  await page.getByLabel("Global value", { exact: true }).fill(secret);
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByRole("article", { name: "token" })).toContainText("Stored · hidden");
  await page.getByRole("button", { name: "Edit token", exact: true }).click();
  await expect(page.getByLabel("Replacement secret")).toHaveValue("");
  await page.getByLabel("Label", { exact: true }).fill("Service token");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByRole("article", { name: "token" })).toContainText("Service token");
  data = await library(page);
  const token = data.variables.find((v: { name: string }) => v.name === "token");
  expect(token).toMatchObject({ kind: "secret", set: true });
  expect(token).not.toHaveProperty("value");
  expect(token).not.toHaveProperty("envelope");
  expect(JSON.stringify(data)).not.toContain(secret);
  expect(await page.locator("body").textContent()).not.toContain(secret);
  expect(await page.evaluate(() => JSON.stringify([localStorage, sessionStorage, history.state]))).not.toContain(secret);

  for (const name of ["Forgejo", "Research"]) {
    await page.getByRole("button", { name: "New collection", exact: true }).click();
    await page.getByLabel("Collection name").fill(name);
    await page.getByRole("checkbox", { name: "attempts", exact: true }).check();
    await page.getByRole("checkbox", { name: "token · secret", exact: true }).check();
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(page.getByRole("heading", { name, exact: true })).toBeVisible();
  }
  data = await library(page);
  expect(data.collections).toHaveLength(2);
  expect(data.collections[0].variable_ids).toEqual(data.collections[1].variable_ids);
  const researchId = data.collections.find((c: { name: string }) => c.name === "Research").id;
  await page.getByRole("button", { name: "Edit collection", exact: true }).click();
  await page.getByLabel("Collection name").fill("Research renamed");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Research renamed", exact: true })).toBeVisible();
  expect((await library(page)).collections.find((c: { id: string }) => c.id === researchId).name).toBe("Research renamed");
  await page.getByRole("button", { name: "Edit collection", exact: true }).click();
  await page.getByRole("button", { name: "Delete collection", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText("stored values stay");
  await page.getByRole("dialog").getByRole("button", { name: "Delete collection", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  data = await library(page);
  expect(data.collections).toHaveLength(1);
  expect(data.variables).toHaveLength(2);
  expect(data.variables.find((v: { id: string }) => v.id === id).value).toBe(4);
  await page.reload();
  await expect(page.getByRole("article", { name: "token" })).toContainText("Stored · hidden");
});

test("unsaved secret input is protected on navigation and discarded on request", async ({ page }) => {
  await page.goto(`http://127.0.0.1:${stack.appPort}/library/variables`);
  await page.getByRole("button", { name: "New secret", exact: true }).click();
  await page.getByLabel("Name", { exact: true }).fill("unsaved_token");
  await page.getByLabel("Global value", { exact: true }).fill("unsaved-test-only-secret");
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Discard your changes?" })).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(page.getByLabel("Name", { exact: true })).toHaveValue("unsaved_token");
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await page.getByRole("button", { name: "Discard changes", exact: true }).click();
  await page.getByRole("button", { name: "New secret", exact: true }).click();
  await expect(page.getByLabel("Global value", { exact: true })).toHaveValue("");
  expect((await library(page)).variables.some((v: { name: string }) => v.name === "unsaved_token")).toBe(false);
});

test("a concurrent real edit refuses the stale save and keeps the draft", async ({ page }) => {
  const base = `http://127.0.0.1:${stack.appPort}`;
  await page.goto(`${base}/library/variables`);
  await page.getByRole("button", { name: "Edit attempts", exact: true }).click();
  const data = await library(page);
  const original = data.variables.find((v: { name: string }) => v.name === "attempts");
  const config = await (await page.request.get(`${base}/api/config`)).json();
  const otherTab = await page.request.patch(`${base}/api/variables/${original.id}`, {
    headers: { Origin: base, "X-CSRF-Token": config.csrf },
    data: { value: 7, expected_updated_at: original.updated_at },
  });
  expect(otherTab.status()).toBe(200);
  await page.getByLabel("Global value", { exact: true }).fill("9");
  const refusal = page.waitForResponse(r => r.url().endsWith(`/api/variables/${original.id}`) && r.request().method() === "PATCH");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  expect((await refusal).status()).toBe(409);
  await expect(page.getByLabel("Global value", { exact: true })).toHaveValue("9");
  expect((await library(page)).variables.find((v: { id: string }) => v.id === original.id).value).toBe(7);
  await page.getByRole("button", { name: "Discard draft & reload current", exact: true }).click();
  await expect(page.getByLabel("Global value", { exact: true })).toHaveValue("7");
});

test("the shared library remains usable in both themes at 1440, 390 and 320 pixels", async ({ page, isMobile }) => {
  test.skip(!!isMobile, "one explicit viewport matrix covers desktop and narrow widths");
  const base = `http://127.0.0.1:${stack.appPort}`;
  const config = await (await page.request.get(`${base}/api/config`)).json();
  for (const input of [
    { name: "repo_url", definition: { value_type: "url", label: "Repository URL" }, value: "https://git.example.test/team/project" },
    { name: "workspace", definition: { value_type: "path", label: "Project workspace" } },
    { name: "review_required", definition: { value_type: "bool" }, value: true },
    { name: "review_model", definition: { value_type: "enum", choices: ["standard", "advanced"] }, value: "standard" },
  ]) {
    const response = await page.request.post(`${base}/api/variables`, { headers: { Origin: base, "X-CSRF-Token": config.csrf }, data: { kind: "text", ...input } });
    expect(response.status()).toBe(201);
  }
  for (const theme of ["dark", "light"]) for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto(`${base}/library/variables`);
    await page.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
    await expect(page.getByRole("article", { name: "repo_url", exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    for (const button of await page.getByRole("main").getByRole("button").all()) {
      const box = await button.boundingBox();
      if (box) { expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR); expect(box.x + box.width).toBeLessThanOrEqual(width + 0.5); }
    }
    await page.screenshot({ path: `design-review/1390-variables-${theme}-${width}.png`, fullPage: true });
    const edit = page.getByRole("button", { name: "Edit token", exact: true });
    await edit.focus(); await edit.press("Enter");
    await expect(page.getByLabel("Replacement secret")).toHaveValue("");
    await page.getByRole("button", { name: "Save", exact: true }).scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await page.screenshot({ path: `design-review/1390-secret-${theme}-${width}.png`, fullPage: true });
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
  }
});

test("keyboard save, visible validation errors and Escape protect an unsaved draft", async ({ page }) => {
  await page.goto(`http://127.0.0.1:${stack.appPort}/library/variables`);
  await page.getByRole("button", { name: "New variable", exact: true }).click();
  await page.getByLabel("Name", { exact: true }).fill("9invalid");
  const value = page.getByLabel("Global value", { exact: true });
  await value.fill("first");
  await value.press("Control+Enter");
  const error = page.getByRole("alert");
  await expect(error).toContainText("starting with a letter");
  await expect(error).toBeFocused();
  await expect(error).toBeInViewport();
  await page.getByLabel("Name", { exact: true }).fill("keyboard_sample");
  await value.press("Control+Enter");
  await expect(page.getByRole("article", { name: "keyboard_sample", exact: true })).toContainText("first");
  await page.getByRole("button", { name: "Edit keyboard_sample", exact: true }).click();
  await value.fill("unsaved"); await value.press("Escape");
  await expect(page.getByRole("dialog", { name: "Discard your changes?" })).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(value).toHaveValue("unsaved");
  await value.press("Escape");
  await page.getByRole("button", { name: "Discard changes", exact: true }).click();
  await expect(page.getByRole("article", { name: "keyboard_sample", exact: true })).toContainText("first");
});

test("editing a secret label preserves its validation and stored value", async ({ page }) => {
  const base = `http://127.0.0.1:${stack.appPort}`;
  const config = await (await page.request.get(`${base}/api/config`)).json();
  const response = await page.request.post(`${base}/api/variables`, {
    headers: { Origin: base, "X-CSRF-Token": config.csrf },
    data: { name: "pattern_secret", kind: "secret", definition: { value_type: "text", pattern: "^[a-z]+$" }, value: "testonlypatternsecret" },
  });
  expect(response.status(), await response.text()).toBe(201);
  const original = await response.json();
  await page.goto(`${base}/library/variables`);
  await page.getByRole("button", { name: "Edit pattern_secret", exact: true }).click();
  await expect(page.getByLabel("Replacement secret")).toHaveValue("");
  await page.getByLabel("Label", { exact: true }).fill("Renamed secret");
  const saved = page.waitForResponse(r => r.url().endsWith(`/api/variables/${original.id}`) && r.request().method() === "PATCH");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  expect((await saved).status()).toBe(200);
  const updated = (await library(page)).variables.find((v: { id: string }) => v.id === original.id);
  expect(updated).toMatchObject({ kind: "secret", set: true, definition: { label: "Renamed secret", pattern: "^[a-z]+$" } });
  expect(updated).not.toHaveProperty("value");
});
