import { expect, test } from "@playwright/test";

// #494: on mobile the drawer's Help / Pulse / Overview / Settings actions must render as ONE
// icon-only row (no text labels), not a stacked, labelled column — recovering vertical space.
// Real-browser layout test (jsdom can't model flex direction / box geometry).

const CONFIG = {
  csrf: "x",
  new_session_engines: ["claude"],
  terminal_backend: "ws",
  auth_mode: "none",
  overview_expanded: [],
  projects_hidden: [],
};

test.describe("Mobile drawer nav — one icon-only row (#494)", () => {
  test.beforeEach(async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "drawer nav row is mobile-specific (≤640px)");
    await page.route("**/api/config", (r) => r.fulfill({ json: CONFIG }));
    await page.route("**/api/sessions**", (r) =>
      r.fulfill({
        json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } },
      }),
    );
    await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
    await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
    await page.goto("/");
    // Open the off-canvas drawer so its action row is on-screen and measurable.
    await page.getByRole("button", { name: /open session list/i }).click();
  });

  test("Help/Pulse/Overview/Settings share a single row, icon-only", async ({ page }) => {
    const actions = page.locator(".sidebar-actions");
    await expect(actions).toBeVisible();

    const items = actions.locator(":scope > *");
    await expect(items).toHaveCount(4);

    const boxes = [];
    for (let i = 0; i < 4; i++) boxes.push((await items.nth(i).boundingBox())!);

    // One row: all four share the same top (within a couple px) and march left → right.
    for (const b of boxes) expect(Math.abs(b.y - boxes[0].y)).toBeLessThanOrEqual(2);
    for (let i = 1; i < 4; i++) expect(boxes[i].x).toBeGreaterThan(boxes[i - 1].x);

    // The container is a single row tall — a stacked column of 4 would be ~160px.
    expect((await actions.boundingBox())!.height).toBeLessThan(64);

    // Icon-only: an aria-label is the affordance, no visible text label remains.
    for (let i = 0; i < 4; i++) {
      expect((await items.nth(i).innerText()).trim()).toBe("");
      expect(await items.nth(i).getAttribute("aria-label")).toBeTruthy();
    }
  });
});
