/** A card tapped AFTER Stop continues, and the page shows it without a reload (#2050), in a real
 *  browser, desktop and phone.
 *
 *  The Commander pressed Stop on HQ's turn and then confirmed the draft-mission card it had
 *  proposed: that tap is newer intent than the Stop, so the server continues once the stopped turn
 *  settles. Before #2050 the page never read a stopped turn again, so the continuation only showed
 *  after a reload. Here the page reads it by itself: once when Stop answers while another turn
 *  runs, and on its bounded rechecks when the stopped turn is read back. The API is mocked.
 */
import { expect, test, type Page } from "@playwright/test";

import { openAsk, pickConversation } from "./askSidebar";
import { mockAskConversations, turnOf, type FakeAsk, type FakeTurn } from "./askStream";
import { mockNightjarShell } from "./nightjarShell";

const CID = "ask_" + "5".repeat(32);
const TID = "11111111-2222-4333-8444-555555555555";
const MID = "msn_" + "b".repeat(32);

function card(state: "pending" | "done"): Record<string, unknown> {
  const done = state === "done";
  return {
    action_id: "a".repeat(32),
    kind: "operation",
    op: "mission.create",
    risk: "reversible",
    label: "Create a draft mission",
    verb: "Create draft",
    title: "Synthetic Applications",
    text: 'Creates a draft mission: "Apply for the job". Nothing runs until you press Begin.',
    needs: [],
    state,
    summary: done
      ? "Draft mission created (Apply for the job). Nothing runs until you press Begin."
      : "",
    reason: "",
    can_confirm: !done,
    can_dismiss: !done,
    ...(done ? { mission: { id: MID } } : {}),
  };
}

function cardTurn(status: "pending" | "stopped", state: "pending" | "done" = "done"): FakeTurn {
  return {
    ...turnOf("plan an application mission", { answer: "" }, TID),
    status,
    answer: null,
    proposals: [card(state)],
    steps: {
      count: 3,
      more: null,
      incomplete: false,
      ...(status === "pending" ? { live: { n: 4, tool: "list_missions", at: 1 } } : {}),
    },
    elapsed_ms: 12_000,
  } as unknown as FakeTurn;
}

function continuation(): FakeTurn {
  return {
    ...turnOf("[BattleLab, not the Commander: the Commander confirmed your card.] " + MID, {
      answer: "The draft is ready; its plan has 10 steps.",
    }),
    via: "card",
  } as unknown as FakeTurn;
}

async function mockHq(page: Page, seed: FakeTurn): Promise<FakeAsk> {
  await mockNightjarShell(page);
  return mockAskConversations(page, [{ id: CID, created_at: 1, updated_at: 2, turns: [seed] }]);
}

test("Stop answered while HQ's continuation runs: the page reads it, no reload", async ({
  page,
}) => {
  // Read back while HQ works, its card not decided yet. The Commander presses Stop, and the card
  // is confirmed (Needs you, another tab) while the stopped turn settles.
  const fake = await mockHq(page, cardTurn("pending", "pending"));
  await page.route(/\/api\/ask\/conversations\/[^/]+\/turns\/[^/]+\/stop$/, async (route) => {
    // The stopped turn settled and HQ continued (the card was tapped after the Stop).
    fake.conversations[0].turns = [cardTurn("stopped"), continuation()];
    await route.fulfill({ json: { turn: cardTurn("stopped"), running: true } });
  });
  await page.goto("/");
  const panel = await openAsk(page);
  await pickConversation(panel);
  await panel.getByTestId("composer-stop").click();
  const turns = panel.getByTestId("ask-turns");
  const cont = turns.getByTestId("ask-continuation");
  await expect(cont).toBeVisible();
  await expect(cont).toContainText("After your confirm");
  await expect(turns.getByText("The draft is ready; its plan has 10 steps.")).toBeVisible();
  await expect(panel.getByTestId("ask-stopped-line")).toBeVisible();
  await expect(turns).not.toContainText("not the Commander");
});

test("a stopped turn read back is read again until its continuation shows", async ({ page }) => {
  const fake = await mockHq(page, cardTurn("stopped"));
  let reads = 0;
  await page.route(/\/api\/ask\/conversations\/ask_[0-9a-f]{32}$/, async (route) => {
    if (route.request().method() === "GET" && ++reads === 2)
      fake.conversations[0].turns = [cardTurn("stopped"), continuation()];
    await route.fallback();
  });
  await page.goto("/");
  const panel = await openAsk(page);
  await pickConversation(panel);
  await expect(panel.getByTestId("ask-operation")).toBeVisible();
  const cont = panel.getByTestId("ask-turns").getByTestId("ask-continuation");
  await expect(cont).toBeVisible({ timeout: 10_000 });
  expect(reads).toBeGreaterThanOrEqual(2);
});
