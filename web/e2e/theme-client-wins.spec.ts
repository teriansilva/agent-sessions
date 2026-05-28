import { expect, test } from "@playwright/test";

// #172: a local theme choice must STICK on reload even when the server's persisted theme
// has drifted. Mocked config returns theme:"dark"; localStorage already has "royal" from
// an earlier explicit click; on load, royal must remain — both the DOM attribute and the
// localStorage value.

test.beforeEach(async ({ page }) => {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        theme: "dark", // server has "dark" but local has "royal"
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } } }),
  );
  await page.route("**/api/projects", (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  // Seed the local choice BEFORE the SPA boots, so the inline pre-paint script + React
  // reconcile both see localStorage already set.
  await page.addInitScript(() => {
    localStorage.setItem("tr-theme", "royal");
  });
});

test("local theme wins over stale server theme on reload (#172)", async ({ page }) => {
  await page.goto("/");
  // The page settled; data-theme should still be royal, not dark.
  await expect(page.locator("html")).toHaveAttribute("data-theme", "royal");
  // Give the reconcile effect a moment to NOT fire.
  await page.waitForTimeout(300);
  await expect(page.locator("html")).toHaveAttribute("data-theme", "royal");
  // And localStorage hasn't been overwritten to "dark".
  const stored = await page.evaluate(() => localStorage.getItem("tr-theme"));
  expect(stored).toBe("royal");
});
