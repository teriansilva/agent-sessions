/** The session pane's own menu is the merged session menu (#2173 P2).
 *
 * The "…" in a session's header (the "Actions" trigger at ≤800px) opens the SAME menu a map
 * window's chrome opens: the Session group (brief, hand off, Share, mission, favorite, rename, tag,
 * move, archive, whatever the pane does not already offer itself), then the Pane group (the head
 * actions that are not chips). Before this the pane's menu listed only its own folded actions, and
 * did not exist at all while every chip fit.
 *
 * What is pinned here, in a real browser, on both projects:
 *  - the trigger is there with every chip on the bar, and its menu carries the Session group;
 *  - Rename, Favorite and Archive from it change the SIDEBAR row without a reload, and the pane's
 *    own row (the menu's next opening reads the new state);
 *  - Archive leaves the pane exactly as archiving the open session from the sidebar does;
 *  - a dialog opened from the menu outlives it, and focus returns to the trigger, still mounted;
 *  - on a phone the menu is an anchored popover (not a bottom sheet), carries both groups, scrolls
 *    to its last entry on a short screen, and can be dismissed.
 */
import { expect, test, type Locator, type Page } from "@playwright/test";
import { ACTIONS_MENU, MORE_MENU } from "./headActions";
import { animationsSettled } from "./nightjarShell";
import { ID, ID2, SHARE_PEER, TITLE, TITLE2, URL_PATH, isMobile, setup, showList } from "./shareFixture";
import { setupBench } from "./terminal/harness";

const T1 = "7c1e3d2f-9a4b-4d8e-8f32-7b5c4d3e2f10";
const T2 = "8d2f4e3a-0b5c-4e9f-9a43-8c6d5e4f3a21";
const T1_TITLE = "Deploy the staging stack";
const T2_TITLE = "Trim the changelog";
const T1_URL = `/s/claude/${T1}`;

type Row = Record<string, unknown> & { id: string; title: string; sticky: boolean; archived: boolean };

/** The session routes the menu's actions call, over ONE mutable set of rows: the list (scoped to
 *  an archived-state, as the server's is), the single-row lookup, and the writes. Registered after
 *  the fixture's own routes, so these answer first. */
async function serveRows(page: Page, rows: Row[]) {
  const writes: string[] = [];
  await page.route(/\/api\/sessions(\?.*)?$/, (r) => {
    const p = new URL(r.request().url()).searchParams;
    const archived = p.get("archived") === "1" || p.get("archived") === "true";
    const sessions = rows.filter((s) => s.archived === archived);
    return r.fulfill({
      json: { sessions, next_offset: null, total: sessions.length, facets: { projects: [], engines: [] } },
    });
  });
  await page.route(/\/api\/sessions\/[^/?]+$/, (r) => {
    const key = decodeURIComponent(new URL(r.request().url()).pathname.split("/").pop()!);
    const row = rows.find((s) => s.id === key);
    return row ? r.fulfill({ json: row }) : r.fulfill({ status: 404, json: { detail: "no such session" } });
  });
  await page.route(/\/api\/sessions\/[^/?]+\/(rename|favorite|unfavorite|archive|unarchive)$/, (r) => {
    const parts = new URL(r.request().url()).pathname.split("/");
    const verb = parts.pop()!;
    const key = decodeURIComponent(parts.pop()!);
    const row = rows.find((s) => s.id === key);
    if (!row || r.request().method() !== "POST") return r.fulfill({ status: 404, json: { detail: "no" } });
    writes.push(`${verb} ${key}`);
    if (verb === "rename") row.title = String((r.request().postDataJSON() as { title: string }).title).trim();
    if (verb === "favorite") row.sticky = true;
    if (verb === "unfavorite") row.sticky = false;
    if (verb === "archive") row.archived = true;
    if (verb === "unarchive") row.archived = false;
    return r.fulfill({ json: { id: key, title: row.title, sticky: row.sticky, archived: row.archived } });
  });
  return writes;
}

function row(engine: string, uuid: string, title: string, ago: number): Row {
  return {
    id: `${engine}:${uuid}`,
    engine,
    uuid,
    short_uuid: uuid.slice(0, 8),
    cwd: "/home/u/proj",
    project: { kind: "folder", id: "/home/u/proj", name: "proj" },
    last_mtime: Math.floor(Date.now() / 1000) - ago,
    first_user_message: title,
    title,
    sticky: false,
    archived: false,
    ai_summary: "",
    mission: null,
  };
}

async function openTerminal(page: Page) {
  await setupBench(page, {
    sessions: [
      { engine: "claude", uuid: T1, title: T1_TITLE },
      { engine: "claude", uuid: T2, title: T2_TITLE },
    ],
  });
  const writes = await serveRows(page, [row("claude", T1, T1_TITLE, 60), row("claude", T2, T2_TITLE, 3600)]);
  if (!isMobile(page)) await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto(T1_URL);
  await expect(page.locator(".xterm-rows")).toBeVisible();
  return writes;
}

async function openApi(page: Page) {
  await setup(page);
  const writes = await serveRows(page, [row("codex-api", ID, TITLE, 120), row("codex-api", ID2, TITLE2, 3600)]);
  if (!isMobile(page)) await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto(URL_PATH);
  await expect(page.getByTestId("structured-empty")).toBeVisible();
  return writes;
}

const head = (page: Page) => page.locator('[class*="panelHead"]');
const trigger = (page: Page) => head(page).locator("[data-head-more]");
const paneMenu = (page: Page) => page.getByRole("menu", { name: isMobile(page) ? ACTIONS_MENU : MORE_MENU });
const group = (menu: Locator, name: string) => menu.locator(`[data-menu-group="${name}"]`);

/** Open the pane's own menu from its trigger. */
async function openPaneMenu(page: Page) {
  const t = trigger(page);
  await expect(t).toBeVisible();
  await t.click();
  const menu = paneMenu(page);
  await expect(menu).toBeVisible();
  await animationsSettled(page);
  return menu;
}

/** The sidebar row for `title`, with the list on screen (a drawer on a phone). */
async function sidebarRow(page: Page, title: string) {
  const list = await showList(page);
  return list.getByRole("listitem").filter({ hasText: title });
}

/** Put the phone's list drawer away again, so the pane's header can be pressed. */
async function hideList(page: Page) {
  if (!isMobile(page)) return;
  const toggle = page.locator("header .navToggle");
  // The open drawer is modal (its scrim covers the toggle), so it closes by Escape.
  if ((await toggle.getAttribute("aria-expanded")) === "true") await page.keyboard.press("Escape");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await animationsSettled(page);
}

// =================================================================================================
// The merged list
// =================================================================================================

test("terminal pane: the … is there with every chip on the bar, and opens Session then Pane", async ({ page }) => {
  await openTerminal(page);
  // Before #2173 P2 a wide pane had no "…" at all (nothing folded), so the session's own actions
  // were out of reach from the pane.
  const menu = await openPaneMenu(page);
  await expect(group(menu, "Session")).toHaveCount(1);
  for (const name of ["Favorite session", "Rename session", "Set session tag", "Move session to a project", "Archive session"])
    await expect(menu.getByRole("menuitem", { name, exact: true })).toHaveCount(1);
  if (isMobile(page)) {
    // The compact header folds every head action: one Pane group, one Share group, after Session.
    await expect(group(menu, "Pane")).toHaveCount(1);
    await expect(group(menu, "Share")).toHaveCount(1);
    await expect(menu.getByRole("menuitem", { name: "Open session brief" })).toHaveCount(1);
    await expect(menu.getByRole("menuitem", { name: "Hand off session to another engine" })).toHaveCount(1);
    const order = await menu
      .locator("[data-menu-group]")
      .evaluateAll((els) => els.map((e) => e.getAttribute("data-menu-group")));
    expect(order).toEqual(["Session", "Pane", "Share"]);
  } else {
    // Recap, Hand off and Share are chips on this bar: the menu does not name them a second time.
    await expect(head(page).locator('[data-head-action="share"]')).toBeVisible();
    await expect(head(page).locator('[data-head-action="handoff"]')).toBeVisible();
    await expect(group(menu, "Share")).toHaveCount(0);
    await expect(menu.getByRole("menuitem", { name: "Open session brief" })).toHaveCount(0);
    await expect(menu.getByRole("menuitem", { name: "Hand off session to another engine" })).toHaveCount(0);
  }
  // A terminal session is not adoptable (#1902): offered by neither group.
  await expect(menu.getByRole("menuitem", { name: /adopt/i })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(menu).toHaveCount(0);
  await expect(trigger(page)).toBeFocused();
});

test("API pane: the same merged menu, the Session group once", async ({ page }) => {
  await openApi(page);
  const menu = await openPaneMenu(page);
  await expect(group(menu, "Session")).toHaveCount(1);
  await expect(menu.getByRole("menuitem", { name: "Rename session", exact: true })).toHaveCount(1);
  await expect(menu.getByRole("menuitem", { name: "Archive session", exact: true })).toHaveCount(1);
  // Share is the pane's own action: a chip on a wide bar, the Pane group's Share on a phone.
  await expect(group(menu, "Share")).toHaveCount(isMobile(page) ? 1 : 0);
  await expect(menu.getByRole("menuitem", { name: SHARE_PEER })).toHaveCount(isMobile(page) ? 1 : 0);
  if (isMobile(page)) await expect(group(menu, "Pane")).toHaveCount(1);
  else expect(await group(menu, "Pane").count()).toBeLessThanOrEqual(1);
});

// =================================================================================================
// Rename, Favorite, Archive: the sidebar row follows
// =================================================================================================

test("Rename and Favorite from the pane's menu update the sidebar row and the pane's own row", async ({ page }) => {
  const writes = await openTerminal(page);
  let menu = await openPaneMenu(page);
  await menu.getByRole("menuitem", { name: "Rename session", exact: true }).click();
  await expect(menu).toHaveCount(0);
  const dialog = page.getByRole("dialog");
  const field = dialog.getByRole("textbox", { name: "Session title" });
  await expect(field).toHaveValue(T1_TITLE);
  await field.fill("Staging stack, take two");
  await field.press("Enter");
  await expect(dialog).toHaveCount(0);
  await expect.poll(() => writes).toContain(`rename claude:${T1}`);
  // No reload, no poll to wait for: the sidebar row carries the new title.
  await expect(await sidebarRow(page, "Staging stack, take two")).toBeVisible();
  await expect(await sidebarRow(page, T1_TITLE)).toHaveCount(0);
  await hideList(page);

  menu = await openPaneMenu(page);
  await menu.getByRole("menuitem", { name: "Favorite session", exact: true }).click();
  await expect.poll(() => writes).toContain(`favorite claude:${T1}`);
  const fav = await sidebarRow(page, "Staging stack, take two");
  await expect(fav.locator('[title="Favorited"]')).toBeVisible();
  await expect((await sidebarRow(page, T2_TITLE)).locator('[title="Favorited"]')).toHaveCount(0);
  await hideList(page);
  // The pane's own row followed too: its menu now offers the opposite.
  menu = await openPaneMenu(page);
  await expect(menu.getByRole("menuitem", { name: "Unfavorite session", exact: true })).toHaveCount(1);
  await expect(menu.getByRole("menuitem", { name: "Favorite session", exact: true })).toHaveCount(0);
});

/** What archiving the OPEN session leaves behind, whichever menu it was pressed in. */
async function expectArchivedAndLeft(page: Page, writes: string[]) {
  await expect.poll(() => writes.filter((w) => w.startsWith("archive"))).toEqual([`archive claude:${T1}`]);
  // The pane is gone: the route left the session (#631), so no socket can relaunch into it.
  await expect(page).toHaveURL(/\/new$/);
  await expect(page.locator(".xterm-rows")).toHaveCount(0);
  await expect(await sidebarRow(page, T1_TITLE)).toHaveCount(0);
  await expect(await sidebarRow(page, T2_TITLE)).toBeVisible();
}

test("Archive from the pane's menu drops the sidebar row and leaves the pane", async ({ page }) => {
  const writes = await openTerminal(page);
  const menu = await openPaneMenu(page);
  await menu.getByRole("menuitem", { name: "Archive session", exact: true }).click();
  await expectArchivedAndLeft(page, writes);
});

// The reference the test above is held to. Not new behaviour: it passes before this change too.
test("reference: archiving the open session from the SIDEBAR ends in the same place", async ({ page }) => {
  const writes = await openTerminal(page);
  const r = await sidebarRow(page, T1_TITLE);
  if (!isMobile(page)) await r.hover();
  await r.getByRole("button", { name: "Session actions" }).click();
  await page
    .getByRole("menu", { name: "Session actions" })
    .getByRole("menuitem", { name: "Archive session", exact: true })
    .click();
  await expectArchivedAndLeft(page, writes);
});

// =================================================================================================
// A dialog outlives the menu
// =================================================================================================

test("a dialog opened from the pane's menu outlives it and returns focus to the trigger", async ({ page }) => {
  await openApi(page);
  const t = trigger(page);
  let menu = await openPaneMenu(page);
  // From the SESSION group: Move to project… (its state lives in the menu's host).
  await menu.getByRole("menuitem", { name: "Move session to a project", exact: true }).click();
  const move = page.getByRole("dialog");
  await expect(move).toBeVisible();
  await animationsSettled(page);
  await expect(page.getByRole("menu")).toHaveCount(0);
  await expect(move).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(move).toHaveCount(0);
  await expect(t).toBeVisible();
  await expect(t).toBeFocused();

  if (!isMobile(page)) return;
  // From the PANE group, on a phone (above 800px Share is a chip, pinned by session-share-menu).
  menu = await openPaneMenu(page);
  await menu.getByRole("menuitem", { name: SHARE_PEER }).click();
  const peer = page.getByRole("dialog", { name: "Share with a peer" });
  await expect(peer).toBeVisible();
  await animationsSettled(page);
  await expect(page.getByRole("menu")).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(peer).toHaveCount(0);
  await expect(t).toBeFocused();
});

// =================================================================================================
// The phone: an anchored popover, both groups, scrollable, dismissable
// =================================================================================================

test("phone, short screen: the Actions menu is anchored, carries both groups, scrolls to its last entry and dismisses", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "touch contract");
  await page.setViewportSize({ width: 740, height: 360 });
  await openTerminal(page);
  const t = head(page).getByTestId("head-actions-menu");
  let menu = await openPaneMenu(page);
  await expect(group(menu, "Session")).toHaveCount(1);
  await expect(group(menu, "Pane")).toHaveCount(1);
  // Anchored under its trigger, not a bottom sheet (docs/design.md §5): no sheet heading, no
  // Cancel row, the terminal still visible beside it, and all of it inside the short viewport.
  const tb = (await t.boundingBox())!;
  const mb = (await menu.boundingBox())!;
  expect(mb.y).toBeGreaterThanOrEqual(tb.y + tb.height - 1);
  expect(mb.y - (tb.y + tb.height)).toBeLessThan(16);
  expect(mb.y + mb.height).toBeLessThanOrEqual(360);
  expect(mb.width).toBeLessThan(740 * 0.7);
  await expect(menu.getByRole("button", { name: "Cancel" })).toBeHidden();
  // Taller than the space under the trigger, so it scrolls inside its own box.
  expect(await menu.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true);
  const items = menu.getByRole("menuitem");
  const first = items.first();
  const last = items.last();
  await expect(first).toBeInViewport({ ratio: 1 });
  await last.scrollIntoViewIfNeeded();
  await expect(last).toBeInViewport({ ratio: 1 });
  const lb = (await last.boundingBox())!;
  expect(lb.y + lb.height).toBeLessThanOrEqual(mb.y + mb.height + 1);
  // The last entry is the pane's Share ▸ Peer…, and a tap on it works.
  await expect(last).toHaveAttribute("aria-label", "Share this terminal's screen with a paired instance");
  await last.tap();
  const peer = page.getByRole("dialog", { name: "Share with a peer" });
  await expect(peer).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(peer).toHaveCount(0);

  // Dismissable: a tap outside closes it (no scrim needed), and so does the trigger itself.
  menu = await openPaneMenu(page);
  await page.touchscreen.tap(30, 300);
  await expect(menu).toHaveCount(0);
  menu = await openPaneMenu(page);
  await expect(t).toHaveAttribute("aria-expanded", "true");
  await t.tap();
  await expect(menu).toHaveCount(0);
  await expect(t).toHaveAttribute("aria-expanded", "false");
});

test("short screen: End, Home and the arrows keep the focused entry inside the menu's visible box", async ({
  page,
}) => {
  // The merged menu is taller than a short screen and scrolls inside its own box. A keyboard
  // move must bring its destination into that box: focus on an entry scrolled out of sight
  // would let Enter run an action nobody can see (#2173 review).
  await page.setViewportSize({ width: 740, height: 360 });
  await openTerminal(page);
  const menu = await openPaneMenu(page);
  expect(await menu.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true);
  const items = menu.getByRole("menuitem");
  const count = await items.count();
  const focusedInsideMenu = async () => {
    const box = await menu.evaluate((el) => {
      const m = el.getBoundingClientRect();
      const a = document.activeElement as HTMLElement | null;
      if (!a || !el.contains(a)) return null;
      const r = a.getBoundingClientRect();
      return { top: r.top - m.top, bottom: m.bottom - r.bottom, vh: window.innerHeight, mb: m.bottom };
    });
    expect(box, "focus is on an entry of the menu").not.toBeNull();
    expect(box!.top).toBeGreaterThanOrEqual(-1);
    expect(box!.bottom).toBeGreaterThanOrEqual(-1);
    expect(box!.mb).toBeLessThanOrEqual(box!.vh + 1);
  };
  await expect(items.first()).toBeFocused();
  await page.keyboard.press("End");
  await expect(items.last()).toBeFocused();
  await focusedInsideMenu();
  await page.keyboard.press("Home");
  await expect(items.first()).toBeFocused();
  await focusedInsideMenu();
  for (let i = 1; i < count; i++) {
    await page.keyboard.press("ArrowDown");
    await focusedInsideMenu();
  }
  await expect(items.last()).toBeFocused();
  // Wrapping from the last entry lands on the first, visibly; and back up again.
  await page.keyboard.press("ArrowDown");
  await expect(items.first()).toBeFocused();
  await focusedInsideMenu();
  await page.keyboard.press("ArrowUp");
  await expect(items.last()).toBeFocused();
  await focusedInsideMenu();
  // The page itself did not scroll under the fixed menu.
  expect(await page.evaluate(() => window.scrollY)).toBe(0);
});
