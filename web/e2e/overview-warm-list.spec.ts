import { expect, test, type Page } from "@playwright/test";

/** #1007 Phase 1 — the map renders from a WARM retained result instead of a blocking spinner.
 *
 *  `Overview` is lazy-loaded inside `<Routes>` and owns `useOverviewSessions`, so it fully
 *  unmounted and refetched on every visit behind a blocking "Loading session map…". On the
 *  author's 1,489-session host that is a 3.46 s cold disk walk (page 1 of 8; pages 2–8 cost 0.3 ms
 *  in total) paid again each time the operator came back.
 *
 *  WHAT IS AND IS NOT ASSERTED HERE. Retention decides what is DRAWN FIRST, never whether to
 *  fetch: every re-entry revalidates. An earlier cut skipped the request inside a 30 s freshness
 *  window, which made correctness depend on every mutating surface announcing itself — and review
 *  found surfaces that structurally cannot, ending with terminal-backed session creation, which
 *  issues no session REST call at all. So these specs assert the WAIT is gone (no spinner over a
 *  warm map) and that returning always picks up reality — never that a request was skipped.
 *
 *  Real-browser rather than jsdom because the subject is a request lifecycle across real route
 *  unmounts, lazy chunk boundaries and the app's actual provider tree. The spinner is asserted with
 *  a MutationObserver installed BEFORE the navigation, not by checking for its absence afterwards:
 *  a blocking spinner that mounts and unmounts between two awaits is invisible to a check made
 *  after the fact, which is exactly how this regression would slip back in. */

const now = Math.floor(Date.now() / 1000);
const BASE = Array.from({ length: 6 }, (_, i) => ({
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
  /** One entry per map page request (`limit=200`). */
  mapPages: number[];
  /** What the next sequence will be served. */
  visible: typeof BASE;
  /** Make the next map sequence fail, once. */
  failNext: boolean;
}

async function mockApp(page: Page): Promise<Harness> {
  const h: Harness = { mapPages: [], visible: BASE, failNext: false };
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
    const u = new URL(r.request().url());
    const limit = Number(u.searchParams.get("limit") ?? "20");
    const offset = Number(u.searchParams.get("offset") ?? "0");
    if (limit === 200) {
      h.mapPages.push(offset);
      if (h.failNext) {
        h.failNext = false;
        return r.fulfill({ status: 500, json: { detail: "scan failed" } });
      }
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
const chip = (page: Page, n: number) =>
  page.locator(".tr-overview .tr-ov-chip", { hasText: `Map session ${n}` });
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

/** Count every appearance of the BLOCKING map spinner from now on. Installed before the
 *  navigation under test, because a spinner that comes and goes between two awaits leaves no
 *  trace for an after-the-fact assertion. */
async function watchSpinner(page: Page) {
  await page.evaluate(() => {
    const w = window as unknown as { __spin: number; __obs?: MutationObserver };
    w.__spin = 0;
    const check = () => {
      if (document.body.textContent?.includes("Loading session map")) w.__spin += 1;
    };
    w.__obs?.disconnect();
    w.__obs = new MutationObserver(check);
    w.__obs.observe(document.body, { childList: true, subtree: true, characterData: true });
    check();
  });
}
const spinnerSeen = (page: Page) =>
  page.evaluate(() => (window as unknown as { __spin: number }).__spin);

async function toPane(page: Page, project: string, n: number) {
  await openDrawer(page, project);
  await rows(page).nth(n - 1).click();
  await expect(page).toHaveURL(new RegExp(`/s/claude/s${n}$`));
}

async function toMap(page: Page, project: string) {
  await openDrawer(page, project);
  await overviewLink(page).click();
  await expect(page).toHaveURL(/\/overview$/);
}

/** 801px is still "desktop" by the ≤800px breakpoint but leaves the map under its 560px window
 *  floor, so a sidebar row NAVIGATES rather than opening a window — one code path for both
 *  projects, and this spec is about data lifetime, not the workspace. */
async function open(page: Page, project: string) {
  if (project !== "mobile") await page.setViewportSize({ width: 801, height: 900 });
}

test("map → pane → map renders warm: the blocking spinner never returns (#1007)", async ({
  page,
}, testInfo) => {
  const project = testInfo.project.name;
  await open(page, project);
  const h = await mockApp(page);

  await page.goto("/overview");
  await expect(chips(page)).toHaveCount(BASE.length);
  expect(h.mapPages).toHaveLength(1);

  await watchSpinner(page);
  await toPane(page, project, 2);
  await toMap(page, project);

  // The map is there on arrival, and the operator never waited behind a spinner...
  await expect(chip(page, 1)).toBeVisible();
  await expect(chips(page)).toHaveCount(BASE.length);
  expect(await spinnerSeen(page)).toBe(0);
  // ...while the revalidation this design always performs did run. Deliberately NOT asserting
  // that a request was skipped: that was the freshness window, and it is gone.
  await expect.poll(() => h.mapPages.length).toBe(2);
});

test("a change made while away is reflected on return, without a spinner (#1007)", async ({
  page,
}, testInfo) => {
  // Subsumes the mission-console and session-creation cases from review: neither surface announces
  // anything, and under always-revalidate neither has to. Any change is picked up by returning.
  const project = testInfo.project.name;
  await open(page, project);
  const h = await mockApp(page);

  await page.goto("/overview");
  await expect(chips(page)).toHaveCount(BASE.length);

  await toPane(page, project, 2);
  // Something happened elsewhere that no client-side mutation channel can see — a session created
  // over the websocket, a mission archived in Pulse.
  h.visible = [
    ...BASE,
    { ...BASE[0], id: "claude:s99", uuid: "s99", short_uuid: "s99", title: "Created elsewhere" },
  ];

  await watchSpinner(page);
  await toMap(page, project);

  await expect(
    page.locator(".tr-overview .tr-ov-chip", { hasText: "Created elsewhere" }),
  ).toBeVisible();
  await expect(chips(page)).toHaveCount(BASE.length + 1);
  expect(await spinnerSeen(page)).toBe(0);
});

test("a failed refresh preserves the prior good result (#1007)", async ({
  page,
}, testInfo) => {
  const project = testInfo.project.name;
  await open(page, project);
  const h = await mockApp(page);

  await page.goto("/overview");
  await expect(chips(page)).toHaveCount(BASE.length);

  await toPane(page, project, 2);
  h.failNext = true;

  await watchSpinner(page);
  await toMap(page, project);

  // The refresh ran and failed...
  await expect.poll(() => h.mapPages.length).toBe(2);
  // ...and the map is exactly as it was: no error screen, no spinner, no emptied canvas.
  await expect(chips(page)).toHaveCount(BASE.length);
  await expect(page.getByText("Couldn’t load sessions.")).toHaveCount(0);
  expect(await spinnerSeen(page)).toBe(0);
});
