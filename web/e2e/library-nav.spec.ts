/** Library (#1294): Templates, Automations, Checklists and Playbooks, under one section.
 *
 *  Library is a split control like Sessions: the label goes to Templates (its first child), and
 *  its chevron opens a menu naming all four. Checklists and Automations moved here from Missions
 *  but kept their `/mission/...` URLs, so on their routes LIBRARY is the section that lights up,
 *  not Missions. The old Settings address for checklists still redirects. A real browser, because
 *  the menu is a portalled popover in the top bar and "where am I" is `aria-current` on laid-out
 *  links.
 */
import { expect, test, type Page } from "@playwright/test";

import {
  AUTOMATIONS_PATH,
  CHECKLISTS_PATH,
  MISSION_PATH,
  PLAYBOOKS_PATH,
  TEMPLATES_PATH,
} from "../src/lib/routes";
import { settingsPath } from "../src/routes/settingsTabs";
import { commonMocks } from "./mission-directions";
import { mockMissions } from "./mission-console";

async function libraryMenu(page: Page) {
  // The phone drawer is modal and makes the header inert; close it first, as `openMapFromNav` does.
  if ((await page.locator(".app.navOpen").count()) > 0) {
    await page.keyboard.press("Escape");
    await expect(page.locator(".app.navOpen")).toHaveCount(0);
  }
  await page.locator('.hud-topbar [data-testid="section-menu-library"]').click();
  return page.locator('[data-testid="section-menu-library-panel"]');
}

test.beforeEach(async ({ page }) => {
  await commonMocks(page);
  await mockMissions(page);
  await page.route("**/api/pulse/orchestrator", (r) =>
    r.fulfill({ status: 500, json: { detail: "off" } }),
  );
});

test("Library names Templates, Automations, Checklists and Playbooks; Missions has no menu", async ({
  page,
}) => {
  await page.goto(MISSION_PATH);
  await expect(
    page.locator('.hud-topbar [data-testid="section-menu-mission"]'),
  ).toHaveCount(0);
  const menu = await libraryMenu(page);
  await expect(menu.locator("a[data-subsection]")).toHaveText([
    "Prompt Templates",
    "Automations",
    "Checklists",
    "Playbooks",
  ]);
  await menu.locator('a[data-subsection="checklists"]').click();

  await expect(page).toHaveURL(new RegExp(`${CHECKLISTS_PATH}$`));
  await expect(page.getByRole("heading", { level: 1, name: "Checklists" })).toBeVisible();
  await expect(page.getByTestId("mission-playbooks")).toBeVisible();
  // On a child's route the parent says "you are in here", not "you are exactly here" — and it is
  // LIBRARY, not Missions, although the URL still starts with /mission.
  const bar = page.locator(".hud-topbar");
  await expect(bar.locator('[data-section="library"]')).toHaveAttribute("aria-current", "true");
  await expect(bar.locator('[data-section="mission"]')).not.toHaveAttribute("aria-current");

  const again = await libraryMenu(page);
  await expect(again.locator('a[data-subsection="checklists"]')).toHaveAttribute(
    "aria-current",
    "page",
  );
  await again.locator('a[data-subsection="automations"]').click();
  await expect(page).toHaveURL(new RegExp(`${AUTOMATIONS_PATH}$`));
  await expect(bar.locator('[data-section="library"]')).toHaveAttribute("aria-current", "true");

  // The label and the menu's first entry go to Templates.
  const third = await libraryMenu(page);
  await third.locator('a[data-subsection="templates"]').click();
  await expect(page).toHaveURL(new RegExp(`${TEMPLATES_PATH}$`));
  await expect(bar.locator('[data-section="library"]')).toHaveAttribute("aria-current", "page");
});

test("Playbooks has a Library entry and a page that says it is in the works", async ({ page }) => {
  await page.goto(MISSION_PATH);
  const menu = await libraryMenu(page);
  await menu.locator('a[data-subsection="playbooks"]').click();
  await expect(page).toHaveURL(new RegExp(`${PLAYBOOKS_PATH}$`));
  const pg = page.getByTestId("playbooks-page");
  await expect(pg.getByRole("heading", { level: 1, name: "Playbooks" })).toBeVisible();
  await expect(pg).toContainText(/in the works/i);
  await expect(pg.getByRole("link", { name: "Checklists" })).toHaveAttribute("href", CHECKLISTS_PATH);
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
