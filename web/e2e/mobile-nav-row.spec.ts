import { expect, test } from "@playwright/test";

// #494: on mobile the drawer's action row must render as ONE icon-only row (no text labels), not a
// stacked, labelled column — recovering vertical space. It was Help / Overview / Templates /
// Settings; since #1058 Overview and Templates are named SECTIONS in the nav (listed, with labels,
// directly above this row), so the row is Help / Settings — the actions that are not routes.
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

  test("Help/Settings share a single row, icon-only", async ({
    page,
  }) => {
    const actions = page.locator(".sidebar-actions");
    await expect(actions).toBeVisible();

    // TWO since #1058: Overview and Templates became named sections in the nav (which the drawer
    // lists above this row), leaving Help and Settings as the only actions that are not routes.
    const items = actions.locator(":scope > *");
    const COUNT = 2;
    await expect(items).toHaveCount(COUNT);

    const boxes = [];
    for (let i = 0; i < COUNT; i++) boxes.push((await items.nth(i).boundingBox())!);

    // One row: all four share the same top (within a couple px) and march left → right.
    for (const b of boxes)
      expect(Math.abs(b.y - boxes[0].y)).toBeLessThanOrEqual(2);
    for (let i = 1; i < boxes.length; i++)
      expect(boxes[i].x).toBeGreaterThan(boxes[i - 1].x);

    // The container is a single row tall — a stacked column would be ~100px.
    expect((await actions.boundingBox())!.height).toBeLessThan(64);

    // Icon-only: an aria-label is the affordance, no visible text label remains. An item is either
    // the control itself or — for Help, which opens a menu since #987 — the anchor wrapper around
    // its trigger; the label belongs on the control, never on the wrapper (naming a generic element
    // is not allowed).
    for (let i = 0; i < COUNT; i++) {
      const item = items.nth(i);
      expect((await item.innerText()).trim()).toBe("");
      const isControl = await item.evaluate((el) => el.matches("a, button"));
      const control = isControl ? item : item.locator(":scope > a, :scope > button");
      await expect(control).toHaveCount(1);
      expect(await control.getAttribute("aria-label")).toBeTruthy();
    }
  });
});
