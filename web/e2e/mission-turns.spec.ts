/** The durable turn, end to end in a real browser (#890).
 *
 * The unit tests pin the wire contract. What only a browser can show is the part this issue is
 * actually about: the transcript is the MISSION TIMELINE, so a turn survives a reload — and a
 * turn still running is found still running rather than found missing.
 */
import { expect, test, type Page } from "@playwright/test";

import { ASK_STREAM, fulfillAsk } from "./askStream";

import {
  MISSION,
  missionList,
  missionRow,
  mockMissions,
  openMissionRail,
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

function evt(seq: number, kind: string, text: string, meta: unknown = null) {
  return {
    seq,
    mission_id: "msn_1",
    at: T,
    kind,
    session_key: null,
    action_id: null,
    text,
    meta,
    settlement: null,
  };
}

async function stub(page: Page, events: unknown[]) {
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
  await page.route(/\/api\/pulse$/, (r) => r.fulfill({ json: OVERVIEW }));
  await page.route("**/api/pulse/orchestrator", (r) =>
    r.fulfill({ status: 500, json: { detail: "off" } }),
  );
  await page.route("**/api/projects**", (r) =>
    r.fulfill({ json: { projects: [] } }),
  );
  await mockMissions(page, {
    missions: missionList([missionRow({ id: "msn_1", title: "Ship it" })]),
    mission: {
      ...MISSION,
      id: "msn_1",
      title: "Ship it",
      events,
      events_next_seq: null,
    },
  });
}

async function openMission(page: Page) {
  await expect(page.getByTestId("mission-console")).toBeVisible();
  // Through the SHELL's control (#940). The console's own `☰` retired with `MissionDrawer`, so
  // `isVisible()` on it was always false and the phone's drawer never opened — leaving every
  // click below aimed at an off-canvas rail, which Playwright calls "visible" because it has a
  // box. A no-op wherever the sidebar is already a docked column.
  await openMissionRail(page);
  await page.locator('[data-testid="rail-mission"]:visible').first().click();
  // …and closed again through the one dialog the shell now owns. `rail-drawer` was
  // `MissionDrawer`'s panel; with that component deleted this matched nothing and left the drawer
  // sitting over every subsequent click.
  if (await page.getByRole("dialog").count()) {
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
  }
}

test("a turn goes to the MISSION route and its answer comes back from the timeline", async ({
  page,
}) => {
  // The whole point of the swap: the composer stops posting to `/api/pulse/ask` — which stores
  // nothing — and the transcript is what the server kept.
  const stored: unknown[] = [];
  let answered = false;
  await stub(page, []);
  await page.route(/\/api\/pulse\/ask(\/stream)?$/, (r) =>
    r.fulfill({
      status: 500,
      json: { detail: "the composer must not use this route" },
    }),
  );
  await page.route("**/api/missions/*/message", async (r) => {
    stored.push(r.request().postDataJSON());
    answered = true;
    return r.fulfill({
      json: {
        turn_id: "t1",
        state: "done",
        answer: "the branch is there",
        matches: [],
      },
    });
  });
  // Once the turn settles, the timeline carries both events — the route writes them.
  await page.route("**/api/missions/msn_1**", async (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    const u = new URL(r.request().url());
    if (u.pathname !== "/api/missions/msn_1") return r.fallback();
    return r.fulfill({
      json: {
        ...MISSION,
        id: "msn_1",
        title: "Ship it",
        events: answered
          ? [
              evt(2, "assistant_msg", "the branch is there"),
              evt(1, "operator_msg", "is it there?"),
            ]
          : [],
        events_next_seq: null,
      },
    });
  });

  await page.goto("/mission");
  await openMission(page);
  await page.getByTestId("composer-input").fill("is it there?");
  await page.getByTestId("composer-send").click();

  await expect.poll(() => stored.length).toBe(1);
  const body = stored[0] as Record<string, unknown>;
  expect(String(body.turn_id ?? "").length).toBeGreaterThan(8);
  expect(body.message).toBe("is it there?");

  // …and the thread shows the conversation, labelled, from the TIMELINE.
  await expect(page.getByTestId("mission-console")).toContainText(
    "the branch is there",
  );
  await expect(page.getByTestId("mission-console")).toContainText(
    "is it there?",
  );
});

/** Re-route the per-mission read so a RELOAD can be served a different mission body than the
 *  first paint was. Registered after `stub`, so it wins (Playwright matches most-recent). */
async function mutableMission(page: Page, next: () => unknown) {
  await page.route(/\/api\/missions\/msn_1(\?.*)?$/, (r) =>
    r.fulfill({ json: next() }),
  );
}

function missionWith(turn: unknown, events: unknown[]) {
  return {
    ...MISSION,
    id: "msn_1",
    title: "Ship it",
    events,
    events_next_seq: null,
    turn,
  };
}

test("a RELOAD during a turn finds it STILL RUNNING, from the store", async ({
  page,
}) => {
  // THE ACTUAL RELOAD. The earlier version of this test only checked the pending row the
  // component was still holding, which is the state a reload destroys — so it passed against a
  // build where the turn was purely local, which was the whole bug.
  await stub(page, []);
  let turn: unknown = null;
  let events: unknown[] = [];
  await mutableMission(page, () => missionWith(turn, events));
  await page.route("**/api/missions/*/message", (r) => {
    turn = {
      turn_id: "t1",
      state: "in_progress",
      text: "run the tests",
      delivery_error: "",
      created_at: T,
    };
    events = [evt(1, "operator_msg", "run the tests", { turn_id: "t1" })];
    return r.fulfill({
      json: { turn_id: "t1", state: "in_progress", actions: [] },
    });
  });

  await page.goto("/mission");
  await openMission(page);
  await page.getByTestId("composer-input").fill("run the tests");
  await page.getByTestId("composer-send").click();
  await expect(page.getByTestId("turn-in-progress")).toBeVisible();

  // …and now the tab really goes away and comes back.
  await page.reload();
  await openMission(page);
  await expect(page.getByTestId("turn-in-progress")).toBeVisible();
  // The operator's message is on the timeline, where the claim wrote it — and the pending row
  // does NOT repeat it. (The detail column at ≥1400px renders the same event a second time by
  // design, which is why this counts THREAD rows rather than occurrences on the page.)
  await expect(
    page.getByTestId("thread-event").filter({ hasText: "run the tests" }),
  ).toHaveCount(1);
  await expect(page.getByTestId("turn-pending")).not.toContainText(
    "run the tests",
  );

  // …AND IT DOES NOT SIT THERE. The turn settles a moment later, and the console shows the
  // answer without waiting out the 150s supervisor cadence — a model call takes seconds, and a
  // reloaded page has no outstanding request whose callback could ask again (#902 review 2,
  // finding 4).
  turn = null;
  events = [
    evt(1, "operator_msg", "run the tests", { turn_id: "t1" }),
    evt(2, "assistant_msg", "all green", { turn_id: "t1" }),
  ];
  await expect(page.getByTestId("turn-in-progress")).toHaveCount(0, {
    timeout: 30_000,
  });
  await expect(page.getByTestId("mission-console")).toContainText("all green");
});

test("a RELOAD finds an AMBIGUOUS turn, and CHECK AGAIN reuses its id", async ({
  page,
}) => {
  // `indeterminate` is the state that exists because nobody can say whether the instruction went
  // out. Losing it on reload resolves the ambiguity in the operator's favour silently, which is
  // the opposite of what the state means.
  await stub(page, []);
  const sent: unknown[] = [];
  await mutableMission(page, () =>
    missionWith(
      {
        turn_id: "t9",
        state: "indeterminate",
        text: "restart the server",
        delivery_error: "the write could not be confirmed",
        created_at: T,
      },
      [evt(1, "operator_msg", "restart the server", { turn_id: "t9" })],
    ),
  );
  await page.route("**/api/missions/*/message", (r) => {
    sent.push(r.request().postDataJSON());
    return r.fulfill({ json: { turn_id: "t9", state: "indeterminate" } });
  });

  await page.goto("/mission");
  await openMission(page);
  await expect(page.getByTestId("turn-indeterminate")).toBeVisible();
  await expect(page.getByTestId("mission-console")).toContainText(
    "could not be confirmed",
  );

  await page.getByTestId("turn-recheck").click();
  await expect.poll(() => sent.length).toBe(1);
  // THE SERVER'S id, so the route replays rather than executing a second time.
  expect((sent[0] as Record<string, unknown>).turn_id).toBe("t9");
});

test("DISMISS acknowledges the ambiguous turn on the SERVER", async ({
  page,
}) => {
  await stub(page, []);
  const acks: string[] = [];
  let turn: unknown = {
    turn_id: "t9",
    state: "indeterminate",
    text: "restart the server",
    delivery_error: "the write could not be confirmed",
    created_at: T,
  };
  await mutableMission(page, () => missionWith(turn, []));
  await page.route("**/api/missions/*/turns/*/ack", (r) => {
    acks.push(r.request().url());
    turn = null;
    return r.fulfill({ json: { turn_id: "t9", acked: true } });
  });

  await page.goto("/mission");
  await openMission(page);
  await page.getByTestId("turn-dismiss").click();
  await expect.poll(() => acks.length).toBe(1);
  expect(acks[0]).toContain("/turns/t9/ack");
  // Gone because the SERVER says it is gone — so a reload does not bring it back.
  await expect(page.getByTestId("turn-indeterminate")).toHaveCount(0);
  await page.reload();
  await openMission(page);
  await expect(page.getByTestId("turn-indeterminate")).toHaveCount(0);
});

test("the ANSWER's matched sessions are reachable from the timeline", async ({
  page,
}) => {
  // `find` and `history` answer by naming sessions. An answer naming a session the operator
  // cannot reach is half an answer, so the matches ride on the event.
  await stub(page, [
    evt(2, "assistant_msg", "two sessions touched it", {
      matches: [
        {
          id: "claude:aaa",
          title: "The ws reconnect fix",
          why: "it edited termSocket",
        },
      ],
    }),
    evt(1, "operator_msg", "which session fixed the reconnect?"),
  ]);
  await page.goto("/mission");
  await openMission(page);
  await expect(page.getByTestId("ask-match")).toContainText(
    "The ws reconnect fix",
  );
  await expect(page.getByTestId("ask-match")).toContainText(
    "it edited termSocket",
  );
  await expect(
    page.getByRole("link", { name: /jump into the ws reconnect fix/i }),
  ).toHaveAttribute("href", "/s/claude/aaa");

  // ONE ROW HERE TOO (#1058). `.matchRow` is a single sheet drawn by both this thread and `/ask`,
  // and the shape is the thing that is easy to change in one place and regress in the other. The
  // text block and the way in sit side by side, on the same line, at a desktop width.
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.waitForTimeout(150);
  const row = page.getByTestId("ask-match");
  const [bb, jb] = await Promise.all([
    row.locator("> div").first().boundingBox(),
    row.getByRole("link").boundingBox(),
  ]).then((b) => b.map((x) => x!));
  expect(jb.x).toBeGreaterThanOrEqual(bb.x + bb.width);
  expect(Math.abs(jb.y + jb.height / 2 - (bb.y + bb.height / 2))).toBeLessThan(2);
});

test("ASK stays transient and says why (#948: was the UNTRACKED view's; #1058: now /ask)", async ({
  page,
}) => {
  // The console has exactly one composer whose turns are NOT durable: the one on the new-mission
  // page, where nothing is selected and there is no mission to file a turn under. It used to be
  // the "Sessions without a mission" view's; that view is gone (#948 P3), and the landing is now
  // where this Ask lives — so the operator still has to be TOLD its answers are not kept.
  await stub(page, []);
  await page.route(ASK_STREAM, (r) =>
    fulfillAsk(r, { answer: "nothing tracked", matches: [] }),
  );
  // ASK IS `/ask` SINCE #1058 — it was the landing's second mode. The claim is unchanged and is
  // still worth making HERE, beside the mission thread's durable turns: those go to
  // `POST /api/missions/{id}/message` and live in the timeline, these do not, and the operator is
  // told which is which. Only the door moved.
  await page.goto("/ask");
  await expect(page.getByTestId("console-title")).toHaveCount(0);
  await page.getByTestId("composer-input").fill("anything?");
  await page.getByTestId("composer-send").click();
  await expect(page.getByTestId("ask-turns")).toContainText("nothing tracked");
  await expect(page.getByTestId("ask-transient")).toContainText(
    "no mission to keep them in",
  );
});
