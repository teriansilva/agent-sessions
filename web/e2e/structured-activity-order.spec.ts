import { expect, test, type Locator } from "@playwright/test";
import { ACTIVE, ID, clients, mockStructuredGuidance } from "./structuredGuidanceFixture";

// A turn's commands read in the order they ran: the calls made after the agent wrote something
// sit below what it wrote, never all in one block above the whole reply.
const FIRST = "Reading the launch path.";
const SECOND = "Now the decide module.";
const THIRD = "All tests pass.";
const REPLY = `${FIRST}\n\n${SECOND}\n\n${THIRD}`;
const call = (id: string, at: number) =>
  ({ id, name: "Bash", summary: `echo ${id}`, outcome: "completed", at });
const TOOLS = [
  call("lead", 0),
  call("after-first", FIRST.length),
  call("after-first-2", FIRST.length),
  call("after-second", REPLY.indexOf(THIRD) - 2),
  call("newest", REPLY.length),
];

const top = async (l: Locator) => (await l.boundingBox())!.y;
async function expectOrder(...items: Locator[]) {
  const ys = [];
  for (const item of items) ys.push(await top(item));
  expect(ys).toEqual([...ys].sort((a, b) => a - b));
}

for (const [engine, mode] of clients) {
  test(`${engine}: commands sit between the messages they ran between`, async ({ page }, testInfo) => {
    const { state } = await mockStructuredGuidance(page, engine, mode);
    Object.assign(state.turns[0], { reply: REPLY, tools: TOOLS });
    await page.goto(`/s/${engine}/${ID}`);
    const turn = page.getByTestId("structured-turn").first();
    const groups = turn.getByTestId("structured-activity");
    await expect(groups).toHaveCount(4);
    await expect(groups.nth(0).locator("summary")).toHaveText("1 commands & tools");
    await expect(groups.nth(1).locator("summary")).toHaveText("2 commands & tools");
    // The turn is still working: that rides on its newest block only.
    await expect(groups.nth(3).locator("summary")).toHaveText("1 commands & tools · Working");
    const reply = turn.getByTestId("structured-reply");
    await expectOrder(
      groups.nth(0), reply.getByText(FIRST), groups.nth(1), reply.getByText(SECOND),
      groups.nth(2), reply.getByText(THIRD), groups.nth(3), turn.getByTestId("structured-working"),
    );
    await groups.nth(3).locator("summary").click();
    await expect(groups.nth(3).getByText("echo newest")).toBeVisible();
    await expect(groups.nth(0).getByText("echo newest")).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
    await page.screenshot({ path: testInfo.outputPath("activity-order.png") });
  });

  test(`${engine}: a turn the agent ran for itself reads in order too`, async ({ page }) => {
    const { state } = await mockStructuredGuidance(page, engine, mode);
    state.state = "idle";
    state.active_turn = null as unknown as string;
    state.turns = [{
      kind: "agent", turn_id: ACTIVE, state: "completed", ts: Date.parse("2026-10-12T07:29:00Z") / 1000,
      reply: REPLY, reply_ts: null, reply_truncated: false, tools: TOOLS, tools_truncated: false,
    }];
    await page.goto(`/s/${engine}/${ID}`);
    const turn = page.getByTestId("structured-agent-turn");
    const groups = turn.getByTestId("structured-agent-activity");
    await expect(groups).toHaveCount(4);
    await expectOrder(
      groups.nth(0), turn.getByText(FIRST), groups.nth(1), turn.getByText(SECOND),
      groups.nth(2), turn.getByText(THIRD), groups.nth(3),
    );
  });
}
