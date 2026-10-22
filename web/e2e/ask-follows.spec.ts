/** Ask follows the sessions it started (#1443 Phase 4a), in a real browser, desktop and
 *  mobile.
 *
 *  What a DOM emulator cannot show: the update rows sit between the turns by time and read as an
 *  LED plus a word; Unfollow is reached by keyboard with a visible ring, at the 44px floor, and
 *  posts the route; the Ask icon's dot appears with its label and clears once the conversation is
 *  on screen; nothing scrolls sideways.
 */
import { expect, test, type Page } from "@playwright/test";

import { openAsk, pickConversation } from "./askSidebar";
import { mockAskConversations, turnOf, type FakeAsk, type FakeConversation } from "./askStream";
import { TOUCH_FLOOR } from "./touchTarget";

const KEY = "claude:11111111-2222-4333-8444-555555555555";

async function mockShell(page: Page, seed: FakeConversation[]): Promise<FakeAsk> {
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        auth_mode: "none",
        username: null,
        terminal_backend: "ws",
        pulse: { configured: true },
        new_session_engines: ["claude"],
        onboarded: true,
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [], total: 0, next_offset: null, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await page.route("**/api/missions**", (r) =>
    r.fulfill({
      json: { missions: [], total: 0, next_offset: null, facets: { projects: [], states: [] } },
    }),
  );
  return mockAskConversations(page, seed);
}

function followed(unread: boolean): FakeConversation {
  const first = turnOf("start a claude session on api to rebase", { answer: "Started it." });
  const second = turnOf("how is it going?", { answer: "It is working." });
  return {
    id: "ask_" + "d".repeat(32),
    created_at: 1,
    updated_at: 2,
    turns: [first, second],
    following: [KEY],
    unread,
    updates: [
      { key: KEY, kind: "finished", title: "api rebase", ts: first.asked_at + 0.5, following: true },
      { key: KEY, kind: "needs_you", title: "api rebase", ts: second.asked_at + 0.5, following: true },
    ],
  };
}

async function noSidewaysScroll(page: Page) {
  const { scroll, inner } = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    inner: window.innerWidth,
  }));
  expect(scroll).toBeLessThanOrEqual(inner);
}

test("updates sit between the turns, read as a word, and Unfollow works by keyboard", async ({
  page,
}) => {
  await mockShell(page, [followed(false)]);
  const unfollowed: string[] = [];
  page.on("request", (req) => {
    if (req.method() === "DELETE" && req.url().includes("/follow/")) unfollowed.push(req.url());
  });
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  await pickConversation(panel);
  const turns = panel.getByTestId("ask-turn");
  const rows = panel.getByTestId("ask-update");
  await expect(rows).toHaveCount(2);
  // The first update belongs after the first turn, the second after the second.
  await expect(turns.nth(0).getByTestId("ask-update")).toHaveCount(1);
  await expect(turns.nth(0).getByTestId("ask-update")).toContainText("Finished its turn");
  await expect(turns.nth(1).getByTestId("ask-update")).toContainText("Needs you");
  await expect(rows.nth(0).getByTestId("ask-update-open")).toHaveAttribute(
    "href",
    "/s/claude/11111111-2222-4333-8444-555555555555",
  );

  const unfollow = rows.nth(1).getByRole("button", { name: "Unfollow api rebase" });
  expect((await unfollow.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  // Keyboard: Tab reaches it from the row's way in, with a visible ring; Enter unfollows.
  await rows.nth(1).getByTestId("ask-update-open").focus();
  await page.keyboard.press("Tab");
  await expect(unfollow).toBeFocused();
  expect(await unfollow.evaluate((el) => getComputedStyle(el).outlineStyle)).not.toBe("none");
  await page.keyboard.press("Enter");
  await expect(panel.getByTestId("ask-update-unfollow")).toHaveCount(0);
  expect(unfollowed).toHaveLength(1);
  expect(decodeURIComponent(unfollowed[0])).toContain(`/follow/${KEY}`);
  await noSidewaysScroll(page);
});

test("the Ask icon's dot says there is an unseen update, and clears once it is on screen", async ({
  page,
}) => {
  await mockShell(page, [followed(true)]);
  await page.goto("/dashboard");
  const toggle = page.getByTestId("ask-toggle");
  await expect(page.getByTestId("ask-unread-dot")).toBeVisible();
  await expect(toggle).toHaveAccessibleName("HQ — new updates from sessions you follow");
  await pickConversation(await openAsk(page));
  await expect(page.getByTestId("ask-update").first()).toBeVisible();
  await expect(page.getByTestId("ask-unread-dot")).toHaveCount(0);
  await expect(toggle).toHaveAccessibleName("HQ");
  await noSidewaysScroll(page);
});

test("a session followed before its first update is listed with Unfollow, by keyboard", async ({
  page,
}) => {
  // Starting a session follows it at once; its first update can come much later. Unfollow must
  // not wait for it (Hermes on #1500).
  const conv = followed(false);
  conv.updates = [];
  conv.followTitles = { [KEY]: "api rebase" };
  await mockShell(page, [conv]);
  const unfollowed: string[] = [];
  page.on("request", (req) => {
    if (req.method() === "DELETE" && req.url().includes("/follow/")) unfollowed.push(req.url());
  });
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  await pickConversation(panel);
  await expect(panel.getByTestId("ask-update")).toHaveCount(0);
  const row = panel.getByTestId("ask-following-row");
  await expect(row).toHaveCount(1);
  await expect(row).toContainText("Following");
  await expect(row).toContainText("api rebase");
  const unfollow = row.getByRole("button", { name: "Unfollow api rebase" });
  expect((await unfollow.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await row.getByTestId("ask-following-open").focus();
  await page.keyboard.press("Tab");
  await expect(unfollow).toBeFocused();
  expect(await unfollow.evaluate((el) => getComputedStyle(el).outlineStyle)).not.toBe("none");
  await page.keyboard.press("Enter");
  await expect(panel.getByTestId("ask-following")).toHaveCount(0);
  expect(unfollowed).toHaveLength(1);
  expect(decodeURIComponent(unfollowed[0])).toContain(`/follow/${KEY}`);
  await noSidewaysScroll(page);
});

test("a nudge that could not be followed, and a spent budget, read as words (#1517)", async ({
  page,
}) => {
  const conv = followed(false);
  conv.updates = [
    { key: KEY, kind: "finished", title: "api rebase", ts: conv.turns[0].asked_at + 0.5, following: true },
    {
      key: "claude:00000000-0000-4000-8000-0000000000dd",
      kind: "not_followed",
      title: "docs pass",
      ts: conv.turns[1].asked_at + 0.5,
      following: false,
    },
  ];
  conv.followsExhausted = true;
  await mockShell(page, [conv]);
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  await pickConversation(panel);
  const row = panel.locator('[data-testid="ask-update"][data-kind="not_followed"]');
  await expect(row).toHaveCount(1);
  await expect(row).toContainText("Not following");
  await expect(row).toContainText("docs pass");
  await expect(row).toContainText("already following 10 sessions");
  await expect(row.getByRole("button", { name: /Unfollow/ })).toHaveCount(0);
  const open = row.getByTestId("ask-update-open");
  expect((await open.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  // Its LED is the unknown status beside the word, never colour alone.
  const led = row.locator('[aria-hidden="true"]').first();
  const unknown = await page.evaluate(() =>
    getComputedStyle(document.documentElement).getPropertyValue("--status-unknown").trim(),
  );
  const bg = await led.evaluate((el) => getComputedStyle(el).backgroundColor);
  const hex = (c: string) =>
    "#" + c.match(/\d+/g)!.slice(0, 3).map((n) => Number(n).toString(16).padStart(2, "0")).join("");
  expect(hex(bg)).toBe(unknown.toLowerCase());
  const notice = panel.getByTestId("ask-follow-exhausted");
  await expect(notice).toHaveText(
    "This conversation no longer follows sessions. Start a new conversation to follow more.",
  );
  await noSidewaysScroll(page);
});

test("a nudge sent from the card is followed by THAT conversation once the follower admits it, and its update is seen (#1517)", async ({
  page,
}) => {
  // The real interleaving (Hermes on #1534): the card settles `delivered` BEFORE the follower's
  // next pass admits it. The open conversation must keep reading until the follow and its update
  // land; nothing but that window is live by then.
  // Three fake-clock phases (reads before admission, the poll that lands it, the unread poll), each
  // fetching for real: past the default 30 s on a loaded runner, so this test has its own budget.
  test.setTimeout(90_000);
  const NUDGED = "claude:00000000-0000-4000-8000-0000000000ee";
  const ask = turnOf("tell the docs session to rebase", { answer: "Proposed a nudge." });
  const card = {
    action_id: "a".repeat(32),
    kind: "nudge",
    key: NUDGED,
    title: "docs pass",
    text: "Rebase onto main.",
    state: "proposed",
    projection: "actionable",
    can_approve: true,
    can_reject: true,
    detail: "",
  };
  const conv: FakeConversation = {
    id: "ask_" + "e".repeat(32),
    created_at: 1,
    updated_at: 2,
    turns: [{ ...ask, proposals: [card] }],
  };
  await page.clock.install();
  await mockShell(page, [conv]);
  const seen: string[] = [];
  page.on("request", (req) => {
    if (req.method() === "POST" && req.url().endsWith("/seen")) seen.push(req.url());
  });
  await page.route("**/api/pulse/actions/*/approve", (r) => {
    Object.assign(card, { state: "delivered", projection: "settled", can_approve: false, can_reject: false });
    return r.fulfill({ json: { id: card.action_id, state: "delivered" } });
  });
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  await pickConversation(panel);
  await panel.getByTestId("ask-nudge-send").click();
  await expect(panel.getByTestId("ask-nudge")).toHaveAttribute("data-state", "delivered");
  await page.clock.runFor(20_000); // reads before the follower's pass find nothing followed yet
  await expect(panel.getByTestId("ask-update")).toHaveCount(0);

  // The follower's pass admits it: the conversation follows the session, which then finishes.
  conv.following = [NUDGED];
  conv.unread = true;
  conv.updates = [{ key: NUDGED, kind: "finished", title: "docs pass", ts: ask.asked_at + 5, following: true }];
  await page.clock.runFor(16_000);
  const row = panel.getByTestId("ask-update");
  await expect(row).toHaveCount(1);
  await expect(row).toContainText("Finished its turn");
  await expect(row).toContainText("docs pass");
  await expect(row.getByRole("button", { name: "Unfollow docs pass" })).toBeVisible();
  // On screen, so it is marked seen: no dot, the conversation is read.
  await expect.poll(() => seen.length).toBeGreaterThan(0);
  expect(conv.unread).toBe(false);
  await expect(page.getByTestId("ask-unread-dot")).toHaveCount(0);

  // Panel closed: a later refusal (the follow cap) is unread until the conversation is on screen.
  await panel.getByRole("button", { name: "Close HQ" }).click();
  conv.unread = true;
  conv.updates = [
    ...conv.updates,
    { key: "claude:00000000-0000-4000-8000-0000000000ef", kind: "not_followed", title: "lint pass", ts: ask.asked_at + 9, following: false },
  ];
  await page.clock.runFor(61_000);
  await expect(page.getByTestId("ask-unread-dot")).toBeVisible();
  await openAsk(page);
  await expect(panel.locator('[data-testid="ask-update"][data-kind="not_followed"]')).toContainText("lint pass");
  await expect(page.getByTestId("ask-unread-dot")).toHaveCount(0);
  await noSidewaysScroll(page);
});
