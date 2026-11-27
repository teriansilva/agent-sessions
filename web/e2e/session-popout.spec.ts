import { expect, test } from "@playwright/test";
import { STRUCTURED_ID } from "./apiLookFixture";
import {
  API,
  API_TITLE,
  expectFillsWindow,
  expectNoShell,
  openPaneMenu,
  PTY,
  PTY_ID,
  PTY_KEY,
  PTY_TITLE,
  setup,
} from "./sessionPopout";
import { POPOUT_PATH } from "../src/lib/routes";

/** Pop out a session: the session alone in its own window or tab, without the app shell.
 *
 *  Real-browser proof on the desktop AND mobile projects. Red on main: there is no "Pop out" item
 *  and no `/pop/:engine/:id` route (it falls through to the dashboard, shell and all). Green: the
 *  item opens a second page on the pop-out route that shows the session pane and NONE of the
 *  shell — no top bar, session list, section nav or footer — and the pane fills the window.
 *
 *  Both runtimes: an API session (the strategic default) and a terminal session, whose pop-out
 *  must ATTACH (a plain socket, never `new=1`). Inside the pop-out the session menu offers neither
 *  "Pop out" nor "To map". Popping the same session out twice reuses its window. */

test("an API session pops out from its pane menu: the conversation alone, no shell", async ({
  page,
}) => {
  await setup(page);
  await page.goto(`/s/${API}/${STRUCTURED_ID}`);
  await expect(page.getByTestId("structured-pane")).toBeVisible();

  const menu = await openPaneMenu(page);
  const [popup] = await Promise.all([
    page.context().waitForEvent("page"),
    menu.getByRole("menuitem", { name: "Pop out session into its own window" }).click(),
  ]);
  await popup.waitForLoadState();
  await expect(popup).toHaveURL(new RegExp(`${POPOUT_PATH}/${API}/${STRUCTURED_ID}$`));
  await expect(popup.getByTestId("structured-pane")).toBeVisible();
  await expect(popup.getByTestId("md-code").first()).toBeVisible();
  await expectNoShell(popup);
  await expectFillsWindow(popup);
  await expect(popup).toHaveTitle(`${API_TITLE} · BattleLab`);

  // Inside the pop-out the menu neither pops out again nor sends it to the map.
  const inner = await openPaneMenu(popup);
  // (Session brief and Hand off are pane chips here, so the merged menu omits their twins.)
  await expect(inner.getByRole("menuitem", { name: "Archive session" })).toBeVisible();
  await expect(inner.getByRole("menuitem", { name: /Pop out/ })).toHaveCount(0);
  await expect(popup.getByRole("button", { name: /open this session as a window on the map/i })).toHaveCount(0);
  await expect(inner.getByRole("menuitem", { name: /window on the map/i })).toHaveCount(0);

  // The main window is untouched: still the session, shell and all.
  await expect(page).toHaveURL(new RegExp(`/s/${API}/${STRUCTURED_ID}$`));
  await expect(page.locator(".hud-topbar")).toBeVisible();
});

test("a terminal session pops out from the sidebar row menu and ATTACHES; a second pop-out reuses the window", async ({
  page,
}, testInfo) => {
  const { sockets } = await setup(page);
  await page.goto(`/s/${API}/${STRUCTURED_ID}`);
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  if (testInfo.project.name === "mobile") await page.locator("header .navToggle").click();
  await expect(page.locator("aside.sidebar")).toBeVisible();

  const openRowMenu = async () => {
    const r = page.getByRole("listitem").filter({ hasText: PTY_TITLE });
    await expect(r).toBeVisible();
    await r.hover();
    await r.getByRole("button", { name: "Session actions" }).click();
    const menu = page.getByRole("menu", { name: "Session actions" });
    await expect(menu).toBeVisible();
    return menu;
  };

  const menu = await openRowMenu();
  const [popup] = await Promise.all([
    page.context().waitForEvent("page"),
    menu.getByRole("menuitem", { name: "Pop out session into its own window" }).click(),
  ]);
  await popup.waitForLoadState();
  await expect(popup).toHaveURL(new RegExp(`${POPOUT_PATH}/${PTY}/${PTY_ID}$`));
  await expect(popup.locator(".xterm")).toBeVisible();
  await expectNoShell(popup);
  await expectFillsWindow(popup);

  // It attached: one socket for this session, and never a launch.
  await expect.poll(() => sockets.filter((u) => u.includes(`/ws/term/${PTY_KEY}`)).length).toBe(1);
  for (const u of sockets) expect(u).not.toContain("new=1");

  // Popping the same session out again goes to ITS window, not a new one.
  if (testInfo.project.name === "mobile" && !(await page.locator("aside.sidebar").isVisible()))
    await page.locator("header .navToggle").click();
  const again = await openRowMenu();
  await again.getByRole("menuitem", { name: "Pop out session into its own window" }).click();
  await expect.poll(() => page.context().pages().length).toBe(2);
  await expect(popup).toHaveURL(new RegExp(`${POPOUT_PATH}/${PTY}/${PTY_ID}$`));
});

test("a pop-out link opened directly shows the session alone", async ({ page }) => {
  await setup(page);
  await page.goto(`${POPOUT_PATH}/${API}/${STRUCTURED_ID}`);
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  await expectNoShell(page);
  await expectFillsWindow(page);
});
