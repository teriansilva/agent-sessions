/** The mission sidebar IS the sessions sidebar, control for control (#948 P2).
 *
 * The operator's report: the Missions sidebar used different fonts, font sizes, weights and form
 * sizes from the Sessions sidebar, and its text read too small. The fix shares the sessions classes
 * rather than copying their numbers, and this spec is what keeps that true: it reads the COMPUTED
 * style of each control pair on `/` and on `/mission` and requires them to be identical — so a
 * future rule that restyles only one side goes red here, whichever side it lands on.
 *
 * Measured on both projects. On the phone project both sidebars are drawers, and the §8 touch floor
 * (44px) must hold on both: aligning must never shrink a touch target.
 */
import { expect, test, type Locator, type Page } from "@playwright/test";
import { missionList, missionRow, mockMissions, openMissionRail } from "./mission-console";

const SESSION = {
  id: "claude:11111111-1111-4111-8111-111111111111",
  engine: "claude",
  uuid: "11111111-1111-4111-8111-111111111111",
  short_uuid: "11111111",
  cwd: "/repo",
  project: { kind: "folder", id: "/repo", name: "/repo" },
  last_mtime: 1_700_000_000,
  first_user_message: "a session row",
  title: "A session row",
  sticky: false,
  archived: false,
};

async function setup(page: Page) {
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        auth_mode: "none",
        terminal_backend: "ws",
        pulse: { configured: true },
        new_session_engines: ["claude"],
        onboarded: true,
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions: [SESSION],
        total: 1,
        next_offset: null,
        facets: { projects: [{ kind: "project", id: "p1", name: "Alpha", count: 1 }], engines: ["claude"] },
      },
    }),
  );
  await page.route("**/api/projects**", (r) => r.fulfill({ json: { projects: [] } }));
  await page.route(/\/api\/pulse$/, (r) => r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }));
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await mockMissions(page, {
    missions: { ...missionList([missionRow({ id: "msn_1", title: "A mission row" })]), facets: { projects: [], states: ["running"] } },
  });
}

const TYPE = ["fontFamily", "fontSize", "fontWeight", "letterSpacing", "textTransform"] as const;
const BOX = ["height", "paddingTop", "paddingLeft"] as const;

async function styleOf(loc: Locator, keys: readonly string[]) {
  await expect(loc).toBeVisible();
  return loc.evaluate((el, ks) => {
    const s = getComputedStyle(el);
    return Object.fromEntries(ks.map((k) => [k, s.getPropertyValue(k.replace(/[A-Z]/g, (c) => "-" + c.toLowerCase()))]));
  }, keys);
}

/** Every control pair, located on its own route. `row` pairs read the shared title/meta nodes. */
function controls(page: Page, route: "sessions" | "missions") {
  const s = route === "sessions";
  const rows = s ? page.locator("ul[aria-label$='sessions'] li").first() : page.getByTestId("rail-mission").first();
  return {
    "new button": s ? page.getByRole("link", { name: "New session" }) : page.getByTestId("rail-new-mission"),
    search: page.getByLabel(s ? "Search sessions" : "Search missions"),
    "project select": page.getByLabel(s ? "Filter by project" : "Filter missions by project"),
    "active tab": page
      .getByRole("tablist", { name: s ? "Archived filter" : "Mission scope" })
      .getByRole("tab", { name: "Active" }),
    "row title": rows.locator("[class*='_title_']").first(),
    "row meta": rows.locator("[class*='_meta_']").first(),
    "head row": page.locator(".sidebar-head"),
  };
}

async function measure(page: Page, route: "sessions" | "missions", mobile: boolean) {
  await page.goto(route === "sessions" ? "/" : "/mission");
  if (mobile) await openMissionRail(page);
  const out: Record<string, unknown> = {};
  for (const [name, loc] of Object.entries(controls(page, route))) {
    const keys = name.startsWith("row ") ? TYPE : name === "head row" ? ["height"] : [...TYPE, ...BOX];
    out[name] = await styleOf(loc.first(), keys);
  }
  return out;
}

test("every sidebar control on /mission computes exactly as its /sessions counterpart", async ({ page }, testInfo) => {
  const mobile = testInfo.project.name === "mobile";
  if (!mobile) await page.setViewportSize({ width: 1440, height: 900 });
  await setup(page);
  const sessions = await measure(page, "sessions", mobile);
  const missions = await measure(page, "missions", mobile);
  for (const name of Object.keys(sessions)) {
    expect(missions[name], `${name} matches the sessions sidebar`).toEqual(sessions[name]);
  }
});

test("both sidebars keep the 44px touch floor on a phone", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "touch floor is a phone property");
  await setup(page);
  for (const route of ["sessions", "missions"] as const) {
    await page.goto(route === "sessions" ? "/" : "/mission");
    await openMissionRail(page);
    const c = controls(page, route);
    for (const name of ["new button", "search", "project select", "active tab"] as const) {
      const box = (await c[name].first().boundingBox())!;
      expect(box.height, `${route} ${name}`).toBeGreaterThanOrEqual(44);
    }
  }
});
