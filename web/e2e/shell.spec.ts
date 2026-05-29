import { expect, test } from "@playwright/test";

/** Phase-0 smoke: the SPA shell renders and routes work on both desktop + mobile
 *  viewports (the harness that ends blind mobile iteration). Backend-dependent
 *  assertions (session list rows, touch-scroll, reconnect-without-blank) arrive
 *  with the terminal in later phases and run against a live app via E2E_BASE_URL. */

test("shell renders + new-session landing at /", async ({ page }) => {
  await page.goto("/");
  // The brand shows in the sidebar header (desktop) or the top bar (mobile); only one
  // is visible per viewport, so filter to the visible instance.
  await expect(page.getByText("BattleLab").filter({ visible: true }).first()).toBeVisible();
  await expect(page.getByRole("heading", { name: /start a new session/i })).toBeVisible();
});

test("deep-link to /s/:engine/:id mounts the terminal (URL = identity)", async ({ page }) => {
  await page.goto("/s/claude/abc123");
  // No backend in the preview, so the ws can't connect — but the xterm pane must mount
  // and a connection status must surface (connecting/reconnecting), never a blank route.
  await expect(page.locator(".xterm")).toBeVisible();
  await expect(page.getByRole("status")).toBeVisible();
});

test("responsive nav: drawer hamburger on mobile; single collapse affordance on desktop", async ({
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
    // Desktop (#132): while the sidebar is expanded the header carries NO collapse toggle —
    // collapse lives only in the sidebar's PanelLeftClose, so there's a single affordance.
    // Collapsing hides the sidebar; THEN the header's expand toggle appears and re-expands.
    await expect(app).not.toHaveClass(/collapsed/);
    await expect(page.locator(".sidebar")).toBeVisible();
    await expect(toggle).toBeHidden(); // no duplicate collapse button while expanded
    await page.getByRole("button", { name: /collapse session list/i }).click();
    await expect(app).toHaveClass(/collapsed/);
    await expect(page.locator(".sidebar")).toBeHidden();
    await expect(toggle).toBeVisible(); // collapsed → header shows the expand toggle
    await toggle.click();
    await expect(app).not.toHaveClass(/collapsed/);
    await expect(page.locator(".sidebar")).toBeVisible();
  }
});

test("no empty gap between the title bar and the pane (#134)", async ({ page }) => {
  await page.goto("/");
  // The pane must start immediately under the header — no wasted band below the title bar.
  const header = await page.locator(".mobilebar").boundingBox();
  const pane = await page.locator(".terminal-pane").boundingBox();
  expect(header).not.toBeNull();
  expect(pane).not.toBeNull();
  expect(Math.abs(pane!.y - (header!.y + header!.height))).toBeLessThan(2);
});

test("opens the fullscreen session overview from the sidebar/header (#139)", async ({
  page,
}, testInfo) => {
  await page.goto("/");
  // Desktop: the entry is in the sidebar topbar; mobile/collapsed: in the always-visible header.
  const entry =
    testInfo.project.name === "mobile"
      ? page.locator(".mobilebar").getByRole("link", { name: /open session overview/i })
      : page.locator(".sidebar .topbar").getByRole("link", { name: /open session overview/i });
  await entry.click();
  await expect(page).toHaveURL(/\/overview$/);
  // The overview surface mounts (loading/empty/error state — never a blank route).
  await expect(page.locator(".tr-overview")).toBeVisible();
});

test("desktop: List ⇄ Map toggle swaps the sidebar body to the overview (#139)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name === "mobile", "toggle lives in the sidebar drawer; covered on desktop");
  await page.goto("/");
  await page.getByRole("tab", { name: /^map$/i }).click();
  // The squeezed overview canvas now renders inside the sidebar.
  await expect(page.locator(".sidebar .tr-overview")).toBeVisible();
  await page.getByRole("tab", { name: /^list$/i }).click();
  await expect(page.locator(".sidebar .tr-overview")).toHaveCount(0);
});

test("layout snapshot (per-project: desktop + mobile viewports)", async ({ page }, testInfo) => {
  await page.goto("/");
  // Screenshot named per project → mobile vs desktop layout regressions are visible/diffable.
  await testInfo.attach(`shell-${testInfo.project.name}`, {
    body: await page.screenshot({ fullPage: true }),
    contentType: "image/png",
  });
});
