import { expect, type Page } from "@playwright/test";

/** Go to the SESSIONS MAP through whichever nav is reachable right now (#1058, #1069).
 *
 *  Since #1069 the map is not a top-level section: it is behind the Sessions chevron in the top
 *  bar (a portalled menu), and the phone drawer no longer repeats the nav. When the mobile drawer
 *  is open it is a MODAL and the shell marks the header `inert` (#940), so the bar resolves, looks
 *  visible and can never be clicked — the drawer is closed first (Escape, its own dismiss path).
 *  Decided from the shell's open state, not the project name, so every width takes one path. */
export async function openMapFromNav(page: Page): Promise<void> {
  if ((await page.locator(".app.navOpen").count()) > 0) {
    await page.keyboard.press("Escape");
    await expect(page.locator(".app.navOpen")).toHaveCount(0);
  }
  await page
    .locator('.hud-topbar [data-testid="section-menu-sessions"]')
    .click();
  await page
    .locator(
      '[data-testid="section-menu-sessions-panel"] a[data-subsection="map"]',
    )
    .click();
}
