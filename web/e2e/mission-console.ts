/** Shared mocks for the console's own reads (#878).
 *
 * `/mission` is now MISSION CONTROL, so every spec that visits it touches `/api/missions` and, once
 * a mission is selected, the three per-mission reads. The specs that predate the console mostly
 * do not care about missions at all — they assert the bell, `ActionRow`, the filter chips or the
 * page's overflow — so they get an EMPTY mission list here and keep their own assertions
 * untouched. That is the point: a Tier-B spec whose assertion changes during this migration is a
 * red flag, not a migration.
 */
import { expect, type Page } from "@playwright/test";

export const EMPTY_MISSIONS = {
  missions: [],
  total: 0,
  limit: 50,
  offset: 0,
  facets: { projects: [], states: [] },
  store_error: null,
};

/** Resolve the LIST response from the request's own query, so a spec can mock PAGES and SCOPES
 *  rather than one fixed answer. The console's pagination and its archived scope are both
 *  expressed purely in the query string, so a mock that ignores it cannot tell a second page
 *  from a first — which is exactly how a regression in either survived. */
export type MissionListResolver = (
  q: URLSearchParams,
) => unknown | Promise<unknown>;

export interface MissionMockOptions {
  /** The rail's list — a fixed body, or a resolver over the query. Defaults to empty. */
  missions?: unknown | MissionListResolver;
  /** `GET /api/missions/{id}` — the mission plus a page of events. */
  mission?: unknown;
  objectives?: unknown;
  context?: unknown;
  /** `GET /api/missions/{id}/now` — the live strip (#1064). */
  now?: unknown;
}

/** Route every mission endpoint the console reads. Call BEFORE `page.goto`. */
export async function mockMissions(
  page: Page,
  opts: MissionMockOptions = {},
): Promise<void> {
  // ORDER MATTERS, and it is the opposite of what reads naturally: Playwright matches the MOST
  // RECENTLY REGISTERED route first. So the catch-all goes on FIRST and the specific patterns
  // LAST — registered the other way round, `**/api/missions**` swallows `/api/missions/{id}` and
  // every per-mission read silently returns the LIST shape, which is not an error anywhere, just
  // a console with no events, no objectives and no tabs.
  await page.route("**/api/missions**", async (r) => {
    const m = opts.missions;
    if (typeof m === "function") {
      const q = new URL(r.request().url()).searchParams;
      // AWAITED, so a resolver can hold a page open — which is how the cross-scope race is
      // driven deterministically rather than by hoping a request is still in flight.
      return r.fulfill({ json: await (m as MissionListResolver)(q) });
    }
    return r.fulfill({ json: m ?? EMPTY_MISSIONS });
  });
  await page.route("**/api/missions/*", (r) =>
    r.fulfill({
      json: opts.mission ?? { ...MISSION, events: [], events_next_seq: null },
    }),
  );
  await page.route("**/api/missions/*/context", (r) =>
    r.fulfill({
      json: opts.context ?? {
        id: "msn_1",
        project_id: "",
        cwd: "",
        sessions: [],
        git: null,
        git_error: null,
      },
    }),
  );
  await page.route("**/api/missions/*/objectives", (r) =>
    r.fulfill({ json: opts.objectives ?? { objectives: [] } }),
  );
  // The live strip (#1064). Registered after the catch-all, so it answers ahead of it.
  await page.route("**/api/missions/*/now", (r) =>
    r.fulfill({ json: opts.now ?? { sessions: [], checked_at: 1_700_000_000 } }),
  );
}

/** A LIST row, exactly as `GET /api/missions` produces one — `session_keys`, and NO `sessions`.
 *
 *  Producer-faithful on purpose. The previous fixture gave every list mission a detail-only
 *  `sessions` array, which is what hid a P0: the console iterated it, and a real list row has
 *  never had one, so `/mission` threw on any non-empty production list while every browser test
 *  stayed green. A fixture that is kinder than the producer tests nothing. */
export function missionRow(over: Record<string, unknown> = {}) {
  return {
    id: "msn_1",
    title: "Kimi transcript adapter",
    project_id: "agent-sessions",
    cwd: "/repo",
    state: "running",
    created_at: 1_700_000_000,
    updated_at: 1_700_000_000,
    closed_at: null,
    archived_at: null,
    outcome: null,
    session_keys: [],
    ...over,
  };
}

/** The DETAIL shape, from `GET /api/missions/{id}` — this one does carry the roster. */
export const MISSION = {
  id: "msn_1",
  title: "Kimi transcript adapter",
  instruction: null,
  brief: null,
  project_id: "agent-sessions",
  cwd: "/repo",
  engine: null,
  engine_source: null,
  state: "running",
  playbook_id: null,
  created_at: 1_700_000_000,
  updated_at: 1_700_000_000,
  closed_at: null,
  archived_at: null,
  archiving_at: null,
  unarchiving_at: null,
  outcome: null,
  sessions: [],
};

export function missionList(
  missions: unknown[],
  storeError: string | null = null,
  /** The digest of the ORDERED ids the page was sliced out of (#896 review 19). The server sends
   *  one on every page and a stitching client requires them to agree, so a fixture that omits it
   *  is a fixture the console must — correctly — treat as unprovable. Default `"snap"`: one
   *  quiet snapshot, which is what almost every test means. A test about tearing passes a
   *  different value for the pages that came from a different list. */
  snapshot: string | null = "snap",
) {
  return {
    missions,
    total: missions.length,
    limit: 50,
    offset: 0,
    facets: { projects: [], states: [] },
    store_error: storeError,
    snapshot,
  };
}

/** Open the mission rail through the SHELL's control (#940).
 *
 *  The console's own `☰` retired with its drawer: at every width the rail now lives in the app
 *  shell's sidebar, so the one control that reveals it is the shell's. Tests that used to click
 *  `rail-drawer-open` go through here instead — which is also the point of the change, since the
 *  operator was meeting two hamburgers on one screen.
 *
 *  A no-op where the sidebar is already a docked column: there is nothing to open, and asserting
 *  on the rail directly is what those cases want.
 */
export async function openMissionRail(
  page: import("@playwright/test").Page,
): Promise<void> {
  const trigger = page.getByRole("button", { name: /Open mission list/i });
  // NO TRIGGER MEANS NO DRAWER, which is the docked-column case and a genuine no-op. Anything
  // else below is a failure to open and is raised (#940 review 3).
  if (!(await trigger.count())) return;
  await trigger.first().click();

  // WAIT FOR THE PANEL, not for a timeout. The drawer slides in on a CSS transform, so a caller
  // that measures its box immediately reads the CLOSED position and concludes nothing happened.
  // `role="dialog"` appears with the modal state, which is the earliest honest signal that the
  // shell has committed to opening.
  //
  // THESE USED TO BE `.catch(() => {})`, and that was wrong in the specific way a helper can be
  // wrong: a drawer that never opened returned as if it had, and the caller went on to count rows
  // in a parked panel and assert something true about nothing. A `waitFor` that times out is the
  // clearest signal available that the shell did not open — swallowing it converts a loud failure
  // into a quiet one exactly where the quiet one is hardest to read.
  await page.getByRole("dialog").waitFor({ state: "visible", timeout: 5000 });

  // …and settle the transform, so a geometry assertion reads the resting position. `x >= 0` is
  // the resting position of an OPEN panel; the closed one sits at roughly `-width`.
  await page.waitForFunction(
    () => {
      const el = document.querySelector("aside.sidebar");
      return el !== null && el.getBoundingClientRect().x >= 0;
    },
    undefined,
    { timeout: 5000 },
  );
}

/** The shell's rail control, for assertions about its state rather than its effect. */
export function missionRailTrigger(page: import("@playwright/test").Page) {
  return page.getByRole("button", { name: /(Open|Collapse) mission list/i });
}

/** Open one details section at either responsive layout (#948 P3).
 *
 *  Below 1400px the details sit behind ONE disclosure (`details-toggle`) above the thread; at
 *  1400px and above that button is `display:none` and the details column is always beside the
 *  thread. So: expand the band when the toggle is on screen and closed, then open the section. */
export async function openMissionDetails(page: Page, section = "objectives") {
  const toggle = page.getByTestId(`detail-${section}`);
  await toggle.waitFor({ state: "attached" });
  const band = page.getByTestId("details-toggle");
  await band.waitFor({ state: "attached" });
  if (
    (await band.isVisible()) &&
    (await band.getAttribute("aria-expanded")) !== "true"
  ) {
    await band.click();
    await expect(band).toHaveAttribute("aria-expanded", "true");
  }
  if ((await toggle.getAttribute("aria-expanded")) !== "true")
    await toggle.click();
}

/** Give the thread its room back: collapse the details band if it is open. A no-op at 1400px and
 *  above, where the band is hidden and the thread is always beside the details. */
export async function openMissionConversation(page: Page) {
  const band = page.getByTestId("details-toggle");
  if (
    (await band.isVisible()) &&
    (await band.getAttribute("aria-expanded")) === "true"
  ) {
    await band.click();
    await expect(band).toHaveAttribute("aria-expanded", "false");
  }
}

/** Open objective row `index`'s ⋯ menu and return the menu (#967 P3).
 *
 *  Rename, Mark not required, Stand down, Move up, Move down and Remove moved off the row into
 *  this menu and kept their testids, so a spec that pressed one of them now opens the menu first —
 *  which is also what the operator does. `scope` narrows the rows (a pane) where a page has more
 *  than one list. The menu is portalled to <body>, so it is looked up on the page. */
export async function openObjectiveMenu(
  page: Page,
  index = 0,
  scope: Page | import("@playwright/test").Locator = page,
) {
  const trigger = scope.getByTestId("objective-menu").nth(index);
  // ON A SETTLED PAGE. The desktop popover closes on any scroll, as the session menus do, because a
  // scroll can slide the trigger out from under it. Opening a section scrolls the details column,
  // and a scroll still running when ⋯ is pressed shuts the menu before the next click lands.
  await trigger.scrollIntoViewIfNeeded();
  await scrollIdle(page);
  await trigger.click();
  const menu = page.getByRole("menu");
  await menu.waitFor({ state: "visible" });
  return menu;
}

/** Resolve once no element has scrolled for `quietMs`. */
async function scrollIdle(page: Page, quietMs = 300) {
  await page.evaluate(
    (quiet) =>
      new Promise<void>((resolve) => {
        const done = () => {
          window.removeEventListener("scroll", again, true);
          resolve();
        };
        let timer = setTimeout(done, quiet);
        const again = () => {
          clearTimeout(timer);
          timer = setTimeout(done, quiet);
        };
        window.addEventListener("scroll", again, true);
      }),
    quietMs,
  );
}

/** Press one objective action through its row's ⋯ menu. */
export async function objectiveAction(
  page: Page,
  testId: string,
  index = 0,
  scope: Page | import("@playwright/test").Locator = page,
) {
  const menu = await openObjectiveMenu(page, index, scope);
  await menu.getByTestId(testId).click();
}

/** Flip the rail between its Active and Archived scopes (#948 P2).
 *
 *  The scope used to be one toggle button (`rail-scope`, "Show archived" / "Show active"); it is
 *  now the sessions sidebar's Active | Archived tab pair. Specs that flipped the scope keep their
 *  meaning through this: click whichever tab is not selected, on the copy that is on screen. */
export async function flipMissionScope(page: Page): Promise<void> {
  const archived = page.locator('[data-testid="rail-scope-archived"]:visible').first();
  const active = page.locator('[data-testid="rail-scope-active"]:visible').first();
  const target = (await archived.getAttribute("aria-selected")) === "true" ? active : archived;
  await target.click();
  // WAIT FOR THE FLIP TO LAND. The decision above reads the tab state NOW, so two flips in a row —
  // or a flip right after something else switched the scope — would otherwise read a state that
  // has not rendered yet and click the tab that is already on its way to being selected.
  await expect(target).toHaveAttribute("aria-selected", "true");
}
