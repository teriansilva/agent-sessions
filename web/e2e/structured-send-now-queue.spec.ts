import { expect, test, type Page } from "@playwright/test";
import { ACTIVE, ID, QUEUED, mockStructuredGuidance } from "./structuredGuidanceFixture";

// #1442: with several messages queued, Send now is offered on the FIRST only and the queue keeps
// its order; a reply Send now cut off says so.
const SECOND = "44444444-2222-4333-8444-555555555555";
type Json = Record<string, unknown>;

async function twoQueued(page: Page, engine = "claude-api", mode = "interrupt", theme = "dark") {
  const fixture = await mockStructuredGuidance(page, engine, mode, theme);
  fixture.state.turns.push({ ...fixture.state.turns[1], turn_id: SECOND, operation_id: SECOND,
    text: "And keep the old breakpoint.", ts: Date.parse("2026-10-12T07:32:00Z") / 1000 });
  await page.goto(`/s/${engine}/${ID}`);
  await expect(page.getByTestId("structured-queued")).toHaveCount(2);
  return fixture;
}

const rows = (page: Page) => page.getByTestId("structured-queued");
const sendNow = (page: Page) => page.getByRole("button", { name: "Send now", exact: true });

for (const theme of ["dark", "light"]) {
  test(`#1442: only the first queued message offers Send now; the rest say where they stand (${theme})`, async ({ page }, testInfo) => {
    await twoQueued(page, "claude-api", "interrupt", theme);
    await expect(sendNow(page)).toHaveCount(1);
    await expect(rows(page).nth(0).getByRole("button", { name: "Send now", exact: true })).toBeVisible();
    await expect(rows(page).nth(0)).toContainText("1st in queue · sends after the current reply");
    await expect(rows(page).nth(0)).toContainText(/Stops .+’s current reply, then sends this — and the messages after it, in order\./);
    await expect(rows(page).nth(1)).toContainText("2nd in queue · sends after the message above");
    await expect(rows(page).nth(1).getByRole("button")).toHaveCount(0);
    const box = await sendNow(page).boundingBox();
    expect(box!.height).toBeGreaterThanOrEqual(44); // design §8
    await page.screenshot({ path: testInfo.outputPath(`queue-${theme}.png`) });
  });
}

test("#1442: Send now goes for the head, and the order shown stays the order sent", async ({ page }) => {
  const { calls, state } = await twoQueued(page);
  await sendNow(page).click();
  await expect.poll(() => calls.length).toBe(1);
  expect(calls[0].queued_turn_id).toBe(QUEUED);
  expect(calls[0].turn_id).toBe(ACTIVE);
  await expect(rows(page).nth(0)).toContainText("Waiting for the current response to stop · sends next");
  await expect(rows(page).nth(1)).toContainText("2nd in queue · sends after the message above");
  await expect(sendNow(page)).toHaveCount(0); // nothing else to jump the line with
  expect((state.turns as Json[]).map((t) => t.turn_id)).toEqual([ACTIVE, QUEUED, SECOND]);
});

test("#1442: Codex adds only the first message to its turn; the next one becomes first", async ({ page }) => {
  const { state } = await twoQueued(page, "codex-api", "steer");
  await expect(sendNow(page)).toHaveCount(1);
  await expect(rows(page).nth(0)).toContainText("Adds this message to the current turn.");
  // The head was steered in; the snapshot moves on.
  (state.turns as Json[]).splice(1, 1, { ...(state.turns as Json[])[1], state: "delivered", delivery: "steered", delivery_turn_id: ACTIVE });
  state.revision++;
  await expect(rows(page)).toHaveCount(1);
  await expect(rows(page).nth(0)).toContainText("1st in queue · sends after the current reply");
  await expect(sendNow(page)).toHaveCount(1);
  await expect(page.getByTestId("structured-sendnow-cut")).toHaveCount(0); // a steer cuts nothing
});

test("#1442: a second tab still showing the old first message is refused, and nothing moves", async ({ page }) => {
  const { state } = await twoQueued(page);
  await page.route(new RegExp(`/api/structured/sessions/claude-api(?::|%3A)${ID}/send-now$`), (r) =>
    r.fulfill({ status: 409, json: { detail: "only the first queued message can be sent now" } }));
  await sendNow(page).click();
  await expect(page.getByText("Send now not applied: only the first queued message can be sent now")).toBeVisible();
  await expect(rows(page).nth(0)).toContainText("1st in queue");
  await expect(rows(page).nth(1)).toContainText("2nd in queue");
  expect((state.turns as Json[]).map((t) => t.turn_id)).toEqual([ACTIVE, QUEUED, SECOND]);
});

test("#1442: a reply Send now cut off says so, instead of reading as a failure", async ({ page }) => {
  const fixture = await mockStructuredGuidance(page, "claude-api", "interrupt");
  const turns = fixture.state.turns as Json[];
  turns[0].state = "interrupted";
  turns[1] = { ...turns[1], state: "running", delivery_turn_id: ACTIVE };
  fixture.state.active_turn = QUEUED;
  await page.goto(`/s/claude-api/${ID}`);
  const note = page.getByTestId("structured-sendnow-cut");
  await expect(note).toHaveText("Interrupted by Send now — your next message is answered below.");
  await expect(page.getByTestId("structured-failed")).toHaveCount(0); // not an alert
});

test("#1442: a plain Interrupt is still just interrupted — no Send now label", async ({ page }) => {
  const fixture = await mockStructuredGuidance(page, "claude-api", "interrupt");
  const turns = fixture.state.turns as Json[];
  turns[0].state = "interrupted";
  turns[1] = { ...turns[1], state: "running" }; // nothing named the interrupted turn
  fixture.state.active_turn = QUEUED;
  await page.goto(`/s/claude-api/${ID}`);
  await expect(page.getByTestId("structured-failed")).toContainText("interrupted");
  await expect(page.getByTestId("structured-sendnow-cut")).toHaveCount(0);
});

test("#1442: when the message Send now cut for then fails, nothing claims it is answered below", async ({ page }) => {
  const fixture = await mockStructuredGuidance(page, "claude-api", "interrupt");
  const turns = fixture.state.turns as Json[];
  turns[0].state = "interrupted";
  turns[1] = { ...turns[1], state: "failed", reason: "not sent: the agent did not accept this turn", delivery_turn_id: ACTIVE };
  fixture.state.active_turn = null as unknown as string;
  await page.goto(`/s/claude-api/${ID}`);
  await expect(page.getByTestId("structured-sendnow-cut")).toHaveCount(0);
  await expect(page.getByTestId("structured-failed").first()).toContainText("interrupted");
});

for (const then of ["goes out", "is refused"] as const) {
  test(`#1442: while the message still waits, nothing is "answered below" — then it ${then}`, async ({ page }) => {
    // The projector's exact waiting shape (tests/test_native_send_now_label.py pins it): target
    // interrupted, message queued + interrupting + linked.
    const fixture = await mockStructuredGuidance(page, "claude-api", "interrupt");
    const turns = fixture.state.turns as Json[];
    turns[0].state = "interrupted";
    turns[1] = { ...turns[1], state: "queued", delivery: "interrupting", delivery_turn_id: ACTIVE };
    fixture.state.active_turn = null as unknown as string;
    await page.goto(`/s/claude-api/${ID}`);
    await expect(page.getByText("Waiting for the current response to stop · sends next")).toBeVisible();
    await expect(page.getByTestId("structured-sendnow-cut")).toHaveCount(0);
    await expect(page.getByTestId("structured-failed").first()).toContainText("interrupted");
    if (then === "goes out") {
      turns[1] = { ...turns[1], state: "running", delivery: undefined };
      fixture.state.active_turn = QUEUED;
    } else {
      turns[1] = { ...turns[1], state: "failed", delivery: undefined, delivery_turn_id: undefined,
        reason: "not sent: the agent did not accept this turn" };
    }
    fixture.state.revision++;
    if (then === "goes out") await expect(page.getByTestId("structured-sendnow-cut")).toBeVisible();
    else {
      await expect(page.getByText("not sent: the agent did not accept this turn")).toBeVisible();
      await expect(page.getByTestId("structured-sendnow-cut")).toHaveCount(0);
    }
  });
}
