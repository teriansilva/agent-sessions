/** A failed first `/api/config` read is retried (the restart after Update now).
 *
 *  It used to be final for the page's lifetime: the operator tile — the only way into Settings
 *  since #1085 — renders nothing without a config, so on the phone Settings simply disappeared,
 *  the dashboard said "Ask needs an AI endpoint" on a configured install, and every POST 403'd
 *  for want of a CSRF token. Real browser: the reported symptom is a missing control in the top
 *  bar at phone width.
 */
import { expect, test } from "@playwright/test";

test("the operator tile, and Settings in it, appears once a failed first config read is retried", async ({
  page,
}) => {
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  let configReads = 0;
  await page.route("**/api/config", (r) => {
    configReads += 1;
    // The server is still coming up after the update: the proxy answers 502.
    if (configReads === 1) return r.fulfill({ status: 502, body: "Bad Gateway" });
    return r.fulfill({
      json: {
        csrf: "x",
        auth_mode: "single-user",
        username: "nightowl",
        terminal_backend: "ws",
        pulse: { configured: true },
        new_session_engines: ["claude"],
        onboarded: true,
      },
    });
  });
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [], total: 0, next_offset: null, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await page.route("**/api/missions**", (r) =>
    r.fulfill({
      json: { missions: [], total: 0, next_offset: null, facets: { projects: [], states: [] } },
    }),
  );

  await page.goto("/dashboard");
  const tile = page.getByTestId("operator-menu");
  await expect(tile).toBeVisible({ timeout: 10_000 });
  expect(configReads).toBeGreaterThanOrEqual(2);

  // Inside the viewport, not merely rendered.
  const box = (await tile.boundingBox())!;
  const vw = page.viewportSize()!.width;
  expect(box.x + box.width).toBeLessThanOrEqual(vw);

  await tile.click();
  await page.getByRole("menuitem", { name: "Settings" }).click();
  await expect(page).toHaveURL(/\/settings/);
});
