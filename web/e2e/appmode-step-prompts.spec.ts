import { expect, test, type Page } from "@playwright/test";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { PY_STACK_AVAILABLE, startStack, type Stack } from "./appmode";
import { HOOK_TIMEOUT_MS } from "./harness";

/** Retained prompts use the real authoring and bundle APIs, never an agent or live HOME. */
test.describe.configure({ mode: "serial" });
test.setTimeout(90_000);
test.skip(!PY_STACK_AVAILABLE, "needs the Python stack (uv)");
let stack: Stack;
test.beforeAll(async () => {
  test.setTimeout(HOOK_TIMEOUT_MS);
  stack = await startStack();
  const root = join(stack.home, ".config", "agent-sessions");
  mkdirSync(root, { recursive: true });
  writeFileSync(join(root, "prefs.json"), JSON.stringify({ onboarded: true, whats_new_seen: "0.20.0" }));
});
test.afterAll(async () => { await stack?.stop(); });

function seed(id: string) {
  const root = join(stack.home, ".config", "agent-sessions", "playbooks", id);
  mkdirSync(join(root, "flows"), { recursive: true });
  writeFileSync(join(root, "playbook.toml"), `format = 2
[identity]
id = "${id}"
name = "Direct prompt proof"
publisher = "Tests"
version = "1.0.0"
domain = "development"
[[variables]]
name = "repository"
kind = "text"
type = "text"
[flows]
default = "main"
`);
  writeFileSync(join(root, "flows", "main.toml"), `format = 2
title = "Delivery"
[[steps]]
id = "implement"
title = "Implement"
brief = "Read the project instructions first."
actor = { kind = "operator" }
[[steps.checklist]]
key = "reviewed"
title = "Independent approval"
probe = "supervisor_judged"
required = true
`);
}
async function stored(page: Page, id: string) {
  const response = await page.request.get(`http://127.0.0.1:${stack.appPort}/api/playbooks/${id}`);
  expect(response.status()).toBe(200);
  return response.json();
}
async function openStep(page: Page, id: string) {
  await page.goto(`http://127.0.0.1:${stack.appPort}/library/playbooks/${id}/edit`);
  const node = page.getByLabel("Step: Implement", { exact: true });
  await expect(node).toBeVisible();
  await node.focus();
  await node.press("Enter");
}
async function save(page: Page, id: string) {
  const pending = page.waitForResponse(r => r.url().endsWith(`/api/playbooks/${id}`) && r.request().method() === "PUT");
  await page.getByRole("button", { name: "Save playbook", exact: true }).click();
  const response = await pending;
  expect(response.status(), await response.text()).toBe(200);
  // Successful bundle adoption returns to workflow selection; reopen the saved step.
  await expect(page.getByText("Playbook saved. Existing projects keep their pinned revision.", { exact: true })).toBeVisible();
  const node = page.getByLabel("Step: Implement", { exact: true });
  await node.focus(); await node.press("Enter");
}

test("manual prompts attach directly, reorder and preserve completion evidence", async ({ page }) => {
  const id = "direct-manual";
  seed(id);
  await openStep(page, id);
  await page.getByRole("button", { name: "Add manual prompt", exact: true }).click();
  await page.getByLabel("Prompt 1 title", { exact: true }).fill("Implementation");
  await page.getByLabel("Prompt 1 instructions", { exact: true }).fill("Implement the agreed change in {{repository}}.");
  await page.getByRole("button", { name: "Add manual prompt", exact: true }).click();
  await page.getByLabel("Prompt 2 title", { exact: true }).fill("Verification");
  await page.getByLabel("Prompt 2 instructions", { exact: true }).fill("Run the relevant tests.");
  await page.getByRole("button", { name: "Move prompt 2 up", exact: true }).click();
  await expect(page.getByLabel("Prompt 1 title", { exact: true })).toHaveValue("Verification");
  await save(page, id);
  const data = await stored(page, id);
  expect(data.documents["playbook.toml"].format).toBe(4);
  const flow = data.documents["flows/main.toml"];
  expect(flow.format).toBe(4);
  expect(flow.steps[0].brief).toBe("Read the project instructions first.");
  expect(flow.steps[0].prompts.map((p: { title: string }) => p.title)).toEqual(["Verification", "Implementation"]);
  expect(flow.steps[0].checklist).toEqual([{ key: "reviewed", title: "Independent approval", probe: "supervisor_judged", required: true }]);
  await openStep(page, id);
  await expect(page.getByLabel("Prompt 2 instructions", { exact: true })).toHaveValue("Implement the agreed change in {{repository}}.");
  await page.getByRole("button", { name: "Remove prompt 1", exact: true }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Remove prompt", exact: true }).click();
  await expect(page.getByLabel("Prompt 1 title", { exact: true })).toHaveValue("Implementation");
});

function source(body = "Review {{repo}}.", fields = [{ name: "repo", label: "Repository", default: "", required: true, source: "template", kind: "text" }], updated_at = 1) {
  const template = { id: "review-template", name: "Review changes", description: "", tags: [], body, fields, images: [], created_at: 1, updated_at, used_count: 0, last_used_at: null };
  writeFileSync(join(stack.home, ".config", "agent-sessions", "templates.json"), JSON.stringify({ version: 3, templates: [template] }));
  return template;
}
async function importPrompt(page: Page) {
  await page.getByRole("button", { name: "Import template", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Template", { exact: true }).selectOption("review-template");
  await dialog.getByLabel("Input for Repository (required)").selectOption("repository");
  await dialog.getByRole("button", { name: "Copy into draft", exact: true }).click();
  await expect(dialog).toHaveCount(0);
}

test("template imports are independent copies; source adoption and deletion preserve evidence", async ({ page }) => {
  const id = "direct-template";
  seed(id); source();
  const effects: string[] = [];
  page.on("request", request => {
    if (request.method() === "POST" && /\/(send|probe|launch|start|used)$/.test(new URL(request.url()).pathname)) effects.push(request.url());
  });
  await openStep(page, id);
  const before = await stored(page, id);
  await importPrompt(page); await importPrompt(page);
  expect((await stored(page, id)).revision).toBe(before.revision);
  await page.getByLabel("Prompt 1 instructions", { exact: true }).fill("My customized review of {{repository}}.");
  await save(page, id);
  let step = (await stored(page, id)).documents["flows/main.toml"].steps[0];
  expect(step.prompts[0].id).not.toBe(step.prompts[1].id);
  expect(step.prompts[0].source.bindings).toEqual({ repo: "repository" });
  const retainedId = step.prompts[0].id;
  source("Review and test {{repo}}.", undefined, 2);
  await page.getByRole("button", { name: "Compare source for prompt 1", exact: true }).click();
  let dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("My customized review of {{repository}}.");
  await expect(dialog).toContainText("Review and test {{repository}}.");
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(page.getByLabel("Prompt 1 instructions", { exact: true })).toHaveValue("My customized review of {{repository}}.");
  await page.getByRole("button", { name: "Compare source for prompt 1", exact: true }).click();
  dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "Adopt source update", exact: true }).click();
  await expect(page.getByLabel("Prompt 1 instructions", { exact: true })).toHaveValue("Review and test {{repository}}.");
  await expect(page.getByLabel("Prompt 2 instructions", { exact: true })).toHaveValue("Review {{repository}}.");
  await save(page, id);
  step = (await stored(page, id)).documents["flows/main.toml"].steps[0];
  expect(step.prompts[0].id).toBe(retainedId);
  expect(step.checklist).toEqual(before.documents["flows/main.toml"].steps[0].checklist);
  const templatePath = join(stack.home, ".config", "agent-sessions", "templates.json");
  expect(JSON.parse(readFileSync(templatePath, "utf8")).templates[0].used_count).toBe(0);
  writeFileSync(templatePath, JSON.stringify({ version: 3, templates: [] }));
  await page.getByRole("button", { name: "Compare source for prompt 1", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText("source template was deleted");
  await page.getByRole("button", { name: "Keep workflow copy", exact: true }).click();
  await openStep(page, id);
  await expect(page.getByLabel("Prompt 1 instructions", { exact: true })).toHaveValue("Review and test {{repository}}.");
  expect(effects).toEqual([]);
});

test("stale imports refuse without changing the draft and secret fields cannot be selected", async ({ page }) => {
  const id = "direct-stale";
  seed(id); source();
  await openStep(page, id);
  await page.getByRole("button", { name: "Import template", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Template", { exact: true }).selectOption("review-template");
  await dialog.getByLabel("Input for Repository (required)").selectOption("repository");
  source("Changed {{repo}}.", undefined, 2);
  await dialog.getByRole("button", { name: "Copy into draft", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("source template changed");
  await expect(dialog.getByLabel("Input for Repository (required)")).toHaveValue("repository");
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(page.getByLabel("Prompt 1 title", { exact: true })).toHaveCount(0);
  source("Read {{repo}}.", [{ name: "repo", label: "Credential", default: "", required: true, source: "template", kind: "secret" }]);
  await page.getByRole("button", { name: "Import template", exact: true }).click();
  await expect(page.getByRole("dialog").locator('option[value="review-template"]')).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Step properties", exact: true })).toBeVisible();
});

test("changed source fields require explicit remapping and retain the local prompt identity", async ({ page }) => {
  const id = "direct-remap";
  seed(id); source(); await openStep(page, id); await importPrompt(page); await save(page, id);
  const prior = (await stored(page, id)).documents["flows/main.toml"].steps[0].prompts[0];
  source("Check {{target}}.", [{ name: "target", label: "Target", default: "", required: true, source: "template", kind: "text" }], 3);
  await page.getByRole("button", { name: "Compare source for prompt 1", exact: true }).click();
  await page.getByRole("button", { name: "Choose source and remap inputs", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Input for Target (required)", { exact: true }).selectOption("repository");
  await dialog.getByRole("button", { name: "Copy into draft", exact: true }).click();
  await expect(page.getByLabel("Prompt 1 instructions", { exact: true })).toHaveValue("Check {{repository}}.");
  await save(page, id);
  const result = (await stored(page, id)).documents["flows/main.toml"].steps[0].prompts[0];
  expect(result.id).toBe(prior.id);
  expect(result.source.bindings).toEqual({ target: "repository" });
});

test("invalid prompt input refuses the save, retains the draft and focuses its field", async ({ page }) => {
  const id = "direct-invalid";
  seed(id); await openStep(page, id);
  const before = await stored(page, id);
  await page.getByRole("button", { name: "Add manual prompt", exact: true }).click();
  const body = page.getByLabel("Prompt 1 instructions", { exact: true });
  await body.fill("Read {{missing_input}}.");
  const pending = page.waitForResponse(r => r.url().endsWith(`/api/playbooks/${id}`) && r.request().method() === "PUT");
  await page.getByRole("button", { name: "Save playbook", exact: true }).click();
  expect((await pending).status()).toBe(422);
  await expect(page.getByRole("alert")).toContainText("prompts[0].body");
  await page.getByRole("button", { name: /^Go to field:/ }).click();
  await expect(body).toBeFocused();
  await expect(body).toHaveValue("Read {{missing_input}}.");
  expect((await stored(page, id)).revision).toBe(before.revision);
  await body.fill("Read {{repository}}.");
  await save(page, id);
});

test("prompt editing and source comparison fit 1440, 390 and 320px in both themes", async ({ page, isMobile }) => {
  test.skip(isMobile, "explicit viewport matrix runs once");
  mkdirSync("design-review", { recursive: true });
  for (const theme of ["dark", "light"] as const) {
    for (const width of [1440, 390, 320]) {
      const id = `direct-visual-${theme}-${width}`;
      seed(id); source();
      await page.setViewportSize({ width, height: 1000 });
      await openStep(page, id);
      await page.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
      await importPrompt(page);
      await page.getByLabel("Prompt 1 instructions", { exact: true }).fill("Review {{repository}} and preserve the independent approval requirement.");
      const prompt = page.getByRole("region", { name: "Prompt 1", exact: true });
      await prompt.scrollIntoViewIfNeeded();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      for (const button of await prompt.getByRole("button").all()) {
        if (await button.isEnabled()) expect((await button.boundingBox())!.height).toBeGreaterThanOrEqual(44);
      }
      await page.screenshot({ path: `design-review/1391-prompts-${theme}-${width}.png` });
      source("Review and test {{repo}}.", undefined, 2);
      await page.getByRole("button", { name: "Compare source for prompt 1", exact: true }).click();
      const dialog = page.getByRole("dialog");
      await expect(dialog).toContainText("Review and test {{repository}}.");
      expect(await dialog.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
      const box = (await dialog.boundingBox())!;
      expect(box.x).toBeGreaterThanOrEqual(0); expect(box.x + box.width).toBeLessThanOrEqual(width);
      await page.screenshot({ path: `design-review/1391-source-${theme}-${width}.png` });
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0);
      await expect(page.getByRole("button", { name: "Compare source for prompt 1", exact: true })).toBeFocused();
      await save(page, id);
    }
  }
});
