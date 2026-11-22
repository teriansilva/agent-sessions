/** HQ's hints in the empty thread (#2265), in a real browser, desktop and mobile.
 *
 *  What a DOM emulator cannot prove: the three hints really are tall enough to hit on a phone, the
 *  block fits the panel without scrolling the page sideways, a tap puts the question in the box and
 *  the box takes focus (the keyboard comes up on a phone) without any request to HQ, a keyboard
 *  focus ring is visible, and closing and reopening the sidebar (which stays MOUNTED while closed)
 *  shows a different set.
 */
import { expect, test, type Page } from "@playwright/test";

import { openAsk } from "./askSidebar";
import { mockAskConversations } from "./askStream";
import { TOUCH_FLOOR } from "./touchTarget";

async function mockShell(page: Page): Promise<string[]> {
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
  await mockAskConversations(page, []);
  // Every request that would ASK or create anything: a hint must make none.
  const asks: string[] = [];
  page.on("request", (req) => {
    if (req.method() !== "GET" && /\/api\/ask\//.test(req.url())) asks.push(req.url());
  });
  return asks;
}

const ids = (page: Page) =>
  page
    .getByTestId("ask-sidebar")
    .getByTestId("hq-hint")
    .evaluateAll((els) => els.map((e) => e.getAttribute("data-hint")));

test("three hints, a tap fills the box and sends nothing, each reopen shows a new set (#2265)", async ({
  page,
}) => {
  const asks = await mockShell(page);
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  const hints = panel.getByTestId("hq-hint");
  await expect(hints).toHaveCount(3);
  await expect(hints.first()).toContainText("Manual");

  // Each is a real target, and the block fits the panel (no sideways page scroll on a phone).
  for (const h of await hints.all()) {
    const box = (await h.boundingBox())!;
    expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  const more = panel.getByTestId("hq-hints-more");
  expect((await more.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  const panelBox = (await panel.boundingBox())!;
  const lastBox = (await hints.last().boundingBox())!;
  expect(lastBox.x + lastBox.width).toBeLessThanOrEqual(panelBox.x + panelBox.width + 1);
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
  ).toBe(true);

  // The composer's own "Enter to send" shortcut is NOT a hint card (Hermes on #2267: both used
  // `.hint` in one CSS module). It keeps its own look, and in a composer 380px or narrower it is
  // hidden, while the question buttons stay visible.
  const shortcut = panel.getByTestId("composer-hint");
  const composerWidth = await panel
    .getByTestId("ask-form")
    .evaluate((el) => el.getBoundingClientRect().width);
  const look = await shortcut.evaluate((el) => {
    const s = getComputedStyle(el);
    return { display: s.display, minHeight: s.minHeight };
  });
  expect(look.display).not.toBe("grid");
  expect(look.minHeight).not.toBe("56px");
  if (composerWidth <= 380) await expect(shortcut).toBeHidden();
  await expect(hints.first()).toBeVisible();

  // A tap fills the box with the question and focuses it; nothing is asked.
  const text = (await hints.nth(1).locator("span").last().textContent())!;
  await hints.nth(1).click();
  const input = panel.getByTestId("composer-input");
  await expect(input).toHaveValue(text);
  await expect(input).toBeFocused();
  expect(asks).toEqual([]);

  // With that draft in the box, the hints are locked and say why in plain view.
  await expect(panel.getByTestId("hq-hints-locked")).toBeVisible();
  await expect(hints.first()).toBeDisabled();
  await input.fill("");
  await expect(panel.getByTestId("hq-hints-locked")).toHaveCount(0);

  // Close and reopen: the console stays mounted, and the set is new.
  const first = await ids(page);
  await panel.getByTestId("ask-close").click(); // the panel's own ✕ (a phone's drawer covers the bar)
  await expect(panel).toHaveAttribute("data-open", "false");
  await openAsk(page);
  await expect.poll(() => ids(page)).not.toEqual(first);
  for (const id of await ids(page)) expect(first).not.toContain(id);
  expect(asks).toEqual([]);
});

test("the keyboard reaches a hint, shows its ring, and Enter fills the box (#2265)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name === "mobile", "a hardware keyboard is a desktop path");
  const asks = await mockShell(page);
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  // From Other ideas, Tab reaches the first hint: a real keyboard move, so :focus-visible holds.
  await panel.getByTestId("hq-hints-more").focus();
  await page.keyboard.press("Tab");
  const hint = panel.getByTestId("hq-hint").first();
  await expect(hint).toBeFocused();
  expect(await hint.evaluate((el) => getComputedStyle(el).outlineStyle)).not.toBe("none");
  const text = (await hint.locator("span").last().textContent())!;
  await page.keyboard.press("Enter");
  await expect(panel.getByTestId("composer-input")).toHaveValue(text);
  expect(asks).toEqual([]);
});
