import { expect, type Locator, type Page } from "@playwright/test";

/** Reach a session-pane header action wherever `HeadActions` put it.
 *
 *  There are three shipped placements, and a spec that is about the ACTION (not the header's
 *  layout) should not care which one it got:
 *
 *   - **≤800px viewport (#948 P6)** — no inline chips at all; ONE "Actions for this session"
 *     trigger (`data-testid="head-actions-menu"`) opens a menu carrying every action. This is a
 *     viewport breakpoint (`useIsMobile`), so it applies to the desktop project too whenever a
 *     spec sets a narrow viewport.
 *   - **wider, the action fits** — an inline chip in the pane header.
 *   - **wider, the action folded (#783)** — an item in the "More session actions" overflow menu.
 *   - **a choice of a chip with a menu (#2173, Share → Public… / Peer…)** — an item of the small
 *     menu that chip opens when it is on the bar; folded, an ordinary item of the menus above.
 *
 *  Every placement keeps the action's accessible name, so callers pass the same name they would
 *  have used for the chip. */

export const ACTIONS_MENU = "Actions for this session";
export const MORE_MENU = "More session actions";
/** A head chip that opens a menu of choices instead of running one command (#2173, Share). */
export const GROUP_CHIP = '[data-head-action][aria-haspopup="menu"]';
/** The Share chip, and the reason its Public… choice is off on a terminal session. */
export const SHARE_CHIP = '[data-head-action="share"]';
export const SHARE_PUBLIC = "Share this session publicly";
export const SHARE_PUBLIC_UNAVAILABLE = "Not available for terminal sessions";
/** The Files toggle's accessible name. Scoped to the header/menu here, so it cannot collide with
 *  the file panel's own "Files" tab. */
export const FILES_ACTION = "Browse session files";

export interface HeadActionOptions {
  /** Narrow to one pane (e.g. a map window) when several headers are mounted. */
  scope?: Locator;
  /** Override the wait for the header to render (a relay-backed spec needs longer). */
  timeout?: number;
}

/** The pane header. `[class*="panelHead"]` because CSS-module class names are hashed. */
export function paneHead(page: Page, scope?: Locator): Locator {
  return (scope ?? page).locator('[class*="panelHead"]');
}

/** The single small-screen trigger. */
export function actionsMenuTrigger(page: Page, scope?: Locator): Locator {
  return paneHead(page, scope).getByTestId("head-actions-menu");
}

/** Wait until the header has rendered its actions, and report the placement it chose. Waits on
 *  what is ACTUALLY there — the collapsed trigger or an inline chip — so there is no sleep and no
 *  race against the first render. */
export async function headActionsReady(
  page: Page,
  opts: HeadActionOptions = {},
): Promise<"collapsed" | "inline"> {
  const trigger = actionsMenuTrigger(page, opts.scope);
  const chip = paneHead(page, opts.scope).locator("[data-head-action]").first();
  await expect(trigger.or(chip).first()).toBeVisible({ timeout: opts.timeout });
  return (await trigger.isVisible()) ? "collapsed" : "inline";
}

async function resolve(
  page: Page,
  name: RegExp | string,
  opts: HeadActionOptions,
): Promise<{ control: Locator; returnsFocusTo: Locator }> {
  const head = paneHead(page, opts.scope);
  if ((await headActionsReady(page, opts)) === "collapsed") {
    const trigger = actionsMenuTrigger(page, opts.scope);
    const menu = page.getByRole("menu", { name: ACTIONS_MENU });
    if (!(await menu.isVisible())) await trigger.click();
    await expect(menu).toBeVisible();
    return { control: menu.getByRole("menuitem", { name }), returnsFocusTo: trigger };
  }
  const chip = head.getByRole("button", { name });
  const more = head.getByRole("button", { name: MORE_MENU });
  const groups = head.locator(GROUP_CHIP);
  await expect(chip.or(more).or(groups).first()).toBeVisible({ timeout: opts.timeout });
  if (await chip.isVisible()) return { control: chip, returnsFocusTo: chip };
  // A choice of a chip that opens a menu (#2173): look in each such chip's menu.
  for (const group of await groups.all()) {
    if ((await group.getAttribute("aria-expanded")) !== "true") await group.click();
    const item = page.getByRole("menu").getByRole("menuitem", { name });
    if (await item.count()) return { control: item, returnsFocusTo: group };
    await page.keyboard.press("Escape");
  }
  const menu = page.getByRole("menu", { name: MORE_MENU });
  if (!(await menu.isVisible())) await more.click();
  await expect(menu).toBeVisible();
  return { control: menu.getByRole("menuitem", { name }), returnsFocusTo: more };
}

/** The control for `name` that is reachable right now: the inline chip, or — after opening the
 *  relevant menu if it is not already open — the menu item. The caller clicks/taps/asserts it. */
export async function headAction(
  page: Page,
  name: RegExp | string,
  opts: HeadActionOptions = {},
): Promise<Locator> {
  return (await resolve(page, name, opts)).control;
}

/** Click the action wherever it lives. Returns the element focus returns to when whatever the
 *  action opened is closed: the chip itself inline, otherwise the menu trigger (a menu item
 *  unmounts with its menu, so `HeadActions` hands the trigger over instead). */
export async function clickHeadAction(
  page: Page,
  name: RegExp | string,
  opts: HeadActionOptions = {},
): Promise<Locator> {
  const { control, returnsFocusTo } = await resolve(page, name, opts);
  await expect(control).toBeVisible({ timeout: opts.timeout });
  await control.click();
  return returnsFocusTo;
}

/** Close a head-actions menu left open by `headAction`, if one is open. */
export async function closeHeadActionsMenu(page: Page): Promise<void> {
  const menu = page.getByRole("menu", { name: new RegExp(`^(${ACTIONS_MENU}|${MORE_MENU})$`) });
  if (!(await menu.isVisible())) return;
  await page.keyboard.press("Escape");
  await expect(menu).toBeHidden();
}
