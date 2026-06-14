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
    const bottom = await menu.evaluate((el) => window.getComputedStyle(el).bottom);
    expect(bottom).toBe("0px");
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
    const bottom = await menu.evaluate((el) => window.getComputedStyle(el).bottom);
    expect(bottom).toBe("0px");
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
    const bottom = await menu.evaluate((el) => window.getComputedStyle(el).bottom);
    expect(bottom).toBe("0px");
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
});
