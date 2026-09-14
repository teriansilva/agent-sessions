import { openMissionConversation,
  flipMissionScope,
} from "./mission-console";
import { openMissionDetails } from "./mission-console";
/** MISSION CONTROL's own browser gates (#878).
 *
 * Four things here cannot be proved in jsdom, which is why they are asserted in a real browser:
 *
 *  - the THREE LAYOUT MODES. Two breakpoints make three modes, and the middle one (1100–1399:
 *    rail is a column, detail is still a tab strip) is the mode neither Playwright project lands
 *    on, so it is the one that ships broken. Both edges of both boundaries are checked, because
 *    an off-by-one in a media query is exactly what a single mid-range width cannot see.
 *  - the DRAWER's modal contract. `aria-modal` is a promise; a breakpoint is not a modal.
 *  - the GEOMETRY INVENTORY. Every interactive control on the phone viewport is enumerated and
 *    measured — not a hand-written list of the ones someone remembered, which is the failure
 *    mode that wording exists to prevent.
 *  - the HONEST STATES, which are acceptance criteria rather than visual promises.
 */
import { expect, test, type Page } from "@playwright/test";

import {
  MISSION,
  missionRailTrigger,
  openMissionRail,
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

const CARD = {
  id: "claude:aaa",
  engine: "claude",
  title: "Kimi transcript adapter",
  cwd: "/home/u/agent-sessions",
  project: {
    kind: "project",
    id: "p1",
    name: "agent-sessions",
    color: "#ffb000",
  },
  last_activity: T - 120,
  ai_summary: "landed the parser",
  intervention_required: false,
  intervention_reason: "",
  reviewed_at: T - 120,
  live: true,
  state: "in_flight",
  synthesis: null,
};

const OVERVIEW = {
  cache_version: 1,
  generated_at: T - 60,
  window_days: 3,
  scan_depth: "fast",
  input_fingerprint: "fp",
  synthesis_skipped: false,
  cards: [CARD],
};

async function stub(page: Page, over: Record<string, unknown> = {}) {
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
    r.fulfill({ json: { ...OVERVIEW, ...over } }),
  );
  await page.route("**/api/pulse/orchestrator", (r) =>
    r.fulfill({ status: 500, json: { detail: "off" } }),
  );
}

/** The LIST row for the held mission — keys only, as the producer emits. */
const HELD_ROW = missionRow({ session_keys: ["claude:aaa"] });
/** …and its DETAIL, which is where the roster lives. */
const HELD = {
  ...MISSION,
  sessions: [{ session_key: "claude:aaa", removed_at: null }],
};

// ==============================================================================================
// The stops, asserted BY NAME so the contract cannot drift from the implementation.
//
// FOUR of them since #942, and they are no longer "the mobile stops": CONTEXT joined when the
// 340px detail column was deleted, and the same strip is now the wide layout too. The column used
// to stack objectives, follow-through and context under three headings, which is why the narrow
// case split at one seam into three stops rather than four.
// ==============================================================================================

test("Conversation and Details open the four ordered disclosures", async ({
  page,
}) => {
  await stub(page);
  await mockMissions(page, {
    missions: missionList([HELD_ROW]),
    mission: { ...HELD, events: [], events_next_seq: null },
  });
  await page.setViewportSize({ width: 412, height: 900 });
  await page.goto("/mission");

  const tabs = stops(page);
  await expect(tabs).toHaveCount(2);
  await expect(tabs.nth(0)).toHaveText("Conversation");
  await expect(tabs.nth(1)).toHaveText("Details");
  await expect(
    page.getByTestId("mission-details").locator("section > button"),
  ).toHaveText([/Context/, /Objectives/, /Follow-through/, /Timeline/]);
  // THREAD is the default — the decision surface, not the log.
  await expect(tabs.nth(0)).toHaveAttribute("aria-selected", "true");

  await tabs.nth(1).click();
  await expect(page.getByText(/objectives/i).first()).toBeVisible();
  await openMissionDetails(page, "timeline");
  // Still scoped to the pane. The detail column that made this query ambiguous is gone (#942),
  // but the scoping is what fails loudly if a second copy is ever mounted again.
  await expect(
    page.getByTestId("mission-details").getByTestId("timeline-empty"),
  ).toBeVisible();
});

// ==============================================================================================
// Two breakpoints, three modes — both edges of both boundaries.
// ==============================================================================================

const rail = (p: Page) => p.getByRole("navigation", { name: /missions/i });
/** The console's OWN stops. The app shell's sidebar carries an "Archived filter" tablist too, so
 *  an unscoped `getByRole("tab")` counts five. */
const stops = (p: Page) =>
  p.getByRole("tablist", { name: /mission view/i }).getByRole("tab");

/* #935 moved the rail into the app shell's sidebar wherever that sidebar is a persistent
 * column — which the shell decides on its OWN breakpoint (<=800px is the off-canvas drawer),
 * not the console's old 1100px one. So 1099 is now a column case: the rail is in the sidebar
 * and the console's drawer trigger stands down.
 *
 * THE SECOND AXIS IS GONE (#942). This used to enumerate `detailIsColumn` too, and 1400 was the
 * one width where it flipped: below it the stop strip carried objectives and the timeline, at and
 * above it a 340px column did, and the strip hid. That was two different layouts on one page —
 * the thing the operator called a mess. There is no column now, so the table has one axis and the
 * detail assertion below is the same at every width. */
for (const [width, railIsColumn] of [
  [412, false],
  [1099, true],
  [1100, true],
  [1399, true],
  [1400, true],
] as const) {
  test(`layout at ${width}px: rail ${railIsColumn ? "column" : "drawer"}, detail tabs`, async ({
    page,
  }) => {
    await stub(page);
    await mockMissions(page, {
      missions: missionList([HELD_ROW]),
      mission: { ...HELD, events: [], events_next_seq: null },
    });
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/mission");
    await expect(page.getByTestId("mission-console")).toBeVisible();

    // The rail is either laid out beside the pane, or it is behind the drawer trigger.
    if (railIsColumn) {
      await expect(rail(page)).toBeVisible();
      // The console's own trigger is GONE at every width now (#940), not merely hidden.
      await expect(page.getByTestId("rail-drawer-open")).toHaveCount(0);
      const r = await rail(page).boundingBox();
      const pane = await page.getByTestId("pane").boundingBox();
      expect(r).not.toBeNull();
      expect(pane).not.toBeNull();
      // Beside, not stacked, and not overlapping.
      expect(r!.x + r!.width).toBeLessThanOrEqual(pane!.x + 1);
    } else {
      // Behind the SHELL's control now, not the console's — which no longer exists.
      await expect(page.getByTestId("rail-drawer-open")).toHaveCount(0);
      await expect(missionRailTrigger(page)).toBeVisible();
      // OFF-CANVAS, not `display:none` — so this is a GEOMETRY claim, not a visibility one.
      // The shell's drawer translates out of the viewport; Playwright still calls that "visible"
      // because it has a box, which is exactly why the old `toBeHidden()` stopped meaning
      // anything once the rail moved into the shell (#940).
      const closed = await rail(page).boundingBox();
      expect(closed).not.toBeNull();
      expect(closed!.x + closed!.width).toBeLessThanOrEqual(1);

      // …and the shell's control brings it in.
      await openMissionRail(page);
      const opened = await rail(page).boundingBox();
      expect(opened!.x).toBeGreaterThanOrEqual(0);
    }

    // ONE LAYOUT, AT EVERY WIDTH (#942). The four tabs are the detail surface, and the column
    // that used to replace them above 1400 is gone rather than hidden — so this is a `toHaveCount`
    // claim about the DOM, not a visibility one that a `display: none` copy would satisfy.
    await expect(page.getByTestId("detail-column")).toHaveCount(0);
    if (width >= 1400)
      await expect(page.getByTestId("stop-details")).toBeHidden();
    else await expect(stops(page)).toHaveCount(2);

    // No sideways scroll at any of the five widths.
    const wide = await page.evaluate(
      () =>
        document.documentElement.scrollWidth >
        document.documentElement.clientWidth,
    );
    expect(wide).toBe(false);
  });
}

// ==============================================================================================
// THE DIALOG CONTRACT MOVED, IT WAS NOT DROPPED (#940).
//
// Two tests lived here — "the rail drawer is a real modal" and "Tab is contained inside the
// drawer" — and they exercised `MissionDrawer`, the console's own panel. That panel is gone: the
// rail lives in the app shell's sidebar at every width now, so the shell owns the modal contract
// and its regressions belong beside it.
//
// They are `mission-shell-layout.spec.ts` → "the phone gets the same one rail, through the shell",
// which asserts more than these did: `aria-modal`, focus in, focus restored on each of three close
// paths, the background regions actually inert, the panel NOT inert, a desktop control proving the
// docked column gains none of it, the same contract on a non-mission route, and the 800→801 resize
// releasing the isolation.
// ==============================================================================================

// ==============================================================================================
// The geometry inventory. EVERY interactive control on the phone viewport, enumerated by the
// browser rather than by me — an inventory that only covers what someone remembered is the
// failure this exists to prevent.
// ==============================================================================================

test("every interactive control on a phone is ≥44px, focusable, and inside the viewport", async ({
  page,
}) => {
  await stub(page);
  // …AND A LOOSE SESSION, so the rail carries an UNTRACKED view: NEW MISSION lives there now
  // that the mission body's composer is the durable one (#890). `mission_id: null` is
  // membership KNOWN and empty — `undefined` means "could not be read", which is a different
  // state and correctly hides the view.
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: {
        ...OVERVIEW,
        cards: [
          CARD,
          // A DIFFERENT key from the one the mission holds: `claude:aaa` is `HELD_ROW`'s, so a
          // card wearing it is that mission's, not an untracked one.
          { ...CARD, id: "claude:loose", title: "Nobody's", mission_id: null },
        ],
      },
    }),
  );
  // AN ARCHIVED MISSION TOO, so the inventory can reach the UNARCHIVE confirmation — two controls
  // that exist only after it is opened, on a scope the default stops never visit (#896 review 7,
  // finding 3). Resolved from the query, because the two scopes are disjoint sets.
  const ARCHIVED_ROW = missionRow({
    id: "msn_arch",
    title: "Shelved",
    archived_at: T - 900,
  });
  // …AND A MISSION IN EACH STATE WHOSE CONTROL ONLY EXISTS THERE (#896 review 15, finding 3).
  //
  // A running mission renders neither BEGIN nor REOPEN, so a sweep over one measured neither —
  // and a fixture whose only objective is already MET renders NOT REQUIRED and the reorder arrows
  // DISABLED, which the selector excludes. Every one of those is a control this PR added, and
  // "the inventory covers every new control" was the acceptance claim.
  const PLANNED_ROW = missionRow({
    id: "msn_plan",
    title: "Not started",
    state: "planned",
  });
  const DONE_ROW = missionRow({
    id: "msn_done",
    title: "Finished",
    state: "done",
  });
  await mockMissions(page, {
    missions: (q: URLSearchParams) =>
      missionList(
        q.get("archived") === "1"
          ? [ARCHIVED_ROW]
          : [HELD_ROW, PLANNED_ROW, DONE_ROW],
      ),
    mission: {
      ...HELD,
      events: [],
      events_next_seq: null,
      // THE SUPERVISOR'S BOARD, without which STAND DOWN does not exist. It is offered only for
      // an objective that is unmet and not already stood down — silencing a met one would be a
      // control with no effect — so the fixture has to produce that state rather than any state.
      supervisor: {
        objectives: [
          {
            key: "checks_green",
            title: "Checks are green",
            gate: true,
            state: "open",
            met: false,
            episode: 1,
            stood_down: false,
            spent: 1,
            remaining: 2,
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
        held_sessions: 1,
        no_session: false,
        checked_at: T - 30,
      },
    },
    objectives: {
      objectives: [
        {
          mission_id: HELD.id,
          key: "checks_green",
          ord: 0,
          title: "Checks are green",
          probe: "none",
          probe_args: null,
          gate: true,
          // UNMET, and that is the point: NOT REQUIRED and the reorder arrows are disabled on a
          // settled objective, and `:not([disabled])` in the sweep's selector excludes exactly
          // those — so a fixture whose only objective was met measured neither.
          state: "open",
          met_at: null,
          observed: null,
          source: "operator",
        },
        {
          mission_id: HELD.id,
          key: "pr_opened",
          ord: 1,
          title: "PR opened",
          probe: "none",
          probe_args: null,
          gate: false,
          state: "met",
          met_at: T - 400,
          observed: null,
          source: "operator",
        },
      ],
    },
  });
  // …and its DETAIL, resolved by id, so selecting it actually paints the archived lifecycle bar.
  await page.route(/\/api\/missions\/msn_arch(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        ...HELD,
        id: "msn_arch",
        title: "Shelved",
        archived_at: T - 900,
        sessions: [],
        events: [],
        events_next_seq: null,
      },
    }),
  );
  // …and the two lifecycle states whose controls exist nowhere else. BEGIN needs a `planned`
  // mission that HOLDS a session (it is disabled without one, and a disabled control is excluded
  // from the sweep); REOPEN exists only on a terminal one.
  await page.route(/\/api\/missions\/msn_plan(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        ...HELD,
        id: "msn_plan",
        title: "Not started",
        state: "planned",
        events: [],
        events_next_seq: null,
      },
    }),
  );
  await page.route(/\/api\/missions\/msn_done(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        ...HELD,
        id: "msn_done",
        title: "Finished",
        state: "done",
        closed_at: T - 60,
        events: [],
        events_next_seq: null,
      },
    }),
  );
  await page.setViewportSize({ width: 412, height: 900 });
  await page.goto("/mission");
  await expect(page.getByTestId("mission-console")).toBeVisible();

  // Walk all three stops AND the drawer, so the inventory covers every control the operator can
  // reach on a phone — not just the ones on the first screen.
  //
  // …and the HIDDEN states too (#896 review 3, finding 5). NEW MISSION and an objective rename
  // are controls that do not exist until something is opened, so a sweep of the default screens
  // measures neither — and they are exactly where a new control gets added without anyone
  // re-checking the touch floor.
  const surfaces: (() => Promise<void>)[] = [
    async () => {},
    async () => void (await openMissionDetails(page, "objectives")),
    async () => {
      // The objective row's own edit controls, plus the rename field it swaps in.
      await openMissionDetails(page, "objectives");
      const rename = page.getByTestId("objective-rename").first();
      if (await rename.isVisible().catch(() => false)) await rename.click();
    },
    async () => void (await openMissionDetails(page, "timeline")),
    async () => {
      // NEW MISSION: a composer MODE, so its form is only in the DOM once opened — and it lives
      // in the UNTRACKED view, because the mission body's composer is the DURABLE one (#890).
      await openMissionRail(page);
      await page.locator('[data-testid="rail-untracked-view"]:visible').click();
      await page.keyboard.press("Escape");
      const mode = page.getByTestId("composer-mode-new");
      if (await mode.isVisible().catch(() => false)) await mode.click();
    },
    async () => {
      await openMissionConversation(page);
      const mode = page.getByTestId("composer-mode-ask");
      if (await mode.isVisible().catch(() => false)) await mode.click();
      await openMissionRail(page);
    },
    async () => {
      // Return from the untracked view to the mission owning this assessment.
      await page.keyboard.press("Escape");
      await openMissionRail(page);
      await page.getByTestId("rail-mission").first().click();
      await openMissionDetails(page, "objectives");
      await openMissionDetails(page, "followThrough");
    },
    async () => {
      // BEGIN: a `planned` mission holding a session. It exists in no other state, so a sweep
      // over the running mission alone never saw it.
      await openMissionRail(page);
      await page.locator('[data-testid="rail-mission"]:visible').nth(1).click();
      await page.keyboard.press("Escape");
      await expect(page.getByTestId("mission-begin")).toBeVisible();
    },
    async () => {
      // REOPEN: a terminal mission. Same argument, other end of the lifecycle.
      await openMissionRail(page);
      await page.locator('[data-testid="rail-mission"]:visible').nth(2).click();
      await page.keyboard.press("Escape");
      await expect(page.getByTestId("mission-reopen")).toBeVisible();
    },
    async () => {
      // …and back to the running one, so the surfaces after this see the state they expect.
      await openMissionRail(page);
      await page
        .locator('[data-testid="rail-mission"]:visible')
        .first()
        .click();
      await page.keyboard.press("Escape");
    },
    async () => {
      // THE UNARCHIVE CONFIRMATION. RECORD ONLY and RESTART AGENTS do not exist until it is
      // opened, and they live on the archived scope, which none of the surfaces above visits —
      // exactly the shape a new control slips through in.
      await page.keyboard.press("Escape");
      await openMissionRail(page);
      await flipMissionScope(page);
      await page
        .locator('[data-testid="rail-mission"]:visible')
        .first()
        .click();
      // Closed through the one dialog the shell owns (#940) — `rail-drawer` was `MissionDrawer`'s
      // panel and that component is deleted, so this matched nothing.
      if (await page.getByRole("dialog").count()) {
        await page.keyboard.press("Escape");
        await expect(page.getByRole("dialog")).toHaveCount(0);
      }
      await page.getByTestId("mission-unarchive").click();
      await expect(page.getByTestId("mission-unarchive-record")).toBeVisible();
    },
  ];

  const offenders: string[] = [];
  const seen: string[] = [];
  let measured = 0;
  let focusChecked = 0;
  for (const visit of surfaces) {
    await visit();
    const found = await page.evaluate(() => {
      // `select` IS on this list, and its absence was a real gap rather than an oversight of
      // taste: the project picker #896 made REQUIRED is a `<select>`, so the one control an
      // operator cannot start a mission without was the one control the inventory never
      // measured (#896 review 10, finding 8).
      const sel =
        'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [role="tab"], [tabindex]:not([tabindex="-1"])';
      // Scoped to the CONSOLE and its drawer — this PR's surface. That is a region, not a list:
      // everything interactive inside it is enumerated, so a control added later is covered
      // automatically. The app shell's own topbar and sidebar are out of this PR's scope and
      // carry pre-existing sub-44px icon buttons; asserting on them here would turn an unrelated
      // backlog item into this PR's problem and make the gate meaningless when it failed.
      // The rail moved into the shell's sidebar (#940), so its controls are enumerated through
      // the rail's own landmark rather than the console's retired drawer. The rest of the shell
      // stays out of scope for the reason above — it carries pre-existing sub-44px icon buttons
      // that are not this surface's to answer for, and pulling the whole <aside> in would turn an
      // unrelated backlog item into a failure here.
      // ONLY WHILE IT IS REACHABLE. The console's old drawer was rendered conditionally, so a
      // closed one contributed no elements at all. The shell's sidebar is always in the DOM and
      // merely translated out of the viewport when closed — so including it unconditionally
      // reported every rail control as "outside the viewport", which is true and irrelevant: the
      // operator cannot reach them, and the check exists to catch controls that overflow while
      // reachable.
      //
      // `inert` IS THE REACHABILITY ANSWER, and geometry was only ever standing in for it (#940
      // review 2). A parked drawer is inert now, and the two disagree for a few frames every time
      // one closes: the panel slides out over a transition, so `left >= 0` still says "on screen"
      // while the attribute already says "unreachable" — and every control inside it then reports
      // `not focusable`, which is the isolation WORKING. Asking the attribute removes the race and
      // says what the loop below actually means.
      const railNav = document.querySelector<HTMLElement>(
        'nav[aria-label="Missions" i]',
      );
      const railOnScreen =
        railNav !== null &&
        railNav.getBoundingClientRect().left >= 0 &&
        railNav.closest("[inert]") === null;
      const roots = [
        document.querySelector('[data-testid="mission-console"]'),
        railOnScreen ? railNav : null,
      ].filter(Boolean) as HTMLElement[];
      const bad: string[] = [];
      const names: string[] = [];
      // THE DRAWER IS A FOCUS TRAP (#878, now the shell's own — #940), and while it is open the
      // controls behind it are correctly unreachable: the background regions are `inert`. So
      // focusability is asked of what the operator can actually reach right now: everything, or,
      // with the drawer open, what is inside it. Asserting it unconditionally would fail on the
      // trap WORKING — which is exactly what happened when this kept looking for the console's
      // retired `rail-drawer` and found nothing, concluding no trap was open.
      const trap = document.querySelector('aside.sidebar[role="dialog"]');
      let focusChecked = 0;
      const els = roots.flatMap((r) =>
        Array.from(r.querySelectorAll<HTMLElement>(sel)),
      );
      for (const el of els) {
        const r = el.getBoundingClientRect();
        // Skip what the operator cannot reach: zero-size, hidden, or off-layout.
        if (r.width === 0 || r.height === 0) continue;
        const cs = getComputedStyle(el);
        if (cs.visibility === "hidden" || cs.display === "none") continue;
        const label = `${el.tagName}.${el.className}`.slice(0, 70);
        const id = el.getAttribute("data-testid");
        if (id) names.push(id);
        // The 44px floor applies to the TAP TARGET, so height is what matters; a control may be
        // narrow (an icon button) but must not be short.
        if (r.height < 44) bad.push(`${label} h=${Math.round(r.height)}`);
        if (r.right > window.innerWidth + 1 || r.left < -1) {
          bad.push(`${label} x=${Math.round(r.left)}..${Math.round(r.right)}`);
        }
        // FOCUSABLE, as the title of this test has always claimed and as nothing in it used to
        // check (#896 review 10, finding 8). A geometry sweep under a title promising focus is
        // worse than no claim: it reads as covered. Measured LAST, after the rectangle, so
        // focus-driven scrolling cannot move what was measured.
        if (!trap || trap.contains(el)) {
          el.focus({ preventScroll: true });
          focusChecked += 1;
          if (document.activeElement !== el) bad.push(`${label} not focusable`);
        }
      }
      (document.activeElement as HTMLElement | null)?.blur();
      return { bad, seen: els.length, names, focusChecked };
    });
    offenders.push(...found.bad);
    measured += found.seen;
    focusChecked += found.focusChecked;
    seen.push(...found.names);
  }
  // THE HIDDEN CONTROLS WERE ACTUALLY REACHED. Without this the walk above can silently stop
  // finding them — a changed testid, a surface that no longer opens — and the region sweep goes
  // on passing over whatever it happened to see. Named because they are the ones this PR added
  // behind a confirmation (#896 review 7, finding 3).
  for (const id of [
    "mission-unarchive-record",
    "mission-unarchive-sessions",
    "objective-rename",
    "composer-mode-new",
    // #896 review 15, finding 3. Each of these exists in exactly one lifecycle or objective
    // state, and the previous fixture rendered none of them: a running mission shows neither
    // BEGIN nor REOPEN, and a settled objective renders NOT REQUIRED and the reorder arrows
    // DISABLED — which `:not([disabled])` excludes. Naming them is what stops the region sweep
    // from passing over whatever it happened to see.
    "mission-begin",
    "mission-reopen",
    "objective-waive",
    "objective-up",
    "objective-down",
    "objective-stand-down",
    // The REQUIRED project picker — the control the sweep could not see at all until `select`
    // joined the selector above.
    "new-mission-project",
  ]) {
    expect(seen, `the inventory never reached ${id}`).toContain(id);
  }
  // An inventory that measured nothing passes vacuously — the exact failure this test's own
  // wording warns about, one level up. Four surfaces, each with several controls.
  // Six surfaces now, including the two hidden states.
  expect(measured).toBeGreaterThan(18);
  // …and the FOCUS half of the title was checked on a real share of them, not skipped into
  // vacuity by a trap that happened to be open (#896 review 10, finding 8).
  expect(focusChecked).toBeGreaterThan(12);
  expect(offenders).toEqual([]);
});

test("a focused control shows a visible focus ring", async ({ page }) => {
  await stub(page);
  await mockMissions(page, { missions: missionList([HELD_ROW]) });
  await page.setViewportSize({ width: 412, height: 900 });
  await page.goto("/mission");
  await page.waitForLoadState("networkidle");

  const tab = page.getByTestId("stop-details");
  await tab.focus();
  const ring = await tab.evaluate((el) => {
    const s = getComputedStyle(el);
    return { style: s.outlineStyle, width: s.outlineWidth };
  });
  // Keyboard operability is not "it can take focus" — it has to be VISIBLE that it did.
  expect(ring.style).not.toBe("none");
  expect(parseFloat(ring.width)).toBeGreaterThan(0);
});

// ==============================================================================================
// Honest states — acceptance criteria, not visual promises.
// ==============================================================================================

test("no missions and no sessions is an invitation, not a blank", async ({
  page,
}) => {
  await stub(page, { cards: [] });
  await mockMissions(page);
  await page.goto("/mission");
  await expect(page.getByTestId("console-empty")).toContainText(
    /nothing tracked yet/i,
  );
});

test("a store that will not answer says so, and never claims you have no missions", async ({
  page,
}) => {
  await stub(page);
  await mockMissions(page, {
    missions: missionList([], "the store is locked"),
  });
  await page.goto("/mission");
  // In the PANE, not only the rail: on a phone the rail is a drawer, so a rail-only notice is
  // invisible exactly when the console is degraded.
  await expect(page.getByTestId("console-store-error")).toBeVisible();
  // …and the live session is still listed: a store outage must not hide running work.
  await expect(page.getByTestId("untracked-session")).toBeVisible();
});

test("with no AI endpoint the composer is disabled and the notice names it", async ({
  page,
}) => {
  await page.route("**/api/config", (r) =>
    r.fulfill({ json: { ...CONFIG, pulse: { configured: false } } }),
  );
  await stub(page);
  await page.unroute("**/api/config");
  await page.route("**/api/config", (r) =>
    r.fulfill({ json: { ...CONFIG, pulse: { configured: false } } }),
  );
  await mockMissions(page, {
    missions: missionList([HELD_ROW]),
    mission: { ...HELD, events: [], events_next_seq: null },
  });
  await page.goto("/mission");
  await expect(page.getByTestId("no-ai-notice")).toContainText(
    /and the composer/i,
  );
  await expect(page.getByTestId("composer-input")).toBeDisabled();
});

test("a stale probe shows its LAST OBSERVED state, and marks nothing met on data it could not fetch", async ({
  page,
}) => {
  await stub(page);
  await mockMissions(page, {
    missions: missionList([HELD_ROW]),
    mission: { ...HELD, events: [], events_next_seq: null },
    objectives: {
      objectives: [
        {
          mission_id: HELD.id,
          key: "checks_green",
          ord: 0,
          title: "Checks green",
          probe: "forge_checks",
          probe_args: null,
          gate: true,
          state: "pending",
          met_at: null,
          observed: {
            stale: true,
            at: T - 900,
            reason: "forge unreachable since 14:22",
          },
          source: "operator",
        },
      ],
    },
  });
  await page.setViewportSize({ width: 1400, height: 900 });
  await page.goto("/mission");
  // THROUGH THE TAB, at 1400 as at 412 (#942). This used to need no click: a 340px detail column
  // rendered the objectives unprompted at this width, which is exactly the second layout the
  // rework deleted. One route to the pane now, and it is the same one on a phone.
  await openMissionDetails(page, "objectives");
  await expect(page.getByTestId("objective-stale")).toContainText(/stale/i);
  await expect(page.getByText(/forge unreachable since 14:22/)).toBeVisible();
  // The objective is NOT met — nothing is marked on data the server could not fetch.
  await expect(page.getByTestId("objective")).toContainText(/pending/i);
});

// ==============================================================================================
// Both themes, at runtime. The board on #878 is dark-only, so it evidences neither the light
// palette nor the contrast the tokens actually produce.
//
// **The ASSERTIONS are the evidence; the screenshot is an aid.** A capture proves nothing on its
// own — nobody diffs it — and under this repo's default `list` reporter it is not even retained
// (only the HTML reporter persists attachments, so locally the image is taken and dropped). What
// actually holds the line is checked below: the body paints its own background, and the rail's
// text is not the colour it sits on, in EITHER theme. That is exactly what a token defined only
// inside a `@media (prefers-color-scheme: dark)` block fails, and exactly what a screenshot
// review would wave through.
// ==============================================================================================

for (const theme of ["dark", "light"] as const) {
  test(`the console renders in the ${theme} theme with a painted background and readable text`, async ({
    page,
  }, testInfo) => {
    // Seed the DEVICE choice before first paint — the device cache wins over `/api/config`, and
    // seeding after load would race the reconcile.
    await page.addInitScript((t) => {
      localStorage.setItem("tr-theme", t);
    }, theme);
    await stub(page);
    await mockMissions(page, {
      missions: missionList([HELD_ROW]),
      mission: { ...HELD, events: [], events_next_seq: null },
    });
    await page.setViewportSize({ width: 1400, height: 900 });
    await page.goto("/mission");
    await page.waitForLoadState("networkidle");
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    await expect(page.getByTestId("mission-console")).toBeVisible();

    // The body must paint its OWN background. A transparent one borrows whatever is behind it,
    // which is how a light-theme page ends up with dark-theme text on a white ground.
    const bg = await page.evaluate(
      () => getComputedStyle(document.body).backgroundColor,
    );
    expect(bg).not.toBe("rgba(0, 0, 0, 0)");
    expect(bg).not.toBe("transparent");

    // …and the rail's text is not the same colour as what it sits on, in EITHER theme. This is
    // the assertion a screenshot cannot make, and the one a token defined only inside a
    // `@media (prefers-color-scheme: dark)` block would fail.
    const rail = page.getByRole("navigation", { name: /missions/i });
    const contrastable = await rail.evaluate((el) => {
      const s = getComputedStyle(el);
      const row = el.querySelector<HTMLElement>('[data-testid="rail-mission"]');
      const rs = row ? getComputedStyle(row) : null;
      return { bg: s.backgroundColor, fg: rs?.color ?? "" };
    });
    expect(contrastable.fg).not.toBe("");
    expect(contrastable.fg).not.toBe(contrastable.bg);

    await testInfo.attach(`mission-console-${theme}.png`, {
      body: await page.screenshot({ fullPage: false }),
      contentType: "image/png",
    });
  });
}

// ==============================================================================================
// THE FEATURE PATHS, in a real browser (#878).
//
// Everything above asserts how the console LOOKS and what it says. These assert what it DOES:
// the mutation, the two paginations, and the scope. Each was previously covered only in jsdom,
// or not at all — and the two defects this block exists to pin (ownership derived from a paged
// list, a refresh that collapses the rail past the server's page cap) are both invisible to a
// mock that answers every list request identically.
// ==============================================================================================

/** A list mock that PAGES: `total` rows, `limit`-sized windows, honouring `offset` and
 *  `archived` — i.e. the server's actual contract rather than one canned answer. */
function pagedMissions(opts: {
  active: ReturnType<typeof missionRow>[];
  archived?: ReturnType<typeof missionRow>[];
}) {
  return (q: URLSearchParams) => {
    const set = q.get("archived") === "1" ? (opts.archived ?? []) : opts.active;
    const offset = Number(q.get("offset") ?? 0);
    // The SERVER CAP. A page is never larger than this however much the client asks for, which
    // is the behaviour that turned a "reload what is open" request into a truncation.
    const LIST_LIMIT_MAX = 200;
    const limit = Math.min(Number(q.get("limit") ?? 50), LIST_LIMIT_MAX);
    return {
      missions: set.slice(offset, offset + limit),
      total: set.length,
      limit,
      offset,
      facets: { projects: [], states: [] },
      store_error: null,
      // THE ORDERED SET THE PAGE WAS CUT FROM (#896 review 19). The server sends one on every
      // page and a stitching client requires them to agree; this fixture serves every page from
      // one unchanging array, so the digest is constant — which is exactly what "these pages
      // came from one snapshot" looks like, and what makes the re-read control's ABSENCE below
      // an assertion rather than an accident.
      snapshot: q.get("archived") === "1" ? "arch" : "live",
    };
  };
}

test("a session held by a mission the rail has NOT loaded is not offered for adoption", async ({
  page,
}) => {
  // The regression. `claude:aaa` belongs to a mission on page 2, and the rail opens on page 1 —
  // so the console has never seen its owner. Derived from the rows in memory it read as unheld
  // and was offered an ADOPT the server refuses with 409; stamped by the server it is simply
  // that mission's session.
  const active = Array.from({ length: 120 }, (_, i) =>
    missionRow({ id: `msn_${i + 1}`, title: `Mission ${i + 1}` }),
  );
  await stub(page, {
    cards: [{ ...CARD, mission_id: "msn_120" }],
  });
  await mockMissions(page, { missions: pagedMissions({ active }) });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/mission");

  await expect(page.getByTestId("rail-mission").first()).toBeVisible();
  // The rail is showing a page, not the set…
  await expect(page.getByTestId("rail-load-more")).toContainText("100 of 120");
  // …and the card owned by the unloaded mission is not in UNTRACKED at all.
  await expect(page.getByTestId("rail-untracked-view")).toHaveCount(0);
  await expect(page.getByTestId("rail-adopt")).toHaveCount(0);
});

test("ADOPT sends the mutation and the session leaves UNTRACKED", async ({
  page,
}) => {
  const posted: { url: string; body: string }[] = [];
  const active = [
    missionRow({ id: "msn_1", title: "Kimi transcript adapter" }),
  ];
  // After the adopt the server holds the session — the list says so on the refetch, which is
  // what the console reloads for.
  let held = false;

  await stub(page, { cards: [{ ...CARD, mission_id: null }] });
  await mockMissions(page, {
    missions: (q: URLSearchParams) =>
      pagedMissions({
        active: held
          ? [missionRow({ id: "msn_1", session_keys: ["claude:aaa"] })]
          : active,
      })(q),
  });
  // AFTER `mockMissions`: Playwright matches the most recently registered route first, and its
  // catch-all `**/api/missions**` also matches this URL.
  await page.route("**/api/missions/*/adopt", async (r) => {
    posted.push({ url: r.request().url(), body: r.request().postData() ?? "" });
    held = true;
    return r.fulfill({
      json: {
        ...MISSION,
        sessions: [{ session_key: "claude:aaa", removed_at: null }],
      },
    });
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/mission");

  // The console opens on a MISSION; ADOPT lives with the session, so the untracked view is
  // where it is offered.
  await page.getByTestId("rail-untracked-view").click();
  const adopt = page.getByTestId("rail-adopt");
  await expect(adopt).toBeEnabled();
  await adopt.click();

  // The mutation actually went, at the selected mission, naming the session.
  await expect.poll(() => posted.length).toBe(1);
  expect(posted[0].url).toContain("/api/missions/msn_1/adopt");
  expect(posted[0].body).toContain("claude:aaa");
  // And the row leaves UNTRACKED rather than sitting there looking adoptable.
  await expect(page.getByTestId("rail-adopt")).toHaveCount(0);
});

test("a refresh after an adopt keeps every page the operator opened, past the server's cap", async ({
  page,
}) => {
  // 250 missions: more than the server will EVER return in one page (`LIST_LIMIT_MAX = 200`).
  // The console asked for `missions.length` in a single request, so once three pages were open
  // the refresh that follows an adopt silently replaced 250 rows with the first 200 — the later
  // pages vanished from the rail, which looks exactly like the missions being gone.
  const active = Array.from({ length: 250 }, (_, i) =>
    missionRow({ id: `msn_${i + 1}`, title: `Mission ${i + 1}` }),
  );
  // Count the LIST requests, so the assertion can wait for the refresh to actually land. Without
  // this the check runs against the pre-refresh rail and passes on a truncating reload — the
  // race that made an earlier version of this test green against the very bug it names.
  const listReqs: string[] = [];
  await stub(page, { cards: [{ ...CARD, mission_id: null }] });
  await mockMissions(page, {
    missions: (q: URLSearchParams) => {
      listReqs.push(q.toString());
      return pagedMissions({ active })(q);
    },
  });
  await page.route("**/api/missions/*/adopt", (r) =>
    r.fulfill({
      json: {
        ...MISSION,
        sessions: [{ session_key: "claude:aaa", removed_at: null }],
      },
    }),
  );
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/mission");

  const more = page.getByTestId("rail-load-more");
  await expect(more).toContainText("100 of 250");
  await more.click();
  await expect(more).toContainText("200 of 250");
  await more.click();
  await expect(page.getByTestId("rail-mission")).toHaveCount(250);

  // The adopt triggers the refresh. Every opened page must come back.
  const before = listReqs.length;
  await page.getByTestId("rail-untracked-view").click();
  await page.getByTestId("rail-adopt").click();

  // Wait for the refresh to be OVER, not merely started: three opened pages is three requests,
  // and asserting before they land tests the rail as it was rather than as the refresh left it.
  await expect
    .poll(() => listReqs.length, { timeout: 10_000 })
    .toBeGreaterThan(before);
  await expect
    .poll(
      async () => {
        const n = listReqs.length;
        await page.waitForTimeout(300);
        return listReqs.length === n;
      },
      { timeout: 10_000 },
    )
    .toBe(true);

  await expect(page.getByTestId("rail-mission")).toHaveCount(250);
  await expect(page.getByTestId("rail-mission").last()).toContainText(
    "Mission 250",
  );
  // …AND PAGING IS NOT ITSELF A DEFECT (#896 review 18). This refresh spanned three pages that
  // AGREED, so the list is complete and no recovery is offered: the re-read exists for a read
  // that could not be proved, not for every rail past the server's one-page cap. Asserting the
  // control's ABSENCE here is what stops it becoming permanent furniture on a long install.
  await expect(page.getByTestId("rail-re-read")).toHaveCount(0);
  // One request per opened page, each within the server's cap — never one oversized ask that the
  // server silently clamps.
  for (const q of listReqs)
    expect(Number(new URLSearchParams(q).get("limit"))).toBeLessThanOrEqual(
      200,
    );
});

test("archived missions are reachable, and refuse adoption while shown", async ({
  page,
}) => {
  // Archiving is not deletion. Without a way back the console loses a mission's objectives,
  // timeline and decisions the moment it is put away.
  await stub(page, { cards: [{ ...CARD, mission_id: null }] });
  await mockMissions(page, {
    missions: pagedMissions({
      active: [missionRow({ id: "msn_1", title: "Kimi transcript adapter" })],
      archived: [
        missionRow({
          id: "msn_old",
          title: "Shipped last month",
          state: "done",
          archived_at: T - 9000,
        }),
      ],
    }),
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/mission");

  await expect(page.getByTestId("rail-mission")).toHaveCount(1);
  await expect(page.getByTestId("rail-mission")).toContainText(
    "Kimi transcript adapter",
  );

  await flipMissionScope(page);
  await expect(page.getByTestId("rail-mission")).toHaveCount(1);
  await expect(page.getByTestId("rail-mission")).toContainText(
    "Shipped last month",
  );
  await page.getByTestId("rail-untracked-view").click();
  // The server refuses every ordinary mutation on an archived mission, so the console must not
  // offer one. Disabled and SAYING WHY — never hidden, and never offered-then-409'd.
  const adopt = page.getByTestId("rail-adopt");
  await expect(adopt).toBeDisabled();
  await expect(adopt).toHaveAttribute("title", /unarchive/i);

  // …and back, which is the half a one-way filter would have shipped broken.
  await flipMissionScope(page);
  await expect(page.getByTestId("rail-mission")).toContainText(
    "Kimi transcript adapter",
  );
  await page.getByTestId("rail-untracked-view").click();
  await expect(page.getByTestId("rail-adopt")).toBeEnabled();
});

test("the timeline pages by CURSOR, and older events append rather than replace", async ({
  page,
}) => {
  const asked: (string | null)[] = [];
  const ev = (seq: number) => ({
    seq,
    at: T - seq,
    kind: "note",
    text: `event ${seq}`,
  });

  await stub(page);
  // Registration order is LAST-WINS, so the catch-all goes on FIRST and the narrower patterns
  // after it — the reverse of how it reads.
  await page.route("**/api/missions**", (r) =>
    r.fulfill({ json: missionList([missionRow({ id: "msn_1" })]) }),
  );
  await page.route(/\/api\/missions\/[^/?]+(\?|$)/, (r) => {
    // `events_before_seq` — the WIRE name. Reading a different key here would have made the
    // mock answer page one to every request, which appends the same events twice: a fixture
    // that tests the fixture.
    const before = new URL(r.request().url()).searchParams.get(
      "events_before_seq",
    );
    asked.push(before);
    return r.fulfill({
      json: before
        ? { ...MISSION, events: [ev(1)], events_next_seq: null }
        : { ...MISSION, events: [ev(3), ev(2)], events_next_seq: 2 },
    });
  });
  await page.route("**/api/missions/*/context", (r) =>
    r.fulfill({
      json: {
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
    r.fulfill({ json: { objectives: [] } }),
  );
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/mission");

  // THROUGH THE TAB (#942). This used to say "no tab click: 1440 is the widest layout mode, where
  // all three panes are on screen at once" — that mode is gone, along with the 340px track that
  // made it. The strip is the detail surface at every width, so the timeline is one press away
  // here exactly as it is at 412.
  await openMissionDetails(page, "timeline");
  await expect(page.getByTestId("timeline-row")).toHaveCount(2);
  await page.getByTestId("timeline-more").click();

  // Three rows, not one: the older page APPENDS. And the second request carried the CURSOR the
  // first page returned — never an offset, which a new event arriving between pages would shift.
  await expect(page.getByTestId("timeline-row")).toHaveCount(3);
  expect(asked).toEqual([null, "2"]);
  // Cursor exhausted, so the control goes rather than offering a page that does not exist.
  await expect(page.getByTestId("timeline-more")).toHaveCount(0);
});

test("a page still in flight cannot land in the scope the operator switched to", async ({
  page,
}) => {
  // The cross-scope race. "Load more" on the ACTIVE rail is issued, the operator switches to
  // Archived while it is still open, and the response then appends — 50 active missions land in
  // the archived rail and overwrite its total, with nothing on screen saying the two sets were
  // mixed. Clearing the list on the switch does not help: the clear happens first and the stale
  // append lands after it.
  const active = Array.from({ length: 150 }, (_, i) =>
    missionRow({ id: `msn_${i + 1}`, title: `Active ${i + 1}` }),
  );
  const archived = [
    missionRow({
      id: "msn_old",
      title: "Shipped last month",
      state: "done",
      archived_at: T - 9000,
    }),
  ];

  // Hold the SECOND active page open until the test releases it.
  let release: (() => void) | null = null;
  const held = new Promise<void>((r) => {
    release = r;
  });

  await stub(page, { cards: [] });
  await mockMissions(page, {
    missions: async (q: URLSearchParams) => {
      const isArchived = q.get("archived") === "1";
      const offset = Number(q.get("offset") ?? 0);
      // Hold the SECOND ACTIVE page open until the test releases it.
      if (!isArchived && offset > 0) await held;
      return pagedMissions({ active, archived })(q);
    },
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/mission");

  await expect(page.getByTestId("rail-load-more")).toContainText("100 of 150");
  // Issue the second page — and do NOT await it; it is held open on purpose.
  await page.getByTestId("rail-load-more").click();

  // …switch scope while it is in flight, and let the archived rail settle.
  await flipMissionScope(page);
  await expect(page.getByTestId("rail-mission")).toHaveCount(1);
  await expect(page.getByTestId("rail-mission")).toContainText(
    "Shipped last month",
  );

  // Now release the stale active page. It must be discarded, not appended.
  release?.();
  // Give it every chance to land: poll for a while, and require the rail to stay put.
  for (let i = 0; i < 6; i += 1) {
    await page.waitForTimeout(150);
    await expect(page.getByTestId("rail-mission")).toHaveCount(1);
  }
  await expect(page.getByTestId("rail-mission")).toContainText(
    "Shipped last month",
  );
  // …and the archived total was not overwritten by the active one, which would resurrect a
  // "Load more" for rows that are not in this scope.
  await expect(page.getByTestId("rail-load-more")).toHaveCount(0);
});
