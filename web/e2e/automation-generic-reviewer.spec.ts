/** Reviewing every pull request on the generic path (#2222), in a real browser on the desktop AND
 *  mobile projects: step 2's "Run once per" and step 3's "Run as a specialist", and the body the
 *  page SENDS through the stateful fake of `/api/automations`. What jsdom cannot tell: the fields'
 *  44 px targets and no clipping at 390 px, the native select's options after the agent changes,
 *  and that the bypass box is really disabled while a specialist is chosen. */
import { expect, test, type Page } from "@playwright/test";

import { AUTOMATION_NEW_PATH } from "../src/lib/routes";
import { mockAutomations, targetsAndOverflow, toReview, wizardHeading, wizardNext } from "./automations";
import { mockRoster } from "./roster";
import { PR_SHAPE, mockWebhooks, webhook } from "./webhooks";

type Json = Record<string, unknown>;

const REVISION = ["repository.full_name", "pull_request.number", "pull_request.head.sha"];

const REPO_HOOK = webhook({
  id: "3d4e5f6a7b8c9d0e",
  name: "chrononote",
  last_delivery_at: Math.floor(Date.now() / 1000) - 60,
  last_event: "pull request synchronized",
  last_shape: [
    ...PR_SHAPE,
    { path: "pull_request.number", type: "number" },
    { path: "pull_request.head.sha", type: "text" },
  ],
});

/** Two specialists on two API agents, and an archived one that is never offered. */
function spec(id: string, engine: string, version: number, archived = false) {
  return {
    id,
    revision: 1,
    archived,
    current: {
      version,
      title: id === "org-reviewer" ? "Org reviewer" : "Docs reviewer",
      summary: "",
      skills: ["org-pr-review"],
      engine,
      model: "model-pro",
      effort: "high",
      capacity: 2,
      created_at: 1,
    },
    grade: null,
    live: 0,
    skills_missing: [],
    skills_outdated: [],
  };
}

async function setup(page: Page, specialists = [
  spec("org-reviewer", "claude-api", 3),
  spec("docs-reviewer", "opencode-api", 1),
  spec("old-reviewer", "claude-api", 2, true),
]) {
  const fake = await mockAutomations(page, {
    engines: [
      { id: "claude-api", ok: true, reason: "" },
      { id: "opencode-api", ok: true, reason: "" },
    ],
  });
  await mockWebhooks(page, [REPO_HOOK]);
  await page.route("**/api/specialists", (r) =>
    r.fulfill({ json: { specialists } }),
  );
  await mockRoster(page);
  await page.route("**/api/fs/dirs**", (r) => r.fulfill({ json: { home: "/repo", path: "/repo", dirs: [] } }));
  return fake;
}

test("once per PR revision, as a pinned specialist: the fields, their checks, and what is sent", async ({ page }) => {
  const fake = await setup(page);
  await page.goto(AUTOMATION_NEW_PATH);
  await page.getByTestId("trigger-kind").getByRole("button", { name: /When a webhook delivers/ }).click();
  await page.getByTestId("trigger-webhook").selectOption(REPO_HOOK.id);
  await wizardNext(page);
  await expect(wizardHeading(page)).toHaveText("Which events start it?");

  // Step 2: "Run once per". The suggestion fills the three revision fields from the delivery.
  const once = page.getByTestId("once-per");
  await expect(once.getByRole("heading", { name: "Run once per (optional)" })).toBeVisible();
  await expect(page.getByTestId("once-path")).toHaveCount(0);
  await page.getByTestId("once-revision").click();
  for (const [i, p] of REVISION.entries()) await expect(page.getByTestId("once-path").nth(i)).toHaveValue(p);
  await expect(page.getByTestId("once-revision")).toHaveCount(0);
  // A list field is refused here, as the server refuses it; removing it clears the problem.
  await page.getByTestId("once-add").click();
  await page.getByTestId("once-path").nth(3).fill("pull_request.labels[].name");
  await expect(page.getByRole("status").filter({ hasText: "a value comes from one field, not a list" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Next", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "Remove pull_request.labels[].name" }).click();
  await expect(page.getByTestId("once-add")).toBeEnabled();
  await targetsAndOverflow(page, '[data-testid="once-per"]');
  await wizardNext(page);

  // Step 3: "Run as a specialist" lists only the specialists on the chosen agent.
  await expect(wizardHeading(page)).toHaveText("What does it do?");
  await page.getByTestId("action-kind").getByRole("button", { name: /Start a session/ }).click();
  const specialist = page.getByTestId("session-specialist");
  await page.getByTestId("session-engine").selectOption("opencode-api");
  await expect(specialist.locator("option")).toHaveText(["None, the agent’s own defaults", /^docs-reviewer|Docs reviewer v1/]);
  await page.getByTestId("session-engine").selectOption("claude-api");
  await expect(specialist.locator("option")).toHaveCount(2); // none + Org reviewer; never the archived one
  await specialist.selectOption("org-reviewer");
  await expect(page.getByTestId("specialist-note")).toContainText("pinned to this version");
  // Switching to an agent the specialist doesn't run on clears the choice.
  await page.getByTestId("session-engine").selectOption("opencode-api");
  await expect(specialist).toHaveValue("");
  await page.getByTestId("session-engine").selectOption("claude-api");
  await specialist.selectOption("org-reviewer");
  await targetsAndOverflow(page, '[data-testid="session-specialist"]');

  await page.getByRole("button", { name: "Choose…" }).click();
  await page.getByRole("dialog").getByRole("button", { name: /^Select/ }).click();
  await page.getByTestId("message-text").fill("Review the pull request");
  // A specialist's session may skip permission prompts and close when done (#2405).
  const more = page.locator("details").filter({ has: page.getByTestId("session-bypass") });
  if (!(await more.getAttribute("open"))) await more.locator("summary").click();
  await expect(page.getByTestId("session-bypass")).toBeEnabled();
  await expect(page.getByTestId("session-close-when-done")).toBeEnabled();

  await toReview(page);
  await page.getByTestId("automation-name").fill("Review every PR on chrononote");
  await page.getByTestId("editor-save").click();
  await expect(page.getByTestId("editor-notice")).toContainText("is off until you enable it");
  const sent = [...fake.requests].reverse().find((r) => r.method === "POST")!.body as Json;
  expect(sent.trigger).toEqual({ kind: "webhook", webhook_id: REPO_HOOK.id, once_per: REVISION });
  expect(sent.action).toMatchObject({
    kind: "start_session",
    engine: "claude-api",
    bypass: false,
    specialist: { specialist_id: "org-reviewer", version: 3 },
  });
});

test("without a choice neither key is sent, and an agent with no specialist says so", async ({ page }) => {
  // Only one specialist, on claude-api: opencode-api has none to offer.
  const fake = await setup(page, [spec("org-reviewer", "claude-api", 3)]);
  await page.goto(AUTOMATION_NEW_PATH);
  await page.getByTestId("trigger-kind").getByRole("button", { name: /When a webhook delivers/ }).click();
  await page.getByTestId("trigger-webhook").selectOption(REPO_HOOK.id);
  await wizardNext(page);
  await wizardNext(page);
  await page.getByTestId("action-kind").getByRole("button", { name: /Start a session/ }).click();
  await page.getByTestId("session-engine").selectOption("opencode-api");
  await expect(page.getByTestId("session-specialist")).toBeDisabled();
  await expect(page.getByTestId("session-specialist").locator("option")).toHaveText(["None available"]);
  await expect(page.getByTestId("specialist-note")).toContainText("No specialist runs on this agent");
  await page.getByTestId("session-engine").selectOption("claude-api");
  await page.getByRole("button", { name: "Choose…" }).click();
  await page.getByRole("dialog").getByRole("button", { name: /^Select/ }).click();
  await page.getByTestId("message-text").fill("Look at it");
  await toReview(page);
  await page.getByTestId("automation-name").fill("Plain session");
  await page.getByTestId("editor-save").click();
  await expect(page.getByTestId("editor-notice")).toContainText("is off until you enable it");
  const sent = [...fake.requests].reverse().find((r) => r.method === "POST")!.body as Json;
  expect(sent.trigger).toEqual({ kind: "webhook", webhook_id: REPO_HOOK.id });
  expect(sent.action).not.toHaveProperty("specialist");
});

test("a failed specialist read says so, instead of “none on this agent”", async ({ page }) => {
  await setup(page);
  // Registered after setup's route: the later route wins.
  await page.route("**/api/specialists", (r) => r.fulfill({ status: 500, json: { detail: "down" } }));
  await page.goto(AUTOMATION_NEW_PATH);
  await page.getByTestId("trigger-kind").getByRole("button", { name: /When a webhook delivers/ }).click();
  await page.getByTestId("trigger-webhook").selectOption(REPO_HOOK.id);
  await wizardNext(page);
  await wizardNext(page);
  await page.getByTestId("action-kind").getByRole("button", { name: /Start a session/ }).click();
  await page.getByTestId("session-engine").selectOption("claude-api");
  await expect(page.getByTestId("specialist-note")).toContainText("Couldn’t read your specialists");
  await expect(page.getByTestId("specialist-note")).not.toContainText("No specialist runs on this agent");
});
