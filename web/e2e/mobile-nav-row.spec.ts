import { expect, test } from "@playwright/test";

// #494 → #1085: the drawer's action row shrank from Help / Overview / Templates / Settings to
// Help / Settings (#1058), and is now gone — Help and Settings live in the operator menu, which the
// top bar keeps at every width. The drawer starts with the list. Real-browser layout test (jsdom
// can't model box geometry).

const CONFIG = {
  csrf: "x",
  new_session_engines: ["claude"],
  terminal_backend: "ws",
  auth_mode: "none",
  overview_expanded: [],
  projects_hidden: [],
};

test.describe("Mobile drawer — no action row (#494, #1085)", () => {
  test.beforeEach(async ({ page }, testInfo) => {
    test.skip(
      testInfo.project.name !== "mobile",
      "drawer nav row is mobile-specific (≤640px)",
    );
    await page.route("**/api/config", (r) => r.fulfill({ json: CONFIG }));
    await page.route("**/api/sessions**", (r) =>
      r.fulfill({
        json: {
          sessions: [],
          next_offset: null,
          total: 0,
          facets: { projects: [], engines: [] },
        },
      }),
    );
    await page.route("**/api/version", (r) =>
      r.fulfill({ json: { version: "test" } }),
    );
    await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
    await page.goto("/");
    // Open the off-canvas drawer so its action row is on-screen and measurable.
    await page.getByRole("button", { name: /open session list/i }).click();
  });

  test("the drawer has no action row: Help and Settings moved to the operator menu (#1085)", async ({
    page,
  }) => {
    const drawer = page.locator("aside.sidebar");
    await expect(drawer).toBeVisible();
    // The row that held `?` and ⚙ is gone, and neither control appears anywhere in the drawer.
    await expect(page.locator(".sidebar-actions")).toHaveCount(0);
    await expect(drawer.getByRole("button", { name: "Help" })).toHaveCount(0);
    await expect(drawer.getByRole("link", { name: "Settings" })).toHaveCount(0);
    // The list controls lead instead: New session sits directly under the head row.
    const head = (await drawer.locator(".sidebar-head").boundingBox())!;
    const newBtn = (await drawer.getByRole("link", { name: /new session/i }).boundingBox())!;
    expect(newBtn.y - (head.y + head.height)).toBeLessThan(24);
    // …and Settings is still one tap away on a phone, in the tile the bar keeps at every width.
    await expect(page.locator(".hud-topbar [data-topbar-keep] [data-testid=operator-menu]")).toHaveCount(1);
  });
});
