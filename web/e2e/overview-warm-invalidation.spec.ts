import { expect, test, type Page } from "@playwright/test";
import { settingsPath } from "../src/routes/settingsTabs";

/** #1007 Phase 1 — a mutation made on ANY surface is reflected when the operator returns to the map.
 *
 *  These began as invalidation regressions from Hermes' review: the retained result was served
 *  without fetching inside a 30 s window, so a bulk archive in Settings or a Favorite in the
 *  sidebar left the map showing rows the operator had just changed. That window is gone — every
 *  re-entry revalidates — so these specs now pin the USER-VISIBLE outcome rather than the
 *  invalidation mechanism that used to produce it.
 *
 *  That distinction matters for how they are written. Asserting "a second `/api/sessions` sequence
 *  ran" would now pass on *any* re-entry and prove nothing about the mutation. So each test asserts
 *  that the map's **rows actually changed** to match what the mutation did, driven through the real
 *  UI so the request travels the app's own transport.
 *
 *  THREE RULES, each because an earlier draft broke it and passed anyway:
 *  1. **Mutate through the real UI.** `page.evaluate(fetch(...))` never enters `lib/api.ts`.
 *  2. **Navigate in-app only.** `page.goto()` is a full document load that discards the retained
 *     result, so any assertion after one is guaranteed by the reload rather than by the behaviour.
 *  3. **Assert unconditionally.** No `if (await locator.count())` guards. */

const now = Math.floor(Date.now() / 1000);
const BASE = Array.from({ length: 4 }, (_, i) => ({
  id: `claude:s${i + 1}`,
  engine: "claude",
  uuid: `s${i + 1}`,
  short_uuid: `s${i + 1}`,
  cwd: "/home/u/proj",
  project: { kind: "project", id: "p1", name: "proj", color: "#ffb000" },
  last_mtime: now - i * 60,
  first_user_message: "",
  title: `Map session ${i + 1}`,
  sticky: false,
  archived: false,
  ai_summary: "",
}));

interface Harness {
  mapPages: number[];
  visible: typeof BASE;
  /** Mutating requests that actually reached the network, as `METHOD path`. */
  mutations: string[];
}

async function mockApp(page: Page): Promise<Harness> {
  const h: Harness = { mapPages: [], visible: BASE, mutations: [] };
  const note = (r: { request: () => { method: () => string; url: () => string } }) => {
    const m = r.request().method();
    if (m !== "GET") h.mutations.push(`${m} ${new URL(r.request().url()).pathname}`);
  };
  await page.route("**/api/**", (r) => {
    note(r);
    return r.fulfill({ json: {} });
  });
  await page.route("**/api/pulse/notifications**", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0 } }),
  );
  await page.route("**/api/**/draft", (r) =>
    r.fulfill({ json: { id: "d", text: "", attachments: [], updated_at: null } }),
  );
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/prompts", (r) => r.fulfill({ json: { prompts: [] } }));
  await page.route("**/api/ai/activity", (r) => r.fulfill({ json: { running: [], last: {} } }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [{ cwd: "/home/u/proj", label: "proj" }] } }),
  );
  await page.route("**/api/projects**", (r) => {
    note(r);
    if (r.request().method() !== "GET") return r.fulfill({ json: { id: "p1", ok: true } });
    return r.fulfill({
      json: {
        projects: [
          {
            id: "p1",
            name: "proj",
            color: "#ffb000",
            archived: false,
            folders: [],
            default_folder: "/home/u/proj",
          },
        ],
      },
    });
  });
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route(/\/api\/sessions\/[^?]+$/, (r) =>
    r.fulfill({ status: 404, json: { detail: "not found" } }),
  );
  await page.route("**/api/sessions?**", (r) => {
    const u = new URL(r.request().url());
    const limit = Number(u.searchParams.get("limit") ?? "20");
    const offset = Number(u.searchParams.get("offset") ?? "0");
    if (limit === 200) {
      h.mapPages.push(offset);
      return r.fulfill({
        json: {
          sessions: h.visible,
          next_offset: null,
          total: h.visible.length,
          facets: { projects: [], engines: ["claude"] },
        },
      });
    }
    const slice = h.visible.slice(offset, offset + limit);
    return r.fulfill({
      json: {
        sessions: slice,
        next_offset: offset + limit < h.visible.length ? offset + limit : null,
        total: h.visible.length,
        facets: { projects: [], engines: ["claude"] },
      },
    });
  });
  await page.route(/\/api\/sessions\/[^/]+\/(favorite|unfavorite)$/, (r) => {
    note(r);
    return r.fulfill({ json: { id: "claude:s1", sticky: r.request().url().endsWith("/favorite") } });
  });
  await page.route("**/api/sessions/archive-older", (r) => {
    note(r);
    return r.fulfill({ json: { archived: 2, skipped: 0 } });
  });
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: ["project:p1"],
        projects_hidden: [],
        project_names: {},
        compose_default: "collapsed",
      },
    }),
  );
  return h;
}

const chips = (page: Page) => page.locator(".tr-overview .tr-ov-chip");
/** The session LIST's rows — scoped to `.sidebarBody` since #1058, because the drawer now also
 *  carries the section nav, whose Sessions entry points at the last session route and so matches a
 *  bare `a[href^="/s/"]`. A CSS locator does not skip `display: none`, so it counted that link on
 *  desktop too. */
const rows = (page: Page) =>
  page.locator('aside.sidebar .sidebarBody a[href^="/s/"]');
/** MAP, from whichever nav is reachable right now (#1058).
 *
 *  There are two: the top bar's row and the drawer's labelled copy. When the mobile drawer is open
 *  it is a MODAL and the shell marks the header `inert` (#940), so the bar's copy resolves, looks
 *  visible, and can never be clicked. One selector picks the right one from the shell's own open
 *  state rather than from the project name, so desktop, 801px and a phone all take the same path. */
const overviewLink = (page: Page) =>
  page.locator(
    '.app.navOpen aside.sidebar a[data-section="map"], .app:not(.navOpen) .hud-topbar a[data-section="map"]',
  );
const settingsLink = (page: Page) =>
  page.getByRole("link", { name: "Settings", exact: true }).filter({ visible: true }).first();

async function openDrawer(page: Page, project: string) {
  if (project !== "mobile") return;
  if (await page.locator(".app.navOpen").count()) return;
  await page.locator("header .navToggle").click();
  await expect(page.locator(".app.navOpen")).toHaveCount(1);
}

async function toMap(page: Page, project: string) {
  await openDrawer(page, project);
  await overviewLink(page).click();
  await expect(page).toHaveURL(/\/overview$/);
}

async function open(page: Page, project: string) {
  if (project !== "mobile") await page.setViewportSize({ width: 801, height: 900 });
}

test("a FAVORITE set from the sidebar is reflected on the map's chip when you return (#1007)", async ({
  page,
}, testInfo) => {
  // Hermes reproduced the original symptom: the warm map still offered "Favorite" after the
  // sidebar had flipped it, because the map derives that label from the retained row. Asserted on
  // the rendered row rather than on a request count, which no longer distinguishes anything.
  const project = testInfo.project.name;
  await open(page, project);
  const h = await mockApp(page);

  await page.goto("/overview");
  await expect(chips(page)).toHaveCount(BASE.length);

  await openDrawer(page, project);
  await rows(page).nth(1).click();
  await expect(page).toHaveURL(/\/s\/claude\/s2$/);

  await openDrawer(page, project);
  const row = page.locator("aside.sidebar li").filter({ hasText: "Map session 1" }).first();
  await row.getByRole("button", { name: "Session actions" }).click();
  await page
    .getByRole("menu", { name: "Session actions" })
    .getByRole("menuitem", { name: "Favorite session" })
    .click();
  // It really went out over the wire — otherwise the rest proves nothing.
  await expect
    .poll(() => h.mutations.filter((m) => m.includes("/favorite")).length)
    .toBeGreaterThan(0);

  // The server now reports it favourited, and the title changes with it so the map's rendered row
  // is observably the refreshed one rather than the retained copy.
  h.visible = BASE.map((s) =>
    s.id === "claude:s1" ? { ...s, sticky: true, title: "Map session 1 ★" } : s,
  );
  await toMap(page, project);

  await expect(
    page.locator(".tr-overview .tr-ov-chip", { hasText: "Map session 1 ★" }),
  ).toBeVisible();
  await expect(chips(page)).toHaveCount(BASE.length);
});

test("a BULK archive in Settings removes those sessions from the map on return (#1007)", async ({
  page,
}, testInfo) => {
  // `Settings.tsx`'s CleanupCard calls `api.archiveOlder`, which the original hand-instrumented
  // channel never covered. Driven through the real two-step confirm.
  const project = testInfo.project.name;
  await open(page, project);
  const h = await mockApp(page);

  await page.goto("/overview");
  await expect(chips(page)).toHaveCount(BASE.length);

  // In-app to Settings → Maintenance. A `goto` would reload the document and discard the retained
  // result, which is the state under test.
  await openDrawer(page, project);
  await settingsLink(page).click();
  await expect(page).toHaveURL(/\/settings/);
  await page
    .locator('nav[aria-label="Settings"]')
    .getByRole("link", { name: "Maintenance" })
    .click();
  await expect(page).toHaveURL(new RegExp(`${settingsPath("maintenance")}$`));

  await page.getByRole("button", { name: "Archive older" }).click();
  await page.getByRole("button", { name: "Confirm archive" }).click();
  await expect
    .poll(() => h.mutations.filter((m) => m.includes("archive-older")).length)
    .toBeGreaterThan(0);

  h.visible = BASE.slice(0, 2);
  await toMap(page, project);

  await expect(chips(page)).toHaveCount(2);
});
