import { expect, type Locator, type Page } from "@playwright/test";

/** Open the Ask sidebar from the corner icon beside the bell (#1294) and return it.
 *
 *  When the phone's session drawer is open it is a MODAL and the header is `inert` (#940), so the
 *  icon resolves, looks visible and can never be clicked — the drawer is closed first (Escape, its
 *  own dismiss path), the way `openMapFromNav` does. */
export async function openAsk(page: Page): Promise<Locator> {
  if ((await page.locator(".app.navOpen").count()) > 0) {
    await page.keyboard.press("Escape");
    await expect(page.locator(".app.navOpen")).toHaveCount(0);
  }
  const toggle = page.locator('.hud-topbar [data-testid="ask-toggle"]');
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
  const panel = page.getByTestId("ask-sidebar");
  await expect(panel).toHaveAttribute("data-open", "true");
  return panel;
}
