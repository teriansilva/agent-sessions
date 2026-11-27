/** One Share entry, two choices, on every session surface (#2173).
 *
 * The pane head carries ONE `share` action. On the bar it is a chip that opens a small anchored
 * menu (Public…, Peer…); folded into the head's "…" or the ≤800px Actions menu it is a "Share"
 * label followed by the two items. The sidebar row's ⋯ menu lists the same two entries and mounts
 * the same two dialogs. On a terminal session Public… is listed but off, with its reason: public
 * links cover API sessions only (#1404).
 *
 * Each path is driven as an operator would: open the menu, pick the choice, see the dialog with
 * the menu gone, close it, and find focus on a control that still exists.
 */
import { expect, test, type Locator, type Page } from "@playwright/test";
import {
  ACTIONS_MENU,
  MORE_MENU,
  SHARE_CHIP,
  SHARE_PUBLIC,
  SHARE_PUBLIC_UNAVAILABLE,
} from "./headActions";
import {
  SHARE_PEER,
  TERM_TITLE,
  TERM_URL,
  TITLE,
  URL_PATH,
  isMobile,
  setup,
  setupTerminal,
  showList,
} from "./shareFixture";
import { animationsSettled } from "./nightjarShell";

const SHARE_TERMINAL = "Share this terminal's screen with a paired instance";
const SHARE_MENU = "Share this session";

const peerDialog = (page: Page) => page.getByRole("dialog", { name: "Share with a peer" });
const publicDialog = (page: Page) => page.getByRole("dialog", { name: "Share publicly" });
const head = (page: Page) => page.locator('[class*="panelHead"]');

async function openApi(page: Page) {
  await setup(page);
  await page.goto(URL_PATH);
  await expect(page.getByTestId("structured-empty")).toBeVisible();
}

async function openTerminal(page: Page) {
  await setupTerminal(page);
  await page.goto(TERM_URL);
  await expect(page.locator(".xterm-rows")).toBeVisible();
}

/** A dialog opened from `opener` closes on Escape and hands focus back to it, still mounted. */
async function closesBackTo(page: Page, dialog: Locator, opener: Locator) {
  await expect(dialog).toBeVisible();
  await animationsSettled(page);
  // The menu that opened it is gone and did not take the dialog with it.
  await expect(page.getByRole("menu")).toHaveCount(0);
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(opener).toBeVisible();
  await expect(opener).toBeFocused();
}

/** A real press at the element's centre. Playwright's own `click()` refuses a disabled control,
 *  which is the very thing under test: the press lands and nothing opens. */
async function pressAt(page: Page, el: Locator) {
  await el.scrollIntoViewIfNeeded();
  const box = (await el.boundingBox())!;
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  if (isMobile(page)) await page.touchscreen.tap(x, y);
  else await page.mouse.click(x, y);
}

// =================================================================================================
// The chip on the bar (above 800px)
// =================================================================================================

test("API session: the Share chip opens Public… and Peer…; each opens its dialog and returns to the chip", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "at ≤800px the head is one Actions menu: no chip");
  await page.setViewportSize({ width: 1600, height: 900 });
  await openApi(page);
  const chip = head(page).locator(SHARE_CHIP);
  await expect(chip).toBeVisible();
  await expect(chip).toHaveText("Share");
  await expect(chip).toHaveAttribute("aria-haspopup", "menu");
  await expect(chip).toHaveAttribute("aria-expanded", "false");
  // One chip, not two: the choices are not controls of their own on the bar.
  await expect(head(page).getByRole("button", { name: SHARE_PUBLIC })).toHaveCount(0);
  await expect(head(page).getByRole("button", { name: SHARE_PEER })).toHaveCount(0);

  await chip.click();
  const menu = page.getByRole("menu", { name: SHARE_MENU });
  await expect(menu).toBeVisible();
  await expect(chip).toHaveAttribute("aria-expanded", "true");
  await expect(menu.getByRole("menuitem")).toHaveText(["Public…", "Peer…"]);
  // An anchored popover under its chip, with focus inside it.
  const c = (await chip.boundingBox())!;
  const m = (await menu.boundingBox())!;
  expect(m.y).toBeGreaterThanOrEqual(c.y + c.height - 1);
  expect(m.y - (c.y + c.height)).toBeLessThan(16);
  expect(Math.abs(m.x + m.width - (c.x + c.width))).toBeLessThan(16);
  await expect(menu.getByRole("menuitem", { name: SHARE_PUBLIC })).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(menu.getByRole("menuitem", { name: SHARE_PEER })).toBeFocused();

  // Escape closes the menu and returns focus to the chip; nothing opened.
  await page.keyboard.press("Escape");
  await expect(menu).toHaveCount(0);
  await expect(chip).toBeFocused();
  await expect(chip).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByRole("dialog")).toHaveCount(0);

  await chip.click();
  await menu.getByRole("menuitem", { name: SHARE_PEER }).click();
  await expect(peerDialog(page)).toHaveAttribute("data-kind", "api");
  await closesBackTo(page, peerDialog(page), chip);

  await chip.click();
  await menu.getByRole("menuitem", { name: SHARE_PUBLIC }).click();
  await closesBackTo(page, publicDialog(page), chip);
});

test("terminal session: Share is the last chip; Public… is listed, off, with its reason, and opens nothing", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "at ≤800px the head is one Actions menu: no chip");
  await page.setViewportSize({ width: 1600, height: 900 });
  await openTerminal(page);
  const chip = head(page).locator(SHARE_CHIP);
  await expect(chip).toBeVisible();
  const ids = await head(page)
    .locator("[data-head-action]")
    .evaluateAll((els) => els.map((e) => e.getAttribute("data-head-action")));
  expect(ids[ids.length - 1]).toBe("share");
  // Hand off keeps its place on the bar (#783): Share did not push it into a fold.
  expect(ids).toContain("handoff");
  // The "…" is on the bar all the same (#2173 P2: it opens the session menu); nothing folded into
  // it, so its menu has no Pane group and does not name Share a second time.
  const more = head(page).getByRole("button", { name: MORE_MENU });
  await expect(more).toHaveCount(1);
  await more.click();
  const merged = page.getByRole("menu", { name: MORE_MENU });
  await expect(merged.locator("[data-menu-group]")).toHaveText(["Session"]);
  await page.keyboard.press("Escape");
  await expect(merged).toHaveCount(0);

  await chip.click();
  const menu = page.getByRole("menu", { name: SHARE_MENU });
  const off = menu.getByRole("menuitem", { name: SHARE_PUBLIC });
  await expect(off).toHaveAttribute("aria-disabled", "true");
  await expect(off).toContainText("Public…");
  await expect(off).toContainText(SHARE_PUBLIC_UNAVAILABLE);
  // Pressing it opens nothing and leaves the menu as it was.
  await pressAt(page, off);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(menu).toBeVisible();

  await menu.getByRole("menuitem", { name: SHARE_TERMINAL }).click();
  await expect(peerDialog(page)).toHaveAttribute("data-kind", "terminal");
  await closesBackTo(page, peerDialog(page), chip);
  await expect(publicDialog(page)).toHaveCount(0);
});

// =================================================================================================
// Folded into the head's "…" (above 800px, a bar with no room)
// =================================================================================================

test('folded into the "…": a Share label once, then the two items; the dialog returns to the "…"', async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "the measured fold is the >800px head");
  await page.setViewportSize({ width: 860, height: 800 });
  await openApi(page);
  const more = head(page).getByRole("button", { name: MORE_MENU });
  await expect(more).toBeVisible();
  // Share is among the first to give way.
  await expect(head(page).locator(SHARE_CHIP)).toHaveCount(0);

  await more.click();
  const menu = page.getByRole("menu", { name: MORE_MENU });
  await expect(menu).toBeVisible();
  // #2173 P2: the "…" is the merged session menu. The folded Share is in its Pane group, once;
  // the Session group does not repeat it.
  await expect(menu.locator("[data-menu-group]")).toHaveText(["Session", "Pane", "Share"]);
  // The label is no control: there is no "Share" row to press and no nested menu behind it.
  await expect(menu.getByRole("menuitem", { name: SHARE_MENU, exact: true })).toHaveCount(0);
  const names = await menu.getByRole("menuitem").evaluateAll((els) => els.map((e) => e.getAttribute("aria-label")));
  const at = names.indexOf(SHARE_PUBLIC);
  expect(at).toBeGreaterThanOrEqual(0);
  expect(names[at + 1]).toBe(SHARE_PEER);
  expect(names.filter((n) => n === SHARE_PUBLIC || n === SHARE_PEER)).toHaveLength(2);

  await menu.getByRole("menuitem", { name: SHARE_PEER }).click();
  await closesBackTo(page, peerDialog(page), more);

  await more.click();
  await menu.getByRole("menuitem", { name: SHARE_PUBLIC }).click();
  await closesBackTo(page, publicDialog(page), more);
});

// =================================================================================================
// The ≤800px Actions menu
// =================================================================================================

test("≤800px, API session: the Actions menu carries the Share group; each choice opens its dialog", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "phone-width contract");
  await openApi(page);
  await expect(head(page).locator("[data-head-action]")).toHaveCount(0);
  const trigger = head(page).getByTestId("head-actions-menu");
  await trigger.click();
  const menu = page.getByRole("menu", { name: ACTIONS_MENU });
  await expect(menu).toBeVisible();
  await expect(menu.locator("[data-menu-group]")).toHaveText(["Session", "Pane", "Share"]);
  await expect(menu.getByRole("menuitem", { name: SHARE_PUBLIC })).toHaveText("Public…");
  await expect(menu.getByRole("menuitem", { name: SHARE_PEER })).toHaveText("Peer…");
  // Still an anchored popover, not a sheet (docs/design.md §5): the pane shows around it.
  expect((await menu.boundingBox())!.width).toBeLessThan(page.viewportSize()!.width);

  await menu.getByRole("menuitem", { name: SHARE_PEER }).tap();
  await closesBackTo(page, peerDialog(page), trigger);

  await trigger.click();
  await menu.getByRole("menuitem", { name: SHARE_PUBLIC }).tap();
  await closesBackTo(page, publicDialog(page), trigger);
});

test("≤800px, terminal session: Public… is off with its reason and opens nothing; no Adopt", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "phone-width contract");
  await openTerminal(page);
  const trigger = head(page).getByTestId("head-actions-menu");
  await trigger.click();
  const menu = page.getByRole("menu", { name: ACTIONS_MENU });
  await expect(menu.locator("[data-menu-group]")).toHaveText(["Session", "Pane", "Share"]);
  const off = menu.getByRole("menuitem", { name: SHARE_PUBLIC });
  await off.scrollIntoViewIfNeeded();
  await expect(off).toBeDisabled();
  await expect(off).toContainText(SHARE_PUBLIC_UNAVAILABLE);
  // A real tap on the disabled row: nothing opens, the menu stays.
  await pressAt(page, off);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(menu).toBeVisible();
  // The server refuses every terminal session, so the head does not offer adoption.
  await expect(menu.getByRole("menuitem", { name: /adopt/i })).toHaveCount(0);

  await menu.getByRole("menuitem", { name: SHARE_TERMINAL }).tap();
  await expect(peerDialog(page)).toHaveAttribute("data-kind", "terminal");
  await closesBackTo(page, peerDialog(page), trigger);
});

test("≤800px on a SHORT screen: the menu scrolls to its last entry, Peer…, and a tap opens it", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "touch contract");
  await page.setViewportSize({ width: 740, height: 360 });
  await openTerminal(page);
  const trigger = head(page).getByTestId("head-actions-menu");
  await trigger.click();
  const menu = page.getByRole("menu", { name: ACTIONS_MENU });
  await expect(menu).toBeVisible();
  const box = (await menu.boundingBox())!;
  expect(box.y + box.height).toBeLessThanOrEqual(360);
  const last = menu.getByRole("menuitem").last();
  await expect(last).toHaveAttribute("aria-label", SHARE_TERMINAL);
  await last.scrollIntoViewIfNeeded();
  await expect(last).toBeInViewport({ ratio: 1 });
  await last.tap();
  await expect(peerDialog(page)).toBeVisible();
});

// =================================================================================================
// The sidebar row's ⋯ menu
// =================================================================================================

async function openRowMenu(page: Page, title: string) {
  const list = await showList(page);
  const row = list.getByRole("listitem").filter({ hasText: title });
  await expect(row).toBeVisible();
  if (!isMobile(page)) await row.hover();
  const trigger = row.getByRole("button", { name: "Session actions" });
  await trigger.click();
  const menu = page.getByRole("menu", { name: "Session actions" });
  await expect(menu).toBeVisible();
  await animationsSettled(page);
  return { menu, trigger };
}

test("the row ⋯ menu of an API session: the Share group once; Peer… and Public… open their dialogs", async ({
  page,
}) => {
  await openApi(page);
  let { menu, trigger } = await openRowMenu(page, TITLE);
  await expect(menu.locator('[data-menu-group="Share"]')).toHaveCount(1);
  const names = await menu.getByRole("menuitem").evaluateAll((els) => els.map((e) => e.getAttribute("aria-label")));
  // After the primary group (brief, hand off, pop out), Public… then Peer….
  expect(names.slice(0, 5)).toEqual([
    "Open session brief",
    "Hand off session to another engine",
    "Pop out session into its own window",
    SHARE_PUBLIC,
    SHARE_PEER,
  ]);
  await expect(menu.getByRole("menuitem", { name: SHARE_PUBLIC })).toHaveText("Public…");
  await expect(menu.getByRole("menuitem", { name: SHARE_PUBLIC })).not.toHaveAttribute("aria-disabled", "true");

  await menu.getByRole("menuitem", { name: SHARE_PEER }).click();
  await expect(peerDialog(page)).toHaveAttribute("data-kind", "api");
  await expect(peerDialog(page).getByText(TITLE)).toBeVisible();
  await expect(page.getByRole("menu")).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(peerDialog(page)).toHaveCount(0);
  // The row's own ⋯ is still there to take focus back.
  await expect(trigger).toBeVisible();
  if (!isMobile(page)) await expect(trigger).toBeFocused();

  ({ menu, trigger } = await openRowMenu(page, TITLE));
  await menu.getByRole("menuitem", { name: SHARE_PUBLIC }).click();
  await expect(publicDialog(page)).toBeVisible();
  await expect(page.getByRole("menu")).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(publicDialog(page)).toHaveCount(0);
  await expect(trigger).toBeVisible();
});

test("the row ⋯ menu of a terminal session: Public… is off with its reason; Peer… shares the screen", async ({
  page,
}) => {
  await openTerminal(page);
  const { menu } = await openRowMenu(page, TERM_TITLE);
  await expect(menu.locator('[data-menu-group="Share"]')).toHaveCount(1);
  const off = menu.getByRole("menuitem", { name: SHARE_PUBLIC });
  await expect(off).toHaveAttribute("aria-disabled", "true");
  await expect(off).toContainText(SHARE_PUBLIC_UNAVAILABLE);
  await pressAt(page, off);
  await expect(page.getByRole("dialog", { name: /share/i })).toHaveCount(0);
  await expect(menu).toBeVisible();

  await menu.getByRole("menuitem", { name: SHARE_TERMINAL }).click();
  await expect(peerDialog(page)).toHaveAttribute("data-kind", "terminal");
  await expect(page.getByRole("menu")).toHaveCount(0);
});
