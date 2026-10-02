import { expect, test, type Page } from "@playwright/test";

import { EMPTY_MISSIONS, missionList, missionRow } from "./mission-console";

/** #1233 — switching sections keeps the session list.
 *
 *  The operator: "when switching between mission and session the lists always reload … it makes
 *  usage of the system very slow." The shell swapped `SessionList` OUT for the mission rail's slot
 *  on Missions, so the hook unmounted and every return paid its bootstrap fetch and lost the pages
 *  already loaded. It is now hidden in place — the Settings contract (#1129) — and Dashboard, Ask
 *  and Templates render with no session sidebar at all, the same way Settings does ("on dashboard,
 *  the sessions list should not appear. same as on templates.").
 *
 *  Each round trip below loads a SECOND page first, so the cursor is state only a surviving hook
 *  holds, then freezes the clock so no 15 s poll can pass for a bootstrap. A remounted list asks for
 *  `{limit: 20, offset: 0}` again and shows 20 rows; a surviving one asks for nothing and shows 40.
 *
 *  The list's two legitimate refetch triggers stay quiet here, so "no request" means "no remount":
 *  nothing in the round trip mutates a session's mission (no `SESSION_MISSION_CHANGED_EVENT`), and
 *  the page never changes visibility. Either firing would make this spec fail, never mask a remount.
 */

const now = Math.floor(Date.now() / 1000);
const TOTAL = 45;
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

interface Req {
  limit: number;
  offset: number;
}

async function mockApp(page: Page): Promise<Req[]> {
  const reqs: Req[] = [];
  // Reverse registration order: the catch-all first, so each page this spec visits but does not
  // assert on answers `{}` and the SHELL is what is under test.
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/pulse/notifications**", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0 } }),
  );
  await page.route("**/api/projects**", (r) =>
    r.fulfill({
      json: { projects: [{ id: "p1", name: "proj", color: "#ffb000", archived: false }] },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/missions**", (r) => r.fulfill({ json: EMPTY_MISSIONS }));
  await page.route(/\/api\/templates(\?.*)?$/, (r) =>
    r.fulfill({ json: { templates: [], limits: { name_max: 120 } } }),
  );
  await page.route(/\/api\/sessions\/[^?]+$/, (r) =>
    r.fulfill({ status: 404, json: { detail: "not found" } }),
  );
  await page.route("**/api/sessions?**", (r) => {
    const u = new URL(r.request().url());
    const limit = Number(u.searchParams.get("limit") ?? "20");
    const offset = Number(u.searchParams.get("offset") ?? "0");
    reqs.push({ limit, offset });
    return r.fulfill({
      json: {
        sessions: ALL.slice(offset, offset + limit),
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

/** The list's own scroll owner — `<ul aria-label="45 sessions">`. */
const listBox = (page: Page) =>
  page.locator('aside.sidebar ul[aria-label$=" sessions"]');

const sectionLink = (page: Page, section: string) =>
  page.locator(`[data-testid="section-nav"] a[data-section="${section}"]`);

/** Load a second page into the list, opening the phone drawer first where there is one. */
async function loadTwoPages(page: Page, mobile: boolean) {
  await page.goto("/");
  if (mobile) await page.locator("header .navToggle").click();
  await expect(sidebarRows(page)).toHaveCount(20);
  await page.getByRole("button", { name: "Load more" }).click();
  await expect(sidebarRows(page)).toHaveCount(40);
  if (mobile) await page.keyboard.press("Escape");
}

/** Back to Sessions, and the SAME list: 40 rows and not one listing request since `settled`. */
async function expectSurvived(page: Page, reqs: Req[], settled: number) {
  await sectionLink(page, "sessions").click();
  await expect(page).toHaveURL(/\/$/);
  // Shown again — the URL moves before the render that un-hides the list commits, and a count
  // alone would also pass on a list still `display: none`.
  await expect(sidebarRows(page).first()).toBeVisible();
  await expect(sidebarRows(page)).toHaveCount(40);
  expect(reqs.slice(settled), "listing requests after the round trip").toEqual([]);
}

test.describe("section switches keep the session list (#1233)", () => {
  test("Missions hides the list in place and the return shows the same pages", async ({
    page,
  }, testInfo) => {
    const mobile = testInfo.project.name === "mobile";
    const reqs = await mockApp(page);
    await page.clock.install();
    await loadTwoPages(page, mobile);
    // Where the operator had scrolled to is part of "the same list" (#1233 goal 1).
    // Resolved on the scroll EVENT, the way an operator's scroll arrives — not on the assignment,
    // which the browser reports a frame later.
    const scrolled = await listBox(page).evaluate(
      (el) =>
        new Promise<number>((done) => {
          el.addEventListener("scroll", () => done(el.scrollTop), { once: true });
          el.scrollTop = 300;
        }),
    );
    expect(scrolled).toBeGreaterThan(200);
    const settled = reqs.length;

    await sectionLink(page, "mission").click();
    await expect(page).toHaveURL(/\/mission/);
    // Hidden while the rail owns the sidebar — never visible under or beside it.
    await expect(sidebarRows(page).first()).toBeHidden();

    await expectSurvived(page, reqs, settled);
    await expect.poll(() => listBox(page).evaluate((el) => el.scrollTop)).toBe(scrolled);
  });

  for (const [name, section, path] of [
    ["Dashboard", "ask", /\/dashboard/],
    ["Templates", "templates", /\/templates/],
  ] as const) {
    test(`${name} renders with no session sidebar, and the list survives the visit`, async ({
      page,
    }, testInfo) => {
      const mobile = testInfo.project.name === "mobile";
      const reqs = await mockApp(page);
      await page.clock.install();
      await loadTwoPages(page, mobile);
      const settled = reqs.length;

      await sectionLink(page, section).click();
      await expect(page).toHaveURL(path);
      // display:none at both widths — the docked column and the phone drawer are this one aside.
      await expect(page.locator("aside.sidebar")).toBeHidden();
      // No control for a surface that does not exist here.
      await expect(page.locator("header .navToggle")).toHaveCount(0);
      await expect(page.locator(".sidebar-resize")).toHaveCount(0);
      if (!mobile) {
        // The pane takes the deck: without the 320px column it starts at its own margin.
        const pane = await page.locator("main.terminal-pane").boundingBox();
        expect(pane!.x, "pane left edge").toBeLessThan(20);
      }

      await expectSurvived(page, reqs, settled);
    });
  }

  test("Ask's page under Dashboard has no session sidebar either", async ({ page }) => {
    await mockApp(page);
    await page.goto("/ask");
    await expect(page.locator("aside.sidebar")).toBeHidden();
    await expect(page.locator("header .navToggle")).toHaveCount(0);
  });

  test("the mission rail paints its last read on return, before the server answers", async ({
    page,
  }) => {
    await mockApp(page);
    // The rail's list, with the SECOND visit's read held open by hand: whatever the rail shows
    // before `release` came from the retained read, not from the network.
    let calls = 0;
    let release: () => void = () => {};
    const held = new Promise<void>((r) => (release = r));
    await page.route("**/api/missions?**", async (r) => {
      calls += 1;
      if (calls > 1) await held;
      await r.fulfill({ json: missionList([missionRow({ title: "Retained mission" })]) });
    });
    const rows = page.getByTestId("rail-mission");

    await page.goto("/");
    await sectionLink(page, "mission").click();
    await expect(rows).toHaveCount(1);
    await sectionLink(page, "sessions").click();
    await expect(page).toHaveURL(/\/$/);

    await sectionLink(page, "mission").click();
    await expect(page).toHaveURL(/\/mission/);
    await expect.poll(() => calls, { message: "the return still revalidates" }).toBe(2);
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toContainText("Retained mission");
    release();
  });
});
