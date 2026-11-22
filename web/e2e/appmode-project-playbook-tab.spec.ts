import { expect, test, type Page } from "@playwright/test";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { PY_STACK_AVAILABLE, startStack, type Stack } from "./appmode";
import { HOOK_TIMEOUT_MS } from "./harness";

/** HQ → Projects → Playbook (#2143 P2, #1949) on the real Python stack, in a throwaway HOME: an
 *  EXISTING project (made with no playbook) gets one from its Playbook tab through the real
 *  review → confirm → bind → apply → verify, verifies again, and has it removed through the real
 *  removal plan — the files appear and go on disk, the project and its folder stay. An archived
 *  project offers no deploy action. No workflow is started. */
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
test.afterAll(async () => {
  await stack?.stop();
});
const base = () => `http://127.0.0.1:${stack.appPort}`;

function seed(id: string) {
  const root = join(stack.home, ".config", "agent-sessions", "playbooks", id);
  mkdirSync(join(root, "template"), { recursive: true });
  mkdirSync(join(root, "flows"), { recursive: true });
  writeFileSync(
    join(root, "playbook.toml"),
    `format = 2
verify = ["materials", "variables"]
[identity]
id = "${id}"
name = "Tab setup ${id}"
publisher = "Tests"
version = "1.0.0"
domain = "operations"
[[variables]]
name = "endpoint"
type = "url"
default = "https://example.test/health"
[[materials]]
path = "RULES.md"
disposition = "managed"
template = true
[flows]
default = "main"
`,
  );
  writeFileSync(join(root, "template", "RULES.md"), "Read {{endpoint}} before working.\n");
  writeFileSync(
    join(root, "flows", "main.toml"),
    `format = 2
title = "Project setup"
[[steps]]
id = "review"
title = "Review the result"
actor = { kind = "operator" }
`,
  );
}

async function write(page: Page, path: string, data: unknown) {
  const csrf = (await (await page.request.get(`${base()}/api/config`)).json()).csrf as string;
  const r = await page.request.fetch(`${base()}${path}`, { method: "POST", data, headers: { "X-CSRF-Token": csrf, Origin: base() } });
  expect(r.status(), `POST ${path}: ${await r.text()}`).toBeLessThan(300);
  return r.json();
}

async function project(page: Page, name: string): Promise<{ id: string; folder: string }> {
  const folder = join(stack.home, name);
  mkdirSync(folder, { recursive: true });
  await page.goto(`${base()}/projects`);
  const made = await write(page, "/api/projects", { name, folders: [folder] });
  return { id: made.id, folder };
}

test("an existing project takes a playbook from its tab, verifies, and gives it back", async ({ page }) => {
  const id = "tab-journey";
  seed(id);
  const effects: string[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST") effects.push(new URL(r.url()).pathname);
  });
  const { id: pid, folder } = await project(page, "tab-journey-project");
  await page.goto(`${base()}/projects/${pid}?tab=playbook`);
  await page.getByLabel("Search playbooks").fill(id);
  await page.getByTestId(`playbook-card-${id}`).getByRole("radio").check();
  await page.getByRole("checkbox", { name: /unsigned local playbook/ }).check();
  const reviewed = page.waitForResponse((r) => r.url().endsWith(`/api/playbooks/${id}/review`) && r.request().method() === "POST");
  await page.getByRole("button", { name: "Review playbook changes" }).click();
  const review = await reviewed;
  expect(review.status(), await review.text()).toBe(200);
  expect(review.request().postDataJSON()).toMatchObject({ project_id: pid, destination: folder });
  expect(review.request().postDataJSON()).not.toHaveProperty("create");
  // Review writes nothing.
  expect(existsSync(join(folder, "RULES.md"))).toBe(false);
  expect(effects.some((p) => /\/(bind|apply)$/.test(p))).toBe(false);

  await page.getByRole("button", { name: "Apply reviewed changes to this project" }).click();
  await expect(page.getByTestId("playbook-verify")).toContainText("Verified");
  await expect(page.getByTestId("playbook-name")).toHaveText(`Tab setup ${id}`);
  expect(readFileSync(join(folder, "RULES.md"), "utf8")).toContain("https://example.test/health");
  const order = effects.filter((p) => /\/(review|confirm|bind|apply)$/.test(p));
  expect(order).toEqual([
    `/api/playbooks/${id}/review`,
    `/api/playbooks/${id}/review/confirm`,
    `/api/projects/${pid}/playbook/bind`,
    `/api/projects/${pid}/playbook/apply`,
  ]);
  expect(effects).not.toContain("/api/projects");
  expect(effects.some((p) => /\/sessions\/new|\/missions\/.*\/start|\/probe|create-target/.test(p))).toBe(false);

  const again = page.waitForResponse((r) => r.url().endsWith(`/api/projects/${pid}/playbook/verify`));
  await page.getByRole("button", { name: "Verify again" }).click();
  expect((await again).status()).toBe(200);

  await page.getByTestId("playbook-remove").click();
  const dialog = page.getByRole("dialog", { name: "Review removal", exact: true });
  await expect(dialog).toContainText("RULES.md");
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  expect(existsSync(join(folder, "RULES.md"))).toBe(true);
  await page.getByTestId("playbook-remove").click();
  await page.getByRole("button", { name: "Remove reviewed playbook", exact: true }).click();
  await expect(page.getByTestId("playbook-done")).toHaveText("Removed. The project and its folders stay.");
  await expect(page.getByRole("radiogroup", { name: "Playbook" })).toBeVisible();
  expect(existsSync(folder)).toBe(true);
  expect(existsSync(join(folder, "RULES.md"))).toBe(false);
  const listed = await (await page.request.get(`${base()}/api/projects`)).json();
  expect(listed.projects.some((p: { id: string }) => p.id === pid)).toBe(true);
});

test("an archived project offers no deploy action", async ({ page }) => {
  seed("tab-archived");
  const { id: pid } = await project(page, "tab-archived-project");
  await write(page, `/api/projects/${pid}/archive`, {});
  await page.goto(`${base()}/projects/${pid}?tab=playbook`);
  await expect(page.getByTestId("playbook-archived")).toBeVisible();
  await expect(page.getByRole("radiogroup", { name: "Playbook" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /Apply reviewed/ })).toHaveCount(0);
});
