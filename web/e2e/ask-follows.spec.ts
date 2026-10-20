/** Ask follows the sessions it started (#1443 Phase 4a), in a real browser, desktop and
 *  mobile.
 *
 *  What a DOM emulator cannot show: the update rows sit between the turns by time and read as an
 *  LED plus a word; Unfollow is reached by keyboard with a visible ring, at the 44px floor, and
 *  posts the route; the Ask icon's dot appears with its label and clears once the conversation is
 *  on screen; nothing scrolls sideways.
 */
import { expect, test, type Page } from "@playwright/test";

import { openAsk } from "./askSidebar";
import { mockAskConversations, turnOf, type FakeAsk, type FakeConversation } from "./askStream";

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
  expect((await unfollow.boundingBox())!.height).toBeGreaterThanOrEqual(44);
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
  await expect(toggle).toHaveAccessibleName("Ask — new updates from sessions you follow");
  await openAsk(page);
  await expect(page.getByTestId("ask-update").first()).toBeVisible();
  await expect(page.getByTestId("ask-unread-dot")).toHaveCount(0);
  await expect(toggle).toHaveAccessibleName("Ask");
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
  await expect(panel.getByTestId("ask-update")).toHaveCount(0);
  const row = panel.getByTestId("ask-following-row");
  await expect(row).toHaveCount(1);
  await expect(row).toContainText("Following");
  await expect(row).toContainText("api rebase");
  const unfollow = row.getByRole("button", { name: "Unfollow api rebase" });
  expect((await unfollow.boundingBox())!.height).toBeGreaterThanOrEqual(44);
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
