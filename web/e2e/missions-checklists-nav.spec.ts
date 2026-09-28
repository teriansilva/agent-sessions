/** The mission checklists live under Missions, not Settings.
 *
 *  Missions is a split control like Sessions and Dashboard: the label still goes to the console,
 *  and its chevron opens a menu naming the console and Checklists. The old Settings address
 *  redirects, and Settings no longer lists the tab. A real browser, because the menu is a
 *  portalled popover in the top bar and "where am I" is `aria-current` on laid-out links.
 */
import { expect, test, type Page } from "@playwright/test";

import { CHECKLISTS_PATH, MISSION_PATH } from "../src/lib/routes";
import { settingsPath } from "../src/routes/settingsTabs";
import { commonMocks } from "./mission-directions";
import { mockMissions } from "./mission-console";

async function missionsMenu(page: Page) {
  // The phone drawer is modal and makes the header inert; close it first, as `openMapFromNav` does.
  if ((await page.locator(".app.navOpen").count()) > 0) {
    await page.keyboard.press("Escape");
    await expect(page.locator(".app.navOpen")).toHaveCount(0);
  }
  await page.locator('.hud-topbar [data-testid="section-menu-mission"]').click();
  return page.locator('[data-testid="section-menu-mission-panel"]');
}

test.beforeEach(async ({ page }) => {
  await commonMocks(page);
  await mockMissions(page);
  await page.route("**/api/pulse/orchestrator", (r) =>
    r.fulfill({ status: 500, json: { detail: "off" } }),
  );
});

test("Missions has a Checklists entry, and it opens the checklist editor as a page", async ({
  page,
}) => {
  await page.goto(MISSION_PATH);
  const menu = await missionsMenu(page);
  await expect(menu.locator("a[data-subsection]")).toHaveText(["Missions", "Checklists"]);
  await menu.locator('a[data-subsection="checklists"]').click();

  await expect(page).toHaveURL(new RegExp(`${CHECKLISTS_PATH}$`));
  await expect(page.getByRole("heading", { level: 1, name: "Checklists" })).toBeVisible();
  await expect(page.getByTestId("mission-playbooks")).toBeVisible();
  // On a child's route the parent says "you are in here", not "you are exactly here".
  await expect(
    page.locator(`.hud-topbar a[href="${MISSION_PATH}"]`).first(),
  ).toHaveAttribute("aria-current", "true");

  const again = await missionsMenu(page);
  await expect(again.locator('a[data-subsection="checklists"]')).toHaveAttribute(
    "aria-current",
    "page",
  );
  // The label and the menu's first entry still go to the console.
  await again.locator('a[data-subsection="mission"]').click();
  await expect(page).toHaveURL(new RegExp(`${MISSION_PATH}$`));
});

test("the old Settings address redirects, and Settings no longer lists Checklists", async ({
  page,
}) => {
  await page.goto("/settings/ai-playbooks");
  await expect(page).toHaveURL(new RegExp(`${CHECKLISTS_PATH}$`));
  await expect(page.getByTestId("mission-playbooks")).toBeVisible();

  // Settings' own navigation has rendered (it links its sections) and no longer links the old tab.
  await page.goto(settingsPath());
  const nav = page.getByRole("navigation", { name: "Settings" }).first();
  await expect(nav.locator(`a[href="${settingsPath("ai-mission-control")}"]`)).toHaveCount(1);
  await expect(nav.locator('a[href="/settings/ai-playbooks"]')).toHaveCount(0);
  await expect(nav.getByRole("link", { name: "Checklists", exact: true })).toHaveCount(0);
});
