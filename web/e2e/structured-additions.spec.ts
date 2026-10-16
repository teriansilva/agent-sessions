import { expect, test, type Page } from "@playwright/test";
import { ACTIVE, ID, QUEUED, mockStructuredGuidance } from "./structuredGuidanceFixture";

const FIRST = "The bottom bar should meet the window edges.";
const SECOND = "Keep the spacing inside the bar.";

async function before(page: Page, earlier: string, later: string) {
  await expect.poll(async () => page.getByText(earlier, { exact: true }).evaluate((el, text) => {
    const next = [...document.querySelectorAll("div, p")].find((node) => node.textContent === text);
    return !!next && !!(el.compareDocumentPosition(next) & Node.DOCUMENT_POSITION_FOLLOWING);
  }, later)).toBe(true);
}

for (const theme of ["dark", "light"]) {
  test(`acknowledged additions precede their response through streaming and reload (${theme})`, async ({ page }, testInfo) => {
    const { state } = await mockStructuredGuidance(page, "codex-api", "steer", theme);
    const added = state.turns[1];
    added.text = FIRST;
    state.turns[0].state = "awaiting_approval";
    state.pending_requests = [{ request_id: "approve", turn_id: ACTIVE, kind: "command", complete: true,
      choices: ["approve", "reject"], payload: { command: "git status --short", cwd: "/home/u/demo" } }];
    await page.goto(`/s/codex-api/${ID}`);
    const response = "I’m checking parallel session startup.";
    const activity = page.getByTestId("structured-activity");
    await activity.locator("summary").click();
    await expect(activity).toHaveAttribute("open", "");
    const approval = page.getByRole("button", { name: "Approve once", exact: true });
    await approval.focus();
    const messageTime = page.getByText(FIRST, { exact: true }).locator("..").locator("time");
    const replyTime = page.getByTestId("structured-reply").locator("time");
    const submitted = await messageTime.getAttribute("datetime");
    const started = await replyTime.getAttribute("datetime");
    // An unacknowledged message stays in the queue below the current reply.
    await before(page, response, FIRST);
    Object.assign(added, { state: "delivered", delivery: "steered", delivery_turn_id: ACTIVE });
    state.revision++;
    await expect(page.getByText("Added to the active turn", { exact: true })).toHaveCount(1);
    await page.screenshot({ path: testInfo.outputPath("acknowledged.png") });
    await before(page, FIRST, response);
    await expect(activity).toHaveAttribute("open", "");
    await expect(approval).toBeFocused();
    await before(page, "Approve once", FIRST);
    await expect(messageTime).toHaveAttribute("datetime", submitted!);
    await expect(replyTime).toHaveAttribute("datetime", started!);
    await expect(page.getByTestId("structured-working")).toHaveCount(1);
    await activity.locator("summary").click();
    state.turns.push({ ...added, turn_id: "second-addition", operation_id: "second-addition", text: SECOND,
      attachments: [{ stored: "20261016-080000-spacing.txt", mime: "text/plain", name: "spacing.txt" }],
      author: { via: "public_link", name: "Guest" } });
    state.turns.push({ ...added, turn_id: "still-queued", operation_id: "still-queued", state: "queued",
      delivery: undefined, delivery_turn_id: undefined, text: "A separate later request." });
    state.turns[0].reply = "Understood. The bar will meet the window edges.";
    state.revision++;
    const reply = state.turns[0].reply;
    await expect(page.getByText(SECOND, { exact: true })).toHaveCount(1);
    await before(page, FIRST, SECOND);
    await before(page, SECOND, reply);
    await before(page, reply, "A separate later request.");
    await expect(page.getByText("Guest · via public link", { exact: true })).toHaveCount(1);
    await expect(page.getByText("spacing.txt", { exact: true })).toHaveCount(1);
    // The target remains the original turn once a later response takes over.
    state.turns[0].state = "completed";
    state.pending_requests = [];
    state.turns[3].state = "running";
    state.active_turn = "still-queued";
    state.revision++;
    await page.reload();
    await before(page, FIRST, SECOND);
    await before(page, SECOND, reply);
    await expect(page.getByText(FIRST, { exact: true })).toHaveCount(1);
    await expect(messageTime).toHaveAttribute("datetime", submitted!);
    await expect(replyTime).toHaveAttribute("datetime", started!);
    await expect(page.getByText("Guest · via public link", { exact: true })).toHaveCount(1);
    await expect(page.getByText("spacing.txt", { exact: true })).toHaveCount(1);
    await expect(page.getByText("Added to the active turn", { exact: true })).toHaveCount(2);
    await page.screenshot({ path: testInfo.outputPath("completed.png") });
  });
}

test("missing target and uncertain deliveries retain their messages", async ({ page }) => {
  const { state } = await mockStructuredGuidance(page, "codex-api", "steer");
  Object.assign(state.turns[1], { state: "delivered", delivery: "steered", delivery_turn_id: "omitted", text: FIRST });
  state.omitted_turns = 1;
  await page.goto(`/s/codex-api/${ID}`);
  await before(page, state.turns[0].reply, FIRST);
  await expect(page.getByText(FIRST, { exact: true })).toHaveCount(1);
  for (const status of ["queued", "delivering", "uncertain", "failed"]) {
    Object.assign(state.turns[1], { state: status, delivery: "steering", delivery_turn_id: ACTIVE });
    state.revision++;
    await expect(page.getByTestId("structured-turn").last()).toHaveAttribute("data-state", status);
    await before(page, state.turns[0].reply, FIRST);
  }
  expect(state.turns[1].turn_id).toBe(QUEUED);
});
