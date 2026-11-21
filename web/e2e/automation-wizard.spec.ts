/** The automation wizard (#2221 P2b), in a real browser on the desktop AND mobile projects.
 *
 *  Five steps in the app's one WizardShell: When → Which events → What → Limits → Review & enable.
 *  What jsdom cannot tell: the step heading taking focus after a real click, 44 px targets, nothing
 *  clipped at 390 px, the folds, and the body the page SENDS — `trigger.when` from a preset or from
 *  Advanced, `message.from_delivery` typed — through the stateful fakes of `/api/automations` and
 *  `/api/webhooks`. The frames are the issue's: no delivery yet, a schedule skipping step 2, a
 *  preset, an Advanced condition, a mapped field with its type, an unknown field and a wrong type
 *  as step problems, limits, and consent on enable. */
import { expect, test, type Page } from "@playwright/test";

import { AUTOMATION_NEW_PATH, automationEditPath } from "../src/lib/routes";
import {
  PROJECT,
  automation,
  backTo,
  mockAutomations,
  targetsAndOverflow,
  toReview,
  toStep,
  wizardHeading,
  wizardNext,
  type FakeOptions,
} from "./automations";
import { mockRoster } from "./roster";
import { PR_SHAPE, mockWebhooks, webhook, type FakeHook } from "./webhooks";

type Json = Record<string, unknown>;

const API_ENGINES = [
  { id: "claude", ok: true, reason: "" },
  { id: "claude-api", ok: true, reason: "" },
];

/** A template with two typed fields, one library field and one typed secret. */
const TEMPLATE = {
  id: "t-review",
  name: "Review a pull request",
  description: "",
  tags: [],
  body: "Review PR {{pr_number}} as {{reviewer}} for {{team}}",
  fields: [
    { name: "pr_number", label: "PR number", default: "", required: true, source: "typed", kind: "text" },
    { name: "reviewer", label: "Reviewer", default: "", required: false, source: "typed", kind: "text" },
    { name: "team", label: "Team", default: "", required: false, source: "library", kind: "text" },
  ],
  images: [],
  created_at: 1,
  updated_at: 1,
  used_count: 0,
  last_used_at: null,
};

const SHOP = webhook({
  id: "0a1b2c3d4e5f6a7b",
  name: "shop repo",
  last_delivery_at: Math.floor(Date.now() / 1000) - 120,
  last_event: "pull request opened",
  last_shape: [...PR_SHAPE, { path: "pull_request.title", type: "text" }],
});
const QUIET = webhook({ id: "1b2c3d4e5f6a7b8c", name: "status page" });
const ODD = webhook({
  id: "2c3d4e5f6a7b8c9d",
  name: "deploy notices",
  last_delivery_at: Math.floor(Date.now() / 1000) - 3600,
  last_event: "delivery",
  last_shape: [
    { path: "status", type: "text" },
    { path: "run.id", type: "number" },
  ],
});

async function setup(page: Page, opts: FakeOptions = {}, hooks: FakeHook[] = [SHOP, QUIET, ODD]) {
  const fake = await mockAutomations(page, {
    engines: API_ENGINES,
    templates: [TEMPLATE],
    ...opts,
  });
  const hookFake = await mockWebhooks(page, hooks);
  await mockRoster(page); // registered last: the real roster, so a terminal agent is known as one
  await page.route("**/api/fs/dirs**", (r) => r.fulfill({ json: { home: "/repo", path: "/repo", dirs: [] } }));
  return { fake, hookFake };
}

function lastWrite(fake: { requests: { method: string; path: string; body: Json | null }[] }, method: string) {
  return [...fake.requests].reverse().find((r) => r.method === method)?.body as Json;
}

/** Step 1: a webhook from the list. */
async function pickWebhook(page: Page, id: string) {
  await page.getByTestId("trigger-kind").getByRole("button", { name: /When a webhook delivers/ }).click();
  await page.getByTestId("trigger-webhook").selectOption(id);
}

/** Step 3's folder, through the app's own picker (its listing mocked in `setup`). */
async function pickFolder(page: Page) {
  await page.getByRole("button", { name: "Choose…" }).click();
  await page.getByRole("dialog").getByRole("button", { name: /^Select/ }).click();
  await expect(page.getByTestId("session-folder")).toHaveValue("/repo");
}

const field = (page: Page, name: string) => page.locator(`[data-testid="template-field"][data-field="${name}"]`);

test("a schedule skips step 2, saves off, and enables through the server's consent", async ({ page }) => {
  const { fake } = await setup(page);
  await page.goto(AUTOMATION_NEW_PATH);
  await expect(wizardHeading(page)).toHaveText("When does it run?");
  await expect(page.getByTestId("trigger-kind").locator('[aria-pressed="true"]')).toContainText("On a schedule");
  await expect(page.getByTestId("events-skipped-note")).toContainText("step 2 is skipped");

  // Next goes straight to What; the heading takes focus.
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await expect(wizardHeading(page)).toHaveText("What does it do?");
  await expect(wizardHeading(page)).toBeFocused();
  // A schedule offers a mission, never a review loop.
  await expect(page.getByTestId("action-kind").getByRole("button")).toHaveText([
    /Start a mission/,
    /Start a session/,
    /Message a session/,
    /Tell HQ/,
  ]);
  await expect(page.getByRole("button", { name: "Next", exact: true })).toBeDisabled();
  await page.getByTestId("mission-project").selectOption(PROJECT.id);
  await page.getByTestId("message-text").fill("Audit the dependencies");

  // Limits: the defaults in one line, the fields folded behind it.
  await wizardNext(page);
  await expect(wizardHeading(page)).toHaveText("Limits");
  await expect(page.getByTestId("limits-summary")).toHaveText(
    "Defaults: one run at a time · at most 48 runs a day · pauses after 3 failures in a row · runs until you turn it off.",
  );
  await expect(page.getByTestId("limit-per-day")).toBeHidden();
  await page.getByTestId("limits-more").locator("summary").click();
  await page.getByTestId("limit-per-day").fill("24");
  await expect(page.getByTestId("limits-summary")).toContainText("Limits: one run at a time · at most 24 runs a day");

  // Back skips step 2 too.
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await expect(wizardHeading(page)).toHaveText("What does it do?");
  await wizardNext(page);
  await wizardNext(page);
  await expect(wizardHeading(page)).toHaveText("Review and enable");
  await expect(page.getByTestId("editor-save")).toBeDisabled();
  await expect(page.getByTestId("editor-problems")).toHaveText("Give it a name");
  await page.getByTestId("automation-name").fill("Nightly dependency audit");
  await page.getByTestId("editor-save").click();
  await expect(page.getByTestId("editor-notice")).toContainText("is off until you enable it");
  const sent = lastWrite(fake, "POST");
  expect(sent.trigger).toEqual({ kind: "schedule", cadence: { kind: "daily", time: "03:00" }, tz: "UTC" });
  expect((sent.policy as Json).max_runs_per_day).toBe(24);
  expect(JSON.stringify(sent)).not.toMatch(/"when"|from_delivery/);

  // The create remounted on the edit route, still on step 5, showing the server's lines.
  await expect(wizardHeading(page)).toHaveText("Review and enable");
  await expect(page.getByTestId("editor-scope-lines")).toContainText("Starts a mission in project p1");
  await page.getByTestId("editor-enable").click();
  const dialog = page.getByTestId("consent-dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByTestId("consent-confirm")).toBeDisabled();
  await dialog.getByTestId("consent-agree").check();
  await dialog.getByTestId("consent-confirm").click();
  await expect(page.getByTestId("editor-notice")).toContainText("is enabled");
  const enabled = [...fake.requests].reverse().find((r) => r.path.endsWith("/enable"))!.body as Json;
  expect(enabled).toMatchObject({ consent: true, scope_digest: "digest-new1-1" });
});

test("no delivery yet: step 2 says to send a test first, and Advanced still works", async ({ page }) => {
  const { fake, hookFake } = await setup(page);
  await page.goto(AUTOMATION_NEW_PATH);
  await pickWebhook(page, QUIET.id);
  await wizardNext(page);
  await expect(wizardHeading(page)).toHaveText("Which events start it?");
  await expect(page.getByTestId("events-no-delivery")).toContainText("“status page” has had no delivery yet");
  await expect(page.getByTestId("events-presets")).toHaveCount(0);
  // Advanced is open: there is nothing else to offer.
  await expect(page.getByTestId("events-advanced")).toHaveAttribute("open", "");
  await page.getByTestId("cond-add").click();
  await page.getByTestId("cond-path").fill("action");
  await page.getByTestId("cond-op").selectOption("one_of");
  // A list: one element per value, each with its own type. A comma stays inside its value.
  const values = page.getByTestId("cond-value-row");
  await expect(values).toHaveCount(1);
  await expect(values.getByTestId("cond-value-remove")).toBeDisabled(); // a list keeps one value
  await values.nth(0).getByTestId("cond-value").fill("opened");
  await page.getByTestId("cond-value-add").click();
  await values.nth(1).getByTestId("cond-value").fill("ops, urgent");
  await page.getByTestId("cond-value-add").click();
  await values.nth(2).getByTestId("cond-value").fill("4");
  await values.nth(2).getByTestId("cond-value-type").selectOption("number");
  await page.getByTestId("cond-value-add").click();
  await values.nth(3).getByTestId("cond-value").fill("dropped");
  await expect(values).toHaveCount(4);
  await values.nth(3).getByTestId("cond-value-remove").click();
  await expect(values).toHaveCount(3);
  // Check again reads the webhook once more.
  const reads = () => hookFake.requests.filter((r) => r.method === "GET" && r.path === `/${QUIET.id}`).length;
  const before = reads();
  await page.getByTestId("events-reload").click();
  await expect.poll(reads).toBe(before + 1);

  await wizardNext(page);
  await page.getByTestId("action-kind").getByRole("button", { name: /Message a session/ }).click();
  await expect(page.getByTestId("action-kind").getByRole("button")).toHaveText([
    /Start a session/,
    /Message a session/,
    /Review loop/,
    /Tell HQ/,
  ]);
  await page.getByTestId("action-kind").getByRole("button", { name: /Start a session/ }).click();
  // New automations use API agents (#2244): a terminal agent isn’t offered for a new one.
  await expect(page.getByTestId("session-engine").locator('option[value="claude"]')).toHaveCount(0);
  await page.getByTestId("session-engine").selectOption("claude-api");
  // The folder is still missing: Next waits on What and says why.
  await expect(page.getByRole("button", { name: "Next", exact: true })).toBeDisabled();
  await expect(page.getByRole("status").filter({ hasText: "Choose a folder" })).toBeVisible();
  await pickFolder(page);
  await page.getByTestId("message-text").fill("A delivery came in");
  await toReview(page);
  await page.getByTestId("automation-name").fill("Status page events");
  await page.getByTestId("editor-save").click();
  await expect(page.getByTestId("editor-notice")).toContainText("is off until you enable it");
  const sent = lastWrite(fake, "POST");
  expect(sent.trigger).toEqual({
    kind: "webhook",
    webhook_id: QUIET.id,
    when: [{ path: "action", op: "one_of", value: ["opened", "ops, urgent", 4] }],
  });
  expect(sent.action).toEqual({
    kind: "start_session",
    engine: "claude-api",
    model: null,
    folder: "/repo",
    bypass: false,
    message: { text: "A delivery came in" },
  });
});

test("a preset is chosen, a field is mapped from the delivery with a type, and the body carries both", async ({ page }) => {
  const { fake } = await setup(page);
  await page.goto(AUTOMATION_NEW_PATH);
  await pickWebhook(page, SHOP.id);
  await expect(page.getByTestId("shared-webhook-panel")).toBeVisible();
  await wizardNext(page);
  await expect(wizardHeading(page)).toBeFocused();
  const presets = page.getByTestId("events-presets");
  await expect(presets.getByTestId("events-preset")).toHaveCount(1);
  await expect(presets).toContainText("PR opened or pushed (drafts skipped)");
  // Nothing is ticked for you: no conditions is every signed delivery, said as such.
  await expect(page.getByTestId("events-every")).toBeChecked();
  await expect(page.getByTestId("events-advanced")).not.toHaveAttribute("open", "");
  await presets.getByTestId("events-preset").check();

  await wizardNext(page);
  await page.getByTestId("action-kind").getByRole("button", { name: /Start a session/ }).click();
  await page.getByTestId("session-engine").selectOption("claude-api");
  await page.getByTestId("message-mode").selectOption("template");
  await page.getByTestId("message-template").selectOption(TEMPLATE.id);
  // A library field can't come from the delivery; a typed field can.
  await expect(field(page, "team").getByTestId("field-source")).toHaveCount(0);
  await field(page, "pr_number").getByTestId("field-source").getByRole("button", { name: "From the delivery" }).click();
  await field(page, "pr_number").getByTestId("map-path").fill("pull_request.title");
  await field(page, "pr_number").getByTestId("map-type").selectOption("number");
  // A type that can't fit what the last delivery had holds the step, said in words.
  await expect(page.getByTestId("step-problems")).toContainText(
    "pr_number is a number; pull_request.title is text",
  );
  await field(page, "pr_number").getByTestId("map-path").fill("number");
  await expect(page.getByTestId("step-problems")).not.toContainText("pull_request.title");
  // A field the last delivery did not have warns, and does not hold the step.
  await field(page, "reviewer").getByTestId("field-source").getByRole("button", { name: "From the delivery" }).click();
  await field(page, "reviewer").getByTestId("map-path").fill("action");
  await field(page, "reviewer").getByTestId("map-type").selectOption("choice");
  await expect(page.getByTestId("step-problems")).toContainText("reviewer: list the allowed values");
  // One element per allowed value: add, fill, remove.
  const choices = field(page, "reviewer").getByTestId("map-choice");
  await choices.nth(0).fill("opened");
  await field(page, "reviewer").getByTestId("map-choice-add").click();
  await choices.nth(1).fill("ops, urgent");
  await field(page, "reviewer").getByTestId("map-choice-add").click();
  await expect(choices).toHaveCount(3);
  await expect(page.getByTestId("step-problems")).toContainText("reviewer: list the allowed values, none empty");
  await field(page, "reviewer").getByTestId("map-choice-remove").nth(2).click();
  await expect(choices).toHaveCount(2);
  await expect(page.getByTestId("step-problems")).not.toContainText("reviewer");

  await pickFolder(page);

  await toReview(page);
  await expect(page.getByTestId("summary-events")).toHaveText("PR opened or pushed (drafts skipped)");
  await page.getByTestId("automation-name").fill("Review every PR");
  await page.getByTestId("editor-save").click();
  await expect(page.getByTestId("editor-notice")).toContainText("is off until you enable it");
  const sent = lastWrite(fake, "POST");
  expect(sent.trigger).toEqual({
    kind: "webhook",
    webhook_id: SHOP.id,
    when: [
      { path: "action", op: "one_of", value: ["opened", "reopened", "synchronized"] },
      { path: "pull_request.draft", op: "is_false" },
      { path: "review", op: "missing" },
    ],
  });
  expect((sent.action as Json).message).toEqual({
    template_id: TEMPLATE.id,
    values: {},
    from_delivery: {
      pr_number: { path: "number", type: "number" },
      reviewer: { path: "action", type: "choice", choices: ["opened", "ops, urgent"] },
    },
  });
});

test("Tell HQ from a webhook: a mapped field, no agent or folder, and the body is the message only", async ({ page }) => {
  const { fake } = await setup(page);
  await page.goto(AUTOMATION_NEW_PATH);
  await pickWebhook(page, SHOP.id);
  await wizardNext(page);
  await expect(wizardHeading(page)).toHaveText("Which events start it?");
  await page.getByTestId("events-presets").getByTestId("events-preset").check();
  await wizardNext(page);
  await expect(wizardHeading(page)).toHaveText("What does it do?");
  const card = page.getByTestId("action-kind-tell_hq");
  await expect(card).toContainText("a message in your notifications; HQ may comment");
  await card.click();
  await expect(card).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByTestId("tell-hq-what")).toContainText("never what it says");
  // No agent, folder or session to choose: only the message.
  await expect(page.getByTestId("session-engine")).toHaveCount(0);
  await expect(page.getByTestId("session-folder")).toHaveCount(0);
  await expect(page.getByTestId("send-session")).toHaveCount(0);
  await page.getByTestId("message-mode").selectOption("template");
  await page.getByTestId("message-template").selectOption(TEMPLATE.id);
  await field(page, "pr_number").getByTestId("field-source").getByRole("button", { name: "From the delivery" }).click();
  await field(page, "pr_number").getByTestId("map-path").fill("number");
  await field(page, "pr_number").getByTestId("map-type").selectOption("number");
  await field(page, "reviewer").getByTestId("field-value").fill("the shop team");
  await expect(page.getByTestId("step-problems")).toHaveCount(0);
  await targetsAndOverflow(page, '[data-testid="automation-editor"]');

  await toReview(page);
  await expect(page.getByTestId("summary-events")).toHaveText("PR opened or pushed (drafts skipped)");
  await page.getByTestId("automation-name").fill("PR arrived");
  await page.getByTestId("editor-save").click();
  await expect(page.getByTestId("editor-notice")).toContainText("is off until you enable it");
  const sent = lastWrite(fake, "POST");
  expect(sent.trigger).toEqual({
    kind: "webhook",
    webhook_id: SHOP.id,
    when: [
      { path: "action", op: "one_of", value: ["opened", "reopened", "synchronized"] },
      { path: "pull_request.draft", op: "is_false" },
      { path: "review", op: "missing" },
    ],
  });
  expect(sent.action).toEqual({
    kind: "tell_hq",
    message: {
      template_id: TEMPLATE.id,
      values: { reviewer: "the shop team" },
      from_delivery: { pr_number: { path: "number", type: "number" } },
    },
  });
  // The server's own lines, after the save: what HQ is told, and that it never acts.
  await expect(page.getByTestId("editor-scope-lines")).toContainText("Puts a message in your notifications");
  await expect(page.getByTestId("editor-scope-lines")).toContainText("it never acts on it");
});

test("Advanced: a condition on a field the last delivery lacked warns; a wrong type holds the step", async ({ page }) => {
  await setup(page);
  await page.goto(AUTOMATION_NEW_PATH);
  await pickWebhook(page, ODD.id);
  await wizardNext(page);
  // An unknown sender: no presets, Advanced only.
  await expect(page.getByTestId("events-unknown-sender")).toBeVisible();
  await expect(page.getByTestId("events-presets")).toHaveCount(0);
  await expect(page.getByTestId("events-advanced")).toHaveAttribute("open", "");
  await page.getByTestId("cond-add").click();
  const row = page.getByTestId("cond-row");
  await row.getByTestId("cond-path").fill("run.id");
  // A fresh row nobody typed into is missing its value (an emptied one would be a value).
  await expect(page.getByTestId("step-problems")).toHaveText("Condition 1: give it a value");
  await row.getByTestId("cond-value").fill("four");
  await expect(page.getByTestId("step-problems")).toHaveText("Condition 1: “four” is not a number; give a number");
  await expect(page.getByRole("button", { name: "Next", exact: true })).toBeDisabled();
  await row.getByTestId("cond-value").fill("4");
  await expect(page.getByTestId("step-problems")).toHaveCount(0);
  await page.getByTestId("cond-add").click();
  await page.getByTestId("cond-row").nth(1).getByTestId("cond-path").fill("pull_request.base.ref");
  await page.getByTestId("cond-row").nth(1).getByTestId("cond-op").selectOption("exists");
  await expect(page.getByTestId("step-warnings")).toHaveText(
    "“pull_request.base.ref” was not in the last delivery — it may never match.",
  );
  await expect(page.getByRole("button", { name: "Next", exact: true })).toBeEnabled();
  await page.getByTestId("cond-row").nth(1).getByTestId("cond-remove").click();
  await expect(page.getByTestId("cond-row")).toHaveCount(1);
});

test("a review loop picks its own events: step 2 says so, and no conditions are sent", async ({ page }) => {
  const loop = automation({
    id: "rv1",
    name: "Review loop · example-org/shop",
    trigger: { kind: "webhook", webhook_id: SHOP.id },
    action: {
      kind: "forge_review_loop",
      project_id: "p1",
      repo: "example-org/shop",
      reviewer: { specialist_id: "shop-reviewer", version: 3 },
      reviewer_login: "shop-reviewer-bot",
      legs: { issues: true, pulls: true },
      max_rounds: { issue: 5, pr: 4 },
      return_to_origin: true,
    },
    policy: { concurrency: "allow", max_concurrent: 3, max_runs_per_day: 48, pause_after_failures: 3, expires_at: null },
    enabled: false,
    state: "off",
  });
  await setup(page, { automations: [loop] });
  await page.goto(automationEditPath("rv1"));
  await expect(wizardHeading(page)).toHaveText("When does it run?");
  await wizardNext(page);
  await expect(page.getByTestId("events-loop")).toContainText("A review loop picks its own events");
  await expect(page.getByTestId("events-advanced")).toHaveCount(0);
});

test("an explicitly empty text value is a value: a rename saves is, is not and one of with their empty strings", async ({ page }) => {
  const when = [
    { path: "action", op: "one_of", value: ["", "opened"] },
    { path: "pull_request.head.ref", op: "is", value: "" },
    { path: "repository.full_name", op: "is_not", value: "" },
  ];
  const { fake } = await setup(page, {
    automations: [
      automation({
        id: "a7",
        name: "Empty values",
        trigger: { kind: "webhook", webhook_id: SHOP.id, when },
        action: {
          kind: "start_session",
          engine: "claude-api",
          model: null,
          folder: "/repo",
          bypass: false,
          message: { text: "A delivery came in" },
        },
        enabled: false,
        state: "off",
      }),
    ],
  });
  await page.goto(automationEditPath("a7"));
  await wizardNext(page);
  await expect(wizardHeading(page)).toHaveText("Which events start it?");
  // Its own conditions: Advanced is open, the empty values shown as empty, nothing held.
  await expect(page.getByTestId("events-advanced")).toHaveAttribute("open", "");
  await expect(page.getByTestId("cond-row")).toHaveCount(3);
  await expect(page.getByTestId("cond-row").nth(0).getByTestId("cond-value")).toHaveCount(2);
  await expect(page.getByTestId("cond-row").nth(0).getByTestId("cond-value").nth(0)).toHaveValue("");
  await expect(page.getByTestId("step-problems")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Next", exact: true })).toBeEnabled();
  await toReview(page);
  await page.getByTestId("automation-name").fill("Empty values, renamed");
  await expect(page.getByTestId("editor-problems")).toHaveCount(0);
  await page.getByTestId("editor-save").click();
  await expect(page.getByTestId("editor-notice")).toContainText("Saved");
  const sent = lastWrite(fake, "PATCH");
  expect(sent.name).toBe("Empty values, renamed");
  expect(sent.trigger).toEqual({ kind: "webhook", webhook_id: SHOP.id, when });
});

const LEGACY_LOOP = automation({
  id: "rv1",
  name: "Review loop · example-org/shop",
  trigger: { kind: "webhook", scheme: "forgejo" },
  action: {
    kind: "forge_review_loop",
    project_id: "p1",
    repo: "example-org/shop",
    reviewer: { specialist_id: "shop-reviewer", version: 3 },
    reviewer_login: "shop-reviewer-bot",
    legs: { issues: true, pulls: true },
    max_rounds: { issue: 5, pr: 4 },
    return_to_origin: true,
  },
  policy: { concurrency: "allow", max_concurrent: 3, max_runs_per_day: 48, pause_after_failures: 3, expires_at: null },
  enabled: false,
  state: "off",
});

for (const [card, title] of [
  ["manual", "Only when I press Run now"],
  ["schedule", "On a schedule"],
] as const)
  test(`a review loop's own webhook left for ${card}: What offers that trigger's actions, and switching back restores it`, async ({ page }) => {
    const { fake } = await setup(page, { automations: [LEGACY_LOOP] });
    await page.goto(automationEditPath("rv1"));
    await expect(page.getByTestId("trigger-webhook-legacy")).toBeVisible();
    await page.getByTestId(`trigger-kind-${card}`).click();
    await expect(page.getByTestId("trigger-kind").locator('[aria-pressed="true"]')).toContainText(title);
    await page.getByRole("button", { name: "Next", exact: true }).click();
    // Step 2 is skipped; What offers what this trigger starts — never only the loop.
    await expect(wizardHeading(page)).toHaveText("What does it do?");
    await expect(page.getByTestId("action-kind").getByRole("button")).toHaveText([
      /Start a mission/,
      /Start a session/,
      /Message a session/,
      /Tell HQ/,
    ]);
    await expect(page.getByTestId("action-kind-start_mission")).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByTestId("action-why")).not.toContainText("own Forgejo webhook");
    await expect(page.getByTestId("step-problems")).not.toContainText("own webhook");
    await page.getByTestId("mission-project").selectOption(PROJECT.id);
    await page.getByTestId("message-text").fill("Audit the dependencies");
    await expect(page.getByRole("button", { name: "Next", exact: true })).toBeEnabled();
    await wizardNext(page);
    await expect(wizardHeading(page)).toHaveText("Limits");

    // Back to the webhook: its own webhook and its loop again, nothing converted.
    await backTo(page, "When does it run?");
    await page.getByTestId("trigger-kind-webhook").click();
    await expect(page.getByTestId("trigger-webhook-legacy")).toBeVisible();
    await expect(page.getByTestId("webhook-panel")).toBeVisible();
    await toStep(page, "What does it do?");
    await expect(page.getByTestId("action-kind").getByRole("button")).toHaveText([/Review loop/]);
    await toReview(page);
    await page.getByTestId("automation-name").fill("Review loop, renamed");
    await page.getByTestId("editor-save").click();
    await expect(page.getByTestId("editor-notice")).toContainText("Saved");
    expect(lastWrite(fake, "PATCH").trigger).toEqual({ kind: "webhook", scheme: "forgejo" });
  });

test("every step: 44 px targets, the heading takes focus, nothing clipped at 390 px", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await setup(page);
  await page.goto(AUTOMATION_NEW_PATH);
  const root = '[data-testid="automation-editor"]';
  await expect(wizardHeading(page)).toHaveText("When does it run?");
  await targetsAndOverflow(page, root);
  await pickWebhook(page, SHOP.id);
  await expect(page.getByTestId("shared-webhook-panel")).toBeVisible();
  await targetsAndOverflow(page, root);
  await wizardNext(page);
  await expect(wizardHeading(page)).toBeFocused();
  await page.getByTestId("events-advanced").locator("summary").click();
  await page.getByTestId("cond-add").click();
  await page.getByTestId("cond-path").fill("number");
  await page.getByTestId("cond-value").fill("4");
  await page.getByTestId("cond-op").selectOption("one_of");
  await page.getByTestId("cond-value-add").click();
  await page.getByTestId("cond-value").nth(1).fill("5");
  await targetsAndOverflow(page, root);
  await wizardNext(page);
  await expect(wizardHeading(page)).toBeFocused();
  await page.getByTestId("action-kind").getByRole("button", { name: /Start a session/ }).click();
  await page.getByTestId("session-engine").selectOption("claude-api");
  await page.getByTestId("message-mode").selectOption("template");
  await page.getByTestId("message-template").selectOption(TEMPLATE.id);
  await field(page, "reviewer").getByTestId("field-source").getByRole("button", { name: "From the delivery" }).click();
  await field(page, "reviewer").getByTestId("map-type").selectOption("choice");
  await targetsAndOverflow(page, root);
});

test("captures each step of the wizard", async ({ page }, info) => {
  await setup(page, {
    automations: [
      automation({
        id: "a9",
        name: "Review every PR",
        trigger: {
          kind: "webhook",
          webhook_id: SHOP.id,
          when: [
            { path: "action", op: "one_of", value: ["opened", "reopened", "synchronized"] },
            { path: "pull_request.draft", op: "is_false" },
            { path: "review", op: "missing" },
          ],
        },
        action: {
          kind: "start_session",
          engine: "claude-api",
          model: null,
          folder: "/repo",
          bypass: false,
          message: {
            template_id: TEMPLATE.id,
            values: {},
            from_delivery: { pr_number: { path: "number", type: "number" } },
          },
        },
        enabled: false,
        state: "off",
        scope_lines: ["Starts when the webhook “shop repo” delivers: action is one of opened, reopened, synchronized"],
      }),
    ],
  });
  await page.goto(automationEditPath("a9"));
  for (const name of ["when", "events", "what", "limits", "review"]) {
    await expect(wizardHeading(page)).toBeVisible();
    if (name === "limits") await page.getByTestId("limits-more").locator("summary").click();
    if (name === "events") await page.getByTestId("events-advanced").locator("summary").click();
    await page.screenshot({ path: info.outputPath(`wizard-${name}.png`), fullPage: true });
    if (name !== "review") await wizardNext(page);
  }
  await toStep(page, "Review and enable");
});
