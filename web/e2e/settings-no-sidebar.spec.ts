import { expect, test, type Page } from "@playwright/test";

/** #1129 — Settings renders WITHOUT the session sidebar.
 *
 *  The shell's `<aside class="sidebar">` is mounted on every route; on `/settings` it must be
 *  HIDDEN — never unmounted. The distinction is load-bearing: `SessionList` renders outside
 *  `<Routes>` on purpose (#1007's continuity contract), and `.app.noSidebar .sidebar` is the
 *  identical DOM state a desktop-collapsed sidebar already produces, so the sidebar's rows,
 *  cursor and poll survive a Settings visit exactly the way they survive a collapsed sidebar.
 *  A `settingsRoute &&` on the JSX would fail this spec's round-trip half — that is the bug
 *  this test is shaped to catch.
 *
 *  The toggle and the resize handle stand down WITH the surface: no control for a panel that
 *  does not exist, at either width. On mobile the off-canvas drawer IS the session sidebar,
 *  so the hamburger stands down there too (§ Settings in the issue).
 *
 *  Real-browser assertions, per the UI-fix rule: `display: none` vs off-canvas-transform is a
 *  layout fact jsdom cannot model — Playwright counts a translated-off-screen drawer as
 *  visible and a `display: none` panel as hidden, which is exactly the red/green line.
 */

const now = Math.floor(Date.now() / 1000);
const TOTAL = 45; // > 2 pages, so "Load more" leaves a cursor only a surviving hook holds
const ALL = Array.from({ length: TOTAL }, (_, i) => ({
  id: `claude:s${i + 1}`,
  engine: "claude",
  uuid: `s${i + 1}`,
  short_uuid: `s${i + 1}`,
  cwd: "/home/u/proj",
  project: { kind: "project", id: "p1", name: "proj", color: "#ffb000" },
  last_mtime: now - i * 60,
  first_user_message: "",
  title: `Sidebar session ${i + 1}`,
  sticky: false,
  archived: false,
  ai_summary: "",
}));

/** One `/api/sessions` LISTING request (`limit != 200` — the map's full-set pages are a
 *  different consumer; this spec never visits the map, but the filter is cheap insurance). */
interface Req {
  limit: number;
  offset: number;
}

async function mockApp(page: Page): Promise<Req[]> {
  const reqs: Req[] = [];
  // Playwright matches routes in REVERSE registration order — catch-all first (the
  // sidebar-continuity idiom): every Settings-tab fetch this spec doesn't care to name
  // resolves as `{}` and the shell is what is under test.
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/pulse/notifications**", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0 } }),
  );
  await page.route("**/api/**/draft", (r) =>
    r.fulfill({ json: { id: "d", text: "", attachments: [], updated_at: null } }),
  );
  await page.route("**/api/projects**", (r) =>
    r.fulfill({
      json: { projects: [{ id: "p1", name: "proj", color: "#ffb000", archived: false }] },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route(/\/api\/sessions\/[^?]+$/, (r) =>
    r.fulfill({ status: 404, json: { detail: "not found" } }),
  );
  await page.route("**/api/sessions?**", (r) => {
    const u = new URL(r.request().url());
    const limit = Number(u.searchParams.get("limit") ?? "20");
    const offset = Number(u.searchParams.get("offset") ?? "0");
    reqs.push({ limit, offset });
    const slice = ALL.slice(offset, offset + limit);
    return r.fulfill({
      json: {
        sessions: slice,
        next_offset: offset + limit < ALL.length ? offset + limit : null,
        total: ALL.length,
        facets: { projects: [], engines: ["claude"] },
      },
    });
  });
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: [],
        projects_hidden: [],
        project_names: {},
        compose_default: "collapsed",
      },
    }),
  );
  return reqs;
}

const sidebarRows = (page: Page) =>
  page.locator('aside.sidebar .sidebarBody a[href^="/s/"]');

/** In-app route to Settings — a `page.goto` would reload the app and remount everything,
 *  which is precisely the lifecycle this spec exists to refute. The operator menu's Settings
 *  item is the real path. */
async function openSettingsInApp(page: Page) {
  await page.getByTestId("operator-menu").click();
  await page.getByRole("menuitem", { name: "Settings" }).click();
  await expect(page).toHaveURL(/\/settings/);
}

test.describe("Settings has no session sidebar (#1129)", () => {
  test("desktop: the docked sidebar is hidden and the pane spans the deck", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name === "mobile", "desktop shell");
    await mockApp(page);
    await page.goto("/settings");
    await expect(page).toHaveURL(/\/settings/);

    // The session sidebar is display:none — hidden, not parked off-screen.
    await expect(page.locator("aside.sidebar")).toBeHidden();
    // No control for a surface that does not exist.
    await expect(page.locator("header .navToggle")).toHaveCount(0);

    // The pane spans the deck: with the 320px column present the floating pane starts at
    // x ≈ 338; without it, at its own 6px margin.
    const pane = await page.locator("main.terminal-pane").boundingBox();
    expect(pane).not.toBeNull();
    expect(pane!.x, "pane left edge").toBeLessThan(20);
  });

  test("desktop: a session route still has both — the removal is Settings-specific", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name === "mobile", "desktop shell");
    await mockApp(page);
    await page.goto("/");
    await expect(page.locator("aside.sidebar")).toBeVisible();
    await expect(page.locator("header .navToggle")).toHaveCount(1);
    await expect(page.locator(".sidebar-resize")).toHaveCount(1);
  });

  test("mobile: the drawer and its hamburger stand down on Settings, and return on a session route", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "phone shell");
    await mockApp(page);
    await page.goto("/settings");
    await expect(page).toHaveURL(/\/settings/);

    // The drawer IS the session sidebar at this width: hidden means display:none, not the
    // closed drawer's off-canvas transform (which Playwright counts as visible).
    await expect(page.locator("aside.sidebar")).toBeHidden();
    await expect(page.locator("header .navToggle")).toHaveCount(0);

    // The way back is the section nav's Sessions entry — and the sidebar is whole again.
    await page
      .locator('[data-testid="section-nav"] a[data-section="sessions"]')
      .click();
    await expect(page.locator("header .navToggle")).toHaveCount(1);
    await page.locator("header .navToggle").click();
    await expect(page.locator("aside.sidebar")).toBeVisible();
  });

  test("the sidebar's state survives a Settings round trip with no remount fetch (desktop)", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name === "mobile", "desktop shell");
    const reqs = await mockApp(page);
    // Before navigation, so the 15 s poll interval is created against the fake clock.
    await page.clock.install();

    await page.goto("/");
    await expect(sidebarRows(page)).toHaveCount(20);
    await page.getByRole("button", { name: "Load more" }).click();
    await expect(sidebarRows(page)).toHaveCount(40);
    const settled = reqs.length;

    // → Settings, in-app. The sidebar goes display:none; the hook must NOT remount.
    await openSettingsInApp(page);
    await expect(page.locator("aside.sidebar")).toBeHidden();

    // → back to a session route via the section nav. Same 40 rows, no bootstrap fetch.
    await page
      .locator('[data-testid="section-nav"] a[data-section="sessions"]')
      .click();
    await expect(page).toHaveURL(/\/$/);
    await expect(sidebarRows(page)).toHaveCount(40);

    // NOT ONE listing request across the round trip. A remount's bootstrap is immediate and
    // the clock is frozen, so no poll tick can be masquerading as one.
    expect(reqs).toEqual([
      { limit: 20, offset: 0 },
      { limit: 20, offset: 20 },
    ]);
    expect(reqs.length).toBe(settled);

    // ...and the poll is alive on the SAME hook instance: its next tick asks for the 40 rows
    // this hook has loaded. A remounted sidebar would have reset to 20.
    await page.clock.fastForward(15_001);
    await expect.poll(() => reqs.length).toBe(settled + 1);
    expect(reqs[settled]).toEqual({ limit: 40, offset: 0 });
  });
});
