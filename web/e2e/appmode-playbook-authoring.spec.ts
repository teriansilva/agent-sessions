import { expect, test, type Locator, type Page } from "@playwright/test";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { PY_STACK_AVAILABLE, startStack, type Stack } from "./appmode";
import { HOOK_TIMEOUT_MS } from "./harness";
import type { Flow } from "../src/components/playbooks/playbookDraft";
import type { EngineInfo } from "../src/types/api";

/** #1192's browser → validator → stored bundle contract. The existing app-mode job runs
 * this glob with Python available; Node-only web jobs explicitly skip it. Use the stack's
 * direct loopback app here: relay transport has its own tests. No coding agent is launched. */
test.describe.configure({ mode: "serial" });
test.setTimeout(90_000);
test.skip(({ isMobile }) => !!isMobile, "viewport matrix runs once in the Python-backed job");
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

function seed(id: string) {
  const root = join(stack.home, ".config", "agent-sessions", "playbooks", id);
  mkdirSync(join(root, "flows"), { recursive: true });
  writeFileSync(join(root, "playbook.toml"), `format = 1
[identity]
id = "${id}"
name = "Reference proof"
publisher = "Tests"
version = "1.0.0"
domain = "development"
[[variables]]
name = "repo"
[[variables]]
name = "branch"
[flows]
default = "main"
`);
  writeFileSync(join(root, "flows", "main.toml"), `format = 1
title = "Implement and review"
[[steps]]
id = "implement"
title = "Implement"
actor = { kind = "agent", engine = "codex", model = "default" }
outputs = ["head_branch", "pr_number"]
[[steps.checklist]]
key = "pr_ready"
title = "Pull request exists"
probe = "forge_pr"
probe_args = { repo = "{{repo}}", branch = "{{branch}}" }
[[steps]]
id = "review"
title = "Review"
actor = { kind = "external", label = "Independent reviewer" }
after = ["implement"]
rework = { to = "implement", when = "review_ready", max_rounds = 4 }
[[steps.checklist]]
key = "review_ready"
title = "Review approved"
probe = "forge_review"
probe_args = { repo = "{{repo}}", branch = "{{steps.implement.head_branch}}" }
`);
}

async function stored(page: Page, id: string) {
  const response = await page.request.get(`http://127.0.0.1:${stack.appPort}/api/playbooks/${id}`);
  expect(response.status()).toBe(200);
  return await response.json() as { revision: string; documents: Record<string, Flow>; files: Record<string, string> };
}
async function selectReview(page: Page) {
  await expect(page.getByRole("region", { name: "Flow steps", exact: true })).toBeVisible();
  if (await page.getByRole("button", { name: "List", exact: true }).count())
    await page.getByRole("button", { name: "List", exact: true }).click();
  await page.getByRole("button", { name: "2. Review", exact: true }).click();
}
async function save(page: Page, id: string, status: number) {
  const response = page.waitForResponse(r => r.url().endsWith(`/api/playbooks/${id}`) && r.request().method() === "PUT");
  await page.getByRole("button", { name: "Save playbook", exact: true }).click();
  const result = await response;
  expect(result.status()).toBe(status);
  return result.json() as Promise<{ field: string }>;
}
const branchValue = (data: Awaited<ReturnType<typeof stored>>) =>
  data.documents["flows/main.toml"].steps[1].checklist?.[0].probe_args?.branch;

for (const theme of ["dark", "light"] as const) {
  for (const width of [1440, 320]) {
    test(`real saves preserve references and refuse invalid targets (${theme}, ${width}px)`, async ({ page }) => {
      const id = `reference-${theme}-${width}`;
      seed(id);
      await page.setViewportSize({ width, height: 1000 });
      await page.goto(`http://127.0.0.1:${stack.appPort}/library/playbooks/${id}/edit`);
      await selectReview(page);
      await page.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
      const branch = page.getByLabel("branch", { exact: true });
      const ancestor = "{{steps.implement.head_branch}}";
      const incompatible = "{{steps.implement.pr_number}}";
      await expect(branch.locator(`option[value="${ancestor}"]`)).toHaveCount(1);
      await expect(branch.locator(`option[value="${incompatible}"]`)).toHaveCount(0);
      await expect(page.getByLabel("branch literal expectation", { exact: true })).toHaveCount(0);

      for (const reference of ["{{branch}}", ancestor]) {
        await branch.selectOption(reference);
        await save(page, id, 200);
        await page.reload();
        await selectReview(page);
        await expect(branch).toHaveValue(reference);
        expect(branchValue(await stored(page, id))).toBe(reference);
      }
      const revision = (await stored(page, id)).revision;
      for (const bad of ["literal-branch", incompatible]) {
        // Normal controls exclude these; a tampered browser must still hit the real validator.
        await branch.evaluate((select, value) => (select as HTMLSelectElement).add(new Option(value, value)), bad);
        await branch.selectOption(bad);
        const refusal = await save(page, id, 422);
        expect(refusal.field).toContain("probe_args.branch");
        await expect(page.getByRole("alert")).toContainText(refusal.field);
        const unchanged = await stored(page, id);
        expect(unchanged.revision).toBe(revision);
        expect(branchValue(unchanged)).toBe(ancestor);
      }
      await branch.selectOption("{{branch}}");
      await save(page, id, 200);
      await page.reload();
      await selectReview(page);
      await expect(branch).toHaveValue("{{branch}}");
    });
  }
}

async function tabTo(page: Page, target: Locator) {
  for (let n = 0; n < 200; n++) {
    if (await target.evaluate(el => el === document.activeElement)) return;
    await page.keyboard.press("Tab");
  }
  throw new Error("Control is not reachable by Tab");
}

test("keyboard-only list editing saves the title and rework bound through the real API", async ({ page }) => {
  const id = "reference-keyboard";
  seed(id);
  await page.goto(`http://127.0.0.1:${stack.appPort}/library/playbooks/${id}/edit`);
  await expect(page.getByRole("region", { name: "Flow steps", exact: true })).toBeVisible();
  await tabTo(page, page.getByRole("button", { name: "List", exact: true }));
  await page.keyboard.press("Enter");
  await tabTo(page, page.getByRole("button", { name: "2. Review", exact: true }));
  await page.keyboard.press("Enter");
  await tabTo(page, page.getByLabel("Step title", { exact: true }));
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.type("Keyboard review");
  await tabTo(page, page.getByLabel("Maximum rework rounds"));
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.type("2");
  await tabTo(page, page.getByRole("button", { name: "Save playbook", exact: true }));
  const response = page.waitForResponse(r => r.url().endsWith(`/api/playbooks/${id}`) && r.request().method() === "PUT");
  await page.keyboard.press("Enter");
  expect((await response).status()).toBe(200);
  const review = (await stored(page, id)).documents["flows/main.toml"].steps[1];
  expect(review.title).toBe("Keyboard review");
  expect(review.rework).toEqual({ to: "implement", when: "review_ready", max_rounds: 2 });
});

const fixture = JSON.parse(readFileSync(new URL("../src/test/roster.fixture.json", import.meta.url), "utf8")) as { engines: EngineInfo[] };
for (const state of ["absent", "retiring", "model-unavailable"] as const) {
  for (const width of [1440, 320]) {
    test(`an unresolved ${state} reference survives a real save byte-identical (${width}px)`, async ({ page }) => {
      const id = `unresolved-${state}-${width}`;
      seed(id);
      const path = join(stack.home, ".config", "agent-sessions", "playbooks", id, "flows", "main.toml");
      const model = state === "model-unavailable" ? "stored-model" : "default";
      writeFileSync(path, readFileSync(path, "utf8").replace('engine = "codex", model = "default"', `engine = "fixture-agent", model = "${model}"`));
      const engine: EngineInfo = {
        ...fixture.engines[0], id: "fixture-agent", present: true,
        status: state === "retiring" ? "retiring" : "active",
        model_select: { supported: true, on_resume: true, configured_elsewhere: false, offered: [] },
      };
      // Only discovery is controlled. Every playbook read/save still reaches the actual app.
      await page.route("**/api/engines", route => route.fulfill({ json: { engines: state === "absent" ? [] : [engine] } }));
      await page.setViewportSize({ width, height: 1000 });
      await page.goto(`http://127.0.0.1:${stack.appPort}/library/playbooks/${id}`);
      await expect(page.getByRole("heading", { name: "Reference proof", exact: true })).toBeVisible();
      const list = page.getByRole("button", { name: "List", exact: true });
      if (await list.count()) await list.click();
      const reason = state === "model-unavailable" ? "Unresolved: model unavailable" : `Unresolved: agent ${state}`;
      await expect(page.getByText(reason, { exact: true })).toBeVisible();
      const original = await stored(page, id);
      await page.goto(`http://127.0.0.1:${stack.appPort}/library/playbooks/${id}/edit`);
      await expect(page.getByLabel("Agent", { exact: true })).toHaveValue("fixture-agent");
      await expect(page.getByLabel("Model", { exact: true })).toHaveValue(model);
      await page.getByLabel("Playbook name", { exact: true }).fill("Edited reference proof");
      await save(page, id, 200);
      await page.reload();
      await expect(page.getByLabel("Agent", { exact: true })).toHaveValue("fixture-agent");
      await expect(page.getByLabel("Model", { exact: true })).toHaveValue(model);
      const reloaded = await stored(page, id);
      expect(reloaded.revision).not.toBe(original.revision);
      expect(reloaded.files["flows/main.toml"]).toBe(original.files["flows/main.toml"]);
    });
  }
}
