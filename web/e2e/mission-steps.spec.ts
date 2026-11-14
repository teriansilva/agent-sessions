/** The running board (#1581 S4a), in a real browser, desktop and phone.
 *
 *  Start a ready step: the revision and the folder the board showed are what is sent. Stop a
 *  running step: a requested stop, shown `stopped` only when the server says so, and `unknown`
 *  when it could not prove it. Redirect sends one message into the step's own session. A
 *  `paused: budget` hold offers to raise the cap. The backend is mocked; every write the SPA sends
 *  is asserted, and every state on screen is the server's.
 */
import { expect, test, type Page } from "@playwright/test";
import {
  MISSION,
  missionList,
  missionRow,
  mockMissions,
  openMissionRail,
} from "./mission-console";
import { TOUCH_FLOOR } from "./touchTarget";

const ID = "msn_steps";
const TITLE = "Apply: Data analyst, Example GmbH";
const KEY = "claude-api:9a41c0de-1111-4111-8111-2c7b2c7b2c7b";
const CWD = "/home/op/work/applications";

type Run = Record<string, unknown>;
type Server = {
  postingRun: Run | null;
  cvRun: Run | null;
  hold: boolean | "operator";
  turns: number;
  stopAnswer: "stopped" | "unknown";
  /** No team yet (every agent step unassigned). */
  unassigned?: boolean;
  /** Stop mission answers with this launch still settling. */
  stopPending?: boolean;
  /** A Stop is recorded (it outlives a budget raise). */
  stopping?: boolean;
};

function run(step: string, state: string, over: Run = {}): Run {
  return {
    step_id: step,
    episode: 1,
    revision: 4,
    state,
    session_key: KEY,
    engine: "claude-api",
    model: "sonnet",
    effort: null,
    use_kind: "primary",
    alternate_index: null,
    use_reason: "",
    project_id: "p-synthetic01",
    cwd: CWD,
    authority: "operator",
    detail: null,
    created_at: 1_763_000_000,
    updated_at: 1_763_000_000,
    ended_at: null,
    specialist: {
      id: step === "o1.cv" ? "cv-writer" : "career-archivist",
      version: 2,
    },
    ...over,
  };
}

function step(id: string, title: string, over: Record<string, unknown> = {}) {
  return {
    id,
    title,
    actor: "agent",
    objective: "o1",
    after: [],
    derived: "waiting",
    assignment: {
      specialist: { id: "career-archivist", version: 2 },
      override: null,
      reason: "fits",
      alternates: [],
      notes: [],
    },
    cwd: CWD,
    can_start: false,
    why_not: "waiting for an earlier step",
    runs: [],
    ...over,
  };
}

function view(s: Server) {
  const postingOpen =
    s.postingRun && ["running", "unknown"].includes(String(s.postingRun.state));
  return {
    mission_id: ID,
    mission_state: "running",
    revision: 4,
    concurrency: { open: (postingOpen ? 1 : 0) + (s.cvRun ? 1 : 0), cap: 2 },
    budget: {
      turns: { used: s.turns, cap: 60 },
      seconds: { used: 4320, cap: 14_400 },
      units: "turns and time",
      estimated: true,
      revision: 1,
      hold: s.hold === "operator" ? "operator" : s.hold ? "budget" : null,
      hold_detail:
        s.hold === "operator"
          ? "paused by you"
          : s.hold
            ? "60 of its 60 turns are used"
            : null,
      hold_at: s.hold ? 1_763_000_500 : null,
      offer: { turns_cap: s.hold === true ? 90 : 60, seconds_cap: 14_400 },
      stopping: !!s.stopping,
    },
    steps: [
      step("o1.intake", "Intake", {
        actor: "operator",
        derived: "done",
        assignment: null,
        cwd: null,
        why_not: "the operator's step",
      }),
      step("o1.posting", "Read the posting", {
        derived: "ready",
        ...(s.unassigned ? { assignment: null } : {}),
        // The server's verdict: a hold refuses a start (`view_sync`), and the board shows it.
        can_start: !s.postingRun && !s.hold && !s.unassigned,
        why_not: s.unassigned
          ? "no specialist assigned"
          : s.postingRun
            ? "already running"
            : s.hold === "operator"
              ? "the mission is paused"
              : s.hold
                ? "the budget is spent"
                : null,
        runs: s.postingRun ? [s.postingRun] : [],
      }),
      step("o1.cv", "Tailor the CV", {
        after: ["o1.posting"],
        ...(s.unassigned ? { assignment: null } : {}),
        runs: s.cvRun ? [s.cvRun] : [],
      }),
    ],
  };
}

async function stub(page: Page, s: Server) {
  const writes: { url: string; body: Record<string, unknown> }[] = [];
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
    r.fulfill({ json: { projects: [] } }),
  );
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({
      json: { notifications: [], unread: 0, uncertain: 0, settled: [] },
    }),
  );
  await page.route("**/api/templates**", (r) =>
    r.fulfill({ json: { templates: [] } }),
  );
  await mockMissions(page, {
    missions: () =>
      missionList([missionRow({ id: ID, title: TITLE, state: "running" })]),
  });
  await page.route(new RegExp(`/api/missions/${ID}(\\?.*)?$`), (r) =>
    r.fulfill({
      json: {
        ...MISSION,
        id: ID,
        title: TITLE,
        state: "running",
        events: [],
        events_next_seq: null,
      },
    }),
  );
  await page.route(`**/api/missions/${ID}/steps`, (r) =>
    r.fulfill({ json: view(s) }),
  );
  const write = (
    glob: string,
    apply: (body: Record<string, unknown>) => unknown,
  ) =>
    page.route(glob, (r) => {
      const body = r.request().postDataJSON() as Record<string, unknown>;
      writes.push({ url: new URL(r.request().url()).pathname, body });
      return r.fulfill({ json: apply(body) });
    });
  await write(`**/api/missions/${ID}/steps/o1.posting/start`, () => {
    s.postingRun = run("o1.posting", "running");
    return {
      outcome: "started",
      reason: "",
      state: "running",
      session_key: KEY,
      run: s.postingRun,
    };
  });
  await write(`**/api/missions/${ID}/steps/o1.posting/stop`, () => {
    s.postingRun = run("o1.posting", s.stopAnswer, {
      detail:
        s.stopAnswer === "stopped"
          ? "the operator stopped it"
          : "the session could not be proved stopped",
    });
    return { run: s.postingRun };
  });
  await write(`**/api/missions/${ID}/budget`, (body) => {
    s.hold = false;
    return {
      budget: {
        ...view(s).budget,
        turns: { used: s.turns, cap: body.turns_cap },
        revision: 2,
      },
    };
  });
  await write("**/api/structured/sessions/**/turns", () => ({
    state: "running",
    revision: 3,
  }));
  await write(`**/api/missions/${ID}/steps/pause`, () => {
    s.hold = "operator";
    return { budget: view(s).budget, interrupted: [], not_interrupted: [] };
  });
  await write(`**/api/missions/${ID}/steps/continue`, () => {
    s.hold = false;
    s.stopping = false;
    return { budget: view(s).budget };
  });
  await write(`**/api/missions/${ID}/steps/stop-all`, () => {
    s.hold = "operator";
    if (s.stopPending) {
      s.postingRun = run("o1.posting", "claimed", { session_key: null });
      return {
        stopped: [],
        pending: [
          {
            ...s.postingRun,
            note: "still being started; it is stopped as soon as its launch settles",
          },
        ],
      };
    }
    s.postingRun = run("o1.posting", "stopped", {
      detail: "the operator stopped it",
    });
    return { stopped: [s.postingRun], pending: [] };
  });
  return writes;
}

async function open(page: Page) {
  await page.goto("/mission");
  await openMissionRail(page);
  await page
    .getByTestId("rail-mission")
    .filter({ hasText: TITLE })
    .first()
    .click();
  await expect(page.getByTestId("console-title")).toHaveText(TITLE);
  const board = page.getByTestId("mission-steps");
  await expect(board).toBeVisible();
  return board;
}

async function shot(page: Page, name: string, project: string) {
  const vp = page.viewportSize();
  if (vp) await page.setViewportSize({ width: vp.width, height: 2400 });
  await page.getByTestId("mission-steps").scrollIntoViewIfNeeded();
  await page
    .getByTestId("mission-steps")
    .screenshot({ path: `test-results/steps-${name}-${project}.png` });
}

test("start a ready step: the revision and the folder shown are sent", async ({
  page,
}, info) => {
  const s: Server = {
    postingRun: null,
    cvRun: null,
    hold: false,
    turns: 3,
    stopAnswer: "stopped",
  };
  const writes = await stub(page, s);
  const board = await open(page);
  const posting = board.getByTestId("step-o1.posting");
  await expect(posting.getByTestId("step-state")).toHaveText("ready");
  await expect(posting).toContainText(`in ${CWD}`);
  await expect(board.getByTestId("step-o1.cv")).toContainText(
    "Waits for o1.posting.",
  );
  await expect(board.getByTestId("budget-meter")).toContainText("3 / 60 turns");
  await expect(board.getByTestId("budget-meter")).toContainText(
    "Estimated: turns and time",
  );
  const start = posting.getByRole("button", { name: "Start step" });
  expect((await start.boundingBox())!.height).toBeGreaterThanOrEqual(
    TOUCH_FLOOR,
  );
  await shot(page, "ready", info.project.name);
  await start.click();
  await expect(posting.getByTestId("step-state")).toHaveText("running");
  await expect(posting.getByTestId("step-run")).toContainText(
    "career-archivist v2",
  );
  await expect(
    posting.getByRole("link", { name: /claude-api:9a41/ }),
  ).toBeVisible();
  expect(writes).toHaveLength(1);
  expect(writes[0].url).toBe(`/api/missions/${ID}/steps/o1.posting/start`);
  expect(writes[0].body).toEqual({ revision: 4, expect_cwd: CWD });
  await shot(page, "running", info.project.name);
});

test("stop a running step: stopped only on the server's proof", async ({
  page,
}, info) => {
  const s: Server = {
    postingRun: run("o1.posting", "running"),
    cvRun: null,
    hold: false,
    turns: 4,
    stopAnswer: "unknown",
  };
  const writes = await stub(page, s);
  const board = await open(page);
  const posting = board.getByTestId("step-o1.posting");
  await posting.getByRole("button", { name: "Stop step…" }).click();
  const dialog = page.getByRole("dialog", { name: "Stop o1.posting?" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("Only when the stop is proved");
  await dialog.getByRole("button", { name: "Stop step" }).click();
  await expect(dialog).toBeHidden();
  // Not proved: the board says unknown and keeps the stop available.
  await expect(posting.getByTestId("step-state")).toHaveText("unknown");
  await expect(board.getByTestId("steps-note")).toContainText(
    "could not be proved stopped",
  );
  s.stopAnswer = "stopped";
  await posting.getByRole("button", { name: "Stop step…" }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Stop step" })
    .click();
  await expect(board.getByTestId("steps-note")).toContainText(
    "o1.posting was stopped.",
  );
  expect(writes.map((w) => w.body)).toEqual([{ episode: 1 }, { episode: 1 }]);
  await shot(page, "stopped", info.project.name);
});

test("redirect sends one message into the step's own session", async ({
  page,
}) => {
  const s: Server = {
    postingRun: run("o1.posting", "running"),
    cvRun: null,
    hold: false,
    turns: 4,
    stopAnswer: "stopped",
  };
  const writes = await stub(page, s);
  const board = await open(page);
  await board
    .getByTestId("step-o1.posting")
    .getByRole("button", { name: "Redirect…" })
    .click();
  const dialog = page.getByRole("dialog", { name: "Redirect o1.posting" });
  const send = dialog.getByRole("button", { name: "Send to o1.posting" });
  await expect(send).toBeDisabled();
  await dialog.getByLabel("Message").fill("Use the 2-page layout.");
  await send.click();
  await expect(board.getByTestId("steps-note")).toContainText(
    "Sent to o1.posting.",
  );
  expect(writes).toHaveLength(1);
  expect(writes[0].url).toBe(
    `/api/structured/sessions/${encodeURIComponent(KEY)}/turns`,
  );
  expect(writes[0].body).toMatchObject({ text: "Use the 2-page layout." });
});

test("paused: budget offers to raise the cap, and nothing else starts", async ({
  page,
}, info) => {
  const s: Server = {
    postingRun: null,
    cvRun: null,
    hold: true,
    turns: 60,
    stopAnswer: "stopped",
  };
  const writes = await stub(page, s);
  const board = await open(page);
  // Nothing else starts: Start is offered but disabled, with the server's reason.
  const posting = board.getByTestId("step-o1.posting");
  await expect(
    posting.getByRole("button", { name: "Start step" }),
  ).toBeDisabled();
  await expect(posting.getByTestId("step-why")).toContainText(
    "The budget is spent",
  );
  await expect(board.getByTestId("budget-hold")).toContainText(
    "PAUSED: BUDGET",
  );
  await expect(board.getByTestId("budget-hold")).toContainText(
    "60 of its 60 turns are used",
  );
  await shot(page, "budget", info.project.name);
  await board.getByRole("button", { name: "Raise to 90 turns" }).click();
  await expect(board.getByTestId("budget-hold")).toBeHidden();
  expect(writes).toHaveLength(1);
  expect(writes[0].body).toEqual({
    turns_cap: 90,
    seconds_cap: 14_400,
    expect_revision: 1,
  });
});

test("pause new steps, then continue", async ({ page }, info) => {
  const s: Server = {
    postingRun: run("o1.posting", "running"),
    cvRun: null,
    hold: false,
    turns: 5,
    stopAnswer: "stopped",
  };
  const writes = await stub(page, s);
  const board = await open(page);
  const pause = board.getByRole("button", { name: "Pause new steps" });
  expect((await pause.boundingBox())!.height).toBeGreaterThanOrEqual(
    TOUCH_FLOOR,
  );
  await pause.click();
  const dialog = page.getByRole("dialog", { name: "Pause new steps" });
  await expect(dialog).toContainText("No new step starts");
  const finish = dialog.getByLabel("Let them finish their current turn");
  await expect(finish).toBeChecked();
  for (const choice of await dialog.locator("label").all()) {
    expect((await choice.boundingBox())!.height).toBeGreaterThanOrEqual(
      TOUCH_FLOOR,
    );
  }
  await dialog.getByLabel(/Interrupt them now/).check();
  await dialog.getByRole("button", { name: "Pause", exact: true }).click();
  await expect(board.getByTestId("steps-paused")).toContainText("PAUSED");
  await shot(page, "paused", info.project.name);
  await board.getByRole("button", { name: "Continue" }).click();
  await expect(board.getByTestId("steps-paused")).toBeHidden();
  expect(writes.map((w) => [w.url.split("/").pop(), w.body])).toEqual([
    ["pause", { interrupt: true }],
    ["continue", {}],
  ]);
});

test("stop the mission's steps: paused, and each stop reported as proved", async ({
  page,
}) => {
  const s: Server = {
    postingRun: run("o1.posting", "running"),
    cvRun: null,
    hold: false,
    turns: 5,
    stopAnswer: "stopped",
  };
  const writes = await stub(page, s);
  const board = await open(page);
  await board.getByRole("button", { name: "Stop mission…" }).click();
  const dialog = page.getByRole("dialog", {
    name: "Stop the mission's steps?",
  });
  await expect(dialog).toContainText("only once its stop is proved");
  await dialog.getByRole("button", { name: "Stop steps" }).click();
  await expect(board.getByTestId("steps-note")).toContainText(
    "1 running step(s) stopped",
  );
  await expect(board.getByTestId("steps-paused")).toBeVisible();
  expect(writes.map((w) => w.url.split("/").pop())).toEqual(["stop-all"]);
});

test("the idle board picks up a team assembled after it loaded", async ({
  page,
}) => {
  const s: Server = {
    postingRun: null,
    cvRun: null,
    hold: false,
    turns: 0,
    stopAnswer: "stopped",
    unassigned: true,
  };
  await stub(page, s);
  const board = await open(page);
  await expect(board.getByTestId("steps-empty")).toContainText(
    "assemble the team first",
  );
  const start = board
    .getByTestId("step-o1.posting")
    .getByRole("button", { name: "Start step" });
  await expect(start).toBeDisabled();
  s.unassigned = false; // the team is assembled elsewhere (HQ, another tab)
  await expect(start).toBeEnabled({ timeout: 25_000 });
  await expect(board.getByTestId("steps-empty")).toBeHidden();
});

test("a stop that could not finish says so, never 'stopped'", async ({
  page,
}) => {
  const s: Server = {
    postingRun: run("o1.posting", "running"),
    cvRun: null,
    hold: false,
    turns: 5,
    stopAnswer: "stopped",
    stopPending: true,
  };
  await stub(page, s);
  const board = await open(page);
  await board.getByRole("button", { name: "Stop mission…" }).click();
  await page
    .getByRole("dialog", { name: "Stop the mission's steps?" })
    .getByRole("button", { name: "Stop steps" })
    .click();
  const note = board.getByTestId("steps-note");
  await expect(note).toContainText("The stop is not finished");
  await expect(note).toContainText("1 still being started (o1.posting)");
  await expect(note).not.toContainText("running step(s) stopped");
});

test("a recorded stop stays named after the budget is raised, and Continue cancels it", async ({
  page,
}) => {
  const s: Server = {
    postingRun: null,
    cvRun: null,
    hold: false,
    turns: 5,
    stopAnswer: "stopped",
    stopping: true,
  };
  const writes = await stub(page, s);
  const board = await open(page);
  await expect(board.getByTestId("steps-paused")).toContainText("STOPPING");
  await board.getByRole("button", { name: "Continue" }).click();
  expect(writes.map((w) => w.url.split("/").pop())).toEqual(["continue"]);
});
