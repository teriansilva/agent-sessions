/** HQ's message card (#1833), in a real browser on desktop and a phone: the exact text before the
 *  tap, Send carrying nothing but the decision, and each outcome in words (sent, may have been
 *  sent, out of date, not sent, dismissed, interrupted after a reload). The server's decide route
 *  is faked; what it does is pinned by `tests/test_ask_messages*.py`. */
import { expect, test, type Page } from "@playwright/test";

import { mockAskConversations, turnOf, type FakeAsk } from "./askStream";
import { mockShell } from "./callFixture";
import { TOUCH_FLOOR } from "./touchTarget";

const PHONE = (page: Page) => (page.viewportSize()?.width ?? 1280) <= 800;
const LONG =
  "Deploy window tonight 22:00–23:00 UTC.\n\n• agent-sessions v0.19.5 — release notes in HQ\n" +
  "• relay restart (≈2 min)\n\nObjections? Reply before 21:00 — danke 🙏 " +
  "and-a-very-long-unbroken-token-".repeat(4);

type Card = Record<string, unknown> & { action_id: string; state: string };

function card(n: number, state: string, over: Record<string, unknown> = {}): Card {
  return {
    action_id: String(n).repeat(32).slice(0, 32),
    kind: "message",
    connector: "Telegram",
    recipient: "Marcus (@marcus)",
    recipient_kind: "private chat",
    text: "Release v0.19.5 is green and ready for your go.",
    state,
    reason: "",
    can_send: state === "pending",
    can_dismiss: state === "pending",
    ...over,
  };
}

async function open(page: Page, cards: Card[]): Promise<FakeAsk> {
  await mockShell(page);
  const fake = await mockAskConversations(page, [
    {
      id: "ask_" + "c".repeat(32),
      created_at: 1,
      updated_at: 2,
      turns: cards.map((p, i) => ({
        ...turnOf(`message ${i}`, { answer: "Here is the exact message; tap Send." }),
        proposals: [p],
      })),
    } as never,
  ]);
  await page.goto(`/hq/c/ask_${"c".repeat(32)}`);
  await expect(page.getByTestId("ask-message").first()).toBeVisible();
  return fake;
}

async function sizes(page: Page) {
  const over = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(over).toBeLessThanOrEqual(0);
  if (!PHONE(page)) return;
  for (const b of await page.getByTestId("ask-message").locator("button:visible").all())
    expect((await b.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
}

test("the card shows the exact text, and Send carries nothing but the decision", async ({ page }) => {
  const pending = card(1, "pending", { text: LONG });
  const fake = await open(page, [pending]);
  const bodies: unknown[] = [];
  await page.route(/\/api\/ask\/proposals\/[0-9a-f]{32}\/decide$/, async (r) => {
    bodies.push(r.request().postDataJSON());
    const c = fake.conversations[0].turns[0].proposals![0] as Card;
    c.state = "sent";
    c.can_send = c.can_dismiss = false;
    return r.fulfill({ status: 202, json: { proposal: { ...c, state: "deciding" } } });
  });
  const m = page.getByTestId("ask-message");
  await expect(m.getByTestId("ask-message-text")).toHaveText(LONG); // the bytes, as typed
  await expect(m.getByText("Leaves BattleLab for Telegram.", { exact: false })).toBeVisible();
  await sizes(page);
  // Keyboard: Send, then Dismiss.
  await m.getByTestId("ask-message-send").focus();
  await page.keyboard.press("Tab");
  await expect(m.getByTestId("ask-message-dismiss")).toBeFocused();
  await m.getByTestId("ask-message-send").click();
  expect(bodies).toEqual([{ decision: "send" }]);
  await expect(m.getByTestId("ask-message-outcome")).toContainText("Sent");
  await expect(m.getByTestId("ask-message-outcome")).toContainText("Telegram accepted it.");
});

test("a pending Matrix room shows who would read it before Send", async ({ page }) => {
  await open(page, [
    card(2, "pending", {
      connector: "Matrix",
      recipient: "Ops",
      // The server's shape (#1833, Hermes 7610): the kind and, apart, the live member count.
      recipient_kind: "room",
      recipient_members: 4,
    }),
  ]);
  const m = page.getByTestId("ask-message");
  const to = m.locator("dd").nth(1);
  await expect(to).toContainText("Ops");
  await expect(to).toContainText("· room · 4 people");
  await expect(m.getByText(/Leaves BattleLab for Matrix/)).toBeVisible();
  await expect(m.getByTestId("ask-message-send")).toHaveText(/Send to Ops/);
  await sizes(page);
});

test("each outcome reads in words, and none offers Send again", async ({ page }) => {
  await open(page, [
    card(3, "unconfirmed", { reason: "no clear answer came back after it went out; it is not sent again" }),
    card(4, "stale", { reason: "Marcus (@marcus) is no longer an allowed recipient; nothing was sent" }),
    card(5, "failed", { reason: "Telegram did not take it (rejected); nothing was sent" }),
    card(6, "dismissed"),
    card(7, "interrupted", {
      reason: "the server stopped while this was being sent: it may have been sent, so check the chat. It is not sent again.",
    }),
    card(8, "deciding"),
  ]);
  const cards = page.getByTestId("ask-message");
  await expect(cards).toHaveCount(6);
  const expected = ["May have been sent", "Out of date", "Not sent", "Dismissed", "May have been sent", "Sending"];
  for (let i = 0; i < expected.length; i++) {
    await expect(cards.nth(i).getByTestId("ask-message-outcome")).toContainText(expected[i]);
    await expect(cards.nth(i).getByTestId("ask-message-send")).toHaveCount(0);
  }
  await expect(cards.nth(1)).toContainText("no longer an allowed recipient");
  await expect(cards.nth(4)).toContainText("It is not sent again");
  await sizes(page);
});
