/** Objectives: drag to reorder, and every row action behind ⋯ (#967 P3), in a real browser.
 *
 * jsdom cannot say anything about the parts of this that break: whether a pointer or a finger on
 * the handle actually moves the row, whether a swipe that starts on the title scrolls instead of
 * dragging (that is `touch-action`, which jsdom does not implement), where the menu lands, and how
 * big a hit target really is. So these assert on the REQUEST the app sends and on the order it then
 * paints, never on a DOM proxy for either.
 *
 * The wire contract they pin: a reorder of any kind — pointer, touch, keyboard, or Move up / Move
 * down in the menu — is ONE `PATCH /objectives` carrying `{op: "reorder", keys: [...]}` with every
 * key, because `_op_reorder` replaces the whole order and refuses anything less.
 */
import { expect, test, type Page } from "@playwright/test";

import {
  MISSION,
  missionList,
  missionRow,
  mockMissions,
  openMissionDetails,
  openMissionRail,
  openObjectiveMenu,
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

/** `mission_supervisor.may_nudge`'s sentence for a mission holding no session. */
const NO_SESSION = "this mission holds no session, so there is nothing to nudge";
const SPENT = "the 3-nudge budget for this episode is spent";

const TITLES: Record<string, string> = {
  branch: "A branch exists for each bug fix",
  pr: "A PR is open for each bug fix",
  checks: "Checks are green on each bug fix PR",
  review: "Each bug fix PR has been reviewed",
};
const FOUR = ["branch", "pr", "checks", "review"];

interface Server {
  /** The order the store holds. A successful reorder replaces it, as the route does. */
  order: string[];
  titles: Record<string, string>;
  patches: unknown[];
  /** Answer the next PATCH with this status instead of applying it. */
  refuse: number | null;
  /** …and what the list became underneath while the operator was dragging. */
  underneath: string[] | null;
  /** Hold the PATCH open until this resolves, so the optimistic order can be observed. */
  hold: Promise<void> | null;
}

function objectiveRows(s: Server) {
  return s.order.map((key, i) => ({
    mission_id: "msn_1",
    key,
    ord: i,
    title: s.titles[key] ?? key,
    probe: "manual",
    probe_args: null,
    gate: key === "checks",
    state: "open",
    met_at: null,
    observed: null,
    source: "test",
  }));
}

function reading(key: string, title: string, why: string) {
  return {
    key,
    title,
    gate: key === "checks",
    state: "open",
    met: false,
    episode: 1,
    stood_down: false,
    awaiting_answer: false,
    spent: 0,
    remaining: 3,
    may_nudge: why === "",
    unreadable: false,
    indeterminate: false,
    live: 0,
    terminal: false,
    why_not: why,
  };
}

async function setup(
  page: Page,
  opts: {
    keys?: string[];
    titles?: Record<string, string>;
    /** The mission holds no session: every reading carries the no-session sentence. */
    noSession?: boolean;
    /** Override one row's sentence. */
    why?: Record<string, string>;
    /** The mission's state. A `done` mission is a finished record: read-only, no ⋯, no handle. */
    state?: string;
  } = {},
): Promise<Server> {
  const keys = opts.keys ?? FOUR;
  const titles = opts.titles ?? TITLES;
  const server: Server = {
    order: [...keys],
    titles,
    patches: [],
    refuse: null,
    underneath: null,
    hold: null,
  };
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
  await page.route("**/api/projects**", (r) =>
    r.fulfill({ json: { projects: [] } }),
  );

  const held = !opts.noSession;
  const supervisor = {
    objectives: keys.map((k) =>
      reading(
        k,
        titles[k] ?? k,
        opts.why?.[k] ?? (opts.noSession ? NO_SESSION : ""),
      ),
    ),
    likely_done: false,
    unmet_gates: 1,
    held_sessions: held ? 1 : 0,
    no_session: !held,
    checked_at: T - 30,
  };
  await mockMissions(page, {
    missions: missionList([
      missionRow({
        session_keys: held ? ["claude:aaa"] : [],
        state: opts.state ?? "running",
      }),
    ]),
    mission: {
      ...MISSION,
      state: opts.state ?? "running",
      ...(opts.state === "done" ? { outcome: "done", closed_at: T - 60 } : {}),
      sessions: held ? [{ session_key: "claude:aaa", removed_at: null }] : [],
      events: [],
      events_next_seq: null,
      supervisor,
    },
  });
  // Registered AFTER mockMissions, so it wins for the objectives URL (most-recent-first).
  await page.route("**/api/missions/*/objectives", async (r) => {
    if (r.request().method() === "PATCH") {
      const body = r.request().postDataJSON() as {
        ops: { op: string; keys?: string[] }[];
      };
      server.patches.push(body);
      if (server.hold) await server.hold;
      if (server.refuse !== null) {
        const status = server.refuse;
        server.refuse = null;
        if (server.underneath) server.order = server.underneath;
        return r.fulfill({
          status,
          json: { detail: "the objective list changed; re-read it and try again" },
        });
      }
      for (const op of body.ops) {
        if (op.op === "reorder" && op.keys) server.order = [...op.keys];
      }
      return r.fulfill({ json: { objectives: objectiveRows(server) } });
    }
    return r.fulfill({ json: { objectives: objectiveRows(server) } });
  });
  return server;
}

/** Open the mission and its OBJECTIVES section, on whatever viewport the project supplies. */
async function openObjectives(page: Page, count: number) {
  await page.goto("/mission");
  await expect(page.getByTestId("mission-console")).toBeVisible();
  await openMissionRail(page);
  await page.locator('[data-testid="rail-mission"]:visible').first().click();
  if (await page.getByRole("dialog").count()) {
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
  }
  await expect(page.getByTestId("mission-state")).toBeVisible();
  await openMissionDetails(page, "objectives");
  await expect(rows(page)).toHaveCount(count);
}

function rows(page: Page) {
  return page.getByTestId("objectives").getByTestId("objective");
}

async function shownOrder(page: Page): Promise<(string | null)[]> {
  return rows(page).evaluateAll((els) =>
    els.map((e) => e.getAttribute("data-key")),
  );
}

/** A box, measured where it is NOW. It never scrolls: measuring one element after scrolling another
 *  into view compares positions taken at two different scroll offsets. Callers scroll first. */
async function box(page: Page, locator: ReturnType<Page["locator"]>) {
  const b = await locator.boundingBox();
  if (!b) throw new Error("no box");
  return b;
}

/** Nothing further arrives: a drop is ONE request, not one plus a retry or a pair of swaps. */
async function exactlyOnePatch(server: Server) {
  await expect.poll(() => server.patches.length).toBe(1);
  await new Promise((r) => setTimeout(r, 600));
  expect(server.patches).toHaveLength(1);
}

// ==============================================================================================
// Dragging
// ==============================================================================================

test("pointer: dragging row 3 above row 1 sends ONE reorder with the full new order", async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.name === "mobile",
    "the phone drags with a finger — the touch test below",
  );
  const server = await setup(page);
  await openObjectives(page, 4);

  const handle = page.getByTestId("objective-handle").nth(2);
  await rows(page).nth(0).scrollIntoViewIfNeeded();
  await handle.scrollIntoViewIfNeeded();
  const h = await box(page, handle);
  const first = await box(page, rows(page).nth(0));
  const third = await box(page, rows(page).nth(2));
  const x = h.x + h.width / 2;
  const y = h.y + h.height / 2;
  // Move the row by the distance between the two row centres: its centre lands on row 1's.
  const dy = first.y + first.height / 2 - (third.y + third.height / 2);

  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x, y - 8, { steps: 4 });
  await page.mouse.move(x, y + dy, { steps: 16 });
  // Mid-drag the row is lifted and nothing has been sent yet.
  await expect(rows(page).nth(2)).toHaveAttribute("class", /objRowDragging/);
  expect(server.patches).toHaveLength(0);
  await page.mouse.up();

  await exactlyOnePatch(server);
  expect(server.patches[0]).toEqual({
    ops: [{ op: "reorder", keys: ["checks", "branch", "pr", "review"] }],
  });
  await expect
    .poll(() => shownOrder(page))
    .toEqual(["checks", "branch", "pr", "review"]);
});

test("keyboard: Space, ArrowUp, Space on a handle sends the same ONE reorder, and is announced", async ({
  page,
}) => {
  const server = await setup(page);
  await openObjectives(page, 4);

  const handle = page.getByTestId("objective-handle").nth(1);
  await handle.scrollIntoViewIfNeeded();
  await handle.focus();
  await page.keyboard.press("Space");
  await expect(handle).toHaveAttribute("aria-pressed", "true");
  await page.keyboard.press("ArrowUp");
  await page.waitForTimeout(250);
  await page.keyboard.press("Space");

  await exactlyOnePatch(server);
  expect(server.patches[0]).toEqual({
    ops: [{ op: "reorder", keys: ["pr", "branch", "checks", "review"] }],
  });
  await expect
    .poll(() => shownOrder(page))
    .toEqual(["pr", "branch", "checks", "review"]);
  // The screen-reader half: the live region says where it landed.
  await expect(page.locator('[id^="DndLiveRegion"]')).toContainText(
    `"${TITLES.pr}" was dropped at position 1 of 4`,
  );
});

/** A finger, through Chromium's own touch input: `Input.dispatchTouchEvent` goes through the same
 *  pipeline as a real touchscreen, so `touch-action` decides whether the page pans or the page's
 *  pointer handlers see the move. Playwright's `touchscreen` only taps. */
async function swipe(
  page: Page,
  from: { x: number; y: number },
  to: { x: number; y: number },
  steps = 16,
) {
  const cdp = await page.context().newCDPSession(page);
  const point = (x: number, y: number) => [{ x: Math.round(x), y: Math.round(y) }];
  await cdp.send("Input.dispatchTouchEvent", {
    type: "touchStart",
    touchPoints: point(from.x, from.y),
  });
  for (let i = 1; i <= steps; i++) {
    const t = i / steps;
    await cdp.send("Input.dispatchTouchEvent", {
      type: "touchMove",
      touchPoints: point(
        from.x + (to.x - from.x) * t,
        from.y + (to.y - from.y) * t,
      ),
    });
    await page.waitForTimeout(16);
  }
  await cdp.send("Input.dispatchTouchEvent", {
    type: "touchEnd",
    touchPoints: [],
  });
  await cdp.detach();
}

test("touch: a finger drag that starts on the handle reorders the list", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "a touch device");
  const server = await setup(page);
  await openObjectives(page, 4);

  await rows(page).nth(0).scrollIntoViewIfNeeded();
  await page.getByTestId("objective-handle").nth(2).scrollIntoViewIfNeeded();
  const h = await box(page, page.getByTestId("objective-handle").nth(2));
  const first = await box(page, rows(page).nth(0));
  const third = await box(page, rows(page).nth(2));
  const x = h.x + h.width / 2;
  const y = h.y + h.height / 2;
  const dy = first.y + first.height / 2 - (third.y + third.height / 2);
  await swipe(page, { x, y }, { x, y: y + dy });

  await exactlyOnePatch(server);
  expect(server.patches[0]).toEqual({
    ops: [{ op: "reorder", keys: ["checks", "branch", "pr", "review"] }],
  });
  await expect
    .poll(() => shownOrder(page))
    .toEqual(["checks", "branch", "pr", "review"]);
});

test("touch: a vertical swipe that starts on a row's title scrolls the column and does not drag", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "a touch device");
  // Enough rows that the column has somewhere to scroll to.
  const keys = Array.from({ length: 14 }, (_, i) => `k${i}`);
  const titles = Object.fromEntries(
    keys.map((k, i) => [k, `Objective number ${i + 1} with a title long enough to wrap`]),
  );
  const server = await setup(page, { keys, titles });
  await openObjectives(page, keys.length);

  const title = rows(page).nth(1).locator("span[title]").first();
  await title.scrollIntoViewIfNeeded();
  const scroller = await title.evaluateHandle((el) => {
    let n: HTMLElement | null = el.parentElement;
    while (n) {
      const oy = getComputedStyle(n).overflowY;
      if ((oy === "auto" || oy === "scroll") && n.scrollHeight > n.clientHeight)
        return n;
      n = n.parentElement;
    }
    return document.scrollingElement as HTMLElement;
  });
  const before = await scroller.evaluate((el) => (el as HTMLElement).scrollTop);
  const t = await box(page, title);
  const x = t.x + Math.min(t.width / 2, 120);
  const y = t.y + t.height / 2;
  await swipe(page, { x, y }, { x, y: y - 220 });
  await page.waitForTimeout(400);

  const after = await scroller.evaluate((el) => (el as HTMLElement).scrollTop);
  expect(after, "the swipe scrolled the column").toBeGreaterThan(before + 40);
  // …and it was not a drag: nothing lifted, nothing sent, the order is the server's.
  await expect(page.locator('[data-testid="objective-handle"][aria-pressed="true"]')).toHaveCount(0);
  expect(server.patches).toHaveLength(0);
  expect(await shownOrder(page)).toEqual(keys);
});

for (const status of [409, 422]) {
  test(`a reorder the server refuses (${status}) goes back to the server's order and says it did not apply`, async ({
    page,
  }) => {
    const server = await setup(page);
    await openObjectives(page, 4);

    let release = () => {};
    server.hold = new Promise<void>((r) => {
      release = r;
    });
    server.refuse = status;
    // The list changed underneath while the operator was dragging.
    server.underneath = ["review", "branch", "pr", "checks"];

    const handle = page.getByTestId("objective-handle").nth(1);
    await handle.scrollIntoViewIfNeeded();
    await handle.focus();
    await page.keyboard.press("Space");
    await expect(handle).toHaveAttribute("aria-pressed", "true");
    await page.keyboard.press("ArrowUp");
    await page.waitForTimeout(250);
    await page.keyboard.press("Space");

    // While the server has not answered, the operator sees the order they chose…
    await expect.poll(() => server.patches.length).toBe(1);
    await expect
      .poll(() => shownOrder(page))
      .toEqual(["pr", "branch", "checks", "review"]);

    // …and once it refuses, the server's order — not the one they dragged — and a sentence.
    release();
    await expect
      .poll(() => shownOrder(page))
      .toEqual(["review", "branch", "pr", "checks"]);
    await expect(page.getByTestId("objectives-reorder-refused")).toContainText(
      "did not apply",
    );
    expect(server.patches).toHaveLength(1);
  });
}

// ==============================================================================================
// The ⋯ menu
// ==============================================================================================

test("⋯ holds every row action: a popover on desktop, a bottom sheet titled 'Objective actions' on a phone", async ({
  page,
}, testInfo) => {
  const server = await setup(page, { noSession: true });
  await openObjectives(page, 4);
  const phone = testInfo.project.name === "mobile";

  const trigger = page.getByTestId("objective-menu").nth(1);
  await trigger.scrollIntoViewIfNeeded();
  const t = await box(page, trigger);
  const menu = await openObjectiveMenu(page, 1);

  const items = menu.getByRole("menuitem");
  await expect(items).toHaveText([
    "Rename",
    "Mark not required",
    /^Stand down\s*No session to nudge$/,
    "Move up",
    "Move down",
    "Remove",
  ]);
  for (const id of [
    "objective-rename",
    "objective-waive",
    "objective-stand-down",
    "objective-up",
    "objective-down",
    "objective-drop",
  ]) {
    await expect(menu.getByTestId(id)).toHaveAttribute("role", "menuitem");
  }
  // Stand down is listed, disabled, and says why.
  const stand = menu.getByTestId("objective-stand-down");
  await expect(stand).toHaveAttribute("aria-disabled", "true");
  await expect(stand).toContainText("No session to nudge");
  // Remove is danger text: the same colour `--danger-text` resolves to.
  const danger = await page.evaluate(() => {
    const probe = document.createElement("span");
    probe.style.color = "var(--danger-text)";
    document.body.appendChild(probe);
    const c = getComputedStyle(probe).color;
    probe.remove();
    return c;
  });
  const removeColor = await menu
    .getByTestId("objective-drop")
    .evaluate((el) => getComputedStyle(el).color);
  expect(removeColor).toBe(danger);
  expect(await menu.getByTestId("objective-rename").evaluate((el) => getComputedStyle(el).color)).not.toBe(danger);

  const m = await menu.boundingBox();
  if (!m) throw new Error("no menu box");
  const vp = page.viewportSize();
  if (!vp) throw new Error("no viewport");
  if (phone) {
    await expect(page.getByText("Objective actions", { exact: true })).toBeVisible();
    await expect(page.getByText("Session actions", { exact: true })).toHaveCount(0);
    await expect(menu.getByRole("button", { name: "Cancel" })).toBeVisible();
    // Pinned to the bottom of the screen, full width.
    expect(m.y + m.height).toBeGreaterThanOrEqual(vp.height - 2);
    expect(m.width).toBeGreaterThanOrEqual(vp.width - 2);
  } else {
    await expect(page.getByText("Objective actions", { exact: true })).toBeHidden();
    // Anchored to its trigger: under it, or flipped above it, right-aligned to it.
    const under = Math.abs(m.y - (t.y + t.height)) <= 8;
    const above = Math.abs(m.y + m.height - t.y) <= 8;
    expect(under || above).toBe(true);
    expect(Math.abs(m.x + m.width - (t.x + t.width))).toBeLessThanOrEqual(2);
  }

  // Move up: the same one full-order op a drag sends.
  await menu.getByTestId("objective-up").click();
  await expect(page.getByRole("menu")).toHaveCount(0);
  await expect.poll(() => server.patches.length).toBe(1);
  expect(server.patches[0]).toEqual({
    ops: [{ op: "reorder", keys: ["pr", "branch", "checks", "review"] }],
  });
  await expect
    .poll(() => shownOrder(page))
    .toEqual(["pr", "branch", "checks", "review"]);

  // Move down, from the row's new position.
  const again = await openObjectiveMenu(page, 0);
  await again.getByTestId("objective-down").click();
  await expect.poll(() => server.patches.length).toBe(2);
  expect(server.patches[1]).toEqual({
    ops: [{ op: "reorder", keys: ["branch", "pr", "checks", "review"] }],
  });
});

test("the drag handle and ⋯ are real 44×44 targets", async ({ page }) => {
  await setup(page);
  await openObjectives(page, 4);
  for (const id of ["objective-handle", "objective-menu"]) {
    const els = page.getByTestId(id);
    await expect(els).toHaveCount(4);
    for (let i = 0; i < 4; i++) {
      await els.nth(i).scrollIntoViewIfNeeded();
      const b = await box(page, els.nth(i));
      expect(b.width, `${id} ${i} width`).toBeGreaterThanOrEqual(44);
      expect(b.height, `${id} ${i} height`).toBeGreaterThanOrEqual(44);
      // The box is the interactive element itself: the point at its corner hits it.
      const hit = await els.nth(i).evaluate((el) => {
        const r = el.getBoundingClientRect();
        const at = document.elementFromPoint(r.left + 2, r.top + 2);
        return at === el || el.contains(at);
      });
      expect(hit, `${id} ${i} is hit at its corner`).toBe(true);
    }
  }
});

// ==============================================================================================
// The no-session reason, once
// ==============================================================================================

test("with no session the reason appears exactly once, above the rows", async ({
  page,
}) => {
  await setup(page, { noSession: true });
  await openObjectives(page, 4);

  const said = page.getByText(NO_SESSION, { exact: true });
  await expect(said).toHaveCount(1);
  const notice = page.getByTestId("objectives-shared-reason");
  await expect(notice).toBeVisible();
  await notice.scrollIntoViewIfNeeded();
  const n = await box(page, notice);
  const first = await box(page, rows(page).first());
  expect(n.y + n.height).toBeLessThanOrEqual(first.y + 1);
  for (let i = 0; i < 4; i++) {
    await expect(rows(page).nth(i).getByText(NO_SESSION)).toHaveCount(0);
  }
});

test("a row whose reason differs keeps its own sentence under a shared one", async ({
  page,
}) => {
  // The producer gives every row the no-session sentence today (`may_nudge` checks the roster
  // first); this pins the client's half of the rule so a row the server ever explains differently
  // is not silently folded into the shared notice.
  await setup(page, { noSession: true, why: { checks: SPENT } });
  await openObjectives(page, 4);

  await expect(page.getByText(NO_SESSION, { exact: true })).toHaveCount(1);
  await expect(page.getByTestId("objectives-shared-reason")).toHaveText(NO_SESSION);
  await expect(rows(page).nth(2).getByText(SPENT)).toBeVisible();
  await expect(page.getByText(SPENT)).toHaveCount(1);
});

// ==============================================================================================
// The full title stays reachable (Hermes on #985)
// ==============================================================================================

/** The distinctive last clause, the part a two-line clamp hides. */
const CLAUSE =
  "and the old service is retired only after the migration report is signed off";
const LONG_TITLE =
  "Every consumer of the billing API has moved to the new client, the nightly exports run from " +
  "the queue instead of cron, the dashboards and alerts point at the new metrics, the runbooks " +
  "describe the new failure modes, the on-call rotation has rehearsed a rollback, " +
  CLAUSE;

/** Where the clause is laid out, against the box the title text actually paints. A clamped line is
 *  still laid out, below the visible box, so a Range over the clause says whether it can be seen. */
async function clauseGeometry(title: ReturnType<Page["locator"]>) {
  return title.evaluate((el, clause) => {
    const node = el.firstChild as Text;
    const i = (node.textContent ?? "").indexOf(clause);
    if (i < 0) throw new Error("clause not in the title");
    const range = document.createRange();
    range.setStart(node, i);
    range.setEnd(node, i + clause.length);
    const c = range.getBoundingClientRect();
    const t = el.getBoundingClientRect();
    return {
      clauseBottom: c.bottom,
      titleBottom: t.bottom,
      scrollHeight: el.scrollHeight,
      clientHeight: el.clientHeight,
    };
  }, CLAUSE);
}

test("a finished mission's long objective can be read to its last clause, and closed again", async ({
  page,
}, testInfo) => {
  const phone = testInfo.project.name === "mobile";
  const server = await setup(page, {
    keys: ["migrate", "docs"],
    titles: { migrate: LONG_TITLE, docs: "Docs updated" },
    state: "done",
  });
  await openObjectives(page, 2);
  // READ-ONLY: no ⋯, no handle, no editor — the row Hermes found with no way to the text.
  await expect(page.getByTestId("objective-menu")).toHaveCount(0);
  await expect(page.getByTestId("objective-handle")).toHaveCount(0);

  const row = rows(page).nth(0);
  const title = row.getByTestId("objective-title");
  const toggle = row.getByTestId("objective-title-toggle");
  await toggle.scrollIntoViewIfNeeded();
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(toggle).toHaveAccessibleName(LONG_TITLE);

  // Clipped at first: the clause is laid out below the box the title paints.
  let g = await clauseGeometry(title);
  expect(g.scrollHeight).toBeGreaterThan(g.clientHeight + 1);
  expect(g.clauseBottom).toBeGreaterThan(g.titleBottom + 1);

  // A finger on the phone, the keyboard on the desktop.
  if (phone) await toggle.tap();
  else {
    await toggle.focus();
    await page.keyboard.press("Enter");
  }
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(toggle).toContainText("less");
  g = await clauseGeometry(title);
  expect(g.scrollHeight).toBeLessThanOrEqual(g.clientHeight + 1);
  expect(g.clauseBottom).toBeLessThanOrEqual(g.titleBottom + 1);
  await toggle.scrollIntoViewIfNeeded();
  const rowBox = await box(page, row);
  g = await clauseGeometry(title);
  expect(g.clauseBottom).toBeLessThanOrEqual(rowBox.y + rowBox.height + 1);
  if (phone) {
    const t = await box(page, toggle);
    expect(t.height).toBeGreaterThanOrEqual(44);
  }

  // …and closed again.
  if (phone) await toggle.tap();
  else await page.keyboard.press("Space");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  g = await clauseGeometry(title);
  expect(g.clauseBottom).toBeGreaterThan(g.titleBottom + 1);

  // A title that fits gains no control.
  await expect(rows(page).nth(1).getByTestId("objective-title-toggle")).toHaveCount(0);
  await expect(rows(page).nth(1).getByRole("button")).toHaveCount(0);
  expect(server.patches).toHaveLength(0);
});

test("on an editable row, opening a long title neither drags nor reorders", async ({
  page,
}, testInfo) => {
  const phone = testInfo.project.name === "mobile";
  const server = await setup(page, {
    keys: ["migrate", "pr", "checks"],
    titles: { migrate: LONG_TITLE, pr: TITLES.pr, checks: TITLES.checks },
  });
  await openObjectives(page, 3);
  const toggle = rows(page).nth(0).getByTestId("objective-title-toggle");
  await toggle.scrollIntoViewIfNeeded();
  if (phone) await toggle.tap();
  else await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await page.waitForTimeout(500);
  await expect(
    page.locator('[data-testid="objective-handle"][aria-pressed="true"]'),
  ).toHaveCount(0);
  expect(server.patches).toHaveLength(0);
  expect(await shownOrder(page)).toEqual(["migrate", "pr", "checks"]);
});
