import { expect, test } from "@playwright/test";

// Real-browser check of the Settings → Session overview exclude checklist (#152): ticking a
// project persists it as excluded via /api/prefs. Network is mocked so Settings renders
// without a backend.

test.beforeEach(async ({ page }) => {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: [],
        overview_excluded: [],
      },
    }),
  );
  await page.route("**/api/projects", (r) =>
    r.fulfill({
      json: {
        projects: [
          { cwd: "/home/u/alpha", label: "Alpha" },
          { cwd: "/home/u/beta", label: "Beta" },
        ],
      },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } } }),
  );
});

test("desktop: ticking a project in Settings persists it as excluded (#152)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name === "mobile", "covered on desktop");
  let prefsBody: unknown = null;
  await page.route("**/api/prefs", async (r) => {
    prefsBody = r.request().postDataJSON();
    await r.fulfill({ json: prefsBody });
  });

  await page.goto("/settings");
  const alpha = page.getByRole("checkbox", { name: /alpha/i });
  await expect(alpha).toBeVisible();
  await expect(alpha).not.toBeChecked();

  await alpha.check();
  await expect.poll(() => prefsBody).toEqual({ overview_excluded: ["/home/u/alpha"] });
  await expect(alpha).toBeChecked();
});
