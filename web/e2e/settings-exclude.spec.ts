import { expect, test } from "@playwright/test";

// Real-browser check of the Settings → Session overview hide checklist (#152 + #174 inverse
// semantics): un-ticking a project persists it as hidden via /api/prefs. The checkbox is
// "Show in sidebar/filter/overview" — checked = visible, unchecked = hidden. Network is
// mocked so Settings renders without a backend.

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

test("desktop: un-ticking a project in Settings persists it as hidden (#152 / #174)", async ({
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
  // New (#174) inverse semantics: nothing hidden → row is shown → checkbox is checked.
  await expect(alpha).toBeChecked();

  await alpha.uncheck();
  await expect.poll(() => prefsBody).toEqual({ projects_hidden: ["/home/u/alpha"] });
  await expect(alpha).not.toBeChecked();
});
