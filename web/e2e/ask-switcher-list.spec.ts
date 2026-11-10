/** HQ's conversation switcher list, in a real browser, desktop and mobile.
 *
 *  With a contact the head carries a portrait and five buttons, so the switcher itself can be a
 *  thumb wide. The list used to be the switcher's width: titles cut to "Hey, …", the day and the
 *  time stacked on two lines, a sideways scrollbar, and an unseen update only a 7 px dot. What a
 *  DOM emulator cannot show: the list's real width, that titles get two lines, that nothing scrolls
 *  sideways, and that typing on a focused row really lands in the search field.
 */
import { expect, test, type Page } from "@playwright/test";

import { openAsk } from "./askSidebar";
import { mockAskConversations, turnOf, type FakeAsk, type FakeConversation } from "./askStream";

const NOW = Math.floor(Date.now() / 1000);
const DAY = 86_400;

function conversations(): FakeConversation[] {
  const c = (n: number, q: string, ago: number, unread = false): FakeConversation => ({
    id: "ask_" + String(n).repeat(32),
    created_at: NOW - ago,
    updated_at: NOW - ago,
    turns: [turnOf(q, { answer: `answer ${n}` })],
    unread,
  });
  return [
    c(1, "Hey, what is running on the shared runner right now?", 60),
    c(2, "Find me the session that's about cloning media generation to pmgpurow from pm-core", 900, true),
    c(3, "What did the overnight deploy change on staging?", 1800),
    c(4, "Give me a status on every mission that is waiting on me", DAY + 600),
    c(5, "Hey HQ, which agents finished in the last hour?", 2 * DAY, true),
    c(6, "Whats the state of the flaky upload retry spec?", 3 * DAY),
    c(7, "Find me the session where we rotated the relay certificates", 12 * DAY),
  ];
}

/** The server keeps the last 50: the list's full length. */
function fifty(): FakeConversation[] {
  return Array.from({ length: 50 }, (_, i) => ({
    id: "ask_" + String(i).padStart(32, "0"),
    created_at: NOW - i * 3600,
    updated_at: NOW - i * 3600,
    turns: [turnOf(`kept question number ${i + 1}`, { answer: `kept answer ${i + 1}` })],
  }));
}

async function mockShell(page: Page, seed = conversations()): Promise<FakeAsk> {
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
        // A contact: the portrait head, where the switcher is narrowest.
        hq_contact: "asia-woman",
        hq_contact_rev: 0,
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

test("the list spans the head, titles read, updates are pinned on top and say so", async ({
  page,
}, info) => {
  await mockShell(page);
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  const trigger = panel.getByTestId("ask-switcher");
  // Two OTHER conversations have an unseen update: the closed switcher says so — with a marker in
  // its corner that leaves the title its room.
  const marker = panel.getByTestId("ask-switcher-updates");
  await expect(marker).toBeVisible();
  await expect(trigger).toHaveAccessibleName(/2 with new updates/);
  const tb = (await trigger.boundingBox())!;
  const mb = (await marker.boundingBox())!;
  expect(mb.x + mb.width).toBeLessThanOrEqual(tb.x + tb.width);
  expect(mb.y).toBeGreaterThanOrEqual(tb.y);
  // It takes no room in the row: it overlaps neither the title nor the chevron.
  const overlaps = (p: { x: number; y: number; width: number; height: number }) =>
    p.x < mb.x + mb.width && mb.x < p.x + p.width && p.y < mb.y + mb.height && mb.y < p.y + p.height;
  const titleBox = (await trigger.locator("span").first().boundingBox())!;
  const chevron = (await trigger.locator("svg").last().boundingBox())!;
  expect(overlaps(titleBox)).toBe(false);
  expect(overlaps(chevron)).toBe(false);

  await trigger.click();
  const menu = panel.getByTestId("ask-switcher-menu");
  await expect(menu).toBeVisible();
  const head = (await panel.getByTestId("ask-head").boundingBox())!;
  const box = (await menu.boundingBox())!;
  const sw = (await trigger.boundingBox())!;
  // The head's width, not the switcher's — which is much narrower with a portrait beside it.
  expect(sw.width).toBeLessThan(head.width * 0.8);
  expect(box.x).toBeGreaterThanOrEqual(head.x - 1);
  expect(box.x + box.width).toBeLessThanOrEqual(head.x + head.width + 1);
  expect(box.width).toBeGreaterThanOrEqual(head.width - 2);

  // Updates first, in words, then the day sections.
  const groups = menu.getByTestId("ask-switcher-group");
  await expect(groups.first()).toHaveText(/Updates · 2/i);
  const items = menu.getByTestId("ask-switcher-item");
  await expect(items).toHaveCount(7);
  await expect(items.nth(0)).toContainText("cloning media generation");
  await expect(items.nth(0).getByTestId("ask-switcher-unread")).toHaveText(/new updates/i);
  await expect(items.nth(1)).toContainText("which agents finished");
  await expect(items.nth(2).getByTestId("ask-switcher-unread")).toHaveCount(0);

  // A title gets most of the list's width and more than one word before it is cut.
  const title = (await items.nth(0).locator("span").first().boundingBox())!;
  expect(title.width).toBeGreaterThan(box.width * 0.75);
  // Nothing in the list scrolls sideways.
  const list = menu.getByRole("listbox");
  expect(await list.evaluate((el) => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(1);
  const { scroll, inner } = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    inner: window.innerWidth,
  }));
  expect(scroll).toBeLessThanOrEqual(inner);
  await info.attach("switcher-open", { body: await page.screenshot(), contentType: "image/png" });
});

test("typing on a focused row searches; Enter in the field opens the first match", async ({
  page,
}, info) => {
  await mockShell(page);
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  const trigger = panel.getByTestId("ask-switcher");
  await trigger.focus();
  await page.keyboard.press("Enter");
  const menu = panel.getByTestId("ask-switcher-menu");
  const items = menu.getByTestId("ask-switcher-item");
  await expect(items.nth(0)).toBeFocused();
  const search = menu.getByTestId("ask-switcher-search");

  await page.keyboard.type("find me");
  await expect(search).toBeFocused();
  await expect(search).toHaveValue("find me");
  await expect(items).toHaveCount(2);
  await expect(items.nth(0)).toContainText("cloning media generation");
  await expect(items.nth(1)).toContainText("relay certificates");
  await info.attach("switcher-search", { body: await page.screenshot(), contentType: "image/png" });

  await page.keyboard.type(" relay");
  await expect(items).toHaveCount(1);
  await page.keyboard.press("Enter");
  await expect(menu).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await expect(trigger).toContainText("rotated the relay certificates");
  await expect(panel.getByTestId("ask-turns")).toContainText("answer 7");

  // No match says so; Escape from the field closes only the list.
  await page.keyboard.press("Enter");
  await expect(menu).toBeVisible();
  await page.keyboard.type("zzz");
  await expect(menu.getByTestId("ask-switcher-search-empty")).toContainText("zzz");
  await page.keyboard.press("Escape");
  await expect(menu).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await expect(panel).toHaveAttribute("data-open", "true");
});

test("at 50 rows the list scrolls inside itself: End reaches the last row, search reaches any (#1873)", async ({
  page,
}) => {
  await mockShell(page, fifty());
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  const trigger = panel.getByTestId("ask-switcher");
  await trigger.focus();
  await page.keyboard.press("Enter");
  const menu = panel.getByTestId("ask-switcher-menu");
  const items = menu.getByTestId("ask-switcher-item");
  await expect(items).toHaveCount(50);
  // The list fits the viewport; its rows scroll, the search field and footer stay put.
  const vh = page.viewportSize()!.height;
  const box = (await menu.boundingBox())!;
  expect(box.y + box.height).toBeLessThanOrEqual(vh + 1);
  const list = menu.getByRole("listbox");
  expect(await list.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true);
  // End: the last (oldest) row has focus and is scrolled into view, inside the list's box.
  await page.keyboard.press("End");
  const last = items.nth(49);
  await expect(last).toBeFocused();
  await expect(last).toContainText("kept question number 50");
  await expect(last).toBeInViewport();
  const lb = (await list.boundingBox())!;
  const rb = (await last.boundingBox())!;
  expect(rb.y + rb.height).toBeLessThanOrEqual(lb.y + lb.height + 1);
  await expect(menu.getByTestId("ask-switcher-search")).toBeInViewport();
  // Typing from there searches the whole list, and Enter opens a match far down it.
  await page.keyboard.type("number 47");
  await expect(items).toHaveCount(1);
  await page.keyboard.press("Enter");
  await expect(menu).toHaveCount(0);
  await expect(trigger).toContainText("kept question number 47");
  await expect(panel.getByTestId("ask-turns")).toContainText("kept answer 47");
});
