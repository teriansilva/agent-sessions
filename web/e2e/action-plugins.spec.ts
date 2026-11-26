/** The plugin actions (#2248 P2) in a real browser, desktop and mobile: each is in the gallery and
 *  the wizard's picker, its inputs are filled in the What step and saved as a plugin action
 *  (`{kind: "plugin", plugin, version: 1, inputs, from_delivery?}`), and an action whose
 *  requirement is not met is unpickable, saying why, with a Set up link (the mockup's needs-setup
 *  state). The gallery is the server's own answer (`fixtures/actions-gallery.json`). */
import { expect, test, type Page } from "@playwright/test";

import { ACTIONS_PATH, AUTOMATION_NEW_PATH } from "../src/lib/routes";
import { settingsPath } from "../src/routes/settingsTabs";
import { ACTION_GALLERY, messenger, mockActions, mockMessengers, withSetupMissing } from "./actions";
import {
  NEW_SIGNING_SECRET,
  PROJECT,
  backTo,
  SIGNING_SECRET,
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
    { path: "pull_request.title", type: "text" },
  ],
});

const TEMPLATE = {
  id: "t-pr",
  name: "PR arrived",
  description: "",
  tags: [],
  body: "PR {{number}}: {{title}}",
  fields: [
    { name: "number", label: "PR number", default: "", required: true, source: "typed", kind: "text" },
    { name: "title", label: "Title", default: "", required: false, source: "typed", kind: "text" },
  ],
  images: [],
  created_at: 1,
  updated_at: 1,
  used_count: 0,
  last_used_at: null,
};

async function setup(page: Page, opts: { gallery?: Parameters<typeof mockActions>[1] } & FakeOptions = {}) {
  const { gallery, ...rest } = opts;
  const fake = await mockAutomations(page, {
    engines: [{ id: "claude-api", ok: true, reason: "" }],
    templates: [TEMPLATE],
    ...rest,
  });
  await mockWebhooks(page, [SHOP]);
  await mockRoster(page);
  await mockActions(page, gallery ?? ACTION_GALLERY);
  await mockMessengers(page, [
    messenger(),
    messenger({
      id: "c_00000000mail",
      manifest_id: "smtp",
      tested: { login: "bot@example.org", repos: 0, at: 1 },
      sender: "bot@example.org",
      recipients: [{ id: "ops@example.org", label: "Ops inbox", kind: "email" }],
    }),
    messenger({ id: "c_0000000matrx", manifest_id: "matrix", send: false, recipients: [] }),
  ]);
  return fake;
}

function created(fake: { requests: { method: string; path: string; body: Json | null }[] }): Json {
  return fake.requests.find((r) => r.method === "POST" && r.path === "")!.body as Json;
}

async function save(page: Page, name: string) {
  await toStep(page, "Review and enable");
  await page.getByTestId("automation-name").fill(name);
  await page.getByTestId("editor-save").click();
  await expect(page.getByTestId("editor-notice")).toContainText("Saved");
}

// ---- the gallery --------------------------------------------------------------------------------

test("the gallery shows the plugin actions in their categories with their effect class", async ({ page }) => {
  await setup(page);
  await page.goto(ACTIONS_PATH);
  const card = page.getByTestId("action-card").filter({ hasText: "Send a message" });
  await expect(card).toContainText("Messaging");
  await expect(card.getByTestId("action-effect")).toHaveText("Notify");
  await expect(card.getByTestId("action-requires")).toHaveText("Ready");
});

// ---- Send a message -----------------------------------------------------------------------------

test("Send a message: pick the messenger and a saved recipient, write the text, save", async ({ page }) => {
  const fake = await setup(page);
  await page.goto(AUTOMATION_NEW_PATH);
  await toStep(page, "What does it do?");
  await page.getByTestId("action-kind-send_message").click();
  await expect(page.getByTestId("action-kind-send_message")).toHaveAttribute("aria-pressed", "true");
  const conn = page.getByTestId("send-connector");
  // A messenger that can't send now is listed, but not choosable.
  await expect(conn.locator("option", { hasText: "Matrix" })).toBeDisabled();
  await conn.selectOption("c_0123456789ab");
  // Only the recipients saved for THIS messenger; no address is typed.
  await expect(page.getByTestId("send-recipient").locator("option")).toHaveText([
    "Choose who receives it…",
    "Marcus · 42",
    "Release crew · -1009",
  ]);
  await page.getByTestId("send-recipient").selectOption("-1009");
  await page.getByTestId("message-text").fill("The nightly backup finished");
  await animationsSettled(page);
  await targetsAndOverflow(page, "main");
  await save(page, "Backup ping");
  expect(created(fake).action).toEqual({
    kind: "plugin",
    plugin: "send_message",
    version: 1,
    inputs: {
      connector: "c_0123456789ab",
      recipient: "-1009",
      message: { text: "The nightly backup finished" },
    },
  });
});

test("Send a message from a webhook: a template field comes from the delivery, on the action", async ({ page }) => {
  const fake = await setup(page);
  await page.goto(AUTOMATION_NEW_PATH);
  await page.getByTestId("trigger-kind").getByRole("button", { name: /When a webhook delivers/ }).click();
  await page.getByTestId("trigger-webhook").selectOption(SHOP.id);
  await toStep(page, "What does it do?");
  await page.getByTestId("action-kind-send_message").click();
  await page.getByTestId("send-connector").selectOption("c_00000000mail");
  await page.getByTestId("send-recipient").selectOption("ops@example.org");
  await expect(page.getByTestId("send-message-what")).toContainText("mail goes from bot@example.org");
  await page.getByTestId("message-mode").selectOption("template");
  await page.getByTestId("message-template").selectOption(TEMPLATE.id);
  const field = (name: string) => page.locator(`[data-testid="template-field"][data-field="${name}"]`);
  await field("number").getByTestId("field-source").getByRole("button", { name: "From the delivery" }).click();
  await field("number").getByTestId("map-path").fill("pull_request.number");
  await field("number").getByTestId("map-type").selectOption("number");
  await field("title").getByTestId("field-value").fill("look at it");
  await save(page, "PR mail");
  expect(created(fake).action).toEqual({
    kind: "plugin",
    plugin: "send_message",
    version: 1,
    inputs: {
      connector: "c_00000000mail",
      recipient: "ops@example.org",
      message: { template_id: TEMPLATE.id, values: { title: "look at it" } },
    },
    from_delivery: { number: { path: "pull_request.number", type: "number" } },
  });
});

test("an action whose requirement is not met can't be picked, says why and links to Set up", async ({ page }) => {
  await setup(page, {
    gallery: withSetupMissing("send_message", "needs Telegram, Matrix or mail set up to send"),
  });
  await page.goto(AUTOMATION_NEW_PATH);
  await toStep(page, "What does it do?");
  const row = page.getByTestId("action-kind-send_message");
  await expect(row).toHaveAttribute("data-state", "needs-setup");
  await expect(row).toHaveAttribute("aria-disabled", "true");
  await expect(row.getByTestId("action-needs")).toHaveText("Needs Telegram, Matrix or mail set up to send");
  // Not a button: pressing the row picks nothing.
  await expect(row.getByRole("button")).toHaveCount(0);
  await row.getByTestId("action-needs").click();
  await expect(page.getByTestId("send-message-fields")).toHaveCount(0);
  await expect(page.getByTestId("action-kind").locator('[aria-pressed="true"]')).toHaveCount(1);
  const setupLink = row.getByTestId("action-setup");
  await expect(setupLink).toHaveText("Set up");
  await expect(setupLink).toHaveAttribute("href", settingsPath("ai-hq"));
  await targetsAndOverflow(page, "main");
});

// ---- captures -----------------------------------------------------------------------------------

for (const theme of ["dark", "light"] as const) {
  test(`captures the plugin actions — ${theme} (#2248 P2)`, async ({ page }, info) => {
    // Three page loads and six full-page shots (1900 px tall on mobile) in one test. Measured
    // 13–17 s on mobile locally; on the shared CI runner the one-worker recheck alone ran 34 s
    // into the 30 s default (task 51282, #2324). Budget sized from that, local to this test.
    test.setTimeout(90_000);
    await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
    const mobile = info.project.name === "mobile";
    await page.setViewportSize(mobile ? { width: 390, height: 1900 } : { width: 1440, height: 1300 });
    const shot = async (name: string) => {
      await animationsSettled(page);
      await page.screenshot({ path: info.outputPath(`p2-${name}-${info.project.name}-${theme}.png`) });
    };
    await setup(page, {
      gallery: withSetupMissing("send_message", "needs Telegram, Matrix or mail set up to send"),
    });
    await page.goto(AUTOMATION_NEW_PATH);
    await toStep(page, "What does it do?");
    await shot("picker-needs-setup");
    await page.unrouteAll({ behavior: "ignoreErrors" });
    await setup(page);
    await page.goto(AUTOMATION_NEW_PATH);
    await toStep(page, "What does it do?");
    await page.getByTestId("action-kind-send_message").click();
    await page.getByTestId("send-connector").selectOption("c_0123456789ab");
    await page.getByTestId("send-recipient").selectOption("42");
    await page.getByTestId("message-text").fill("The nightly backup finished");
    await shot("send-message");
    await page.getByTestId("action-kind-call_webhook").click();
    await page.getByTestId("webhook-destination").fill("https://hooks.example.org/battlelab");
    await page.getByTestId("webhook-field-add").click();
    await page.getByTestId("webhook-field-name").fill("job");
    await page.getByTestId("webhook-field-value").fill("nightly");
    await save(page, "Tell the shop");
    await expect(page.getByTestId("destination-secret")).toBeVisible();
    await shot("webhook-secret-once");
    await page.unrouteAll({ behavior: "ignoreErrors" });
    await setup(page);
    await page.goto(AUTOMATION_NEW_PATH);
    await toStep(page, "What does it do?");
    await page.getByTestId("action-kind-publish_artifact").click();
    await page.getByTestId("artifact-project").selectOption(PROJECT.id);
    await page.getByTestId("artifact-path").fill("reports/nightly.md");
    await page.getByTestId("artifact-title-value").fill("Nightly report");
    await shot("publish-artifact");
    await page.getByTestId("action-kind-write_memory").click();
    await page.getByTestId("note-project").selectOption(PROJECT.id);
    await page.getByTestId("note-title").fill("Nightly build memory");
    await page.getByTestId("message-text").fill("The nightly build needs 12 GB of RAM");
    await shot("write-memory");
    await page.goto(ACTIONS_PATH);
    await expect(page.getByTestId("action-card")).toHaveCount(ACTION_GALLERY.actions.length);
    await shot("gallery");
  });
}

// ---- Call a webhook -----------------------------------------------------------------------------

test("Call a webhook: an https address, typed fields, and the secret shown once after save", async ({ page }) => {
  const fake = await setup(page);
  await page.goto(AUTOMATION_NEW_PATH);
  await page.getByTestId("trigger-kind").getByRole("button", { name: /When a webhook delivers/ }).click();
  await page.getByTestId("trigger-webhook").selectOption(SHOP.id);
  await toStep(page, "What does it do?");
  await page.getByTestId("action-kind-call_webhook").click();
  await expect(page.getByTestId("action-kind-call_webhook").getByTestId("action-effect")).toHaveText(
    "Write · external",
  );
  const dest = page.getByTestId("webhook-destination");
  await dest.fill("http://hooks.example.org/battlelab");
  await expect(page.getByText("The address must start with https://").first()).toBeVisible();
  await dest.fill("https://hooks.example.org/battlelab");
  await expect(page.getByTestId("webhook-allow-private")).not.toBeChecked();
  await page.getByTestId("webhook-field-add").click();
  await page.getByTestId("webhook-field-name").nth(0).fill("job");
  await page.getByTestId("webhook-field-value").nth(0).fill("nightly");
  await page.getByTestId("webhook-field-add").click();
  const second = page.getByTestId("webhook-field").nth(1);
  await second.getByTestId("webhook-field-name").fill("number");
  await second.getByTestId("webhook-field-source").getByRole("button", { name: "From the delivery" }).click();
  await second.getByTestId("webhook-field-path").fill("pull_request.number");
  await second.getByTestId("webhook-field-type").selectOption("number");
  await animationsSettled(page);
  await targetsAndOverflow(page, "main");
  await save(page, "Tell the shop");
  expect(created(fake).action).toEqual({
    kind: "plugin",
    plugin: "call_webhook",
    version: 1,
    inputs: {
      destination: "https://hooks.example.org/battlelab",
      allow_private: false,
      fields: { job: "nightly" },
    },
    from_delivery: { number: { path: "pull_request.number", type: "number" } },
  });
  // The secret from the save's answer, shown once; Done forgets it, and a read never has it.
  const once = page.getByTestId("destination-secret");
  await expect(once.getByTestId("destination-secret-value")).toHaveText(SIGNING_SECRET);
  await expect(once).toContainText("X-BattleLab-Signature");
  await once.getByTestId("destination-secret-done").click();
  await expect(page.getByTestId("destination-secret")).toHaveCount(0);
  await page.reload();
  await backTo(page, "What does it do?");
  await expect(page.getByTestId("webhook-secret-state")).toContainText("BattleLab can’t show it again");
  await expect(page.getByText(SIGNING_SECRET)).toHaveCount(0);
  // A new one, on demand: shown once again.
  await page.getByTestId("webhook-new-secret").click();
  await expect(page.getByTestId("destination-secret-value")).toHaveText(NEW_SIGNING_SECRET);
});

// ---- Publish an artifact ------------------------------------------------------------------------

test("Publish an artifact: a project, a path inside it, a title from the delivery", async ({ page }) => {
  const fake = await setup(page);
  await page.goto(AUTOMATION_NEW_PATH);
  await page.getByTestId("trigger-kind").getByRole("button", { name: /When a webhook delivers/ }).click();
  await page.getByTestId("trigger-webhook").selectOption(SHOP.id);
  await toStep(page, "What does it do?");
  await page.getByTestId("action-kind-publish_artifact").click();
  await page.getByTestId("artifact-project").selectOption(PROJECT.id);
  await page.getByTestId("artifact-path").fill("/etc/passwd");
  await expect(page.getByText("The path is relative to the project’s folder").first()).toBeVisible();
  await page.getByTestId("artifact-path").fill("reports/nightly.md");
  await page.getByTestId("artifact-title-source").getByRole("button", { name: "From the delivery" }).click();
  await page.getByTestId("artifact-title-path").fill("pull_request.title");
  await page.getByTestId("artifact-why-value").fill("the nightly run");
  await expect(page.getByTestId("publish-artifact-what")).toContainText("PRIVATE");
  await animationsSettled(page);
  await targetsAndOverflow(page, "main");
  await save(page, "Publish nightly");
  expect(created(fake).action).toEqual({
    kind: "plugin",
    plugin: "publish_artifact",
    version: 1,
    inputs: { project_id: PROJECT.id, path: "reports/nightly.md", title: "", why: "the nightly run" },
    from_delivery: { title: { path: "pull_request.title", type: "text" } },
  });
});

// ---- Write a memory note ------------------------------------------------------------------------

test("Write a memory note: a project, a title and the note's text", async ({ page }) => {
  const fake = await setup(page);
  await page.goto(AUTOMATION_NEW_PATH);
  await toStep(page, "What does it do?");
  await page.getByTestId("action-kind-write_memory").click();
  await page.getByTestId("note-project").selectOption(PROJECT.id);
  await page.getByTestId("note-title").fill("Nightly build memory");
  await page.getByTestId("message-text").fill("The nightly build needs 12 GB of RAM");
  await expect(page.getByTestId("write-memory-what")).toContainText("reviews the note");
  await animationsSettled(page);
  await targetsAndOverflow(page, "main");
  await save(page, "Remember the build");
  expect(created(fake).action).toEqual({
    kind: "plugin",
    plugin: "write_memory",
    version: 1,
    inputs: {
      project_id: PROJECT.id,
      title: "Nightly build memory",
      message: { text: "The nightly build needs 12 GB of RAM" },
    },
  });
});
