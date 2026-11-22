import { expect, test } from "@playwright/test";
import { PROJECTS_FOLDERS_PATH } from "../src/lib/routes";

// Real-browser check of the folder hide list (#152 + #174 inverse semantics), on HQ → Projects →
// Folders & discovery since #2143: switching a folder off persists it as hidden via /api/prefs.
// The switch is "Show in sidebar/filter/overview" — on = visible, off = hidden. Network is mocked
// so the page renders without a backend.

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
      },
    }),
  );
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        folders: [
          { cwd: "/home/u/alpha", label: "Alpha" },
          { cwd: "/home/u/beta", label: "Beta" },
        ],
      },
    }),
  );
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [] } }),
  );
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
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

test("desktop: switching a folder off persists it as hidden (#152 / #174)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name === "mobile", "covered on desktop");
  let prefsBody: unknown = null;
  await page.route("**/api/prefs", async (r) => {
    prefsBody = r.request().postDataJSON();
    await r.fulfill({ json: prefsBody });
  });

  await page.goto(PROJECTS_FOLDERS_PATH);
  const alpha = page.getByRole("switch", { name: /alpha/i });
  await expect(alpha).toBeVisible();
  // Inverse semantics (#174): nothing hidden → the folder is shown → the switch is on.
  await expect(alpha).toHaveAttribute("aria-checked", "true");

  await alpha.click();
  await expect
    .poll(() => prefsBody)
    .toEqual({ projects_hidden: ["/home/u/alpha"] });
  await expect(alpha).toHaveAttribute("aria-checked", "false");
});
