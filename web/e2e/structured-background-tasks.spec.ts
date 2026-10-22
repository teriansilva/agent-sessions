import { expect, test, type Page } from "@playwright/test";

import { ID, mockStructuredGuidance } from "./structuredGuidanceFixture";
import { TOUCH_FLOOR } from "./touchTarget";

// #1461: the worker knows which background tasks are alive. Show its reported activity,
// including a reported wait, without guessing a task's state from a quiet transcript.
interface BackgroundTask {
  task_id: string;
  state: string;
  description?: string;
  task_type?: string;
  last_tool_name?: string;
  summary?: string;
  started_at: number;
  updated_at: number;
}

const TITLE = "RTM build 39991";
const WAITING = "Waiting for RTM deployment";
const TASK_ID = "rtm-build-39991";

async function backgroundTasks(page: Page, theme = "dark", tasks?: BackgroundTask[]) {
  const fixture = await mockStructuredGuidance(page, "claude-api", "interrupt", theme);
  const now = Math.floor(Date.now() / 1000);
  const reported = tasks ?? [{
    task_id: TASK_ID,
    state: "running",
    description: TITLE,
    task_type: "local_bash",
    last_tool_name: "Bash",
    summary: WAITING,
    started_at: now - 72,
    updated_at: now - 4,
  }];
  fixture.state.state = "idle";
  fixture.state.active_turn = "";
  fixture.state.turns[0].state = "completed";
  Object.assign(fixture.state.native, {
    background_active: true,
    background_turn: null,
    background_tasks: reported,
  });
  await page.goto(`/s/claude-api/${ID}`);
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  return { ...fixture, tasks: reported };
}

const activity = (page: Page) => page.getByTestId("structured-background-work");
const taskRow = (page: Page) => page.getByTestId("structured-background-task");

for (const theme of ["dark", "light"]) {
  test(`#1461: named background task shows its reported wait while its state stays Running (${theme})`, async ({ page }, testInfo) => {
    await backgroundTasks(page, theme);
    // This also records the old bare row when run against the pre-feature build.
    await page.screenshot({ path: testInfo.outputPath(`background-observed-${theme}.png`) });
    const task = taskRow(page);
    await expect(task).toHaveCount(1);
    await expect(task.getByText(TITLE, { exact: true })).toBeVisible();
    await expect(task.getByText("Running", { exact: true })).toBeVisible();
    await expect(task.getByText(WAITING, { exact: true }).filter({ visible: true }).first()).toBeVisible();
    // Waiting is the agent's reported summary, never an invented state or progress estimate.
    await expect(task.getByText("Waiting", { exact: true })).toHaveCount(0);
    await expect(task.getByText(/\d+%/)).toHaveCount(0);
    const details = task.locator("details");
    await expect(details).not.toHaveAttribute("open", "");
    await expect(task.getByRole("button", { name: /^interrupt/i })).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath(`background-task-${theme}.png`) });
  });
}

test("#1461: reported task progress updates live and keyboard-open details survive snapshot reads", async ({ page }, testInfo) => {
  const fixture = await backgroundTasks(page);
  const task = taskRow(page);
  await expect(task).toHaveCount(1);
  const details = task.locator("details");
  const disclosure = details.locator("summary");
  await expect(disclosure).toContainText("Details");
  expect((await disclosure.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await disclosure.focus();
  await page.keyboard.press("Enter");
  await expect(details).toHaveAttribute("open", "");
  // Details add task facts/timestamps; they do not repeat the description and latest report.
  await expect(task.getByText(TITLE, { exact: true }).filter({ visible: true })).toHaveCount(1);
  await expect(task.getByText(WAITING, { exact: true }).filter({ visible: true })).toHaveCount(1);
  await expect(task.getByText(TASK_ID, { exact: true })).toBeVisible();
  await expect(task.getByText("Bash", { exact: true }).filter({ visible: true }).first()).toBeVisible();
  await expect(details.getByText("First observed", { exact: true })).toBeVisible();
  await expect(details.getByText("Last reported", { exact: true })).toBeVisible();
  await expect(details.locator(`time[datetime="${new Date(fixture.tasks[0].started_at * 1000).toISOString()}"]`)).toBeVisible();
  await expect(details.locator(`time[datetime="${new Date(fixture.tasks[0].updated_at * 1000).toISOString()}"]`)).toBeVisible();

  fixture.tasks[0].summary = "RTM deployment finished; checking the import extraction";
  fixture.tasks[0].last_tool_name = "WebFetch";
  fixture.tasks[0].updated_at = Math.floor(Date.now() / 1000);
  fixture.state.revision++;
  await expect(task.getByText(fixture.tasks[0].summary, { exact: true }).filter({ visible: true })).toHaveCount(1);
  await expect(task.getByText("WebFetch", { exact: true }).filter({ visible: true }).first()).toBeVisible();
  await expect(details.locator(`time[datetime="${new Date(fixture.tasks[0].updated_at * 1000).toISOString()}"]`)).toBeVisible();
  await expect(details).toHaveAttribute("open", "");
  await expect(disclosure).toBeFocused();
  await page.screenshot({ path: testInfo.outputPath("background-task-details-updated.png") });
  await page.keyboard.press("Space");
  await expect(details).not.toHaveAttribute("open", "");
  await expect(task.getByText(TASK_ID, { exact: true })).toBeHidden();
});

test("#1461: background task activity appears once before the first queued message", async ({ page }) => {
  const fixture = await backgroundTasks(page);
  fixture.state.turns.push({ ...fixture.state.turns[1],
    turn_id: "33333333-2222-4333-8444-666666666666",
    operation_id: "33333333-2222-4333-8444-666666666666",
    text: "Then summarise them.",
  });
  fixture.state.revision++;
  await expect(page.getByTestId("structured-turn")).toHaveCount(3);
  await expect(activity(page)).toHaveCount(1);
  await expect(taskRow(page)).toHaveCount(1);
  const previous = page.getByTestId("structured-turn").filter({ hasText: "Check startup." });
  const firstQueued = page.getByTestId("structured-turn").filter({ hasText: "Only inspect the files." });
  const previousBox = (await previous.boundingBox())!;
  const activityBox = (await activity(page).boundingBox())!;
  const queuedBox = (await firstQueued.boundingBox())!;
  expect(activityBox.y).toBeGreaterThanOrEqual(previousBox.y + previousBox.height - 1);
  expect(activityBox.y + activityBox.height).toBeLessThanOrEqual(queuedBox.y + 1);
});

test("#1461: background task details and focus survive first queued message insertion and removal", async ({ page }, testInfo) => {
  const fixture = await backgroundTasks(page);
  const firstQueued = { ...fixture.state.turns[1] };
  fixture.state.turns = [fixture.state.turns[0]];
  fixture.state.revision++;
  await expect(page.getByTestId("structured-turn")).toHaveCount(1);
  const details = taskRow(page).locator("details");
  const disclosure = details.locator("summary");
  await disclosure.focus();
  await page.keyboard.press("Enter");
  await expect(details).toHaveAttribute("open", "");
  await expect(disclosure).toBeFocused();
  await page.screenshot({ path: testInfo.outputPath("background-details-no-queue.png") });

  // The same worker/task moves from the conversation's end to before its first queued turn.
  // It must keep its existing DOM disclosure/focus through that placement change.
  fixture.state.turns.push(firstQueued);
  fixture.state.revision++;
  await expect(page.getByTestId("structured-turn")).toHaveCount(2);
  await page.screenshot({ path: testInfo.outputPath("background-details-first-queued.png") });
  await expect(details).toHaveAttribute("open", "");
  await expect(disclosure).toBeFocused();

  fixture.state.turns = [fixture.state.turns[0]];
  fixture.state.revision++;
  await expect(page.getByTestId("structured-turn")).toHaveCount(1);
  await expect(details).toHaveAttribute("open", "");
  await expect(disclosure).toBeFocused();
  await page.screenshot({ path: testInfo.outputPath("background-details-queue-removed.png") });
});

test("#1461: long task descriptions and expanded reports fit the viewport", async ({ page }, testInfo) => {
  const now = Math.floor(Date.now() / 1000);
  const description = `Inspect deployment ${"rtm-build-39991/".repeat(35)}`;
  const summary = `Waiting for RTM deployment\n${"reported-output-with-no-spaces".repeat(40)}`;
  await backgroundTasks(page, "dark", [{
    task_id: TASK_ID, state: "running", description, summary,
    task_type: "local_bash", last_tool_name: "Bash",
    started_at: now - 72, updated_at: now - 4,
  }]);
  const task = taskRow(page);
  await expect(task).toHaveCount(1);
  await task.locator("details > summary").click();
  await expect(task.locator("details")).toHaveAttribute("open", "");
  await expect(task.getByText(TASK_ID, { exact: true })).toBeVisible();
  await expect(task).toContainText(description);
  await expect(task).toContainText(summary);
  const viewport = page.viewportSize()!;
  const box = (await task.boundingBox())!;
  expect(box.x).toBeGreaterThanOrEqual(-1);
  expect(box.x + box.width).toBeLessThanOrEqual(viewport.width + 1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
  // Details may scroll internally, but their content cannot stretch the transcript sideways.
  const horizontalOverflow = await task.evaluate((el) => el.scrollWidth - el.clientWidth);
  expect(horizontalOverflow).toBeLessThanOrEqual(1);
  await page.screenshot({ path: testInfo.outputPath("background-task-long-details.png") });
});

test("#1461: worker exit removes live task activity even when its last journal flag remains active", async ({ page }) => {
  const fixture = await backgroundTasks(page);
  await expect(taskRow(page)).toHaveCount(1);
  // Worker exit writes no new journal event: lifecycle polling must re-read equal revisions.
  fixture.state.native.worker = "";
  await expect(activity(page)).toHaveCount(0);
  await expect(taskRow(page)).toHaveCount(0);
});

test("#1461: simultaneous tasks keep their own reported state and progress", async ({ page }, testInfo) => {
  const now = Math.floor(Date.now() / 1000);
  const pausedTitle = "Collect RTM smoke-test report";
  const pausedSummary = "Pending deployment approval";
  const fixture = await backgroundTasks(page, "dark", [
    {
      task_id: TASK_ID, state: "running", description: TITLE, task_type: "local_bash",
      last_tool_name: "Bash", summary: WAITING,
      started_at: now - 72, updated_at: now - 4,
    },
    {
      task_id: "rtm-smoke-report", state: "paused", description: pausedTitle,
      summary: pausedSummary, started_at: now - 30, updated_at: now - 8,
    },
  ]);
  await expect(taskRow(page)).toHaveCount(2);
  const running = taskRow(page).filter({ hasText: TITLE });
  const paused = taskRow(page).filter({ hasText: pausedTitle });
  await expect(running.getByText("Running", { exact: true })).toBeVisible();
  await expect(running.getByText(WAITING, { exact: true })).toBeVisible();
  await expect(paused.getByText("Paused", { exact: true })).toBeVisible();
  await expect(paused.getByText(pausedSummary, { exact: true })).toBeVisible();
  await expect(paused.getByText("Waiting", { exact: true })).toHaveCount(0);

  fixture.tasks[0].summary = "Downloading the RTM build artifact";
  fixture.tasks[0].updated_at = Math.floor(Date.now() / 1000);
  fixture.state.revision++;
  await expect(running.getByText(fixture.tasks[0].summary, { exact: true })).toBeVisible();
  await expect(paused.getByText("Paused", { exact: true })).toBeVisible();
  await expect(paused.getByText(pausedSummary, { exact: true })).toBeVisible();
  await expect(paused).not.toContainText(fixture.tasks[0].summary);
  await page.screenshot({ path: testInfo.outputPath("background-multiple-tasks.png") });
});

test("#1461: a task reported without descriptive metadata retains the generic fallback", async ({ page }) => {
  const now = Math.floor(Date.now() / 1000);
  await backgroundTasks(page, "dark", [{
    task_id: "unknown-background-task", state: "running",
    started_at: now - 20, updated_at: now - 5,
  }]);
  await expect(activity(page)).toContainText("A background task is running");
  await expect(taskRow(page)).toHaveCount(0);
  await expect(activity(page).getByText("Waiting", { exact: true })).toHaveCount(0);
});

test("#1461: an unnamed task alongside named work keeps its native state inspectable", async ({ page }) => {
  const now = Math.floor(Date.now() / 1000);
  const unknownId = "metadata-not-yet-reported";
  await backgroundTasks(page, "dark", [
    {
      task_id: TASK_ID, state: "running", description: TITLE,
      started_at: now - 72, updated_at: now - 4,
    },
    {
      task_id: unknownId, state: "pending",
      started_at: now - 20, updated_at: now - 5,
    },
  ]);
  await expect(taskRow(page)).toHaveCount(2);
  await expect(taskRow(page).first().getByText(TITLE, { exact: true })).toBeVisible();
  const unnamed = taskRow(page).nth(1);
  await expect(unnamed.getByText("Pending", { exact: true })).toBeVisible();
  await expect(unnamed.getByText("Waiting", { exact: true })).toHaveCount(0);
  await unnamed.locator("details > summary").click();
  await expect(unnamed.getByText(unknownId, { exact: true })).toBeVisible();
});

test("#1461: faster background polling preserves the idle Send snapshot revision", async ({ page }) => {
  const fixture = await backgroundTasks(page);
  fixture.state.turns = [fixture.state.turns[0]];
  fixture.tasks[0].summary = "Background poll received; RTM deployment is still running";
  fixture.state.revision++;
  // Observing changed task text proves the pane read this revision during background-only work.
  await expect(taskRow(page).getByText(fixture.tasks[0].summary, { exact: true })).toBeVisible();
  await expect(page.getByTestId("structured-turn")).toHaveCount(1);
  const submissions: Record<string, unknown>[] = [];
  await page.route(new RegExp(`/api/structured/sessions/claude-api(?::|%3A)${ID}/turns$`), (route) => {
    submissions.push(route.request().postDataJSON());
    return route.fulfill({ status: 202, json: { state: "queued" } });
  });
  const text = "Check the import when deployment finishes.";
  await page.getByRole("textbox").first().fill(text);
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => submissions.length).toBe(1);
  // Background observation is faster, but this remains an idle operator submission. Widening
  // the turn's active flag would silently drop expected_revision from this request.
  expect(submissions[0]).toMatchObject({ text, expected_revision: fixture.state.revision });
  expect(submissions[0].operation_id).toMatch(/^[0-9a-f-]{36}$/);
});

test("#1461: an older worker with no task details retains the generic background fallback", async ({ page }) => {
  const fixture = await mockStructuredGuidance(page, "claude-api", "interrupt");
  fixture.state.state = "idle";
  fixture.state.active_turn = "";
  fixture.state.turns[0].state = "completed";
  fixture.state.native.background_active = true;
  await page.goto(`/s/claude-api/${ID}`);
  await expect(activity(page)).toContainText("A background task is running");
  await expect(activity(page)).toContainText("nothing to interrupt");
  await expect(taskRow(page)).toHaveCount(0);
});
