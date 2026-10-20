import { expect, test, type Page } from "@playwright/test";
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { PY_STACK_AVAILABLE, startStack, type Stack } from "./appmode";
import { HOOK_TIMEOUT_MS } from "./harness";

/** Real review/create/bind/apply/verify in a throwaway HOME. No workflow is started. */
test.describe.configure({ mode: "serial" });
test.setTimeout(90_000);
test.skip(!PY_STACK_AVAILABLE, "needs the Python stack (uv)");
let stack: Stack;
test.beforeAll(async () => {
  test.setTimeout(HOOK_TIMEOUT_MS);
  stack = await startStack();
  const config = join(stack.home, ".config", "agent-sessions");
  mkdirSync(config, { recursive: true });
  writeFileSync(join(config, "prefs.json"), JSON.stringify({ onboarded: true, whats_new_seen: "0.20.0", project_roots: [stack.home] }));
});
test.afterAll(async () => { await stack?.stop(); });
function seed(id: string) {
  const root = join(stack.home, ".config", "agent-sessions", "playbooks", id);
  mkdirSync(join(root, "template"), { recursive: true });
  mkdirSync(join(root, "flows"), { recursive: true });
  writeFileSync(join(root, "playbook.toml"), `format = 2
verify = ["materials", "variables"]
[identity]
id = "${id}"
name = "Project setup ${id}"
publisher = "Tests"
version = "1.0.0"
domain = "operations"
[[variables]]
name = "endpoint"
type = "url"
default = "https://example.test/health"
[[variables]]
name = "token"
kind = "secret"
[[connections]]
name = "service"
kind = "http-endpoint"
url = "{{endpoint}}"
verify = "status"
[[materials]]
path = "RULES.md"
disposition = "managed"
template = true
[flows]
default = "main"
`);
  writeFileSync(join(root, "template", "RULES.md"), "Read {{endpoint}} before working.\n");
  writeFileSync(join(root, "flows", "main.toml"), `format = 2
title = "Project setup"
[[steps]]
id = "review"
title = "Review the result"
actor = { kind = "operator" }
[[steps.checklist]]
key = "approved"
title = "Independent approval"
probe = "supervisor_judged"
required = true
`);
  return root;
}
const next = (page: Page) => page.getByRole("button", { name: "Next", exact: true }).click();
async function gallery(page: Page, id: string, name: string) {
  await page.goto(`http://127.0.0.1:${stack.appPort}/projects/new`);
  await page.getByLabel("Project name").fill(name);
  await next(page); await next(page); await next(page);
  await expect(page.getByRole("heading", { name: "Choose a playbook", exact: true })).toBeVisible();
  await page.getByLabel("Search playbooks").fill(id);
  await page.getByTestId(`playbook-card-${id}`).getByRole("radio").check();
  await page.getByRole("checkbox", { name: /unsigned local playbook/ }).check();
}
async function review(page: Page, id: string, name: string, secret = "fictional-input-for-verification") {
  await gallery(page, id, name);
  await next(page);
  await expect(page.getByRole("heading", { name: /^Set up / })).toBeVisible();
  if (secret) {
    await page.getByRole("radiogroup", { name: "Source for token" }).getByRole("radio", { name: "Value", exact: true }).check();
    await page.getByLabel("Value for token", { exact: true }).fill(secret);
  }
  const response = page.waitForResponse((r) => r.url().endsWith(`/api/playbooks/${id}/review`) && r.request().method() === "POST");
  await next(page);
  const result = await response; expect(result.status(), await result.text()).toBe(200);
  await expect(page.getByRole("heading", { name: "Resolved targets" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Create project and apply", exact: true })).toBeDisabled();
  await page.getByRole("checkbox", { name: /Confirm this target/ }).check();
}
async function create(page: Page) {
  const posted = page.waitForResponse((r) => /\/api\/projects$/.test(r.url()) && r.request().method() === "POST");
  await page.getByRole("button", { name: "Create project and apply", exact: true }).click();
  const response = await posted; expect(response.status(), await response.text()).toBe(200);
  return (await response.json()) as { id: string; default_folder: string };
}
async function verified(page: Page) {
  await expect(page.getByRole("heading", { name: "Verification: passed", exact: true })).toBeVisible();
}
for (const theme of ["dark", "light"]) test(`new folder follows reviewed create → bind → apply → verify (${theme})`, async ({ page }) => {
  const id = `journey-${theme}`; seed(id);
  const effects: string[] = [];
  page.on("request", (request) => { if (request.method() === "POST") effects.push(new URL(request.url()).pathname); });
  const secret = "fictional-wizard-secret";
  await page.addInitScript((value) => localStorage.setItem("theme", value), theme);
  await review(page, id, id, secret);
  expect(existsSync(join(stack.home, id))).toBe(false);
  expect(effects.some((p) => /\/(bind|apply|create-target)$/.test(p))).toBe(false);
  expect(await page.locator("body").innerText()).not.toContain(secret);
  const created = await create(page); await verified(page);
  expect(readFileSync(join(created.default_folder, "RULES.md"), "utf8")).toContain("https://example.test/health");
  const ordered = effects.filter((p) => /\/(bind|apply|create-target)$/.test(p) || p === "/api/projects");
  expect(ordered).toEqual([`/api/playbooks/${id}/review/create-target`, "/api/projects", `/api/projects/${created.id}/playbook/bind`, `/api/projects/${created.id}/playbook/apply`]);
  expect(effects.some((p) => /\/sessions\/new|\/missions\/.*\/start|\/probe/.test(p))).toBe(false);
  const storage = await page.evaluate(() => JSON.stringify([localStorage, sessionStorage, history.state, location.href]));
  expect(storage).not.toContain(secret);
  if (theme === "light") {
    await page.getByRole("button", { name: "Review removal of this playbook", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Review removal", exact: true });
    await expect(dialog).toContainText("RULES.md");
    await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
    expect(existsSync(join(created.default_folder, "RULES.md"))).toBe(true);
    await page.getByRole("button", { name: "Review removal of this playbook", exact: true }).click();
    await page.getByRole("button", { name: "Remove reviewed playbook", exact: true }).click();
    await expect(page.getByText("Deployment: removed.", { exact: true })).toBeVisible();
    expect(existsSync(created.default_folder)).toBe(true);
    expect(existsSync(join(created.default_folder, "RULES.md"))).toBe(false);
    const entity = await page.request.get(`http://127.0.0.1:${stack.appPort}/api/projects`);
    expect((await entity.json()).projects.some((p: { id: string }) => p.id === created.id)).toBe(true);
  }
});
for (const changed of [false, true]) test(`project-create refusal retains the created folder across review refresh (source changed=${changed})`, async ({ page }) => {
  const id = `retry-entity-${changed}`; const source = seed(id); let projects = 0;
  if (test.info().project.name === "mobile") await page.setViewportSize({ width: 320, height: 1000 });
  await page.route("**/api/projects", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    if (++projects === 1) return route.fulfill({ status: 422, json: { detail: "Project name is invalid" } });
    return route.continue();
  });
  await review(page, id, id);
  await page.getByRole("button", { name: "Create project and apply", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Project name is invalid" })).toBeVisible();
  expect(existsSync(join(stack.home, id))).toBe(true);
  expect(existsSync(join(stack.home, id, "RULES.md"))).toBe(false);
  if (changed) {
    const path = join(source, "playbook.toml");
    writeFileSync(path, readFileSync(path, "utf8").replace('version = "1.0.0"', 'version = "1.0.1"'));
  }
  await page.getByRole("button", { name: "Refresh review", exact: true }).click();
  await expect(page.getByRole("checkbox", { name: /Confirm this target/ })).toBeVisible();
  await expect(page.getByRole("checkbox", { name: /Confirm this target/ })).not.toBeChecked();
  await page.getByRole("checkbox", { name: /Confirm this target/ }).check();
  await expect(page.getByRole("status").filter({ hasText: "This wizard already created the folder" })).toBeVisible();
  mkdirSync("design-review", { recursive: true });
  await page.evaluate((theme) => { document.documentElement.dataset.theme = theme; }, changed ? "light" : "dark");
  await page.getByRole("status").filter({ hasText: "This wizard already created the folder" }).evaluate((el) => el.scrollIntoView({ block: "start" }));
  await page.screenshot({ path: `design-review/1187-recovery-${changed}-${test.info().project.name}.png` });
  await create(page); await verified(page);
  expect(projects).toBe(2);
});
for (const replaced of [false, true]) test(`a folder refusal after CREATE lets another folder be reviewed and created (replaced=${replaced})`, async ({ page }) => {
  const id = `move-folder-${replaced}`; seed(id); let projects = 0;
  if (test.info().project.name === "mobile") await page.setViewportSize({ width: 320, height: 1000 });
  await page.route("**/api/projects", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    if (++projects === 1) return route.fulfill({ status: 409, json: { detail: "That folder belongs to another project" } });
    return route.continue();
  });
  await review(page, id, id);
  await page.getByRole("button", { name: "Create project and apply", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "belongs to another project" })).toBeVisible();
  const first = join(stack.home, id);
  expect(existsSync(first)).toBe(true);
  if (replaced) {
    renameSync(first, `${first}-moved`); mkdirSync(first);
    await page.getByRole("button", { name: "Refresh review", exact: true }).click();
    await expect(page.getByText(/created folder was replaced/)).toBeVisible();
  }
  await page.getByRole("button", { name: "Change the folder", exact: true }).click();
  await page.getByLabel("Folder name").fill(`${id}-b`);
  await next(page); await next(page); await next(page);
  const reviewed = page.waitForResponse((r) => r.url().endsWith(`/api/playbooks/${id}/review`) && r.request().method() === "POST");
  await next(page);
  const response = await reviewed; expect(response.status(), await response.text()).toBe(200);
  expect(response.request().postDataJSON()).toMatchObject({ destination: join(stack.home, `${id}-b`), create: true });
  await expect(page.getByText(/not been confirmed/)).toHaveCount(0);
  await page.getByRole("checkbox", { name: /Confirm this target/ }).check();
  const created = await create(page); await verified(page);
  expect(created.default_folder).toBe(join(stack.home, `${id}-b`));
  expect(readFileSync(join(created.default_folder, "RULES.md"), "utf8")).toContain("https://example.test/health");
  expect(existsSync(join(first, "RULES.md"))).toBe(false);
  expect(projects).toBe(2);
});
test("failed bind retries the same project ID; no second project is created", async ({ page }) => {
  const id = "retry-binding"; seed(id);
  let projects = 0; const binds: string[] = [];
  page.on("request", (r) => { if (r.method() === "POST" && /\/api\/projects$/.test(r.url())) projects++; });
  await page.route("**/api/projects/*/playbook/bind", async (route) => {
    binds.push(route.request().postData()!);
    if (binds.length === 1) return route.fulfill({ status: 503, json: { detail: "Binding temporarily unavailable" } });
    return route.continue();
  });
  await review(page, id, id);
  const created = await create(page);
  await expect(page.getByRole("alert")).toContainText("Binding temporarily unavailable");
  expect(existsSync(join(created.default_folder, "RULES.md"))).toBe(false);
  await page.getByRole("button", { name: "Retry binding", exact: true }).click(); await verified(page);
  expect(projects).toBe(1); expect(binds[1]).toBe(binds[0]);
});
test("a lost apply response is reconciled and verification failure never says absent", async ({ page }) => {
  const id = "retry-applied"; seed(id);
  const applications: string[] = []; let verifies = 0; let projects = 0;
  page.on("request", (r) => { if (r.method() === "POST" && /\/api\/projects$/.test(r.url())) projects++; });
  await page.route("**/api/projects/*/playbook/apply", async (route) => {
    applications.push(route.request().postData()!);
    if (applications.length === 1) { await route.fetch(); return route.abort("failed"); }
    return route.continue();
  });
  await page.route("**/api/projects/*/playbook/verify", (route) => ++verifies === 1 ? route.fulfill({ status: 503, json: { detail: "Verification unavailable" } }) : route.continue());
  await review(page, id, id); const created = await create(page);
  await expect(page.getByText("Deployment: applied.", { exact: true })).toBeVisible();
  expect(existsSync(join(created.default_folder, "RULES.md"))).toBe(true);
  await page.getByRole("button", { name: "Re-verify", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Verification unavailable");
  await page.getByRole("button", { name: "Re-verify", exact: true }).click(); await verified(page);
  expect(projects).toBe(1); expect(applications.length).toBe(2); expect(applications[1]).toBe(applications[0]);
});
for (const occupied of [false, true]) test(`a folder appearing after review refuses create (populated=${occupied})`, async ({ page }) => {
  const id = `race-${occupied ? "populated" : "empty"}`; seed(id); let projects = 0;
  page.on("request", (r) => { if (r.method() === "POST" && /\/api\/projects$/.test(r.url())) projects++; });
  await review(page, id, id);
  const destination = join(stack.home, id); mkdirSync(destination);
  if (occupied) writeFileSync(join(destination, "RULES.md"), "Foreign file, keep me");
  await page.getByRole("button", { name: "Create project and apply", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText(/already exists|changed/);
  expect(projects).toBe(0);
  if (occupied) expect(readFileSync(join(destination, "RULES.md"), "utf8")).toBe("Foreign file, keep me");
});
test("a replacement after CREATE refuses APPLY and keeps the existing project", async ({ page }) => {
  const id = "race-replacement"; seed(id); let projects = 0;
  page.on("request", (r) => { if (r.method() === "POST" && /\/api\/projects$/.test(r.url())) projects++; });
  await page.route("**/api/projects/*/playbook/apply", async (route) => {
    const destination = join(stack.home, id); renameSync(destination, `${destination}-original`); mkdirSync(destination);
    writeFileSync(join(destination, "RULES.md"), "Replacement stays untouched");
    await route.continue();
  });
  await review(page, id, id); await create(page);
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Project created", exact: true })).toBeVisible();
  expect(projects).toBe(1);
  expect(readFileSync(join(stack.home, id, "RULES.md"), "utf8")).toBe("Replacement stays untouched");
});
test("a source update invalidates review before effects and can be explicitly re-reviewed", async ({ page }) => {
  const id = "stale-review"; const source = seed(id); let projects = 0;
  page.on("request", (r) => { if (r.method() === "POST" && /\/api\/projects$/.test(r.url())) projects++; });
  await review(page, id, id);
  const path = join(source, "playbook.toml"); writeFileSync(path, readFileSync(path, "utf8").replace('version = "1.0.0"', 'version = "1.0.1"'));
  await page.getByRole("button", { name: "Create project and apply", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText(/changed|revision/); expect(projects).toBe(0);
  await page.getByRole("button", { name: "Refresh review", exact: true }).click();
  await expect(page.getByRole("checkbox", { name: /Confirm this target/ })).not.toBeChecked();
  await page.getByRole("checkbox", { name: /Confirm this target/ }).check();
  await create(page); await verified(page); expect(projects).toBe(1);
});

test("a missing configured default is explained and No playbook still creates normally", async ({ page }) => {
  await page.route("**/api/playbooks", async (route) => {
    const response = await route.fetch(); const data = await response.json();
    await route.fulfill({ response, json: { ...data, default: "missing-configured-default" } });
  });
  await page.goto(`http://127.0.0.1:${stack.appPort}/projects/new`);
  await page.getByLabel("Project name").fill("without-playbook");
  await next(page); await next(page); await next(page);
  await expect(page.getByText(/configured default.*missing-configured-default/)).toBeVisible();
  await expect(page.getByRole("radio", { name: /^No playbook/ })).toBeChecked();
  await next(page);
  await page.getByRole("button", { name: "Create project", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Project created", exact: true })).toBeVisible();
  expect(existsSync(join(stack.home, "without-playbook", "RULES.md"))).toBe(false);
});

test("a secret-reference-file bundle explains its limitation without blocking No playbook", async ({ page }) => {
  const id = "needs-project-id"; const root = seed(id);
  const manifest = join(root, "playbook.toml"); writeFileSync(manifest, readFileSync(manifest, "utf8").replace("format = 2", "format = 3"));
  writeFileSync(join(root, "template", "RULES.md"), "Read the secret from {{secret_path:token}}.\n");
  await page.goto(`http://127.0.0.1:${stack.appPort}/projects/new`);
  await page.getByLabel("Project name").fill(id); await next(page); await next(page); await next(page);
  await page.getByTestId(`playbook-card-${id}`).getByRole("radio").check();
  await expect(page.getByRole("alert")).toContainText("needs an existing project before review");
  await expect(page.getByRole("button", { name: "Next", exact: true })).toBeDisabled();
  await page.getByRole("radio", { name: /^No playbook/ }).check();
  await next(page); await page.getByRole("button", { name: "Create project", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Project created", exact: true })).toBeVisible();
});

test("an absent agent and unavailable requested model stay visibly unresolved", async ({ page }) => {
  const id = "unresolved-assignment"; const root = seed(id);
  const path = join(root, "flows", "main.toml"); writeFileSync(path, readFileSync(path, "utf8").replace('actor = { kind = "operator" }', 'actor = { kind = "agent", engine = "absent-agent", model = "future-model" }'));
  await gallery(page, id, id); await next(page);
  await expect(page.getByLabel("Agent for Review the result", { exact: true })).toHaveValue("absent-agent");
  await expect(page.getByLabel("Model for Review the result", { exact: true })).toHaveValue("future-model");
  await expect(page.getByText(/This assignment is unresolved/)).toBeVisible();
  await next(page);
  await expect(page.getByText(/main:review: Unassigned/)).toBeVisible();
  const displayed = await page.locator("body").innerText();
  expect(displayed).toContain("agent unavailable");
});

test("gallery, inputs, review and result fit both themes at 1440, 390 and 320px", async ({ page, isMobile }) => {
  test.skip(isMobile, "explicit viewport matrix runs once");
  test.setTimeout(180_000);
  mkdirSync("design-review", { recursive: true });
  async function capture(state: string, theme: string, width: number) {
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const short = await page.locator("main").evaluate((root) => Array.from(root.querySelectorAll<HTMLButtonElement>("button"))
      .filter((el) => !el.disabled && el.getClientRects().length)
      .map((el) => ({ text: el.textContent?.trim(), height: el.getBoundingClientRect().height, right: el.getBoundingClientRect().right }))
      .filter((rect) => rect.height < 44 || rect.right > innerWidth + .5));
    expect(short).toEqual([]);
    await page.screenshot({ path: `design-review/1187-${state}-${theme}-${width}.png` });
  }
  for (const theme of ["dark", "light"]) for (const width of [1440, 390, 320]) {
    const id = `visual-${theme}-${width}`; seed(id);
    await page.setViewportSize({ width, height: 1000 });
    await gallery(page, id, id);
    await page.evaluate((theme) => { document.documentElement.dataset.theme = theme; }, theme);
    await page.getByRole("heading", { name: "Choose a playbook", exact: true }).scrollIntoViewIfNeeded();
    await capture("gallery", theme, width);
    await next(page);
    await page.getByRole("radiogroup", { name: "Source for token" }).getByRole("radio", { name: "Value", exact: true }).check();
    await page.getByLabel("Value for token", { exact: true }).fill("fictional-preview-secret");
    await page.getByRole("heading", { name: /^Set up / }).scrollIntoViewIfNeeded();
    await capture("inputs", theme, width);
    await next(page);
    await expect(page.getByRole("heading", { name: "Resolved targets" })).toBeVisible();
    await page.getByRole("heading", { name: "Review and create", exact: true }).scrollIntoViewIfNeeded();
    await capture("review", theme, width);
    await page.getByRole("heading", { name: "Resolved targets", exact: true }).scrollIntoViewIfNeeded();
    await capture("targets", theme, width);
    await page.getByRole("checkbox", { name: /Confirm this target/ }).check();
    await create(page); await verified(page);
    await page.getByRole("heading", { name: "Project created", exact: true }).scrollIntoViewIfNeeded();
    await capture("done", theme, width);
  }
});
