import { expect, test, type Page } from "@playwright/test";
import { ACTIVE, ID, QUEUED, mockStructuredGuidance } from "./structuredGuidanceFixture";

// #1475: a sent message the agent's own turn runs ahead of (a task notification the CLI took
// from its own queue first). The message is waiting, not working; nothing offers an Interrupt
// that would stop something other than it names; Esc and Send now wait until the message runs.
const OWN = "33333333-4444-4555-8666-777777777777";
const EARLIER = "00000000-2222-4333-8444-555555555555";

type Json = Record<string, unknown>;

async function waitingBehindOwnTurn(page: Page, theme = "dark") {
  const fixture = await mockStructuredGuidance(page, "claude-api", "interrupt", theme);
  const { state } = fixture;
  const [active, queued] = state.turns;
  state.turns = [
    { ...active, turn_id: EARLIER, operation_id: EARLIER, state: "interrupted", attachments: [],
      text: "Please plan the played-with feature.", reply: "I’m mapping how guests join rooms." },
    { ...active, turn_id: ACTIVE, operation_id: ACTIVE, state: "running", attachments: [], tools: [],
      text: "It’s a private list on your profile — no messaging inside the app.", reply: "",
      ts: Date.parse("2026-10-15T13:01:09Z") / 1000, reply_ts: null },
    { kind: "agent", turn_id: OWN, state: "running", reply: "", reply_truncated: false,
      ts: Date.parse("2026-10-15T13:01:10Z") / 1000, tools: [], tools_truncated: false },
    { ...queued, turn_id: QUEUED, operation_id: QUEUED, text: "And show it on the results screen." },
  ];
  Object.assign(state.native, {
    background_active: true, background_turn: OWN, send_now_background: "interrupt", interrupt_own: true,
  });
  const interrupts: Json[] = [];
  await page.route(new RegExp(`/api/structured/sessions/claude-api(?::|%3A)${ID}/interrupt$`), (r) => {
    interrupts.push(r.request().postDataJSON());
    return r.fulfill({ status: 202, json: { handoff: "sent" } });
  });
  await page.goto(`/s/claude-api/${ID}`);
  await expect(page.getByTestId("structured-agent-turn")).toBeVisible();
  return { ...fixture, interrupts };
}

const waiting = (page: Page) => page.getByTestId("structured-waiting-own-turn");
const ahead = (page: Page) => page.getByTestId("structured-agent-ahead");

for (const theme of ["dark", "light"]) {
  test(`#1475: a message waiting behind the agent's own turn reads waiting, with no Interrupt (${theme})`, async ({ page }, testInfo) => {
    const { interrupts } = await waitingBehindOwnTurn(page, theme);
    await expect(waiting(page)).toHaveText(/^Waiting · .+ is finishing a turn of its own first$/);
    await expect(page.getByTestId("structured-working")).toHaveCount(0);
    await expect(ahead(page)).toHaveText("Your message is next — it starts when this turn ends.");
    await expect(page.getByTestId("structured-agent-no-interrupt")).toHaveCount(0);
    await expect(page.getByTestId("structured-agent-interrupt")).toHaveCount(0);
    await expect(page.getByRole("button", { name: /^Interrupt/ })).toHaveCount(0);
    // The queued message has nothing running to go past yet: no Send now.
    await expect(page.getByTestId("structured-queued")).toBeVisible();
    await expect(page.getByRole("button", { name: "Send now", exact: true })).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath(`own-turn-ahead-${theme}.png`), fullPage: true });
    const composer = page.getByRole("textbox").first();
    await composer.click();
    await composer.press("Escape");
    await page.waitForTimeout(300);
    expect(interrupts).toHaveLength(0);
  });
}

test("#1475: once the message starts, it is working again with Interrupt, Esc and Send now", async ({ page }, testInfo) => {
  const { interrupts, state } = await waitingBehindOwnTurn(page);
  await expect(waiting(page)).toBeVisible();
  // The message's start ends the agent's own turn (the codec closes it at the message's `started`).
  state.turns[2].state = "interrupted";
  state.native.background_turn = null;
  state.revision++;
  await expect(waiting(page)).toHaveCount(0);
  await expect(ahead(page)).toHaveCount(0);
  await expect(page.getByTestId("structured-working")).toBeVisible();
  await expect(page.getByRole("button", { name: "Send now", exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("own-turn-ahead-started.png"), fullPage: true });
  const composer = page.getByRole("textbox").first();
  await composer.click();
  await composer.press("Escape");
  await expect.poll(() => interrupts.length).toBe(1);
  expect(interrupts[0].turn_id).toBe(ACTIVE);
});
