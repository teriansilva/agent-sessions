import { openMissionConversation } from "./mission-console";
import { openMissionDetails } from "./mission-console";
/** #942 — the console reads as one thing.
 *
 *  Every claim here is a GEOMETRY or an AGREEMENT claim, and the defects they guard were all
 *  invisible to the suite that already existed: the composer was `toBeVisible()` and it was —
 *  sitting mid-page above 60% emptiness. Visibility cannot tell "on screen" from "in the right
 *  place". Boxes can.
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
const OVERVIEW = {
  cache_version: 1,
  generated_at: T - 60,
  window_days: 3,
  scan_depth: "fast",
  input_fingerprint: "fp",
  synthesis_skipped: false,
  cards: [],
};

/** The mission's own objective rows, and the assessment made FROM them.
 *
 *  Two reads, one set: `mission_supervisor.assess` iterates the mission's objectives, so an
 *  assessment entry and an objective with the same `key` are the same objective. Since #942 the
 *  objective row is what renders the reading, so a fixture where the two disagree tests a state
 *  the producer cannot emit. */
const OBJ_ROWS = [
  {
    mission_id: "msn_1",
    key: "checks",
    ord: 0,
    title: "CI is green",
    probe: "manual",
    probe_args: null,
    gate: true,
    state: "open",
    met_at: null,
    observed: null,
    source: "test",
  },
  {
    mission_id: "msn_1",
    key: "docs",
    ord: 1,
    title: "The docs say what changed",
    probe: "manual",
    probe_args: null,
    gate: false,
    state: "open",
    met_at: null,
    observed: null,
    source: "test",
  },
];

const SPENT_SENTENCE = "the 3-nudge budget for this episode is spent";

const SUPERVISOR = {
  objectives: [
    {
      key: "checks",
      title: "CI is green",
      gate: true,
      state: "open",
      met: false,
      episode: 2,
      stood_down: false,
      awaiting_answer: false,
      spent: 3,
      remaining: 0,
      may_nudge: false,
      unreadable: false,
      indeterminate: false,
      live: 0,
      terminal: false,
      why_not: SPENT_SENTENCE,
    },
    {
      key: "docs",
      title: "The docs say what changed",
      gate: false,
      state: "open",
      met: false,
      episode: 1,
      stood_down: false,
      awaiting_answer: false,
      spent: 0,
      remaining: 3,
      may_nudge: true,
      unreadable: false,
      indeterminate: false,
      live: 0,
      terminal: false,
      why_not: "",
    },
  ],
  likely_done: false,
  unmet_gates: 1,
  checked_at: T,
};

async function stub(
  page: Page,
  rows: unknown[],
  detail: Record<string, unknown> = {},
  objectives?: unknown,
) {
  await page.route("**/api/config", (r) => r.fulfill({ json: CONFIG }));
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [] } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [] } }),
  );
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
  await page.route(/\/api\/pulse$/, (r) => r.fulfill({ json: OVERVIEW }));
  await page.route("**/api/pulse/orchestrator", (r) =>
    r.fulfill({ status: 500, json: { detail: "off" } }),
  );
  await mockMissions(page, {
    missions: missionList(rows),
    mission: { ...MISSION, events: [], events_next_seq: null, ...detail },
    objectives,
  });
}

const ROWS = [
  missionRow({
    id: "msn_1",
    title: "Kimi transcript adapter",
    state: "running",
  }),
  missionRow({
    id: "msn_2",
    title: "Fix the relay reconnect storm",
    state: "review",
  }),
];

// ==============================================================================================
// The dead band — the operator's actual complaint, as a measurement.
// ==============================================================================================

for (const [w, h] of [
  [1600, 950],
  [1280, 900],
  [412, 915],
] as const) {
  test(`the composer sits on the bottom edge at ${w}x${h} (#942)`, async ({
    page,
  }) => {
    await stub(page, ROWS);
    await page.setViewportSize({ width: w, height: h });
    await page.goto("/mission");
    await expect(page.getByTestId("mission-console")).toBeVisible();
    await page.getByTestId("pane").waitFor();

    const dock = await page.locator('[class*="composerDock"]').boundingBox();
    const shell = await page.getByTestId("mission-console").boundingBox();
    expect(dock).not.toBeNull();
    expect(shell).not.toBeNull();

    // THE CLAIM: the composer's bottom edge is the console's bottom edge. It used to be the last
    // child of the scrolling pane, so it came to rest just under the final event with the rest of
    // the column empty — on a quiet mission that was most of the screen.
    const dockBottom = dock!.y + dock!.height;
    const shellBottom = shell!.y + shell!.height;
    expect(Math.abs(dockBottom - shellBottom)).toBeLessThanOrEqual(2);
  });
}

test("the thread takes the slack: a taller viewport grows the pane, not the gap (#942)", async ({
  page,
}) => {
  await stub(page, ROWS);
  await page.setViewportSize({ width: 1400, height: 700 });
  await page.goto("/mission");
  await page.getByTestId("pane").waitFor();
  const short = await page.getByTestId("pane").boundingBox();

  await page.setViewportSize({ width: 1400, height: 1100 });
  await page.waitForTimeout(300);
  const tall = await page.getByTestId("pane").boundingBox();

  // 400px more viewport should land on the THREAD. A pane that does not grow means the extra
  // height went somewhere the operator cannot use — which is what the dead band was.
  expect(tall!.height - short!.height).toBeGreaterThan(300);
});

// ==============================================================================================
// The header must not contradict the body.
// ==============================================================================================

for (const w of [1280, 1400, 1600] as const) {
  test(`the header names the mission the body is rendering at ${w}px (#942)`, async ({
    page,
  }) => {
    await stub(page, ROWS);
    await page.setViewportSize({ width: w, height: 900 });
    await page.goto("/mission");
    await expect(page.getByTestId("mission-state")).toBeVisible();

    // THE DEFECT: at 1280 this read "Select a mission" while that mission's own `running` state
    // and its controls rendered underneath. The title came from console state the body pushed up
    // after its detail fetch, so it was null for the whole of that round trip.
    const title = await page.getByTestId("console-title").innerText();
    expect(title).not.toMatch(/select a mission|no missions yet/i);
    expect(title.trim()).toBe("Kimi transcript adapter");
  });
}

test("the header names the mission BEFORE its detail lands (#942)", async ({
  page,
}) => {
  // THE HONEST REPRO. The three width cases above wait for the body to finish, and by then the
  // pushed-up `title` has arrived — so they pass against the broken code too. The contradiction
  // lived in the ROUND TRIP, so this test holds the detail request open and asserts the header
  // inside that window, where the old code read "Select a mission" over a selected mission.
  await stub(page, ROWS);
  let release: () => void = () => {};
  const held = new Promise<void>((r) => {
    release = r;
  });
  // Registered AFTER `stub`, so it wins: Playwright matches the most recently added route first.
  await page.route(/\/api\/missions\/msn_1(\?.*)?$/, async (r) => {
    await held;
    await r.fulfill({
      json: { ...MISSION, events: [], events_next_seq: null },
    });
  });

  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/mission");

  // The RAIL has its rows — the list call was never held — and a mission is auto-selected. The
  // detail is still in flight, which is the whole point.
  await expect(page.getByTestId("rail-mission").first()).toBeVisible();
  await expect(page.getByTestId("console-title")).toHaveText(
    "Kimi transcript adapter",
  );

  // …and it still agrees once the detail lands, so the fallback is not merely masking it.
  release();
  await expect(page.getByTestId("mission-state")).toBeVisible();
  await expect(page.getByTestId("console-title")).toHaveText(
    "Kimi transcript adapter",
  );
});

test("with nothing selected the header says so, and no state is claimed (#942)", async ({
  page,
}) => {
  // The control: the fix must not make the header assert a mission when there is none.
  await stub(page, []);
  await page.setViewportSize({ width: 1400, height: 900 });
  await page.goto("/mission");
  await expect(page.getByTestId("mission-console")).toBeVisible();
  await expect(page.getByTestId("console-title")).toHaveText(
    /no missions yet/i,
  );
  await expect(page.getByTestId("mission-state")).toHaveCount(0);
});

// ==============================================================================================
// One primary, and the rest behind ⋯.
// ==============================================================================================

test("one primary action inline; the destructive lifecycle is behind the overflow (#942)", async ({
  page,
}) => {
  await stub(page, ROWS);
  await page.setViewportSize({ width: 1600, height: 950 });
  await page.goto("/mission");
  await expect(page.getByTestId("mission-state")).toBeVisible();

  // Inline: the state's own next step, and nothing else.
  await expect(page.getByTestId("mission-done")).toBeVisible();
  await expect(page.getByTestId("mission-overflow")).toBeVisible();
  for (const hidden of [
    "mission-failed",
    "mission-abandon",
    "mission-archive",
  ]) {
    await expect(page.getByTestId(hidden)).toHaveCount(0);
  }

  // …and they are all one press away, not gone.
  await page.getByTestId("mission-overflow").click();
  const menu = page.getByTestId("mission-overflow-menu");
  await expect(menu).toBeVisible();
  for (const shown of [
    "mission-failed",
    "mission-abandon",
    "mission-archive",
  ]) {
    await expect(menu.getByTestId(shown)).toBeVisible();
  }
});

test("the overflow is a real menu: Escape closes it and focus returns (#942)", async ({
  page,
}) => {
  await stub(page, ROWS);
  await page.setViewportSize({ width: 1600, height: 950 });
  await page.goto("/mission");
  const trigger = page.getByTestId("mission-overflow");
  await trigger.click();
  await expect(page.getByTestId("mission-overflow-menu")).toBeVisible();

  await page.keyboard.press("Escape");
  await expect(page.getByTestId("mission-overflow-menu")).toHaveCount(0);
  await expect(trigger).toBeFocused();
});

test("the overflow does NOT claim to be modal — the console stays reachable (#942)", async ({
  page,
}) => {
  // A small anchored menu over a page that is still usable is not a surface that owns the screen.
  // Declaring `aria-modal` on it would tell a screen reader the console had gone away.
  await stub(page, ROWS);
  await page.setViewportSize({ width: 1600, height: 950 });
  await page.goto("/mission");
  await page.getByTestId("mission-overflow").click();
  await expect(page.getByTestId("mission-overflow-menu")).toBeVisible();
  await expect(
    page.locator('[data-testid="mission-overflow-menu"][aria-modal]'),
  ).toHaveCount(0);
  await expect(page.locator("[inert]")).toHaveCount(0);
});

test("the overflow honours the ARROW KEYS its role promises (#942)", async ({
  page,
}) => {
  // `role="menu"` is a contract about the keyboard, not a label: Up/Down move between items,
  // Home/End jump to the ends, and the menu is one tab stop. A menu that leaves Tab to walk its
  // items reads correct in the accessibility tree and behaves wrong under a screen reader — which
  // is exactly the "a modal hook is not a menu primitive" note on #942.
  await stub(page, ROWS);
  await page.setViewportSize({ width: 1600, height: 950 });
  await page.goto("/mission");
  await page.getByTestId("mission-overflow").click();
  const menu = page.getByTestId("mission-overflow-menu");
  await expect(menu).toBeVisible();

  // A `running` mission's secondary set, in DOM order.
  const order = ["mission-failed", "mission-abandon", "mission-archive"];
  for (const id of order) await expect(menu.getByTestId(id)).toBeVisible();

  // Down from the freshly focused wrapper lands on the FIRST item, not on nothing.
  await page.keyboard.press("ArrowDown");
  await expect(menu.getByTestId(order[0])).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(menu.getByTestId(order[1])).toBeFocused();

  // End / Home are the ends, and Down wraps.
  await page.keyboard.press("End");
  await expect(menu.getByTestId(order[order.length - 1])).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(menu.getByTestId(order[0])).toBeFocused();
  await page.keyboard.press("ArrowUp");
  await expect(menu.getByTestId(order[order.length - 1])).toBeFocused();
  await page.keyboard.press("Home");
  await expect(menu.getByTestId(order[0])).toBeFocused();

  // …and the items are reachable by name, so the roving focus has not stolen their identity.
  await expect(menu.getByTestId(order[0])).toHaveAttribute("role", "menuitem");
});

// ==============================================================================================
// Tabs, not a panel stack — the 340px third track is gone.
// ==============================================================================================

/** Select the first mission. At 412 the rail auto-selects; at desktop widths it does not until
 *  the row is clicked, and a helper that only did one passed on one project and asserted against
 *  an unselected console on the other. */
async function selectFirst(page: Page) {
  const row = page.getByTestId("rail-mission").first();
  if ((await row.count()) && (await row.isVisible())) await row.click();
  await expect(page.getByTestId("mission-state")).toBeVisible();
}

for (const [w, h] of [
  [1600, 950],
  [412, 915],
] as const) {
  test(`all four disclosures are reachable at ${w}x${h}, and each opens its own pane (#942)`, async ({
    page,
  }) => {
    await stub(
      page,
      ROWS,
      { supervisor: SUPERVISOR },
      { objectives: OBJ_ROWS },
    );
    await page.setViewportSize({ width: w, height: h });
    await page.goto("/mission");
    await selectFirst(page);

    await openMissionDetails(page, "objectives");
    await openMissionDetails(page, "followThrough");
    await expect(page.getByTestId("objectives")).toBeVisible();
    await expect(page.getByTestId("detail-context")).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    await openMissionDetails(page, "timeline");
    await expect(page.getByTestId("detail-timeline")).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    await openMissionConversation(page);
    await expect(page.getByTestId("composer-input")).toBeVisible();
  });
}

test("the wide workspace uses the available width for conversation and details (#944)", async ({
  page,
}) => {
  await stub(page, ROWS, { supervisor: SUPERVISOR }, { objectives: OBJ_ROWS });
  await page.setViewportSize({ width: 1600, height: 950 });
  await page.goto("/mission");
  await selectFirst(page);

  // A GEOMETRY CLAIM, not a class one. The old layout put `grid-template-columns: 1fr 340px` on
  // the split at ≥1400px; asserting the class is absent would pass against a track re-added under
  // a different name. What the operator experiences is the thread being 340px narrower than the
  // content area, so that is what is measured.
  const pane = await page.getByTestId("pane").boundingBox();
  const shell = await page.getByTestId("mission-console").boundingBox();
  expect(pane).not.toBeNull();
  expect(shell).not.toBeNull();
  const details = (await page.getByTestId("mission-details").boundingBox())!;
  expect(shell!.width - pane!.width - details.width).toBeLessThan(60);

  // …and nothing is rendered off to the side of it.
  await expect(page.getByTestId("mission-details")).toBeVisible();
});

test("a half-typed message survives a tab round trip (#942)", async ({
  page,
}) => {
  // OBJECTIVES has a composer of its own ("Add an objective"). Two inputs on one surface is how a
  // draft gets eaten — and losing an unsent turn to a tab press is a worse bug than the layout
  // this phase set out to fix.
  await stub(page, ROWS, { supervisor: SUPERVISOR }, { objectives: OBJ_ROWS });
  await page.setViewportSize({ width: 1200, height: 950 });
  await page.goto("/mission");
  await selectFirst(page);

  const draft = "half a thought about the relay";
  const composer = page.getByTestId("composer-input");
  await composer.fill(draft);

  await openMissionDetails(page, "objectives");
  await openMissionDetails(page, "followThrough");
  await expect(page.getByTestId("objectives")).toBeVisible();
  // The objective composer is a DIFFERENT input and starts empty — it never inherits the draft.
  await expect(page.getByTestId("objective-add-input")).toHaveValue("");

  await openMissionConversation(page);
  await expect(page.getByTestId("composer-input")).toHaveValue(draft);
});

// ==============================================================================================
// FOLLOW-THROUGH folded onto the rows it was always describing.
// ==============================================================================================

test("the supervisor's reading renders ON the objective it describes (#942)", async ({
  page,
}) => {
  await stub(page, ROWS, { supervisor: SUPERVISOR }, { objectives: OBJ_ROWS });
  await page.setViewportSize({ width: 1600, height: 950 });
  await page.goto("/mission");
  await selectFirst(page);
  await openMissionDetails(page, "objectives");
  await openMissionDetails(page, "followThrough");

  const rows = page.getByTestId("objectives").getByTestId("objective");
  await expect(rows).toHaveCount(2);

  // THE JOIN IS BY KEY, and this is what proves it: the two rows get DIFFERENT readings, so a
  // fold that paired them by position or attached one reading to every row fails here. `checks`
  // is the spent gate; `docs` is free.
  const checks = rows.nth(0);
  await expect(checks).toHaveAttribute("data-key", "checks");
  await expect(checks).toHaveAttribute("data-board", "spent");
  await expect(checks.getByText("SPENT", { exact: true })).toBeVisible();
  await expect(checks.getByText("GATE", { exact: true })).toBeVisible();
  await expect(checks.getByText("3/3")).toBeVisible();
  await expect(checks.getByText("· ep 2")).toBeVisible();
  // The server's sentence, verbatim and on the row it is about.
  await expect(checks.getByText(SPENT_SENTENCE)).toBeVisible();

  const docs = rows.nth(1);
  await expect(docs).toHaveAttribute("data-board", "ready");
  await expect(docs.getByText("READY", { exact: true })).toBeVisible();
  await expect(docs.getByText(SPENT_SENTENCE)).toHaveCount(0);

  // …and the standalone panel it came from is gone, not merely hidden.
  await expect(page.getByTestId("supervisor-board")).toHaveCount(0);
});

test("STAND DOWN moved to the row and kept its episode fence (#942)", async ({
  page,
}) => {
  await stub(page, ROWS, { supervisor: SUPERVISOR }, { objectives: OBJ_ROWS });
  await page.setViewportSize({ width: 1600, height: 950 });
  await page.goto("/mission");
  await selectFirst(page);
  await openMissionDetails(page, "objectives");
  await openMissionDetails(page, "followThrough");

  const row = page.getByTestId("objectives").getByTestId("objective").first();
  const btn = row.getByTestId("objective-stand-down");
  // The EPISODE THE ROW WAS RENDERED AT rides on the element, so a stale tap is a 409 rather than
  // silencing a report nobody has seen. Folding the control onto the objective row changed where
  // it sits and nothing about that fence.
  await expect(btn).toHaveAttribute("data-episode", "2");
  // …and it clears the coarse-pointer floor where it now lives.
  const box = await btn.boundingBox();
  expect(box?.height ?? 0).toBeGreaterThanOrEqual(20);
  const rowBox = await row.boundingBox();
  expect(rowBox?.height ?? 0).toBeGreaterThanOrEqual(44);
});

test("the mission-level notices survive the fold, including the no-rows case (#942)", async ({
  page,
}) => {
  // These are the part of FOLLOW-THROUGH that is about the MISSION, and some of them have no row
  // to attach to. `unmeasured` is the one whose whole meaning is that there are no rows — a naive
  // fold drops exactly that one, because it folds into a list that is empty.
  await stub(
    page,
    ROWS,
    { supervisor: { ...SUPERVISOR, objectives: [], unmet_gates: 0 } },
    { objectives: [] },
  );
  await page.setViewportSize({ width: 1600, height: 950 });
  await page.goto("/mission");
  await selectFirst(page);
  await openMissionDetails(page, "objectives");
  await openMissionDetails(page, "followThrough");

  await expect(page.getByTestId("supervisor-unmeasured")).toBeVisible();
  await expect(page.getByTestId("supervisor-unreadable")).toHaveCount(0);
});

test("an unmet gate is still counted above the rows (#942)", async ({
  page,
}) => {
  await stub(page, ROWS, { supervisor: SUPERVISOR }, { objectives: OBJ_ROWS });
  await page.setViewportSize({ width: 1600, height: 950 });
  await page.goto("/mission");
  await selectFirst(page);
  await openMissionDetails(page, "objectives");
  await openMissionDetails(page, "followThrough");
  await expect(page.getByTestId("supervisor-unmet-gates")).toHaveText(
    "1 unmet gate",
  );
});

test("the overflow lets Tab LEAVE — it is a menu, not a trap (#942)", async ({
  page,
}) => {
  // THE OUTCOME, which is what the operator meets: the menu goes away and focus is not inside it.
  //
  // It does NOT isolate `containFocus`, and saying it did would be false: the menu's own Tab
  // handler closes on the key, so the same end state is reached whether or not the hook is still
  // trying to contain focus. The two mechanisms are separated by the unit test on the hook
  // (`useModalDrawer.test.tsx`), which asks it directly whether it moved focus — the only level at
  // which the flag is observable at all.
  await stub(page, ROWS);
  await page.setViewportSize({ width: 1600, height: 950 });
  await page.goto("/mission");
  const trigger = page.getByTestId("mission-overflow");
  const menu = page.getByTestId("mission-overflow-menu");

  for (const key of ["Tab", "Shift+Tab"] as const) {
    await trigger.click();
    await expect(menu).toBeVisible();
    await page.keyboard.press("ArrowDown");
    await expect(menu.getByTestId("mission-failed")).toBeFocused();

    await page.keyboard.press(key);
    // Gone, and focus is OUTSIDE it — on the control the menu belongs to, never wrapped back to
    // another item. Asserted in both directions: a trap that holds one way is still a trap.
    await expect(menu).toHaveCount(0);
    await expect(trigger).toBeFocused();
    const inside = await page.evaluate(() => {
      const m = document.querySelector('[data-testid="mission-overflow-menu"]');
      return !!m && m.contains(document.activeElement);
    });
    expect(inside, `focus stayed inside the menu after ${key}`).toBe(false);
  }
});

// ==============================================================================================
// Both themes, on the surfaces this issue introduced.
// ==============================================================================================

for (const theme of ["dark", "light"] as const) {
  test(`the reworked console is readable in the ${theme} theme (#942)`, async ({
    page,
  }) => {
    // Seed the DEVICE choice before first paint — the device cache wins over `/api/config`, and
    // seeding after load would race the reconcile.
    await page.addInitScript((t) => {
      localStorage.setItem("tr-theme", t);
    }, theme);
    await stub(
      page,
      ROWS,
      { supervisor: SUPERVISOR },
      { objectives: OBJ_ROWS },
    );
    await page.setViewportSize({ width: 1600, height: 950 });
    await page.goto("/mission");
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    await selectFirst(page);

    /** A colour claim a screenshot cannot make: this element's text is not the colour of the
     *  nearest ancestor that actually paints a background. That is exactly what a token defined
     *  only inside a `@media (prefers-color-scheme: dark)` block fails in the other theme. */
    const readable = (testId: string) =>
      page
        .getByTestId(testId)
        .first()
        .evaluate((el) => {
          const fg = getComputedStyle(el as HTMLElement).color;
          let n: HTMLElement | null = el as HTMLElement;
          let bg = "rgba(0, 0, 0, 0)";
          while (n) {
            const c = getComputedStyle(n).backgroundColor;
            if (c && c !== "rgba(0, 0, 0, 0)" && c !== "transparent") {
              bg = c;
              break;
            }
            n = n.parentElement;
          }
          return { fg, bg };
        });

    // THE HEADER ROW — the title and the action cluster this issue put on one line.
    for (const id of ["console-title", "mission-state", "mission-done"]) {
      const { fg, bg } = await readable(id);
      expect(fg, `${id} has no colour in ${theme}`).not.toBe("");
      expect(fg, `${id} is its own background in ${theme}`).not.toBe(bg);
      expect(bg, `nothing paints behind ${id} in ${theme}`).not.toBe(
        "rgba(0, 0, 0, 0)",
      );
    }

    // THE FOLDED SUPERVISOR CELL — the badge is a small mono WORD, which is where the raw status
    // hues fail on the light ground. `contrast.test.ts` pins the rule; this pins that the rule is
    // actually reached at runtime, in both themes.
    await openMissionDetails(page, "objectives");
    await openMissionDetails(page, "followThrough");
    const cell = await readable("supervisor-cell");
    expect(cell.fg).not.toBe(cell.bg);
    const badge = await page
      .getByTestId("supervisor-cell")
      .first()
      .locator("span")
      .first()
      .evaluate((el) => getComputedStyle(el).color);
    expect(badge).not.toBe("");
    expect(badge).not.toBe(cell.bg);
  });
}

// ==============================================================================================
// The long cases — the box checks above use a short thread, which is the easy one.
// ==============================================================================================

test("a LONG thread still leaves the composer on the bottom edge, and its last event reachable (#942)", async ({
  page,
}) => {
  const events = Array.from({ length: 60 }, (_, i) => ({
    seq: i + 1,
    kind: "operator_msg",
    at: T + i,
    text: `turn number ${i + 1}, long enough to wrap on a narrow column and take a line or two`,
    meta: {},
  }));
  await stub(page, ROWS, { events, events_next_seq: null });
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/mission");
  await expect(page.getByTestId("mission-state")).toBeVisible();

  // The dock is still ON the bottom edge — a thread that overflows must scroll, not push it off.
  const dock = (await page.locator('[class*="composerDock"]').boundingBox())!;
  const shell = (await page.getByTestId("mission-console").boundingBox())!;
  expect(
    Math.abs(dock.y + dock.height - (shell.y + shell.height)),
  ).toBeLessThanOrEqual(2);

  // …and the last event is reachable by scrolling the PANE, not the page.
  await expect(page.getByTestId("thread-event")).toHaveCount(60);
  const last = page.getByTestId("thread-event").last();
  await last.scrollIntoViewIfNeeded();
  await expect(last).toBeInViewport();
  const wide = await page.evaluate(
    () =>
      document.documentElement.scrollWidth >
      document.documentElement.clientWidth + 1,
  );
  expect(wide).toBe(false);
});

test("a LONG objective list scrolls inside the pane, composer still docked (#942)", async ({
  page,
}) => {
  const many = Array.from({ length: 25 }, (_, i) => ({
    mission_id: "msn_1",
    key: `k${i}`,
    ord: i,
    title: `Objective ${i + 1} — a title long enough to wrap on a narrow column`,
    probe: "manual",
    probe_args: null,
    gate: i % 5 === 0,
    state: "open",
    met_at: null,
    observed: null,
    source: "test",
  }));
  const sup = {
    ...SUPERVISOR,
    objectives: many.map((o, i) => ({
      key: o.key,
      title: o.title,
      gate: o.gate,
      state: "open",
      met: false,
      episode: 1,
      stood_down: false,
      awaiting_answer: false,
      spent: i % 4,
      remaining: 3 - (i % 4),
      may_nudge: i % 4 === 0,
      unreadable: false,
      indeterminate: false,
      live: 0,
      terminal: false,
      why_not: i % 4 === 0 ? "" : SPENT_SENTENCE,
    })),
  };
  await stub(page, ROWS, { supervisor: sup }, { objectives: many });
  await page.setViewportSize({ width: 412, height: 915 });
  await page.goto("/mission");
  await selectFirst(page);
  await openMissionDetails(page, "objectives");
  await openMissionDetails(page, "followThrough");
  await expect(page.getByTestId("objective")).toHaveCount(25);

  await expect(page.getByTestId("composer-input")).toBeHidden();
  const scroll = page.getByTestId("mission-details");
  expect(await scroll.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(
    true,
  );

  const last = page.getByTestId("objective").last();
  await last.scrollIntoViewIfNeeded();
  await expect(last).toBeInViewport();
  // The server's prose is unbounded and this is 412px — it must wrap, not widen the page.
  const wide = await page.evaluate(
    () =>
      document.documentElement.scrollWidth >
      document.documentElement.clientWidth + 1,
  );
  expect(wide).toBe(false);
});

// ==============================================================================================
// #942 review 1–3: three regressions the phase tests above could not see.
// ==============================================================================================

test("a FAILED objectives read keeps the supervisor's reading and its STAND DOWN (#942)", async ({
  page,
}) => {
  // THE FOLD'S OWN FAILURE MODE. These are two independent client reads, and the join renders a
  // row only where the objective LIST has one. When `/objectives` fails while the detail lands
  // with a populated assessment, an inner join shows nothing: no badge, no refusal sentence, no
  // STAND DOWN — underneath a mission-level notice still saying "1 unmet gate". Before
  // follow-through folded in, the supervisor's own board rendered from its own list and was
  // untouched by that failure, so this is a regression the fold introduced.
  await stub(page, ROWS, { supervisor: SUPERVISOR }, { objectives: OBJ_ROWS });
  // Registered last, so it wins over `mockMissions`.
  await page.route(/\/api\/missions\/[^/]+\/objectives(\?.*)?$/, (r) =>
    r.fulfill({ status: 503, json: { detail: "store unwell" } }),
  );
  await page.setViewportSize({ width: 1600, height: 950 });
  await page.goto("/mission");
  await selectFirst(page);
  await openMissionDetails(page, "objectives");
  await openMissionDetails(page, "followThrough");

  // The assessment survives on rows of its own.
  const rows = page.getByTestId("objective");
  await expect(rows).toHaveCount(2);
  await expect(page.getByTestId("supervisor-cell")).toHaveCount(2);
  await expect(page.getByTestId("objective-stand-down").first()).toBeVisible();
  await expect(page.getByText(SPENT_SENTENCE)).toBeVisible();
  await expect(page.getByTestId("supervisor-unmet-gates")).toBeVisible();

  // …and the failure is NAMED rather than rendered as "no objectives yet". "We could not look"
  // and "there are none" are different claims and the operator acts differently on each.
  await expect(page.getByTestId("objectives-empty")).toHaveCount(0);

  // A row built from the assessment alone offers no EDIT controls: reorder is meaningless without
  // the list that defines the order, and a rename aimed at a list we could not read is a write on
  // an unknown. STAND DOWN is offered because it acts on the ASSESSMENT.
  await expect(page.getByTestId("objective-rename")).toHaveCount(0);
  await expect(page.getByTestId("objective-drop")).toHaveCount(0);
});

test("a failed objectives read says so instead of 'no objectives yet' (#942)", async ({
  page,
}) => {
  // The same failure with NO assessment to fall back on — the case where the empty state is all
  // the operator gets, so what it says is the whole answer.
  await stub(page, ROWS, {}, { objectives: [] });
  await page.route(/\/api\/missions\/[^/]+\/objectives(\?.*)?$/, (r) =>
    r.fulfill({ status: 503, json: { detail: "store unwell" } }),
  );
  await page.setViewportSize({ width: 1600, height: 950 });
  await page.goto("/mission");
  await selectFirst(page);
  await openMissionDetails(page, "objectives");
  await openMissionDetails(page, "followThrough");

  await expect(page.getByTestId("objectives-unreadable")).toBeVisible();
  await expect(page.getByTestId("objectives-unreadable")).toContainText(
    /not a claim that this mission has none/i,
  );
  await expect(page.getByTestId("objectives-empty")).toHaveCount(0);
});

test("a confirmation rerender does not throw focus out of the open menu (#942)", async ({
  page,
}) => {
  // `inertRefs: []` is a fresh array every render, and the focus effect depended on its identity —
  // so a re-render inside the menu tore that effect down and back up, and its cleanup's queued
  // frame put focus back on `⋯` while the menu was still open. The next Enter then operated the
  // TRIGGER instead of confirming, and the arrows stopped reaching the menu.
  await stub(page, ROWS);
  await page.setViewportSize({ width: 1600, height: 950 });
  await page.goto("/mission");
  const trigger = page.getByTestId("mission-overflow");
  await trigger.click();
  const menu = page.getByTestId("mission-overflow-menu");
  await expect(menu).toBeVisible();

  await page.keyboard.press("ArrowDown");
  await expect(menu.getByTestId("mission-failed")).toBeFocused();

  // The rerender: the item becomes its own confirmation.
  await page.keyboard.press("Enter");
  await expect(menu.getByTestId("mission-failed")).toContainText(/^CONFIRM/);

  // FOCUS IS STILL ON THE ITEM, not on the trigger behind it.
  await expect(menu).toBeVisible();
  await expect(menu.getByTestId("mission-failed")).toBeFocused();
  await expect(trigger).not.toBeFocused();

  // …and the arrows still reach the menu, which is the other half of what was lost.
  await page.keyboard.press("ArrowDown");
  await expect(menu.getByTestId("mission-abandon")).toBeFocused();
});

test("the ⋯ trigger closes the menu it opened, by real pointer (#942)", async ({
  page,
}) => {
  // TWO REAL CLICKS, because a synthetic `click()` cannot see this: it dispatches no mousedown,
  // and mousedown is where the hook decided the press was "outside" and closed the menu — leaving
  // the click that followed to toggle it straight back open.
  await stub(page, ROWS);
  await page.setViewportSize({ width: 1600, height: 950 });
  await page.goto("/mission");
  const trigger = page.getByTestId("mission-overflow");
  const menu = page.getByTestId("mission-overflow-menu");

  await trigger.click();
  await expect(menu).toHaveCount(1);
  await trigger.click();
  await expect(menu).toHaveCount(0);
  await expect(trigger).toHaveAttribute("aria-expanded", "false");

  // …and it still opens again, so "never opens" cannot pass this.
  await trigger.click();
  await expect(menu).toHaveCount(1);
});

test("a RECOVERED objectives read stops claiming it could not be read (#942)", async ({
  page,
}) => {
  // THE STATE THE ERROR FIX ITSELF CREATED. `objectivesFailed` first lived beside the one load
  // path that set it, so every OTHER accepted install — the pending-producer poll, the settlement
  // recovery — applied fresh rows and left "could not be read" on screen above them. The producer
  // stops being pending once it settles, so its fast poll stops too and nothing later repairs the
  // flag: the console tells the operator the list is unreadable forever, over a settled producer.
  let objCalls = 0;
  let missionCalls = 0;

  await stub(page, ROWS);
  // Registered after `stub`, so these win. The FIRST objectives read fails; the next succeeds
  // with a genuinely empty list, which is the interesting case — "none" and "unreadable" are the
  // two answers this test has to keep apart.
  await page.route(/\/api\/missions\/[^/]+\/objectives(\?.*)?$/, (r) => {
    objCalls += 1;
    return objCalls === 1
      ? r.fulfill({ status: 503, json: { detail: "store unwell" } })
      : r.fulfill({ json: { objectives: [] } });
  });
  // …and the mission settles the producer on the second read: `pending` → `skipped`.
  await page.route(/\/api\/missions\/msn_1(\?.*)?$/, (r) => {
    missionCalls += 1;
    return r.fulfill({
      json: {
        ...MISSION,
        events: [],
        events_next_seq: null,
        objectives_state: missionCalls === 1 ? "pending" : "skipped",
      },
    });
  });

  await page.setViewportSize({ width: 1600, height: 950 });
  await page.goto("/mission");
  await selectFirst(page);
  await openMissionDetails(page, "objectives");
  await openMissionDetails(page, "followThrough");

  // The failure is shown first — the honest answer while it is the only one we have.
  await expect(page.getByTestId("objectives-unreadable")).toBeVisible();

  // …and then the producer settles and a successful read lands. The pane must report what the
  // PRODUCER said, not the read that failed before it.
  await expect(page.getByTestId("objectives-unavailable")).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.getByTestId("objectives-unavailable")).toContainText(
    /no objectives were proposed/i,
  );
  await expect(page.getByTestId("objectives-unreadable")).toHaveCount(0);
});

test("CONTEXT with nothing selected explains ITSELF, not the timeline (#942)", async ({
  page,
}) => {
  // CONTEXT became reachable when the tabs replaced the detail column, and fell through to the
  // timeline's sentence — an explanation about a surface the operator is not looking at.
  await stub(page, []);
  await page.setViewportSize({ width: 1600, height: 950 });
  await page.goto("/mission");
  await expect(page.getByTestId("mission-console")).toBeVisible();

  await page.getByTestId("stop-details").click();
  const ctx = page.getByTestId("no-mission-details");
  await expect(ctx).toBeVisible();
  await expect(ctx).toContainText(
    /context, objectives, follow-through and timeline belong to a mission/i,
  );

  await page.getByTestId("stop-details").click();
  await expect(page.getByTestId("no-mission-details")).toContainText(
    /timeline belong to a mission/i,
  );
});
