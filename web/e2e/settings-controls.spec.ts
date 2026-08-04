import { expect, test } from "@playwright/test";

// #683: native checkbox / radio controls in Settings were rendering at the browser default
// (unbranded, uncapped) so they looked oversized and drifted out of line with their labels on
// narrow / streamed viewports. They must now be brand-accented (amber) and size-capped, matching
// the folder-row checkboxes. Network is mocked so Settings renders without a backend.

test.beforeEach(async ({ page }) => {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: [],
        projects_hidden: [],
        onboarded: true,
      },
    }),
  );
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [{ cwd: "/home/u/test", label: "test" }] } }),
  );
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [] } }),
  );
  await page.route("**/api/system", (r) =>
    r.fulfill({
      json: { auto_update: true, current: "test", channel: "stable" },
    }),
  );
  await page.route("**/api/projects**", (r) =>
    r.fulfill({ json: { projects: [] } }),
  );
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
});

// Default brand accent (--accent = #ffb000). A capped native control is ≤ 18px on both axes.
const AMBER = "rgb(255, 176, 0)";

async function expectBrandedAndCapped(
  page,
  role: "checkbox" | "radio",
  name: RegExp,
) {
  const el = page.getByRole(role, { name });
  await expect(el).toBeVisible();
  const accent = await el.evaluate((n) => getComputedStyle(n).accentColor);
  expect(accent).toBe(AMBER);
  const box = await el.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.width).toBeLessThanOrEqual(18);
  expect(box!.height).toBeLessThanOrEqual(18);
}

test("Updates auto-update checkbox is brand-accented and size-capped (#683)", async ({
  page,
}) => {
  await page.goto("/settings/system");
  await expectBrandedAndCapped(page, "checkbox", /automatic updates/i);
});

test("Session-overview mode radios are brand-accented and size-capped (#683)", async ({
  page,
}) => {
  await page.goto("/settings/projects");
  await expectBrandedAndCapped(page, "radio", /show all/i);
  await expectBrandedAndCapped(page, "radio", /only included/i);
});
