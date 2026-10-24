import { expect, test, type Page } from "@playwright/test";
import { ACTIVE, ID, QUEUED, mockStructuredGuidance } from "./structuredGuidanceFixture";
import { TOUCH_FLOOR } from "./touchTarget";

// #1644: Send now on Claude falls back to a Stop, which would also end the agent's background
// tasks. That refusal is a CHOICE, not a dead end: Wait (the queue, as always) or Stop all and
// send, naming every task it ends; a change since the prompt asks again. Before sending, the
// composer says when Send now would only queue. A queued message shows how long it has waited.
type Json = Record<string, unknown>;
const SEND_NOW = new RegExp(`/api/structured/sessions/claude-api(?::|%3A)${ID}/send-now$`);
const TASKS = [
  { task_id: "bvitest01", state: "running", description: "vitest run --maxWorkers=4",
    started_at: Date.parse("2026-10-12T07:20:00Z") / 1000, updated_at: Date.parse("2026-10-12T07:30:00Z") / 1000 },
  { task_id: "bmonitor2", state: "running", description: "CI watch #1599",
    started_at: Date.parse("2026-10-12T05:00:00Z") / 1000, updated_at: Date.parse("2026-10-12T07:30:00Z") / 1000 },
];

async function blocked(page: Page, theme = "dark", state = "would_stop_background") {
  const fixture = await mockStructuredGuidance(page, "claude-api", "interrupt", theme);
  Object.assign(fixture.state.native as Json, {
    worker: "33333333-2222-4333-8444-555555555555",
    background_active: true,
    background_tasks: TASKS.map((t) => ({ ...t })),
    send_now_state: state,
  });
  const posts: Json[] = [];
  return { ...fixture, posts };
}

const choice = (page: Page) => page.getByTestId("structured-stop-choice");
const sendNow = (page: Page) => page.getByTestId("structured-queued").getByRole("button", { name: "Send now", exact: true });

for (const theme of ["dark", "light"]) {
  test(`#1644: a refused Send now offers Wait or Stop all and send, naming the tasks (${theme})`, async ({ page }, testInfo) => {
    const { posts } = await blocked(page, theme);
    await page.route(SEND_NOW, (r) => {
      posts.push(r.request().postDataJSON());
      return r.fulfill({ status: 409, json: {
        detail: "Send now would also stop this agent's background agents here; the message is sent when the current turn ends",
        reason: "would_stop_background" } });
    });
    await page.goto(`/s/claude-api/${ID}`);
    await sendNow(page).click();
    await expect(choice(page)).toBeVisible();
    await expect(choice(page)).toContainText("Send now would stop background tasks");
    await expect(page.getByTestId("structured-stop-choice-tasks").getByRole("listitem")).toHaveText([
      "vitest run --maxWorkers=4", "CI watch #1599"]);
    await expect(page.getByTestId("structured-error")).toHaveCount(0); // a choice, not an error
    const wait = choice(page).getByRole("button", { name: "Wait — send after this turn" });
    const stop = choice(page).getByRole("button", { name: "Stop all and send" });
    for (const b of [wait, stop]) {
      const box = await b.boundingBox();
      expect(box!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR); // design §8
    }
    await page.screenshot({ path: testInfo.outputPath(`stop-choice-${theme}.png`) });
    await wait.click();
    await expect(choice(page)).toHaveCount(0);
    await expect(page.getByTestId("structured-queued")).toContainText("1st in queue");
    expect(posts).toHaveLength(1);
    expect(posts[0].stop_all).toBeUndefined(); // the first press never stops anything
  });
}

test("#1644: Stop all and send confirms the worker and the exact task ids", async ({ page }) => {
  const { posts, state } = await blocked(page);
  await page.route(SEND_NOW, (r) => {
    const body = r.request().postDataJSON();
    posts.push(body);
    if (!body.stop_all) {
      return r.fulfill({ status: 409, json: { detail: "would stop", reason: "would_stop_background" } });
    }
    (state.turns as Json[])[1].delivery = "interrupting";
    state.revision++;
    return r.fulfill({ status: 202, json: { handoff: "sent", operation_id: body.operation_id } });
  });
  await page.goto(`/s/claude-api/${ID}`);
  await sendNow(page).click();
  await choice(page).getByRole("button", { name: "Stop all and send" }).click();
  await expect.poll(() => posts.length).toBe(2);
  expect(posts[1]).toMatchObject({
    turn_id: ACTIVE,
    queued_turn_id: QUEUED,
    stop_all: { worker: "33333333-2222-4333-8444-555555555555", task_ids: ["bvitest01", "bmonitor2"] },
  });
  expect(posts[1].operation_id).not.toBe(posts[0].operation_id); // its own request
  await expect(choice(page)).toHaveCount(0);
  await expect(page.getByText("Waiting for the current response to stop · sends next")).toBeVisible();
});

test("#1644: tasks that changed since the prompt are shown and confirmed again", async ({ page }) => {
  const { posts, state } = await blocked(page);
  let confirms = 0;
  await page.route(SEND_NOW, (r) => {
    const body = r.request().postDataJSON();
    posts.push(body);
    if (!body.stop_all) {
      return r.fulfill({ status: 409, json: { detail: "would stop", reason: "would_stop_background" } });
    }
    confirms++;
    if (confirms === 1) {
      // One task finished and one started between the prompt and the click.
      (state.native as Json).background_tasks = [
        { ...TASKS[1] },
        { task_id: "be2esmoke", state: "running", description: "e2e smoke",
          started_at: Date.parse("2026-10-12T07:31:00Z") / 1000, updated_at: Date.parse("2026-10-12T07:31:00Z") / 1000 },
      ];
      state.revision++;
      return r.fulfill({ status: 409, json: { detail: "changed", reason: "inventory_changed" } });
    }
    return r.fulfill({ status: 202, json: { handoff: "sent", operation_id: body.operation_id } });
  });
  await page.goto(`/s/claude-api/${ID}`);
  await sendNow(page).click();
  await choice(page).getByRole("button", { name: "Stop all and send" }).click();
  await expect(choice(page)).toContainText("Background tasks changed");
  const items = page.getByTestId("structured-stop-choice-tasks").getByRole("listitem");
  await expect(items).toHaveText(["CI watch #1599", "+ e2e smoke", "vitest run --maxWorkers=4 (ended)"]);
  await choice(page).getByRole("button", { name: "Stop these 2 and send" }).click();
  await expect.poll(() => posts.length).toBe(3);
  expect((posts[2].stop_all as Json).task_ids).toEqual(["bmonitor2", "be2esmoke"]);
  await expect(choice(page)).toHaveCount(0);
});

for (const theme of ["dark", "light"]) {
  test(`#1644: before sending, the composer says Send now would only queue (${theme})`, async ({ page }, testInfo) => {
    await page.addInitScript(() => localStorage.setItem("agent-sessions.always-send-now", "1"));
    const { posts } = await blocked(page, theme);
    await page.route(SEND_NOW, (r) => { posts.push(r.request().postDataJSON()); return r.fulfill({ status: 500 }); });
    await page.goto(`/s/claude-api/${ID}`);
    const hint = page.getByTestId("structured-sendnow-hint");
    await expect(hint).toHaveText("Send now unavailable: the agent has 2 background tasks running. Send queues behind its current turn.");
    await expect(page.getByRole("textbox", { name: /Message/ })).toHaveAttribute("placeholder", /queues behind the agent’s current turn/);
    await page.screenshot({ path: testInfo.outputPath(`hint-${theme}.png`) });
    // Always send now never presses a Send now that would be refused, and never Stop all.
    await page.waitForTimeout(1500);
    expect(posts).toHaveLength(0);
  });
}

test("#1644: an unreported Send now state reads as unknown, not unavailable", async ({ page }) => {
  await blocked(page, "dark", "unknown");
  await page.goto(`/s/claude-api/${ID}`);
  await expect(page.getByTestId("structured-sendnow-hint")).toHaveAttribute("data-state", "unknown");
  await expect(page.getByTestId("structured-sendnow-hint")).toContainText("Send now status unknown");
});

test("#1644: a ready worker shows no hint", async ({ page }) => {
  await blocked(page, "dark", "ready");
  await page.goto(`/s/claude-api/${ID}`);
  await expect(page.getByTestId("structured-queued")).toBeVisible();
  await expect(page.getByTestId("structured-sendnow-hint")).toHaveCount(0);
});

test("#1644: a queued message shows how long it has waited, the same after a reload", async ({ page }) => {
  await blocked(page);
  await page.clock.setFixedTime(new Date("2026-10-12T10:49:00Z"));
  await page.goto(`/s/claude-api/${ID}`);
  await expect(page.getByTestId("structured-queued-since")).toHaveText("Queued for 3h 18m");
  await page.reload();
  await expect(page.getByTestId("structured-queued-since")).toHaveText("Queued for 3h 18m");
});

// Hermes on #1650: a turn or queued head that moved is a changed binding too. The choice is asked
// again for the message first in line NOW, or explains that nothing is left to stop for.
const SECOND = "44444444-2222-4333-8444-555555555555";

test("#1644: a new head since the prompt is confirmed again, for that message", async ({ page }) => {
  const { posts, state } = await blocked(page);
  const turns = state.turns as Json[];
  turns.push({ ...turns[1], turn_id: SECOND, operation_id: SECOND, text: "And keep the old breakpoint.",
    ts: Date.parse("2026-10-12T07:32:00Z") / 1000 });
  await page.route(SEND_NOW, (r) => {
    const body = r.request().postDataJSON();
    posts.push(body);
    if (!body.stop_all) return r.fulfill({ status: 409, json: { detail: "would stop", reason: "would_stop_background" } });
    if (posts.filter((p) => p.stop_all).length === 1) {
      // Another tab sent the first message meanwhile: the second is first in line now.
      turns.splice(1, 1);
      state.revision++;
      return r.fulfill({ status: 409, json: { detail: "only the first queued message can be sent now", reason: "inventory_changed" } });
    }
    return r.fulfill({ status: 202, json: { handoff: "sent", operation_id: body.operation_id } });
  });
  await page.goto(`/s/claude-api/${ID}`);
  await sendNow(page).first().click();
  await expect(choice(page)).toContainText("For “Only inspect the files.”");
  await choice(page).getByRole("button", { name: "Stop all and send" }).click();
  await expect(choice(page)).toContainText("Background tasks changed");
  await expect(choice(page)).toContainText("For “And keep the old breakpoint.”");
  await choice(page).getByRole("button", { name: "Stop these 2 and send" }).click();
  await expect.poll(() => posts.length).toBe(3);
  expect(posts[2]).toMatchObject({ queued_turn_id: SECOND, turn_id: ACTIVE });
});

for (const after of ["the turn ended", "nothing is waiting"] as const) {
  test(`#1644: when ${after} since the prompt, the choice closes and says why`, async ({ page }) => {
    const { posts, state } = await blocked(page);
    const turns = state.turns as Json[];
    await page.route(SEND_NOW, (r) => {
      const body = r.request().postDataJSON();
      posts.push(body);
      if (!body.stop_all) return r.fulfill({ status: 409, json: { detail: "would stop", reason: "would_stop_background" } });
      if (after === "the turn ended") {
        turns[0].state = "completed";
        state.active_turn = null as unknown as string;
        state.state = "queued";
      } else {
        turns.splice(1, 1);
      }
      state.revision++;
      return r.fulfill({ status: 409, json: { detail: "the message is no longer queued behind that turn", reason: "inventory_changed" } });
    });
    await page.goto(`/s/claude-api/${ID}`);
    await sendNow(page).click();
    await choice(page).getByRole("button", { name: "Stop all and send" }).click();
    await expect(choice(page)).toHaveCount(0);
    await expect(page.getByTestId("structured-error")).toHaveText(after === "the turn ended"
      ? "Stop all and send not needed: the agent’s turn ended, and your message is next in line."
      : "Stop all and send not applied: no message is waiting any more.");
    expect(posts).toHaveLength(2);
  });
}
