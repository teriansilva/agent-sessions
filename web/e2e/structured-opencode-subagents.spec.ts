import { expect, test, type Page } from "@playwright/test";
import { ID, mockStructuredGuidance } from "./structuredGuidanceFixture";

// #1891: an opencode API session's `task` subagents are the same background-strip rows as
// Claude's tasks (#1726), and Send now while one runs is the same choice (#1644): Wait, or Stop
// all and send naming it. The snapshot is what the opencode worker journals: description and
// subagent type, no progress report (ACP carries none).
type Json = Record<string, unknown>;
const ENGINE = "opencode-api";
const SEND_NOW = new RegExp(`/api/structured/sessions/${ENGINE}(?::|%3A)${ID}/send-now$`);
const now = () => Math.floor(Date.now() / 1000);
const SUBAGENTS = [
  { task_id: "call_ab8ebc9d508848df9155a417", state: "running", description: "Research flights to Alps hubs",
    task_type: "general", started_at: now() - 6 * 60, updated_at: now() - 6 * 60 },
  { task_id: "call_98ffc5de975b4c158e57ba14", state: "running", description: "Research overland rail and bus",
    task_type: "general", started_at: now() - 5 * 60, updated_at: now() - 5 * 60 },
];

async function opencode(page: Page, theme = "dark") {
  const fixture = await mockStructuredGuidance(page, ENGINE, "interrupt", theme);
  Object.assign(fixture.state.native as Json, {
    worker: "33333333-2222-4333-8444-555555555555",
    background_active: true,
    background_tasks: SUBAGENTS.map((t) => ({ ...t, source: "engine" })),
    send_now_state: "would_stop_background",
  });
  return fixture;
}

const strip = (page: Page) => page.getByTestId("structured-background-work");
const toggle = (page: Page) => page.getByTestId("structured-background-strip").locator("> button");
const choice = (page: Page) => page.getByTestId("structured-stop-choice");
const sendNow = (page: Page) =>
  page.getByTestId("structured-queued").getByRole("button", { name: "Send now", exact: true });

for (const theme of ["dark", "light"]) {
  test(`#1891: running opencode subagents are strip rows with their subagent type (${theme})`, async ({ page }, testInfo) => {
    await opencode(page, theme);
    await page.goto(`/s/${ENGINE}/${ID}`);
    await expect(strip(page)).toHaveCount(1);
    await expect(toggle(page)).toContainText("2 agent tasks running");
    await expect(toggle(page)).toContainText("Research flights to Alps hubs");
    await toggle(page).click();
    const rows = page.getByTestId("structured-background-task");
    await expect(rows).toHaveCount(2);
    await expect(rows.first()).toBeVisible();
    await expect(rows.first()).toContainText("Research flights to Alps hubs");
    await expect(rows.first()).toContainText("Running");
    await rows.first().getByText("Details").click();
    await expect(rows.first()).toContainText("general");
    await page.screenshot({ path: testInfo.outputPath(`opencode-subagents-${theme}.png`) });
  });
}

test("#1891: Send now while an opencode subagent runs offers Wait or Stop all and send, never a silent cancel", async ({ page }) => {
  const fixture = await opencode(page);
  const posts: Json[] = [];
  await page.route(SEND_NOW, (r) => {
    const body = r.request().postDataJSON();
    posts.push(body);
    if (!body.stop_all) {
      return r.fulfill({ status: 409, json: {
        detail: "Send now would also stop this agent's background agents here; the message is sent when the current turn ends",
        reason: "would_stop_background" } });
    }
    fixture.state.revision++;
    return r.fulfill({ status: 202, json: { handoff: "sent", operation_id: body.operation_id } });
  });
  await page.goto(`/s/${ENGINE}/${ID}`);
  await sendNow(page).click();
  await expect(choice(page)).toBeVisible();
  await expect(page.getByTestId("structured-stop-choice-tasks").getByRole("listitem")).toHaveText([
    "Research flights to Alps hubs", "Research overland rail and bus"]);
  expect(posts).toHaveLength(1);
  expect(posts[0].stop_all).toBeUndefined(); // the first press never stops a subagent
  await choice(page).getByRole("button", { name: "Stop all and send" }).click();
  await expect.poll(() => posts.length).toBe(2);
  expect(posts[1]).toMatchObject({
    stop_all: { worker: "33333333-2222-4333-8444-555555555555",
      task_ids: SUBAGENTS.map((t) => t.task_id) },
  });
});
