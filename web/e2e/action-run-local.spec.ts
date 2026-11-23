/** Run a command and Run a script (#2248 P3) in a real browser, desktop and mobile: both are in the
 *  gallery under Run locally; the wizard edits the argument list one row per element (the program
 *  always fixed, an option keeping the next row fixed, a delivery value as one whole element), picks
 *  a script from the project, and saves the exact plugin shape; the consent shows the server's own
 *  lines (`fixtures/run-local-consent.json`), honesty lines included; a changed script shows why it
 *  needs approval again. Nothing here runs anything: the server is mocked. */
import { expect, test, type Page } from "@playwright/test";

import { ACTIONS_PATH, AUTOMATION_NEW_PATH, AUTOMATIONS_PATH } from "../src/lib/routes";
import { ACTION_SCRIPTS_API } from "../src/lib/apiPaths";
import { ACTION_GALLERY, mockActions } from "./actions";
import {
  PROJECT,
  RUN_LOCAL_CONSENT,
  automation,
  mockAutomations,
  targetsAndOverflow,
  toStep,
  type FakeOptions,
} from "./automations";
import { animationsSettled } from "./nightjarShell";
import { mockRoster } from "./roster";
import { mockWebhooks, webhook } from "./webhooks";

type Json = Record<string, unknown>;

const SHOP = webhook({
  id: "0a1b2c3d4e5f6a7b",
  name: "shop repo",
  last_delivery_at: Math.floor(Date.now() / 1000) - 120,
  last_event: "pull request opened",
  last_shape: [
    { path: "pull_request.number", type: "number" },
    { path: "pull_request.head.ref", type: "text" },
  ],
});

const SCRIPTS = {
  folder: "/repo",
  scripts: [
    { path: "scripts/build-docs.sh", size: 812 },
    { path: "scripts/deploy-preview.sh", size: 1460 },
  ],
  truncated: false,
};

async function setup(page: Page, opts: FakeOptions = {}) {
  const fake = await mockAutomations(page, {
    engines: [{ id: "claude-api", ok: true, reason: "" }],
    ...opts,
  });
  await mockWebhooks(page, [SHOP]);
  await mockRoster(page);
  await mockActions(page, ACTION_GALLERY);
  const scriptReads: string[] = [];
  await page.route(`**${ACTION_SCRIPTS_API}?*`, (r) => {
    scriptReads.push(new URL(r.request().url()).searchParams.get("project_id") ?? "");
    return r.fulfill({ json: SCRIPTS });
  });
  return { fake, scriptReads };
}

function created(fake: { requests: { method: string; path: string; body: Json | null }[] }): Json {
  return fake.requests.find((r) => r.method === "POST" && r.path === "")!.body as Json;
}

async function fromWebhook(page: Page) {
  await page.getByTestId("trigger-kind").getByRole("button", { name: /When a webhook delivers/ }).click();
  await page.getByTestId("trigger-webhook").selectOption(SHOP.id);
}

async function save(page: Page, name: string) {
  await toStep(page, "Review and enable");
  await page.getByTestId("automation-name").fill(name);
  await page.getByTestId("editor-save").click();
  await expect(page.getByTestId("editor-notice")).toContainText("Saved");
}

async function fillScript(page: Page) {
  await page.getByTestId("action-kind-run_script").click();
  await page.getByTestId("run-project").selectOption(PROJECT.id);
  await expect(page.getByTestId("run-script").locator("option")).toHaveText([
    "Choose a script…",
    "scripts/build-docs.sh",
    "scripts/deploy-preview.sh",
  ]);
  await page.getByTestId("run-script").selectOption("scripts/deploy-preview.sh");
  await page.getByTestId("run-arg-add").click();
  await page.getByTestId("run-arg-value").first().fill("--");
  await page.getByTestId("run-arg-add").click();
  const second = page.getByTestId("run-arg").nth(1);
  await second.getByTestId("run-arg-source").getByRole("button", { name: "From the delivery" }).click();
  await second.getByTestId("run-arg-path").fill("pull_request.number");
  await second.getByTestId("run-arg-type").selectOption("number");
  await page.getByTestId("run-env-add").click();
  await page.getByTestId("run-env-name").fill("pr_number");
  const env = page.getByTestId("run-env-row").first();
  await env.getByTestId("run-env-source").getByRole("button", { name: "From the delivery" }).click();
  await env.getByTestId("run-env-path").fill("pull_request.number");
  await env.getByTestId("run-env-type").selectOption("number");
}

// ---- the gallery --------------------------------------------------------------------------------

test("the gallery has a Run locally category with both actions, effect Run · local", async ({ page }) => {
  await setup(page);
  await page.goto(ACTIONS_PATH);
  for (const name of ["Run a command", "Run a script"]) {
    const card = page.getByTestId("action-card").filter({ hasText: name });
    await expect(card).toContainText("Run locally");
    await expect(card.getByTestId("action-effect")).toHaveText("Run · local");
  }
});

// ---- Run a script -------------------------------------------------------------------------------

test("Run a script: pick the script, an argument and a variable from the delivery, save; the consent is honest", async ({
  page,
}) => {
  const { fake, scriptReads } = await setup(page);
  await page.goto(AUTOMATION_NEW_PATH);
  await fromWebhook(page);
  await toStep(page, "What does it do?");
  await fillScript(page);
  expect(scriptReads).toContain(PROJECT.id);
  await expect(page.getByTestId("run-local-reach")).toContainText("It runs as your user");
  await animationsSettled(page);
  await targetsAndOverflow(page, "main");
  await save(page, "Deploy preview on every PR");
  expect(created(fake).action).toEqual({
    kind: "plugin",
    plugin: "run_script",
    version: 1,
    inputs: {
      project_id: PROJECT.id,
      script: "scripts/deploy-preview.sh",
      args: [{ value: "--" }, { from: "arg_2" }],
      env: [{ name: "PR_NUMBER", from: "env_pr_number" }],
      timeout_minutes: 5,
      outputs: {},
    },
    from_delivery: {
      arg_2: { path: "pull_request.number", type: "number" },
      env_pr_number: { path: "pull_request.number", type: "number" },
    },
  });
  const lines = page.getByTestId("editor-scope-lines");
  await expect(lines).toContainText("Runs the script scripts/deploy-preview.sh");
  await expect(lines).toContainText("(sha256 3f9a");
  await expect(lines).toContainText("Never through a shell");
  await expect(lines).toContainText("<the delivery's pull_request.number, as a number>");
  await expect(lines).toContainText("as your user op");
  await expect(lines).toContainText("Stops after 5 minutes; keeps at most 64 KB");
  await expect(lines).toContainText("Secrets never go in as values");
  await expect(lines).toContainText("it asks for your approval again");
  await expect(lines).toContainText("neither is a filesystem or network sandbox");
});

// ---- Run a command ------------------------------------------------------------------------------

test("Run a command: the program is always fixed, an option keeps the next row fixed", async ({ page }) => {
  const { fake } = await setup(page);
  await page.goto(AUTOMATION_NEW_PATH);
  await fromWebhook(page);
  await toStep(page, "What does it do?");
  await page.getByTestId("action-kind-run_command").click();
  await page.getByTestId("run-project").selectOption(PROJECT.id);
  // The program row has no "from the delivery" choice: it is always fixed.
  await expect(page.getByTestId("run-arg-program").getByTestId("run-arg-source")).toHaveCount(0);
  await page.getByTestId("run-program").fill("./build.sh");
  await expect(page.getByText("for a file of the project, use Run a script").first()).toBeVisible();
  await page.getByTestId("run-program").fill("make");
  await page.getByTestId("run-arg-add").click();
  await page.getByTestId("run-arg-value").first().fill("-C");
  await page.getByTestId("run-arg-option").first().check();
  await page.getByTestId("run-arg-add").click();
  // The row after an option is its value: fixed, no delivery choice offered.
  await expect(page.getByTestId("run-arg").nth(1).getByTestId("run-arg-source")).toHaveCount(0);
  await page.getByTestId("run-arg-value").nth(1).fill("site");
  await page.getByTestId("run-env-add").click();
  await page.getByTestId("run-env-name").fill("MODE");
  await page.getByTestId("run-env-value").fill("fast");
  await page.getByTestId("run-timeout").fill("10");
  await animationsSettled(page);
  await targetsAndOverflow(page, "main");
  await save(page, "Build the site");
  expect(created(fake).action).toEqual({
    kind: "plugin",
    plugin: "run_command",
    version: 1,
    inputs: {
      project_id: PROJECT.id,
      argv: [{ value: "make" }, { value: "-C", option: true }, { value: "site" }],
      env: [{ name: "MODE", value: "fast" }],
      timeout_minutes: 10,
      outputs: {},
    },
  });
  const lines = page.getByTestId("editor-scope-lines");
  await expect(lines).toContainText('Runs this command: ["make", "-C" (option), "site"]');
  await expect(lines).toContainText("The program is /usr/bin/make (sha256 4444");
  await expect(lines).toContainText("What it can reach: everything your user can");
});

// ---- drift --------------------------------------------------------------------------------------

function changedScript(): Json {
  return automation({
    name: "Nightly docs build",
    state: "needs_reapproval",
    needs_reapproval: true,
    paused: true,
    reapproval_reason: RUN_LOCAL_CONSENT.drift_reason,
    next_run: null,
    action: {
      kind: "plugin",
      plugin: "run_script",
      version: 1,
      inputs: {
        project_id: PROJECT.id,
        script: "scripts/build-docs.sh",
        args: [],
        env: [],
        timeout_minutes: 5,
        outputs: {},
      },
    },
    scope_lines: RUN_LOCAL_CONSENT.run_script,
  });
}

test("a changed script pauses the automation and asks again, saying what changed", async ({ page }) => {
  await setup(page, { automations: [changedScript()] });
  await page.goto(AUTOMATIONS_PATH);
  await page.getByTestId("why-not-running").getByRole("button", { name: "Review and approve" }).click();
  await expect(page.getByTestId("consent-reason")).toContainText(
    "scripts/build-docs.sh changed since you approved it (sha256 71c0…9ab2 → 08d4…e3f1)",
  );
  const dialog = page.getByTestId("consent-dialog");
  await expect(dialog).toContainText("Never through a shell");
  await expect(dialog).toContainText("neither is a filesystem or network sandbox");
});

// ---- captures -----------------------------------------------------------------------------------

for (const theme of ["dark", "light"] as const) {
  test(`captures Run locally — ${theme} (#2248 P3)`, async ({ page }, info) => {
    await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
    const mobile = info.project.name === "mobile";
    await page.setViewportSize(mobile ? { width: 390, height: 2400 } : { width: 1440, height: 1800 });
    const shot = async (name: string) => {
      await animationsSettled(page);
      await page.screenshot({ path: info.outputPath(`p3-${name}-${info.project.name}-${theme}.png`) });
    };
    await setup(page);
    await page.goto(AUTOMATION_NEW_PATH);
    await fromWebhook(page);
    await toStep(page, "What does it do?");
    await fillScript(page);
    await shot("run-script");
    await save(page, "Deploy preview on every PR");
    // The saved scope re-renders the panel: wait for the server's lines, then bring them in view.
    const scopeLines = page.getByTestId("editor-scope-lines");
    await expect(scopeLines).toContainText("Never through a shell");
    await scopeLines.scrollIntoViewIfNeeded();
    await shot("run-script-consent");
    await page.unrouteAll({ behavior: "ignoreErrors" });
    await setup(page, { automations: [changedScript()] });
    await page.goto(AUTOMATIONS_PATH);
    await page.getByTestId("why-not-running").getByRole("button", { name: "Review and approve" }).click();
    await expect(page.getByTestId("consent-reason")).toBeVisible();
    await shot("drift");
    await page.goto(ACTIONS_PATH);
    await shot("gallery");
  });
}
