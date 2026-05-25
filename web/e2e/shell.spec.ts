import { expect, test } from "@playwright/test";

/** Phase-0 smoke: the SPA shell renders and routes work on both desktop + mobile
 *  viewports (the harness that ends blind mobile iteration). Backend-dependent
 *  assertions (session list rows, touch-scroll, reconnect-without-blank) arrive
 *  with the terminal in later phases and run against a live app via E2E_BASE_URL. */

test("shell renders + new-session landing at /", async ({ page }) => {
  await page.goto("/");
  // The brand shows in the sidebar header (desktop) or the top bar (mobile); only one
  // is visible per viewport, so filter to the visible instance.
  await expect(page.getByText("TermRoyale").filter({ visible: true }).first()).toBeVisible();
  await expect(page.getByRole("heading", { name: /start a new session/i })).toBeVisible();
});

test("deep-link to /s/:engine/:id mounts the terminal (URL = identity)", async ({ page }) => {
  await page.goto("/s/claude/abc123");
  // No backend in the preview, so the ws can't connect — but the xterm pane must mount
  // and a connection status must surface (connecting/reconnecting), never a blank route.
  await expect(page.locator(".xterm")).toBeVisible();
  await expect(page.getByRole("status")).toBeVisible();
});

test("responsive nav: hamburger toggles the drawer on mobile, hidden on desktop", async ({
  page,
}, testInfo) => {
  await page.goto("/");
  const toggle = page.getByRole("button", { name: /toggle session list/i });
  const app = page.locator(".app");

  if (testInfo.project.name === "mobile") {
    await expect(toggle).toBeVisible();
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(app).not.toHaveClass(/navOpen/);
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(app).toHaveClass(/navOpen/);
    // Tapping the backdrop (where it's exposed, right of the ~320px drawer) closes it.
    await page.getByRole("button", { name: /close session list/i }).click({
      position: { x: 390, y: 320 },
    });
    await expect(app).not.toHaveClass(/navOpen/);
  } else {
    // Desktop: the sidebar is always present, so the hamburger is hidden.
    await expect(toggle).toBeHidden();
  }
});

test("layout snapshot (per-project: desktop + mobile viewports)", async ({ page }, testInfo) => {
  await page.goto("/");
  // Screenshot named per project → mobile vs desktop layout regressions are visible/diffable.
  await testInfo.attach(`shell-${testInfo.project.name}`, {
    body: await page.screenshot({ fullPage: true }),
    contentType: "image/png",
  });
});
