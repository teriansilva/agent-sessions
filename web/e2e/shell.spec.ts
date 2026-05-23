import { expect, test } from "@playwright/test";

/** Phase-0 smoke: the SPA shell renders and routes work on both desktop + mobile
 *  viewports (the harness that ends blind mobile iteration). Backend-dependent
 *  assertions (session list rows, touch-scroll, reconnect-without-blank) arrive
 *  with the terminal in later phases and run against a live app via E2E_BASE_URL. */

test("shell renders + new-session landing at /", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("agent-sessions")).toBeVisible(); // sidebar header
  await expect(page.getByRole("heading", { name: /start a new session/i })).toBeVisible();
});

test("deep-link to /s/:engine/:id renders the session route (URL = identity)", async ({ page }) => {
  await page.goto("/s/claude/abc123");
  await expect(page.getByText("claude:abc123")).toBeVisible();
});

test("layout snapshot (per-project: desktop + mobile viewports)", async ({ page }, testInfo) => {
  await page.goto("/");
  // Screenshot named per project → mobile vs desktop layout regressions are visible/diffable.
  await testInfo.attach(`shell-${testInfo.project.name}`, {
    body: await page.screenshot({ fullPage: true }),
    contentType: "image/png",
  });
});
