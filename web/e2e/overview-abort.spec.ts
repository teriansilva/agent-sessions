import { expect, test, type Page, type Route } from "@playwright/test";

/** #1007 Phase 2 — leaving the map mid-sequence CANCELS it: the in-flight page is aborted at the
 *  network boundary, no further page is requested, nothing is committed, and no error is shown.
 *
 *  WHAT THIS DOES AND DOES NOT SAVE. Measured on the author's 1,489-session host, page 1 of the
 *  map's sequence is a 3,551 ms cold walk and pages 2–8 cost 0.3 ms in total — and the server runs
 *  that walk in `asyncio.to_thread`, so a browser abort cannot stop a scan already running. By the
 *  time an operator leaves, the expensive page is usually done. What an abort buys is on the
 *  CLIENT: a sequence nobody is watching stops issuing round trips and stops downloading the one
 *  in flight, and an abandoned sequence provably never commits. So these specs assert request
 *  lifecycles at the browser's network boundary — never anything about server work.
 *
 *  Driven through the real `lib/api.ts` transport by the app's own map route, and navigated
 *  IN-APP only: `page.evaluate(fetch(...))` would bypass the transport under test, and a
 *  `page.goto()` after the first load reloads the document and discards the retained result this
 *  spec checks is intact. The only `goto` is the initial load. */

const now = Math.floor(Date.now() / 1000);
/** The map pages by 200; the mock serves ONE row per page so a partially-committed sequence
 *  would show up as a chip count, not hide inside a big one. */
const STEP = 200;
const PAGES = 8;
const row = (i: number, title = `Map session ${i + 1}`) => ({
  id: `claude:s${i + 1}`,
  engine: "claude",
  uuid: `s${i + 1}`,
  short_uuid: `s${i + 1}`,
  cwd: "/home/u/proj",
  project: { kind: "project", id: "p1", name: "proj", color: "#ffb000" },
  last_mtime: now - i * 60,
  first_user_message: "",
  title,
  sticky: false,
  archived: false,
  ai_summary: "",
});
const ALL = Array.from({ length: PAGES }, (_, i) => row(i));

interface Harness {
  /** Every map page request (`limit=200`) that reached the network, by offset, in order. */
  mapPages: number[];
  /** Map page requests that ended in a FAILURE at the network boundary (an abort), by offset. */
  failed: { offset: number; error: string }[];
  /** Map page requests that completed normally, by offset. */
  finished: number[];
  /** Hold the next map request at this offset instead of answering it. */
  holdAt: number | null;
  /** The held request, once it arrives. */
  held: Route | null;
  /** What each map page serves. */
  rows: typeof ALL;
}

const offsetOf = (url: string) => Number(new URL(url).searchParams.get("offset") ?? "0");
const isMapPage = (url: string) =>
  url.includes("/api/sessions?") && new URL(url).searchParams.get("limit") === "200";

function mapPage(h: Harness, offset: number) {
  const i = offset / STEP;
  return {
    sessions: h.rows.slice(i, i + 1),
    next_offset: i < PAGES - 1 ? offset + STEP : null,
    total: h.rows.length,
    facets: { projects: [], engines: ["claude"] },
  };
}

async function mockApp(page: Page): Promise<Harness> {
  const h: Harness = {
    mapPages: [],
    failed: [],
    finished: [],
    holdAt: null,
    held: null,
    rows: ALL,
  };
  // Observed at the browser's network boundary, independently of how the route answers.
  page.on("requestfailed", (req) => {
    if (isMapPage(req.url()))
      h.failed.push({ offset: offsetOf(req.url()), error: req.failure()?.errorText ?? "" });
  });
  page.on("requestfinished", (req) => {
    if (isMapPage(req.url())) h.finished.push(offsetOf(req.url()));
  });

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
  await page.route(/\/api\/sessions\/[^?]+$/, (r) =>
    r.fulfill({ status: 404, json: { detail: "not found" } }),
  );
  await page.route("**/api/sessions?**", (r) => {
    const url = r.request().url();
    if (isMapPage(url)) {
      const offset = offsetOf(url);
      h.mapPages.push(offset);
      if (h.holdAt === offset) {
        h.holdAt = null;
        h.held = r;
        return;
      }
      return r.fulfill({ json: mapPage(h, offset) });
    }
    // The sidebar (limit=20): a different consumer, answered plainly.
    const u = new URL(url);
    const limit = Number(u.searchParams.get("limit") ?? "20");
    const offset = offsetOf(url);
    return r.fulfill({
      json: {
        sessions: h.rows.slice(offset, offset + limit),
        next_offset: offset + limit < h.rows.length ? offset + limit : null,
        total: h.rows.length,
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
const rows = (page: Page) => page.locator('aside.sidebar a[href^="/s/"]');
const overviewLink = (page: Page) =>
  page.getByRole("link", { name: "Open session overview" }).filter({ visible: true }).first();

/** Idempotent: the drawer only closes on NAVIGATION. */
async function openDrawer(page: Page, project: string) {
  if (project !== "mobile") return;
  if (await page.locator(".app.navOpen").count()) return;
  await page.locator("header .navToggle").click();
  await expect(page.locator(".app.navOpen")).toHaveCount(1);
}

async function toPane(page: Page, project: string, n: number) {
  await openDrawer(page, project);
  await rows(page).nth(n - 1).click();
  await expect(page).toHaveURL(new RegExp(`/s/claude/s${n}$`));
  await expect(page.locator(".tr-overview")).toHaveCount(0); // the map route really unmounted
}

async function toMap(page: Page, project: string) {
  await openDrawer(page, project);
  await overviewLink(page).click();
  await expect(page).toHaveURL(/\/overview$/);
}

/** 801px is still "desktop" by the ≤800px breakpoint but leaves the map under its 560px window
 *  floor, so a sidebar row NAVIGATES rather than opening a window over the map — which would keep
 *  the route mounted and make "leaving" a different code path. */
async function open(page: Page, project: string) {
  if (project !== "mobile") await page.setViewportSize({ width: 801, height: 900 });
}

/** Count every appearance of the blocking spinner AND the map's error text from now on. Installed
 *  before the navigations under test: a flash between two awaits leaves no trace for an
 *  after-the-fact check. */
async function watchStates(page: Page) {
  await page.evaluate(() => {
    const w = window as unknown as {
      __seen: { spinner: number; error: number };
      __obs?: MutationObserver;
    };
    w.__seen = { spinner: 0, error: 0 };
    const check = () => {
      const text = document.body.textContent ?? "";
      if (text.includes("Loading session map")) w.__seen.spinner += 1;
      if (text.includes("Couldn’t load sessions")) w.__seen.error += 1;
    };
    w.__obs?.disconnect();
    w.__obs = new MutationObserver(check);
    w.__obs.observe(document.body, { childList: true, subtree: true, characterData: true });
    check();
  });
}
const seen = (page: Page) =>
  page.evaluate(
    () => (window as unknown as { __seen: { spinner: number; error: number } }).__seen,
  );

/** Let the page run anything a settled response could still trigger — body read, JSON parse, the
 *  sequence's continuation — before a "nothing more happened" assertion is made. */
const settle = (page: Page) => page.evaluate(() => new Promise((r) => setTimeout(r, 500)));

test("leaving the map mid-sequence aborts the in-flight page and requests no further page (#1007)", async ({
  page,
}, testInfo) => {
  const project = testInfo.project.name;
  await open(page, project);
  const h = await mockApp(page);

  // The one full document load: a complete 8-page sequence, retained above the router.
  await page.goto("/overview");
  await expect(chips(page)).toHaveCount(PAGES);
  expect(h.mapPages).toEqual(ALL.map((_, i) => i * STEP));

  await toPane(page, project, 2);

  // Return: the retained map paints warm while a revalidation pages behind it. Page 3 is held, so
  // the sequence is genuinely mid-flight — two pages collected, six to go.
  h.holdAt = 2 * STEP;
  await watchStates(page);
  await toMap(page, project);
  await expect.poll(() => h.held !== null).toBe(true);
  await expect(chips(page)).toHaveCount(PAGES);
  const issuedBeforeLeaving = h.mapPages.length;
  expect(h.mapPages.slice(PAGES)).toEqual([0, STEP, 2 * STEP]);
  expect(h.failed).toEqual([]);

  // Leave, in-app, while page 3 is still in flight — then answer it, as a slow server eventually
  // would. Once the browser has dropped the request, the answer may be refused.
  await toPane(page, project, 3);
  await h.held!.fulfill({ json: mapPage(h, 2 * STEP) }).catch(() => {});
  await settle(page);

  // 1. No further page went out after leaving. (Asserted first, because it holds independently of
  //    HOW the sequence stopped: a sequence that let page 3 land and kept paging fails here.)
  expect(h.mapPages.length).toBe(issuedBeforeLeaving);

  // 2. The page in flight was CANCELLED at the network boundary, not downloaded and then ignored.
  await expect.poll(() => h.failed.map((f) => f.offset)).toEqual([2 * STEP]);
  expect(h.failed[0].error).toMatch(/ABORTED|cancel/i);
  expect(h.finished.filter((o) => o === 2 * STEP)).toHaveLength(1); // the first load's page 3 only

  // 3. Neither the abort nor the navigation showed the error or the blocking spinner.
  expect(await seen(page)).toEqual({ spinner: 0, error: 0 });
});

test("an abandoned sequence never commits: the retained map is intact on return, then refreshes (#1007)", async ({
  page,
}, testInfo) => {
  const project = testInfo.project.name;
  await open(page, project);
  const h = await mockApp(page);

  await page.goto("/overview");
  await expect(chips(page)).toHaveCount(PAGES);
  await toPane(page, project, 2);

  // Revalidation #1 serves RENAMED rows, so any of its pages reaching the map would be visible —
  // and it is abandoned after collecting two of eight.
  h.rows = ALL.map((_, i) => row(i, `Abandoned ${i + 1}`));
  h.holdAt = 2 * STEP;
  await watchStates(page);
  await toMap(page, project);
  await expect.poll(() => h.held !== null).toBe(true);
  await toPane(page, project, 3);
  await expect.poll(() => h.failed.map((f) => f.offset)).toEqual([2 * STEP]);
  await h.held!.fulfill({ json: mapPage(h, 2 * STEP) }).catch(() => {});
  await settle(page);
  const issuedBeforeReturn = h.mapPages.length;

  // Revalidation #2 serves the rows the server now has; its FIRST page is held so the retained
  // result can be inspected before anything new could have committed.
  h.rows = ALL.map((_, i) => row(i, `Fresh ${i + 1}`));
  h.held = null;
  h.holdAt = 0;
  await toMap(page, project);
  await expect.poll(() => h.held !== null && h.mapPages.length > issuedBeforeReturn).toBe(true);

  // Intact: all eight original rows — not the two the abandoned sequence collected, and none of
  // its renamed titles.
  await expect(chips(page)).toHaveCount(PAGES);
  await expect(page.locator(".tr-overview .tr-ov-chip", { hasText: "Map session 1" })).toBeVisible();
  await expect(page.locator(".tr-overview .tr-ov-chip", { hasText: "Abandoned" })).toHaveCount(0);

  // The fresh sequence is not affected by the aborted one: it pages to completion and commits.
  await h.held!.fulfill({ json: mapPage(h, 0) });
  await expect(page.locator(".tr-overview .tr-ov-chip", { hasText: "Fresh" })).toHaveCount(PAGES);
  expect(h.mapPages.slice(issuedBeforeReturn)).toEqual(ALL.map((_, i) => i * STEP));
  expect(await seen(page)).toEqual({ spinner: 0, error: 0 });
});
