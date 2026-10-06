/** Every top-bar section menu opens INSIDE the viewport.
 *
 *  The portalled panel is right-aligned to its chevron. On a phone the leftmost chevrons sit far
 *  enough left that a right-aligned 220px panel ran off the left edge — "Dashboard" read as
 *  "nboard" and its second entry was unreachable. A real browser, because this is layout.
 */
import { expect, test } from "@playwright/test";

import { commonMocks } from "./mission-directions";

const MENUS = ["sessions", "library"] as const;

test.beforeEach(async ({ page }) => {
  await commonMocks(page);
});

for (const id of MENUS) {
  test(`the ${id} section menu stays inside the viewport`, async ({ page }) => {
    await page.goto("/");
    if ((await page.locator(".app.navOpen").count()) > 0) {
      await page.keyboard.press("Escape");
      await expect(page.locator(".app.navOpen")).toHaveCount(0);
    }
    await page.locator(`.hud-topbar [data-testid="section-menu-${id}"]`).click();
    const panel = page.locator(`[data-testid="section-menu-${id}-panel"]`);
    await expect(panel).toBeVisible();

    const vw = page.viewportSize()!.width;
    const box = (await panel.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(vw);

    // Every entry is on screen and actionable — measured, then clicked.
    const items = panel.locator("a[data-subsection]");
    const n = await items.count();
    expect(n).toBeGreaterThan(1);
    for (let i = 0; i < n; i++) {
      const b = (await items.nth(i).boundingBox())!;
      expect(b.x).toBeGreaterThanOrEqual(0);
      expect(b.x + b.width).toBeLessThanOrEqual(vw);
    }
    const last = items.nth(n - 1);
    const href = await last.getAttribute("href");
    await last.click();
    await expect(page).toHaveURL(new RegExp(`${href}$`));
  });
}
