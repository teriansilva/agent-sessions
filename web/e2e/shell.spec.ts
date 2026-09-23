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

test("opens the fullscreen session map from the Sessions sub-menu at every width (#139/#211, #1058, #1069)", async ({
  page,
}) => {
  await page.goto("/");
  // #1069: the map is a sub-menu entry of Sessions, behind the chevron beside its label — at every
  // width, so there is still no drawer hop on mobile. The drawer's nested copy is asserted below.
  const bar = page.locator(".hud-topbar");
  await expect(bar.getByRole("link", { name: "Map", exact: true })).toHaveCount(
    0,
  );
  const chevron = bar.getByRole("button", { name: "Sessions menu" });
  await chevron.click();
  const menu = page.getByRole("menu", { name: "Sessions menu" });
  await expect(menu.getByRole("menuitem")).toHaveText([
    "Sessions",
    "Sessions map",
  ]);
  // The panel is portalled out of the top bar's stacking context: a real click must LAND on it,
  // not on the pane underneath (#752/#987). `click()` would fail if something else was on top.
  await menu.getByRole("menuitem", { name: "Sessions map" }).click();
  await expect(page).toHaveURL(/\/overview$/);
  // The overview surface mounts (loading/empty/error state — never a blank route).
  await expect(page.locator(".tr-overview")).toBeVisible();
  await expect(menu).toHaveCount(0);
  // On the map the Sessions parent says "the page is under me", not "I am the page".
  await expect(
    bar.getByRole("link", { name: "Sessions", exact: true }),
  ).toHaveAttribute("aria-current", "true");
});

test("the Sessions label still goes straight to the sessions view; Escape closes its menu back onto the chevron (#1069)", async ({
  page,
}) => {
  await page.goto("/ask");
  const bar = page.locator(".hud-topbar");
  // Ask leads the row.
  await expect(bar.locator(".section-nav > *").first()).toContainText("Ask");
  const chevron = bar.getByRole("button", { name: "Sessions menu" });
  await chevron.click();
  const menu = page.getByRole("menu", { name: "Sessions menu" });
  await expect(menu).toBeVisible();
  // Focus moved into the menu (its first item), and Escape hands it back to the trigger.
  await expect(menu.getByRole("menuitem").first()).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(menu).toHaveCount(0);
  await expect(chevron).toBeFocused();
  // One click on the label — no menu in the way of the common case.
  await bar.getByRole("link", { name: "Sessions", exact: true }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(
    bar.getByRole("link", { name: "Sessions", exact: true }),
  ).toHaveAttribute("aria-current", "page");
});

test("on a phone the drawer carries no second copy of the sections, and its filters fold (#1069)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "the drawer only exists ≤800px");
  await page.goto("/");
  await page.locator(".navToggle").click();
  const sidebar = page.locator("aside.sidebar");
  await expect(sidebar).toBeVisible();
  // The bar above already shows every section; the drawer used to repeat all of them.
  await expect(sidebar.locator("a[data-section], a[data-subsection]")).toHaveCount(0);
  await expect(sidebar.getByRole("navigation", { name: "Sections" })).toHaveCount(0);

  // The filter block folds behind one header and gives its height back to the list.
  const toggle = sidebar.getByTestId("filters-toggle");
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  const search = sidebar.getByLabel("Search sessions");
  await expect(search).toBeVisible();
  const openBottom = (await toggle.evaluate((el) => el.parentElement!.getBoundingClientRect().bottom));
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(search).toBeHidden();
  const foldedBottom = (await toggle.evaluate((el) => el.parentElement!.getBoundingClientRect().bottom));
  // This fixture has one engine and no mission facet, so the block is search + project select +
  // the Active/Archived row: three 44px rows plus gaps. A live install with more agents and
  // missions gives back more.
  expect(openBottom - foldedBottom).toBeGreaterThanOrEqual(3 * 44);
  // …and the choice survives a reload on this device.
  await page.reload();
  await page.locator(".navToggle").click();
  await expect(sidebar.getByTestId("filters-toggle")).toHaveAttribute("aria-expanded", "false");
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

test("Missions carries a BETA tag inside its own tile at every width, and keeps its name (#1085)", async ({
  page,
}) => {
  await page.goto("/");
  for (const width of [320, 412, 800, 801, 1440]) {
    await page.setViewportSize({ width, height: 740 });
    const link = page
      .locator(".hud-topbar")
      .getByRole("navigation", { name: "Main sections" })
      .getByRole("link", { name: "Missions", exact: true });
    await expect(link).toHaveAttribute("title", "Missions (beta)");
    const tag = link.locator(".section-nav-beta");
    await expect(tag, `tag at ${width}`).toBeVisible();
    const t = (await tag.boundingBox())!;
    const l = (await link.boundingBox())!;
    expect(t.x, `tag inside at ${width}`).toBeGreaterThanOrEqual(l.x);
    expect(t.x + t.width, `tag inside at ${width}`).toBeLessThanOrEqual(l.x + l.width + 0.5);
    expect(t.y).toBeGreaterThanOrEqual(l.y);
    expect(t.y + t.height).toBeLessThanOrEqual(l.y + l.height + 0.5);
  }
});
