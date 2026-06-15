import { expect, test } from "@playwright/test";

const now = Math.floor(Date.now() / 1000);
const sessions = [
  {
    id: "claude:aaa",
    engine: "claude",
    uuid: "aaa",
    short_uuid: "aaa",
    cwd: "/home/u/proj",
    project: { kind: "folder", id: "/home/u/proj", name: "proj" },
    last_mtime: now,
    first_user_message: "",
    title: "First session",
    sticky: false,
    sort_key: 0,
    archived: false,
    working: false,
  },
];

test.describe("Mobile Context Menu", () => {
  test.beforeEach(async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "drawer behavior is mobile-specific");

    await page.route("**/api/config", (r) =>
      r.fulfill({
        json: {
          csrf: "x",
          new_session_engines: ["claude"],
          terminal_backend: "ws",
          auth_mode: "none",
          overview_expanded: [],
          projects_hidden: [],
        },
      }),
    );
    await page.route("**/api/sessions**", (r) =>
      r.fulfill({
        json: {
          sessions,
          next_offset: null,
          total: sessions.length,
          facets: {
            projects: [{ kind: "folder", id: "/home/u/proj", name: "/home/u/proj" }],
            engines: ["claude"]
          }
        },
      }),
    );
    await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
    await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));

    // Go to the home page.
    await page.goto("/");
    // Ensure we are in mobile view (the drawer toggle should be visible).
    await expect(page.locator("header .navToggle")).toBeVisible();
  });

  test("actions trigger is visible and clickable on mobile (390px)", async ({ page }) => {
    // Open the sidebar drawer.
    await page.locator("header .navToggle").click();

    // Wait for sidebar to be visible.
    const sidebar = page.locator("aside.sidebar");
    await expect(sidebar).toBeVisible();

    // Find the first session row.
    const firstRow = page.locator("ul[aria-label] li").first();
    await expect(firstRow).toBeVisible();

    // The "..." button (Session actions) should be visible even without hover.
    const actionsTrigger = firstRow.getByRole("button", { name: "Session actions" });

    // Check if it's visible.
    await expect(actionsTrigger).toBeVisible();

    // Check opacity (should be 1 on mobile).
    const opacity = await actionsTrigger.evaluate((el) => window.getComputedStyle(el.parentElement!).opacity);
    expect(opacity).toBe("1");

    // Click it.
    await actionsTrigger.click();

    // The menu (bottom sheet) should open.
    const menu = page.getByRole("menu", { name: "Session actions" });
    await expect(menu).toBeVisible();

    // Check if it's at the bottom (bottom: 0).
    // The sheet is anchored to the visible bottom of the (dynamic) viewport — its lower
    // edge sits at the viewport floor rather than behind a mobile browser toolbar.
    const atViewportBottom = await menu.evaluate(
      (el) => Math.round(el.getBoundingClientRect().bottom) === window.innerHeight,
    );
    expect(atViewportBottom).toBe(true);
  });

  test("actions trigger is visible and functional in the 640px-800px range", async ({ page }) => {
    // Set viewport to 700px.
    await page.setViewportSize({ width: 700, height: 800 });

    // Open the sidebar drawer.
    await page.locator("header .navToggle").click();

    // Find the first session row.
    const firstRow = page.locator("ul[aria-label] li").first();
    await expect(firstRow).toBeVisible();

    const actionsTrigger = firstRow.getByRole("button", { name: "Session actions" });
    await expect(actionsTrigger).toBeVisible();

    // Click it.
    await actionsTrigger.click();

    // The menu should open.
    const menu = page.getByRole("menu", { name: "Session actions" });
    await expect(menu).toBeVisible();

    // In this range (700px), it should now be a bottom sheet (as it is <= 800px).
    // The sheet is anchored to the visible bottom of the (dynamic) viewport — its lower
    // edge sits at the viewport floor rather than behind a mobile browser toolbar.
    const atViewportBottom = await menu.evaluate(
      (el) => Math.round(el.getBoundingClientRect().bottom) === window.innerHeight,
    );
    expect(atViewportBottom).toBe(true);
  });

  test("actions trigger is visible and functional at 768px (iPad portrait)", async ({ page }) => {
    // Specifically requested by Hermes review.
    await page.setViewportSize({ width: 768, height: 1024 });

    await page.locator("header .navToggle").click();
    const firstRow = page.locator("ul[aria-label] li").first();
    await expect(firstRow).toBeVisible();

    const actionsTrigger = firstRow.getByRole("button", { name: "Session actions" });
    await expect(actionsTrigger).toBeVisible();

    await actionsTrigger.click();
    const menu = page.getByRole("menu", { name: "Session actions" });
    await expect(menu).toBeVisible();

    // At 768px it should be a bottom sheet now (as it is <= 800px).
    // The sheet is anchored to the visible bottom of the (dynamic) viewport — its lower
    // edge sits at the viewport floor rather than behind a mobile browser toolbar.
    const atViewportBottom = await menu.evaluate(
      (el) => Math.round(el.getBoundingClientRect().bottom) === window.innerHeight,
    );
    expect(atViewportBottom).toBe(true);
  });

  test("actions trigger is visible on devices that might report hover support (tablet/hybrid)", async ({ page }) => {
    // Some browsers/devices (like iPad or Chrome with a mouse) might not match `hover: none`.
    // We want to ensure that if isMobile (<= 800px) is true, the actions are visible.
    await page.setViewportSize({ width: 800, height: 1000 });

    await page.locator("header .navToggle").click();
    const firstRow = page.locator("ul[aria-label] li").first();
    const actionsTrigger = firstRow.getByRole("button", { name: "Session actions" });

    await expect(actionsTrigger).toBeVisible();
  });

  // Regression (#405 follow-up): the bottom sheet used to be `position: fixed; bottom: 0`
  // with no height cap and no internal scroll, so on a real phone its lower actions + Cancel
  // sat behind the browser's bottom toolbar (the visual ≠ layout viewport gap) and a tall
  // sheet overflowed off the top with no way to scroll back — "menu opens but is cut off".
  // The sheet now lives in a dynamic-viewport (100dvh) wrapper, is capped + scrollable, and
  // every action stays reachable. (Headless Chromium can't model the visual/layout split, so
  // we assert the structural guarantees that make the cut-off impossible.)
  test("bottom sheet is viewport-bounded, scrollable, and fully reachable", async ({ page }) => {
    // Configure AI review so the sheet carries its tallest item set (Review / Exclude /
    // Rename / Archive + Cancel) — the case most likely to overflow a short phone.
    await page.route("**/api/config", (r) =>
      r.fulfill({
        json: {
          csrf: "x",
          new_session_engines: ["claude"],
          terminal_backend: "ws",
          auth_mode: "none",
          overview_expanded: [],
          projects_hidden: [],
          ai_review: { configured: true },
        },
      }),
    );
    await page.reload();

    await page.locator("header .navToggle").click();
    const firstRow = page.locator("ul[aria-label] li").first();
    await firstRow.getByRole("button", { name: "Session actions" }).click();

    const menu = page.getByRole("menu", { name: "Session actions" });
    await expect(menu).toBeVisible();

    // Bounded to the viewport + internally scrollable (was max-height:none / overflow:visible).
    const shape = await menu.evaluate((el) => {
      const cs = window.getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return {
        maxHeightSet: cs.maxHeight !== "none",
        overflowY: cs.overflowY,
        top: Math.round(r.top),
        bottom: Math.round(r.bottom),
        fullWidth: Math.round(r.width) === window.innerWidth,
        vh: window.innerHeight,
      };
    });
    expect(shape.maxHeightSet).toBe(true);
    expect(shape.overflowY).toBe("auto");
    expect(shape.fullWidth).toBe(true);
    expect(shape.top).toBeGreaterThanOrEqual(0); // never overflows above the viewport
    expect(shape.bottom).toBe(shape.vh); // pinned to the visible floor

    // Every action AND Cancel are inside the viewport (reachable, not clipped).
    const cancel = menu.getByRole("button", { name: "Cancel" });
    for (const item of [...(await menu.getByRole("menuitem").all()), cancel]) {
      const within = await item.evaluate((el) => {
        const r = el.getBoundingClientRect();
        return r.top >= 0 && r.bottom <= window.innerHeight + 1;
      });
      expect(within).toBe(true);
    }

    // The wrapper above the sheet is click-through: a tap there reaches the scrim and closes.
    await page.touchscreen.tap(page.viewportSize()!.width / 2, 20);
    await expect(menu).toBeHidden();
  });

  // Regression: on a real phone, Chrome/Safari show/hide the URL bar on the very tap that
  // opens the sheet, firing `resize` (and `scroll`) events. The sheet used to bind those as
  // close triggers (they only make sense for the trigger-anchored desktop popover), so it
  // flickered shut the instant you pressed ⋯. The mobile sheet must survive a viewport
  // resize/scroll — it's pinned to the viewport, not the trigger.
  test("sheet survives a viewport resize/scroll (no URL-bar flicker)", async ({ page }) => {
    await page.locator("header .navToggle").click();
    const firstRow = page.locator("ul[aria-label] li").first();
    await firstRow.getByRole("button", { name: "Session actions" }).click();

    const menu = page.getByRole("menu", { name: "Session actions" });
    await expect(menu).toBeVisible();

    // Android URL-bar collapse ⇒ a window resize / scroll while the sheet is open.
    await page.evaluate(() => window.dispatchEvent(new Event("resize")));
    await page.evaluate(() => window.dispatchEvent(new Event("scroll")));
    await page.waitForTimeout(150);

    // Still open — the sheet does not dismiss itself on viewport chrome changes.
    await expect(menu).toBeVisible();
  });
});
