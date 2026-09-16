import { expect, test, type Page } from "@playwright/test";

/** #1007 Phase 1, task 1 — PIN the sidebar's survive-navigation behaviour.
 *
 *  `SessionsProvider` is mounted above `RouterProvider` and `SessionList` renders inside `Layout`
 *  but OUTSIDE `<Routes>` (`App.tsx`), so the sidebar's `useSessionsList` state — rows, cursor,
 *  filters and the 15 s poll — already survives `/overview` ⇄ `/s/:engine/:id`. Nothing asserted
 *  it. The property rests purely on a component's POSITION in the tree, which any layout refactor
 *  can move without an obvious signal, silently converting the sidebar into a second
 *  refetch-on-every-navigation surface. This is that guard, added BEFORE the map change so it
 *  holds independently of it.
 *
 *  WHY COUNTING REQUESTS IS NOT ENOUGH, and why this test is shaped the way it is.
 *  A remount's bootstrap fetch and a 15 s poll tick are the SAME request on the wire — both
 *  `offset=0`, both silent, same path. A test that merely counts `/api/sessions` calls therefore
 *  passes against the very bug it claims to catch: it cannot tell a refetch from a refresh.
 *
 *  So the discriminator is `limit`, which is hook-LOCAL state that only a surviving instance can
 *  carry. The poll asks for `max(PAGE, rowCount)` (`useSessionsList` → `refresh`), so after one
 *  "Load more" a surviving hook polls with `limit=40`. A REMOUNTED hook would reset to 20 rows and
 *  poll with `limit=20` — and would fire its bootstrap immediately on arrival rather than waiting
 *  for a tick. Both halves are asserted here: zero requests across the navigations themselves,
 *  then a tick that still carries the 40 rows the hook held before them.
 *
 *  The clock is installed BEFORE navigation (the `notification-retire.spec.ts` idiom) so the 15 s
 *  interval is created against the fake clock. That is what makes "no request fired during the
 *  navigations" a real assertion instead of a race against a poll tick that might land mid-test,
 *  and it lets the tick below be fired deliberately rather than waited out.
 */

const now = Math.floor(Date.now() / 1000);
const TOTAL = 45; // > 2 pages, so "Load more" leaves a cursor a remount would lose
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

/** One `/api/sessions` LISTING request. `limit` is what separates the two consumers AND the two
 *  lifecycles: the sidebar pages by 20 and polls by its loaded row count; the map pages by 200. */
interface Req {
  limit: number;
  offset: number;
}

/** The sidebar's requests. The map's full-set pages (`limit=200`) are a different consumer and
 *  must never be counted as sidebar activity. */
const sidebarReqs = (reqs: Req[]) => reqs.filter((r) => r.limit !== 200);
const mapReqs = (reqs: Req[]) => reqs.filter((r) => r.limit === 200);

async function mockApp(page: Page): Promise<Req[]> {
  const reqs: Req[] = [];
  // Playwright matches routes in REVERSE registration order — catch-all first.
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
  // The pane's single-session lookup (#867) — a different route from the listing below.
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
        // Collapsed: this spec is about the sidebar's lifetime, not the map's contents.
        overview_expanded: [],
        projects_hidden: [],
        project_names: {},
        compose_default: "collapsed",
      },
    }),
  );
  return reqs;
}

const rows = (page: Page) => page.locator('aside.sidebar a[href^="/s/"]');

/** The two `/overview` links (topbar + sidebar head) share an aria-label; whichever is on screen
 *  for this project is the one to press. */
const overviewLink = (page: Page) =>
  page.getByRole("link", { name: "Open session overview" }).filter({ visible: true }).first();

/** Mobile keeps the sidebar off-canvas; opening the drawer does NOT remount `SessionList`. */
async function openDrawer(page: Page, project: string) {
  if (project !== "mobile") return;
  await page.locator("header .navToggle").click();
  await expect(page.locator("aside.sidebar")).toBeVisible();
}

test("the sidebar list survives /overview ⇄ /s/:engine/:id with no mount fetch, while its 15s poll keeps running (#1007)", async ({
  page,
}, testInfo) => {
  const project = testInfo.project.name;
  // 801px is still "desktop" by the ≤800px breakpoint, but leaves the map under its 560px window
  // floor — so `mapReady` is false and a sidebar row NAVIGATES rather than opening a window
  // (`overview-windows.spec.ts`). That keeps one code path for both projects here, and keeps this
  // spec about the sidebar rather than about the workspace.
  if (project !== "mobile") await page.setViewportSize({ width: 801, height: 900 });
  const reqs = await mockApp(page);
  // Before navigation, so the 15 s interval is created against the fake clock.
  await page.clock.install();

  await page.goto("/");
  await openDrawer(page, project);
  await expect(rows(page)).toHaveCount(20);

  // Page 2 — the cursor + row count that ONLY this hook instance holds.
  await page.getByRole("button", { name: "Load more" }).click();
  await expect(rows(page)).toHaveCount(40);
  expect(sidebarReqs(reqs).map((r) => [r.limit, r.offset])).toEqual([
    [20, 0],
    [20, 20],
  ]);
  const settled = sidebarReqs(reqs).length;

  // → /overview
  await overviewLink(page).click();
  await expect(page).toHaveURL(/\/overview$/);
  await expect(page.locator(".tr-overview")).toBeVisible();
  await expect(rows(page)).toHaveCount(40);

  // → /s/:engine/:id, pressed from the sidebar itself
  await openDrawer(page, project);
  await rows(page).first().click();
  await expect(page).toHaveURL(/\/s\/claude\/s1$/);
  await expect(rows(page)).toHaveCount(40);

  // → back to /overview
  await openDrawer(page, project);
  await overviewLink(page).click();
  await expect(page).toHaveURL(/\/overview$/);
  await expect(rows(page)).toHaveCount(40);

  // The map really did mount and fetch — otherwise the navigations above proved nothing.
  expect(mapReqs(reqs).length).toBeGreaterThan(0);

  // NOT ONE sidebar request across any of those navigations. A remount's bootstrap is immediate,
  // and the clock is frozen, so no poll tick can be masquerading as one here.
  expect(sidebarReqs(reqs).length).toBe(settled);

  // ...and the poll is still ALIVE on the SAME hook instance: its next tick asks for the 40 rows
  // this hook has loaded. A remounted sidebar would have reset to 20 and asked for 20.
  await page.clock.fastForward(15_001);
  await expect.poll(() => sidebarReqs(reqs).length).toBe(settled + 1);
  expect(sidebarReqs(reqs)[settled]).toEqual({ limit: 40, offset: 0 });
});
