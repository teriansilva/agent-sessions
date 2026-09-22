import { expect, test } from "@playwright/test";

/** Phase-0 smoke: the SPA shell renders and routes work on both desktop + mobile
 *  viewports (the harness that ends blind mobile iteration). Backend-dependent
 *  assertions (session list rows, touch-scroll, reconnect-without-blank) arrive
 *  with the terminal in later phases and run against a live app via E2E_BASE_URL. */

test("shell renders + new-session landing at /", async ({ page }) => {
  await page.goto("/");
  // The BATTLELAB wordmark lives in the full-width command topbar (one bar, all widths).
  await expect(page.locator(".hud-brand")).toBeVisible();
  await expect(
    page.getByRole("heading", { name: /start a new session/i }),
  ).toBeVisible();
});

test("deep-link to /s/:engine/:id mounts the terminal (URL = identity)", async ({
  page,
}) => {
  await page.goto("/s/claude/abc123");
  // No backend in the preview, so the ws can't connect — but the xterm pane must mount
  // and a CONNECTION status must surface (connecting/reconnecting), never a blank route.
  // Filtered by text since #392: the sidebar carries an always-mounted (empty until a
  // Review-now outcome lands) status live region, so the bare role query is ambiguous —
  // and role=status takes no name from content, so a name filter can't disambiguate.
  await expect(page.locator(".xterm")).toBeVisible();
  await expect(
    page.getByRole("status").filter({ hasText: /connect/i }),
  ).toBeVisible();
});

test("responsive nav: drawer hamburger on mobile; single collapse affordance on desktop", async ({
  page,
}, testInfo) => {
  await page.goto("/");
  // One command-bar toggle for all widths (#211 redux): drawer on mobile, collapse on desktop.
  const toggle = page.locator(".navToggle");
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
    // Desktop: the single command-bar toggle collapses, then re-expands the sidebar.
    await expect(app).not.toHaveClass(/collapsed/);
    await expect(page.locator(".sidebar")).toBeVisible();
    await expect(toggle).toBeVisible();
    await toggle.click();
    await expect(app).toHaveClass(/collapsed/);
    await expect(page.locator(".sidebar")).toBeHidden();
    await toggle.click();
    await expect(app).not.toHaveClass(/collapsed/);
    await expect(page.locator(".sidebar")).toBeVisible();
  }
});

test("the command topbar spans the top and the pane floats below it (#134/#211)", async ({
  page,
}) => {
  await page.goto("/");
  const top = await page.locator(".hud-topbar").boundingBox();
  const pane = await page.locator(".terminal-pane").boundingBox();
  expect(top).not.toBeNull();
  expect(pane).not.toBeNull();
  expect(top!.y).toBeLessThan(4); // topbar pinned to the top
  // The pane is a floating panel below the topbar (deliberate margin — no overlap, no huge gap).
  expect(pane!.y).toBeGreaterThanOrEqual(top!.y + top!.height - 1);
  expect(pane!.y - (top!.y + top!.height)).toBeLessThan(24);
});

test("opens the fullscreen session map from the top bar at every width (#139/#211, #1058)", async ({
  page,
}) => {
  await page.goto("/");
  // #1058: the map is a NAMED SECTION in the bar, not an unlabelled icon that collapses into the
  // drawer below 640px — so there is no drawer hop on mobile any more. The drawer still lists it
  // (with its label); that copy is asserted separately below.
  await page
    .locator(".hud-topbar")
    .getByRole("link", { name: "Map", exact: true })
    .click();
  await expect(page).toHaveURL(/\/overview$/);
  // The overview surface mounts (loading/empty/error state — never a blank route).
  await expect(page.locator(".tr-overview")).toBeVisible();
});

test("on a phone the drawer names the same five sections the bar shows as icons (#1058)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "the drawer only exists ≤800px");
  await page.goto("/");
  await page.locator(".navToggle").click();
  const drawer = page
    .locator(".sidebar")
    .getByRole("navigation", { name: "Sections" });
  await expect(drawer.getByRole("link")).toHaveText([
    "Sessions",
    "Missions",
    "Ask",
    "Map",
    "Templates",
  ]);

  // …and the names are RENDERED, not merely present. `toHaveText` reads `textContent`, which the
  // bar's icon-only rule leaves intact while clipping the label to 1px — so the assertion above
  // passed against a drawer showing five unlabelled boxes. Width is what distinguishes them.
  for (const label of await drawer.locator(".section-nav-label").all()) {
    const box = (await label.boundingBox())!;
    expect(box.width, "a drawer section label is clipped").toBeGreaterThan(20);
  }
  // The BAR's copy, at this width, is the opposite — clipped, and that is deliberate.
  const barLabel = page
    .locator(".hud-topbar .section-nav .section-nav-label")
    .first();
  expect((await barLabel.boundingBox())!.width).toBeLessThan(4);
  await drawer.getByRole("link", { name: "Map", exact: true }).click();
  await expect(page).toHaveURL(/\/overview$/);
  // Navigating closes the drawer — the shell's route effect, unchanged by #1058.
  await expect(page.locator(".app.navOpen")).toHaveCount(0);
});

test("layout snapshot (per-project: desktop + mobile viewports)", async ({
  page,
}, testInfo) => {
  await page.goto("/");
  // Screenshot named per project → mobile vs desktop layout regressions are visible/diffable.
  await testInfo.attach(`shell-${testInfo.project.name}`, {
    body: await page.screenshot({ fullPage: true }),
    contentType: "image/png",
  });
});
