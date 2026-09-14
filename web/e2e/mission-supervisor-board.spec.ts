import { openMissionDetails } from "./mission-console";
/** The supervisor's follow-through, in a real browser (#885) — on the objective rows since #942.
 *
 * jsdom already pins the classification and the wording (`MissionSupervisorBoard.test.tsx`). What
 * it cannot pin, and what is asserted here:
 *
 *  - the reading actually REACHES the console. It is wired through `MissionConsole` →
 *    `ObjectivesPane` → `MissionObjectives` from `d.mission?.supervisor`, and a detail payload
 *    that carries `supervisor` while the pane reads it from somewhere else renders a bare list
 *    with no error anywhere — green units, blank UI. This spec drives the real fetch path, so
 *    that gap is visible. It also pins the JOIN the fold introduced: the assessment is matched to
 *    the objective by `key`, so a row whose reading went missing is a row with no badge.
 *  - the four boards are DISTINGUISHABLE on screen, not merely present in the DOM: each badge is
 *    laid out, visible, and carries its own colour. A colour assertion is only meaningful in a
 *    real engine, since jsdom resolves no custom properties.
 *  - the refusal sentence is READABLE at 412 px rather than clipped — the reason the operator is
 *    given is worthless if the phone truncates it, and this is prose of unbounded length sitting
 *    in a grid cell.
 *  - the row meets the 44 px coarse-pointer floor the design rules set.
 */
import { expect, test, type Page } from "@playwright/test";

import {
  MISSION,
  missionList,
  missionRow,
  mockMissions,
} from "./mission-console";

const T = 1_700_000_000;

const CONFIG = {
  csrf: "x",
  new_session_engines: ["claude"],
  terminal_backend: "ws",
  auth_mode: "none",
  overview_expanded: [],
  projects_hidden: [],
  pulse: { configured: true },
};

async function stub(page: Page) {
  await page.route("**/api/config", (r) => r.fulfill({ json: CONFIG }));
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions: [],
        next_offset: null,
        total: 0,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({
      json: { notifications: [], unread: 0, uncertain: 0, settled: [] },
    }),
  );
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: {
        cache_version: 1,
        generated_at: T - 60,
        window_days: 3,
        scan_depth: "medium",
        input_fingerprint: "fp",
        synthesis_skipped: false,
        banner: null,
        cards: [],
      },
    }),
  );
  await page.route("**/api/pulse/orchestrator", (r) =>
    r.fulfill({ status: 500, json: { detail: "off" } }),
  );
}

const HELD_SENTENCE =
  "the operator asked not to be told about this objective again";
const SPENT_SENTENCE = "the 3-nudge budget for this episode is spent";
const WAITING_SENTENCE =
  "a previous nudge may or may not have been delivered; not sending another";

/** Four objectives, one per board — the whole point is that they are told apart. */
const SUPERVISOR = {
  objectives: [
    {
      key: "held",
      title: "Stood down by the operator",
      gate: false,
      state: "open",
      met: false,
      episode: 2,
      stood_down: true,
      spent: 1,
      remaining: 2,
      may_nudge: false,
      why_not: HELD_SENTENCE,
    },
    {
      key: "spent",
      title: "Budget exhausted",
      gate: true,
      state: "open",
      met: false,
      episode: 1,
      stood_down: false,
      spent: 3,
      remaining: 0,
      may_nudge: false,
      why_not: SPENT_SENTENCE,
    },
    {
      key: "waiting",
      title: "A nudge whose fate is unknown",
      gate: false,
      state: "open",
      met: false,
      episode: 1,
      stood_down: false,
      spent: 1,
      remaining: 2,
      may_nudge: false,
      why_not: WAITING_SENTENCE,
    },
    {
      key: "ready",
      title: "Free to nudge",
      gate: false,
      state: "open",
      met: false,
      episode: 1,
      stood_down: false,
      spent: 0,
      remaining: 3,
      may_nudge: true,
      why_not: "",
    },
  ],
  likely_done: false,
  unmet_gates: 1,
  checked_at: T,
};

const ROW = missionRow({ session_keys: [] });

/** THE OBJECTIVE ROWS THE ASSESSMENT WAS MADE FROM.
 *
 *  `mission_supervisor.assess` iterates `_objective_rows(mission_id)`, so a supervisor entry
 *  always has a matching objective — and since #942 the objective row is what renders the badge.
 *  A fixture with four assessments over an empty objective list is a state the producer cannot
 *  emit, and folding against it would show nothing while every unit test stayed green. */
const OBJECTIVES = {
  objectives: SUPERVISOR.objectives.map((o, i) => ({
    mission_id: "msn_1",
    key: o.key,
    ord: i,
    title: o.title,
    probe: "manual",
    probe_args: null,
    gate: o.gate,
    state: o.state,
    met_at: null,
    observed: null,
    source: "test",
  })),
};

async function openConsole(page: Page, supervisor: unknown | undefined) {
  await stub(page);
  const mission: Record<string, unknown> = {
    ...MISSION,
    events: [],
    events_next_seq: null,
  };
  if (supervisor !== undefined) mission.supervisor = supervisor;
  await mockMissions(page, {
    missions: missionList([ROW]),
    mission,
    // The rows the assessment describes — see OBJECTIVES. Supplied even in the ABSENT-assessment
    // case, so that test isolates "no supervisor" rather than also having no list.
    objectives: OBJECTIVES,
  });
  await page.goto("/mission");
}

/** Reach the OBJECTIVES pane on either project.
 *
 *  The two layouts differ in one step and it is not cosmetic: at 412 px the drawer auto-selects the
 *  single mission, while at 1280 px (the MIDDLE layout mode — rail is a column, detail is still a
 *  tab strip) nothing is selected until the rail row is clicked. A helper that only did the tab
 *  click passed on mobile and asserted against a hidden pane on desktop. */
async function showObjectives(page: Page) {
  const railRow = page
    .getByRole("navigation", { name: /missions/i })
    .getByRole("button", { name: /Kimi transcript adapter/i });
  if ((await railRow.count()) > 0 && (await railRow.first().isVisible())) {
    await railRow.first().click();
  }
  await openMissionDetails(page, "objectives");
  await openMissionDetails(page, "followThrough");
}

/** Scoped to the DISPLAYED copy. The console used to mount the detail pane twice — a drawer and
 *  an inline 340px column, both in the DOM at every width with one hidden — which made an
 *  unscoped `getByTestId` a strict-mode violation and `.first()` a coin flip. #942 deleted the
 *  column, so there is one copy now; the scoping stays because it costs nothing and it is what
 *  fails loudly if a second copy is ever reintroduced. */
const shown = (page: Page, id: string) =>
  page.locator(`[data-testid="${id}"]:visible`);

test("all four boards reach the console and are told apart on screen", async ({
  page,
}) => {
  await openConsole(page, SUPERVISOR);
  await showObjectives(page);

  const board = shown(page, "objectives");
  await expect(board).toBeVisible();

  const rows = board.getByRole("listitem");
  await expect(rows).toHaveCount(4);
  await expect(rows.nth(0)).toHaveAttribute("data-board", "held");
  await expect(rows.nth(1)).toHaveAttribute("data-board", "spent");
  await expect(rows.nth(2)).toHaveAttribute("data-board", "waiting");
  await expect(rows.nth(3)).toHaveAttribute("data-board", "ready");

  // Every badge is laid out and visible — not merely in the DOM. Taken from the supervisor's
  // cell rather than from the row: the row's own first span is the objective's state dot.
  for (let i = 0; i < 4; i++) {
    const badge = rows
      .nth(i)
      .getByTestId("supervisor-cell")
      .locator("span")
      .first();
    await expect(badge).toBeVisible();
    const box = await badge.boundingBox();
    expect(box?.width ?? 0).toBeGreaterThan(0);
  }

  // Colour is load-bearing per the design rules, and a custom property only resolves in a real
  // engine: READY and SPENT must not paint the same, or the boards are decorative.
  const colourOf = (i: number) =>
    rows
      .nth(i)
      .getByTestId("supervisor-cell")
      .locator("span")
      .first()
      .evaluate((el) => getComputedStyle(el).color);
  expect(await colourOf(3)).not.toBe(await colourOf(1));
  expect(await colourOf(1)).not.toBe(await colourOf(2));
});

test("the server's refusal sentence is readable, not clipped", async ({
  page,
}) => {
  await openConsole(page, SUPERVISOR);
  await showObjectives(page);

  const board = shown(page, "objectives");
  for (const sentence of [HELD_SENTENCE, SPENT_SENTENCE, WAITING_SENTENCE]) {
    const el = board.getByText(sentence);
    await expect(el).toBeVisible();
    // Not truncated: the rendered box is tall enough to hold every line it wrapped to.
    const clipped = await el.evaluate(
      (n) =>
        n.scrollHeight > n.clientHeight + 1 ||
        n.scrollWidth > n.clientWidth + 1,
    );
    expect(clipped, `"${sentence.slice(0, 30)}…" is clipped`).toBe(false);
  }

  // The page itself must not scroll sideways to fit that prose.
  const overflow = await page.evaluate(
    () =>
      document.documentElement.scrollWidth >
      document.documentElement.clientWidth + 1,
  );
  expect(overflow).toBe(false);
});

test("a row meets the 44px coarse-pointer floor", async ({ page }) => {
  await openConsole(page, SUPERVISOR);
  await showObjectives(page);
  const rows = shown(page, "objectives").getByRole("listitem");
  for (let i = 0; i < 4; i++) {
    const box = await rows.nth(i).boundingBox();
    expect(box?.height ?? 0).toBeGreaterThanOrEqual(44);
  }
});

test("an ABSENT assessment says so in the console, and never shows a clean board", async ({
  page,
}) => {
  // The detail payload carries NO `supervisor` key — exactly what the route emits when `assess()`
  // could not run. The operator must be able to tell that from "nothing to follow up".
  await openConsole(page, undefined);
  await showObjectives(page);

  await expect(shown(page, "supervisor-unreadable")).toBeVisible();
  // Not merely "the displayed one is absent" — no copy of a clean reading exists anywhere. The
  // objective LIST is still there (the mission has objectives); what must not appear is any
  // supervisor cell, which is what a clean board would now look like.
  await expect(page.getByTestId("supervisor-cell")).toHaveCount(0);
  await expect(page.getByTestId("supervisor-unmeasured")).toHaveCount(0);
});
