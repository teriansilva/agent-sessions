import { expect, type Page } from "@playwright/test";

/** Open Settings the way an operator does since #1085: from the operator tile's menu.
 *
 *  The top-bar ⚙ and the drawer's copy of it are gone; Settings lives in the tile's menu, which
 *  the bar keeps at every width. The tile renders only once `/api/config` names an `auth_mode`, so
 *  a spec that uses this must mock one. An open phone drawer makes the header `inert` (#940), so it
 *  is closed first — with Escape, its own dismiss path. */
export async function openSettingsFromMenu(page: Page): Promise<void> {
  if ((await page.locator(".app.navOpen").count()) > 0) {
    await page.keyboard.press("Escape");
    await expect(page.locator(".app.navOpen")).toHaveCount(0);
  }
  await page.getByTestId("operator-menu").click();
  await page
    .getByTestId("operator-menu-panel")
    .getByRole("menuitem", { name: "Settings" })
    .click();
}
