import { expect, test } from "@playwright/test";

// #211 Phase 2: a device-cached custom accent must be applied pre-paint (inline `--accent`
// on <html>) and stick across reloads, with the local choice winning over a stale server
// accent — the same local-wins contract as the theme (#172). Exercises the index.html boot
// script + AccentProvider end-to-end in a real browser (jsdom can't run the inline script).

test.beforeEach(async ({ page }) => {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        accent: "#ffb000", // server still on the default amber…
      },
    }),
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
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [] } }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [] } }),
  );
  // …but this device picked tactical green earlier.
  await page.addInitScript(() => {
    localStorage.setItem("tr-accent", "#3fbf6f");
  });
});

test("a device-cached custom accent is applied pre-paint and wins over the server (#211)", async ({
  page,
}) => {
  await page.goto("/");
  const accent = () =>
    page.evaluate(() =>
      document.documentElement.style.getPropertyValue("--accent").trim(),
    );
  await expect.poll(accent).toBe("#3fbf6f");
  // Green is light enough that the on-accent ink is the near-black, not white.
  const onAccent = await page.evaluate(() =>
    document.documentElement.style.getPropertyValue("--on-accent").trim(),
  );
  expect(onAccent).toBe("#0b0b0d");
  // Stable after the reconcile effect runs (no flip to the server's amber).
  await page.waitForTimeout(300);
  await expect.poll(accent).toBe("#3fbf6f");
  expect(await page.evaluate(() => localStorage.getItem("tr-accent"))).toBe(
    "#3fbf6f",
  );
});
