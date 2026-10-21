/** Mission Control P4 (#967) and the browser half of Start again (#966 P2): the thread draws state
 *  changes as graphics, and a start that failed before anything was typed can be started again.
 *
 *  Every fixture below is the SERVER's own shape: the `state` event meta `settle_dispatch` writes
 *  (`from`, `to`, `detail`, `seed_outcome`, `teardown_confirmed`, `retry_eligible`, `message`), the
 *  `plan` / `plan_edit` / `planning` metas of `put_plan` and the planner, and the detail fields
 *  `get_mission` adds (`retry_eligible`, `seed_outcome`, `retry_reason`). Events arrive newest first,
 *  as `get_mission` orders them.
 *
 *  Eligibility is the DETAIL's, never the event's: the event's `retry_eligible` is a settle-time
 *  snapshot. The fixtures keep the two apart where that matters.
 */
import { expect, test, type Locator, type Page } from "@playwright/test";
import {
  MISSION,
  missionList,
  missionRow,
  mockMissions,
  openMissionRail,
} from "./mission-console";

const T = 1_700_000_000;
const UUID = "f48fde5a-1111-2222-3333-444444444444";
const KEY = `claude:${UUID}`;

const PLAN = {
  plan_id: "pln_2",
  mission_id: "msn_eligible",
  project_id: "p1",
  cwd: "/repo/alpha",
  engine: "claude",
  engine_reason: "it is a python repo",
  brief: "Fix the upload retry and open a PR",
  created_at: T,
  project_options: [{ id: "p1", name: "Alpha", cwd: "/repo/alpha" }],
  engine_options: [{ id: "claude", label: "Claude" }],
};
const OBJECTIVES = [
  {
    mission_id: "msn_eligible",
    key: "pr",
    ord: 0,
    title: "A PR is open",
    probe: "manual",
    probe_args: null,
    gate: true,
    state: "unmet",
    met_at: null,
    observed: null,
    source: "model",
  },
];

let seq = 0;
function ev(
  kind: string,
  text: string | null,
  meta: Record<string, unknown> | null,
  at = T,
  sessionKey: string | null = null,
) {
  seq += 1;
  return {
    seq,
    mission_id: "msn",
    at: at + seq * 60,
    kind,
    session_key: sessionKey,
    action_id: null,
    text,
    meta,
    settlement: null,
  };
}

const NOT_TYPED =
  "The session never became ready, so nothing was typed. Start again restores the plan.";
const NEVER_READY = "the session never became ready (first-paint never true)";

/** A failed-before-briefed mission: planned without an endpoint, started, failed, started again, failed
 *  again. Oldest first here; reversed when served. */
function eligibleEvents() {
  seq = 0;
  return [
    ev("operator_msg", "Fix the upload retry and open a PR", null),
    ev("planning", "no plan proposed: no AI endpoint is configured", {
      outcome: "skipped",
      generation: 1,
    }),
    ev("plan", PLAN.brief, {
      plan_id: "pln_1",
      project_id: "p1",
      engine: "claude",
      engine_reason: "",
      generation: 1,
    }),
    ev("state", null, { from: "draft", to: "planned", released: [] }),
    ev("state", null, { from: "planned", to: "dispatching", released: [] }),
    // THE OLDER FAILURE: a record, never an action.
    // PRODUCER-SHAPED (PR #986 review): `settle_dispatch` writes NO `session_key` on this event for
    // a launch that came up and failed; it carries `launch_session_key` from the dispatch record.
    ev("state", `dispatching -> failed: ${NEVER_READY}`, {
      from: "dispatching",
      to: "failed",
      detail: NEVER_READY,
      seed_outcome: "not_attempted",
      teardown_confirmed: true,
      retry_eligible: true,
      message: NOT_TYPED,
      launch_session_key: KEY,
    }),
    // …and `_orphaned_after_launch` records the teardown on a SEPARATE, keyed `session` event.
    ev("session", `the session ${KEY} was stopped`, { outcome: "stopped", stopped: true, record_kept: false }, T, KEY),
    ev("state", null, {
      from: "failed",
      to: "planned",
      released: [],
      start_again: true,
      seed_outcome: "not_attempted",
      plan_id: "pln_2",
    }),
    ev("plan_edit", null, { plan_id: "pln_2", changed: ["engine", "brief"] }),
    ev("state", null, { from: "planned", to: "dispatching", released: [] }),
    // PRODUCER-SHAPED (PR #986 review): `settle_dispatch` writes NO `session_key` on this event for
    // a launch that came up and failed; it carries `launch_session_key` from the dispatch record.
    ev("state", `dispatching -> failed: ${NEVER_READY}`, {
      from: "dispatching",
      to: "failed",
      detail: NEVER_READY,
      seed_outcome: "not_attempted",
      teardown_confirmed: true,
      retry_eligible: true,
      message: NOT_TYPED,
      launch_session_key: KEY,
    }),
    // …and `_orphaned_after_launch` records the teardown on a SEPARATE, keyed `session` event.
    ev("session", `the session ${KEY} was stopped`, { outcome: "stopped", stopped: true, record_kept: false }, T, KEY),
  ];
}

const DELIVERED_REASON = "the brief was typed, so the agent may already have acted on it";

/** A delivered-then-failed mission. The EVENT's snapshot says eligible and the DETAIL says not, so a
 *  client that read the snapshot would offer a retry the server refuses. */
function deliveredEvents() {
  seq = 0;
  return [
    ev("state", null, { from: "planned", to: "dispatching", released: [] }),
    ev("state", "dispatching -> failed: the agent exited after the brief", {
      from: "dispatching",
      to: "failed",
      detail: "the agent exited after the brief",
      seed_outcome: "delivered",
      teardown_confirmed: true,
      retry_eligible: true,
      message: "The brief was typed before the launch failed. It cannot be started again.",
      launch_session_key: KEY,
    }),
    ev("session", `the session ${KEY} was stopped`, { outcome: "stopped", stopped: true, record_kept: false }, T, KEY),
  ];
}

const QUESTION_NOTICE =
  "No question could be asked about the PR objective: the reply offered no usable options.";

/** A running mission whose supervisor could not ask a question: the one thing the server records as
 *  an `error` event (`mission_questions._notice`). Not a start failure. */
function questionEvents() {
  seq = 0;
  return [
    ev("state", null, { from: "planned", to: "dispatching", released: [] }),
    ev("state", "dispatching -> running", {
      from: "dispatching",
      to: "running",
      detail: "",
      session_key: KEY,
    }),
    ev("error", QUESTION_NOTICE, { objective: "pr" }),
  ];
}

/** A running notes-only mission, in the server's exact shapes (#1063). The checklist and adoption
 *  events carry everything in `meta` and nothing in `text`, which is what drew them as an empty
 *  "Objective" / "Session" box; the `probe` event stands in for any meta-only kind this file does not
 *  know, which must still never be an empty box. */
function checklistEvents() {
  seq = 0;
  return [
    ev("operator_msg", "review all open bugs and bring them to a viable state", null),
    ev("objective", "no checklist applied: no default checklist is set", null),
    ev("objective", "", {
      by: "instantiation",
      ops: [
        { op: "add", key: "note_1", source: "model" },
        { op: "add", key: "note_2", source: "model" },
      ],
      reopened: false,
    }),
    ev("state", "draft -> planned", { from: "draft", to: "planned", plan_id: "pln_9" }),
    ev("state", "planned -> dispatching", { from: "planned", to: "dispatching" }),
    ev("session", "", { adopted: "primary" }, T, KEY),
    ev("state", `dispatching -> running: dispatched claude in /repo/alpha`, {
      from: "dispatching",
      to: "running",
      detail: "dispatched claude in /repo/alpha",
      session_key: KEY,
    }),
    ev("objective", "", { by: "operator", ops: [{ op: "waive", key: "note_2" }], reopened: false }),
    ev("probe", "", { key: "note_1", secret: "never printed" }),
    ev("session", "", { detached: true }, T, KEY),
  ];
}

const TITLES: Record<string, string> = {
  msn_eligible: "Fix the open acme-app bugs",
  msn_delivered: "Ship the crop fix",
  msn_question: "Watch the upload retry",
  msn_checklist: "Review all open acme-app bugs",
};
const RUNNING = new Set(["msn_question", "msn_checklist"]);

type Stub = {
  posts: unknown[];
  /** What the next `POST /state` answers. */
  answer: "ok" | 409;
};

async function stub(page: Page, theme: "dark" | "light" = "dark"): Promise<Stub> {
  const s: Stub = { posts: [], answer: "ok" };
  let eligibleState: "failed" | "planned" = "failed";
  await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
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
  await page.route("**/api/projects**", (r) =>
    r.fulfill({ json: { projects: [{ id: "p1", name: "Alpha", folders: ["/repo/alpha"] }] } }),
  );
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await page.route("**/api/templates**", (r) => r.fulfill({ json: { templates: [] } }));
  await mockMissions(page, {
    missions: missionList(
      Object.keys(TITLES).map((id) =>
        missionRow({
          id,
          title: TITLES[id],
          project_id: "p1",
          state: RUNNING.has(id) ? "running" : "failed",
        }),
      ),
    ),
    objectives: { objectives: OBJECTIVES },
  });
  await page.route(/\/api\/missions\/msn_[a-z]+(\?.*)?$/, (r) => {
    const id = new URL(r.request().url()).pathname.split("/").pop()!;
    const base = {
      ...MISSION,
      id,
      title: TITLES[id],
      project_id: "p1",
      objectives: OBJECTIVES,
      objectives_state: "done",
      events_next_seq: null,
    };
    if (id === "msn_checklist") {
      return r.fulfill({
        json: {
          ...base,
          state: "running",
          plan: null,
          events: checklistEvents().reverse(),
          retry_eligible: false,
          seed_outcome: null,
          retry_reason: null,
        },
      });
    }
    if (id === "msn_question") {
      return r.fulfill({
        json: {
          ...base,
          state: "running",
          plan: null,
          events: questionEvents().reverse(),
          retry_eligible: false,
          seed_outcome: null,
          retry_reason: null,
        },
      });
    }
    if (id === "msn_delivered") {
      return r.fulfill({
        json: {
          ...base,
          state: "failed",
          outcome: "failed",
          closed_at: T,
          plan: null,
          events: deliveredEvents().reverse(),
          retry_eligible: false,
          seed_outcome: "delivered",
          retry_reason: DELIVERED_REASON,
        },
      });
    }
    const events = eligibleEvents();
    if (eligibleState === "planned") {
      events.push(
        ev("state", null, {
          from: "failed",
          to: "planned",
          released: [],
          start_again: true,
          seed_outcome: "not_attempted",
          plan_id: "pln_3",
        }),
      );
    }
    return r.fulfill({
      json:
        eligibleState === "failed"
          ? {
              ...base,
              state: "failed",
              outcome: "failed",
              closed_at: T,
              plan: null,
              events: events.reverse(),
              retry_eligible: true,
              seed_outcome: "not_attempted",
              retry_reason: null,
            }
          : {
              ...base,
              state: "planned",
              plan: { ...PLAN, plan_id: "pln_3" },
              plan_state: "ready",
              events: events.reverse(),
              retry_eligible: false,
              seed_outcome: "not_attempted",
              retry_reason: null,
            },
    });
  });
  await page.route(/\/api\/missions\/msn_[a-z]+\/state$/, async (r) => {
    s.posts.push(r.request().postDataJSON());
    if (s.answer === 409) {
      return r.fulfill({
        status: 409,
        json: {
          detail:
            "mission msn_eligible cannot be started again: a launch for this mission is still in flight or not yet accounted for",
        },
      });
    }
    eligibleState = "planned";
    return r.fulfill({ json: { ...MISSION, id: "msn_eligible", state: "planned" } });
  });
  return s;
}

async function open(page: Page, id: string) {
  await page.goto("/mission");
  await select(page, id);
}

/** Pick a mission from the rail of the page as it stands. After a reload the section opens on the
 *  new-mission page (the selection is deliberately not persisted, #948), so the mission is picked
 *  again; everything it shows then comes from the stored events alone. */
async function select(page: Page, id: string) {
  await openMissionRail(page);
  await page.getByTestId("rail-mission").filter({ hasText: TITLES[id] }).first().click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByTestId("console-title")).toHaveText(TITLES[id]);
  await expect(page.getByTestId("thread-event").first()).toBeVisible();
}

/** Every visible leaf in a row carries text: no empty text row, whatever the event left blank. */
async function emptyLeaves(row: Locator) {
  return row.evaluate((root) =>
    [...root.querySelectorAll("*")]
      .filter(
        (el) =>
          el.children.length === 0 &&
          !el.closest('[aria-hidden="true"], svg') &&
          !(el as HTMLElement).hidden &&
          getComputedStyle(el).display !== "none" &&
          (el.textContent ?? "").trim() === "",
      )
      .map((el) => el.outerHTML.slice(0, 120)),
  );
}

// ---------------------------------------------------------------------------------------------------
// State changes, plans and planning outcomes
// ---------------------------------------------------------------------------------------------------

test("a state change is a from → to chip pair: no raw kind label and no empty text row (#967)", async ({
  page,
}) => {
  await stub(page);
  await open(page, "msn_eligible");

  const rows = page.getByTestId("thread-state");
  // draft→planned, planned→dispatching, failed→planned, planned→dispatching. The two failures from
  // `dispatching` are failure blocks, not rows.
  await expect(rows).toHaveCount(4);
  const pairs = await rows.evaluateAll((els) =>
    els.map((el) => [
      el.querySelector('[data-testid="state-chip-from"]')?.textContent,
      el.querySelector('[data-testid="state-chip-to"]')?.textContent,
    ]),
  );
  // Newest first, as the server orders them; `dispatching` reads "starting", as the rail says it.
  expect(pairs).toEqual([
    ["planned", "starting"],
    ["failed", "planned"],
    ["planned", "starting"],
    ["draft", "planned"],
  ]);
  for (const row of await rows.all()) {
    await expect(row.getByTestId("state-chip-from").getByTestId("state-chip-dot")).toBeAttached();
    await expect(row.getByTestId("state-chip-to").getByTestId("state-chip-dot")).toBeAttached();
    expect(await emptyLeaves(row)).toEqual([]);
  }
  // No row anywhere in the thread is labelled with the raw kind.
  const thread = page.getByTestId("pane");
  await expect(thread.getByText(/^(state|plan|plan_edit|planning)$/i)).toHaveCount(0);
  for (const row of await page.getByTestId("thread-event").all()) {
    expect(await emptyLeaves(row)).toEqual([]);
  }

  // The operator's message is a message; the state change is not.
  await expect(thread.getByRole("article")).toHaveCount(1);
  await expect(thread.getByRole("article")).toContainText("Fix the upload retry and open a PR");
  await expect(thread.getByRole("group", { name: /^State change: draft to planned/ })).toHaveCount(1);
});

test("a checklist or session change reads as a sentence, and no meta-only event is an empty box (#1063)", async ({
  page,
}) => {
  await stub(page);
  await open(page, "msn_checklist");

  // Newest first: the operator's waiver, then the plan's two additions.
  const checklist = page.getByTestId("thread-checklist");
  await expect(checklist).toHaveCount(2);
  await expect(checklist.nth(0)).toContainText(/Checklist\s*·\s*1 waived\s*·\s*by you/);
  await expect(checklist.nth(1)).toContainText(/Checklist\s*·\s*2 added\s*·\s*from the plan/);

  const sessions = page.getByTestId("thread-session");
  await expect(sessions).toHaveCount(2);
  await expect(sessions.nth(0)).toContainText("Session released");
  await expect(sessions.nth(1)).toContainText("Session adopted as primary");
  await expect(sessions.nth(1).getByRole("link", { name: KEY })).toHaveAttribute(
    "href",
    `/s/claude/${UUID}`,
  );

  // A sentence the server wrote for a person keeps its generic row, text and all.
  await expect(page.getByTestId("pane")).toContainText("no checklist applied: no default checklist is set");

  // An unknown meta-only kind is one bare line with its name — never a box over nothing, and never
  // its meta.
  await expect(page.getByTestId("thread-system-bare")).toHaveText(/^Probe/);
  await expect(page.getByTestId("pane")).not.toContainText(/never printed|note_1|note_2|[{}]/);

  // THE OLD SYMPTOM, asserted directly: a row whose whole text is its humanised kind ("Objective",
  // "Session") over a box with nothing in it. `emptyLeaves` alone would not catch that — the old
  // generic row rendered no body element at all, so it had no empty leaf to find.
  const bare = await page
    .getByTestId("thread-event")
    .evaluateAll((els) => els.map((el) => (el.textContent ?? "").trim()));
  expect(bare.filter((txt) => /^(Objective|Session)$/.test(txt))).toEqual([]);
  for (const row of await page.getByTestId("thread-event").all()) {
    expect(await emptyLeaves(row)).toEqual([]);
  }
});

test("a plan is one compact row with its brief behind Show brief; an edit names its fields; planning says what happened (#967)", async ({
  page,
}) => {
  await stub(page);
  await open(page, "msn_eligible");

  const plan = page.getByTestId("thread-plan");
  await expect(plan).toHaveCount(1);
  await expect(plan).toContainText(/Plan ready\s*·\s*Alpha\s*·\s*claude/);
  // The brief is not on screen until asked for.
  await expect(page.getByTestId("thread-plan-brief")).toHaveCount(0);
  const toggle = plan.getByRole("button", { name: /show brief/i });
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByTestId("thread-plan-brief")).toHaveText(PLAN.brief);
  // Keyboard-operable: Enter closes it again.
  await toggle.focus();
  await page.keyboard.press("Enter");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByTestId("thread-plan-brief")).toHaveCount(0);

  const edit = page.getByTestId("thread-plan-edit");
  await expect(edit).toHaveCount(1);
  await expect(edit).toContainText(/Plan edited\s*·\s*engine, brief/);
  await expect(edit).not.toContainText(PLAN.brief);

  const planning = page.getByTestId("thread-planning");
  await expect(planning).toHaveCount(1);
  await expect(planning).toHaveAttribute("data-outcome", "skipped");
  await expect(planning).toContainText(/no plan proposed/i);
  await expect(planning).toContainText("no AI endpoint is configured");
  // Nothing raw: no JSON, no meta keys.
  await expect(page.getByTestId("pane")).not.toContainText(/[{}]|generation|plan_id/);
});

// ---------------------------------------------------------------------------------------------------
// The failure block and Start again (#966 P2)
// ---------------------------------------------------------------------------------------------------

test("a failed-before-briefed mission shows Start again; clicking it returns the mission to planned (#966)", async ({
  page,
}, info) => {
  const s = await stub(page);
  await open(page, "msn_eligible");

  const blocks = page.getByTestId("thread-failure");
  await expect(blocks).toHaveCount(2);
  const latest = blocks.first();
  const older = blocks.nth(1);

  // Header: the chips. Body: the plain-language message, then the technical detail in mono.
  await expect(latest.getByTestId("state-chip-from")).toHaveText("starting");
  await expect(latest.getByTestId("state-chip-to")).toHaveText("failed");
  await expect(latest.getByTestId("thread-failure-message")).toHaveText(NOT_TYPED);
  await expect(latest.getByTestId("thread-failure-detail")).toHaveText(NEVER_READY);

  // ONLY THE LATEST FAILURE OFFERS ACTIONS.
  await expect(older.locator("button, a")).toHaveCount(0);

  // BOTH ENTRY POINTS, one function.
  const block = latest.getByTestId("thread-start-again");
  const header = page.getByTestId("mission-start-again");
  await expect(block).toBeVisible();
  await expect(header).toBeVisible();
  await expect(page.getByTestId("mission-reopen")).toHaveCount(0);
  const log = latest.getByTestId("thread-open-session");
  await expect(log).toHaveText(/Open session log/);
  await expect(log).toHaveAttribute("href", `/s/claude/${UUID}`);
  await expect(latest.getByTestId("thread-why-no-retry")).toHaveCount(0);

  // AFTER A RELOAD the link is still there, read from the stored events alone (PR #986 review).
  await page.reload();
  await select(page, "msn_eligible");
  await expect(log).toHaveText(/Open session log/);
  await expect(log).toHaveAttribute("href", `/s/claude/${UUID}`);
  await expect(older.locator("button, a")).toHaveCount(0);

  // THE BORDER IS THE STATUS HUE, 3px, computed in this browser.
  const [border, down] = await latest.evaluate((el) => {
    const probe = document.createElement("span");
    probe.style.color = "var(--status-down)";
    document.body.appendChild(probe);
    const want = getComputedStyle(probe).color;
    probe.remove();
    const s = getComputedStyle(el);
    return [[s.borderLeftWidth, s.borderLeftStyle, s.borderLeftColor], want];
  });
  expect(border).toEqual(["3px", "solid", down]);

  if (info.project.name === "mobile") {
    // Phones: 44px floor, and Start again and the log link are full width.
    const acts = await latest.getByTestId("thread-failure-actions").boundingBox();
    for (const b of [block, log]) {
      const bb = (await b.boundingBox())!;
      expect(bb.height).toBeGreaterThanOrEqual(44);
      expect(Math.abs(bb.width - acts!.width)).toBeLessThan(1.5);
    }
  } else {
    for (const b of [block, log]) expect((await b.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  }

  await block.click();
  await expect.poll(() => s.posts.length).toBe(1);
  expect(s.posts[0]).toEqual({ from: "failed", to: "planned" });

  // The re-read lands: planned, and Begin behaves as for any planned mission.
  await expect(page.getByTestId("mission-state")).toHaveText("planned");
  await expect(page.getByTestId("mission-begin")).toBeVisible();
  await expect(page.getByTestId("mission-begin")).toBeEnabled();
  await expect(page.getByTestId("mission-start-again")).toHaveCount(0);
  await expect(page.getByTestId("thread-start-again")).toHaveCount(0);
  // The failures stay in the record, without actions, and the Start again change is a chip pair.
  await expect(page.getByTestId("thread-failure")).toHaveCount(2);
  await expect(page.getByTestId("thread-failure").locator("button, a")).toHaveCount(0);
  const again = page.getByTestId("thread-state").first();
  await expect(again.getByTestId("state-chip-from")).toHaveText("failed");
  await expect(again.getByTestId("state-chip-to")).toHaveText("planned");
  expect(s.posts).toHaveLength(1);
});

test("a delivered-then-failed mission does not offer Start again, anywhere; Why no retry? gives the server's reason (#966)", async ({
  page,
}, info) => {
  const s = await stub(page);
  await open(page, "msn_delivered");

  const block = page.getByTestId("thread-failure");
  await expect(block).toHaveCount(1);
  await expect(block.getByTestId("thread-failure-message")).toHaveText(
    "The brief was typed before the launch failed. It cannot be started again.",
  );
  // The event's snapshot says eligible; the detail says not. The detail decides.
  await expect(page.getByTestId("thread-start-again")).toHaveCount(0);
  await expect(page.getByTestId("mission-start-again")).toHaveCount(0);

  const openSession = block.getByTestId("thread-open-session");
  await expect(openSession).toHaveText(/^\s*Open session\s*$/);
  await expect(openSession).toHaveAttribute("href", `/s/claude/${UUID}`);

  const why = block.getByRole("button", { name: "Why no retry?" });
  await expect(why).toHaveAttribute("aria-expanded", "false");
  await expect(block.getByTestId("thread-retry-reason")).toHaveCount(0);
  await why.click();
  await expect(why).toHaveAttribute("aria-expanded", "true");
  await expect(block.getByTestId("thread-retry-reason")).toHaveText(DELIVERED_REASON);

  if (info.project.name === "mobile") {
    const acts = (await block.getByTestId("thread-failure-actions").boundingBox())!;
    const bb = (await openSession.boundingBox())!;
    expect(bb.height).toBeGreaterThanOrEqual(44);
    expect(Math.abs(bb.width - acts.width)).toBeLessThan(1.5);
    expect((await why.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  }

  // AFTER A RELOAD, still Open session, still the launched session (PR #986 review).
  await page.reload();
  await select(page, "msn_delivered");
  await expect(openSession).toHaveText(/^\s*Open session\s*$/);
  await expect(openSession).toHaveAttribute("href", `/s/claude/${UUID}`);
  await expect(page.getByTestId("thread-start-again")).toHaveCount(0);
  expect(s.posts).toHaveLength(0);
});

test("a Start again answered 409 shows the server's words and changes nothing (#966)", async ({ page }) => {
  const s = await stub(page);
  s.answer = 409;
  await open(page, "msn_eligible");

  await page.getByTestId("mission-start-again").click();
  await expect.poll(() => s.posts.length).toBe(1);
  expect(s.posts[0]).toEqual({ from: "failed", to: "planned" });

  const error = page.getByTestId("thread-start-again-error");
  await expect(error).toHaveText(
    "mission msn_eligible cannot be started again: a launch for this mission is still in flight or not yet accounted for",
  );
  await expect(error).toHaveAttribute("role", "alert");
  await expect(page.getByTestId("mission-state")).toHaveText("failed");
  await expect(page.getByTestId("mission-begin")).toHaveCount(0);
  // The re-read re-enables the controls; nothing was applied optimistically.
  await expect(page.getByTestId("thread-start-again")).toBeEnabled();
  await expect(page.getByTestId("mission-start-again")).toBeEnabled();
});

/** A token's colour as this browser computes it, in the same format as a computed background. */
async function tokenColour(page: Page, name: string) {
  return page.evaluate((n) => {
    const probe = document.createElement("span");
    probe.style.backgroundColor = `var(${n})`;
    probe.style.color = `var(${n})`;
    document.body.appendChild(probe);
    const c = getComputedStyle(probe).backgroundColor;
    probe.remove();
    return c;
  }, name);
}

test("a question notice (`error`) is a compact error row, never a failed-start block; starting is neutral and running is green (#967)", async ({
  page,
}) => {
  await stub(page);
  await open(page, "msn_question");

  const row = page.getByTestId("thread-error");
  await expect(row).toHaveCount(1);
  await expect(row).toHaveAttribute("aria-label", "Error");
  await expect(row.getByTestId("thread-error-text")).toHaveText(QUESTION_NOTICE);
  await expect(row.locator("svg")).toHaveCount(1);
  // No chips, no actions, and not a live alert that re-announces.
  await expect(row.locator('[data-testid^="state-chip"], button, a')).toHaveCount(0);
  await expect(page.locator('[role="alert"]')).toHaveCount(0);
  await expect(page.getByTestId("thread-failure")).toHaveCount(0);
  await expect(page.getByTestId("thread-start-again")).toHaveCount(0);
  await expect(page.getByTestId("mission-start-again")).toHaveCount(0);

  const danger = await page.evaluate(() => {
    const probe = document.createElement("span");
    probe.style.color = "var(--danger-text)";
    document.body.appendChild(probe);
    const c = getComputedStyle(probe).color;
    probe.remove();
    return c;
  });
  expect(await row.getByTestId("thread-error-text").evaluate((el) => getComputedStyle(el).color)).toBe(
    danger,
  );

  // STATUS COLOUR IS LOAD-BEARING (§3): starting is the neutral --text-2 dot, running is green.
  const textTwo = await tokenColour(page, "--text-2");
  const up = await tokenColour(page, "--status-up");
  expect(textTwo).not.toBe(up);
  const dots = await page.getByTestId("thread-state").evaluateAll((els) =>
    els.map((el) =>
      ["state-chip-from", "state-chip-to"].map((id) => [
        el.querySelector(`[data-testid="${id}"]`)?.textContent,
        getComputedStyle(el.querySelector(`[data-testid="${id}"] [data-testid="state-chip-dot"]`)!)
          .backgroundColor,
      ]),
    ),
  );
  // Newest first: starting → running, then planned → starting.
  expect(dots[0]).toEqual([
    ["starting", textTwo],
    ["running", up],
  ]);
  expect(dots[1][1]).toEqual(["starting", textTwo]);
  // The header chip reads the same helper.
  await expect(page.getByTestId("mission-state")).toHaveText("running");
  expect(
    await page.getByTestId("mission-state-dot").evaluate((el) => getComputedStyle(el).backgroundColor),
  ).toBe(up);
});
