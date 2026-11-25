/** The running board under a plan approval (#1951 slice B), in a real browser, desktop and phone.
 *
 *  Frames 10 and 7 of `docs/mockups/plan-approval-1951-b/`: HQ starts the ready steps under the
 *  approval (the board says so, and each run says who started it); a launch the approval refused
 *  shows as **refused** with old → new and its code, and the operator's own Start is still there;
 *  **Ask me before this step starts** holds a step, and the operator's tap is what is sent. The
 *  backend is mocked; every write the SPA sends is asserted, and every state on screen is the
 *  server's.
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

const ID = "msn_runner";
const TITLE = "Apply: Data analyst, Example GmbH";
const KEY = "claude-api:9a41c0de-1111-4111-8111-2c7b2c7b2c7b";
const CWD = "/home/op/work/applications";

type Server = {
  held: Set<string>;
  refused: boolean;
  started: boolean;
  ranOnce?: boolean;
};

/** The server's line on a step that has run already (`mission_steps.view_sync`). */
const RAN_TEXT =
  "This step has run already, and HQ starts a step only once; start it again yourself.";

function step(id: string, title: string, over: Record<string, unknown> = {}) {
  return {
    id,
    title,
    actor: "agent",
    objective: "o1",
    after: [],
    derived: "waiting",
    assignment: {
      specialist: { id: "cv-writer", version: 2 },
      override: null,
      reason: "fits",
      alternates: [],
      notes: [],
    },
    cwd: CWD,
    can_start: false,
    why_not: "waiting for an earlier step",
    held: false,
    hold_text: null,
    refusal: null,
    runs: [],
    ...over,
  };
}

function view(s: Server) {
  const postingRun = {
    step_id: "o1.posting",
    episode: 1,
    revision: 6,
    state: "running",
    session_key: KEY,
    engine: "claude-api",
    model: "sonnet",
    effort: null,
    use_kind: "primary",
    alternate_index: null,
    use_reason: "",
    project_id: "p-synthetic01",
    cwd: "/home/op/work/archive",
    authority: "approval",
    started_by: "started by HQ under your approval",
    detail: null,
    created_at: 1_763_000_000,
    updated_at: 1_763_000_000,
    ended_at: null,
    specialist: { id: "career-archivist", version: 2 },
  };
  const cvRun = {
    ...postingRun,
    step_id: "o1.cv",
    cwd: CWD,
    authority: "operator",
    started_by: "started by you",
  };
  return {
    mission_id: ID,
    title: TITLE,
    mission_state: "running",
    revision: 6,
    concurrency: { open: s.started ? 2 : 1, cap: 2 },
    budget: null,
    approval: { id: "apr-00000000000000000001", revision: 6 },
    runner:
      "HQ starts each agent step of revision 6 when it is ready, on its approved specialist, " +
      "after checking the approval again. A step you hold waits for your Start.",
    steps: [
      s.ranOnce
        ? step("o1.posting", "Read the posting", {
            derived: "ready",
            can_start: true,
            why_not: null,
            runner_done: true,
            runner_text: RAN_TEXT,
            runs: [
              {
                ...postingRun,
                state: "stopped",
                authority: "operator",
                started_by: "started by you",
                ended_at: 1_763_000_050,
              },
            ],
          })
        : step("o1.posting", "Read the posting", {
            derived: "waiting",
            why_not: "already running",
            runs: [postingRun],
          }),
      step("o1.cv", "Tailor the CV", {
        derived: "ready",
        can_start: !s.started,
        why_not: s.started ? "already running" : null,
        refusal:
          s.refused && !s.started
            ? {
                boundary: "launch",
                at: 1_763_000_100,
                codes: ["specialist_version_changed"],
                reasons: [
                  {
                    code: "specialist_version_changed",
                    text: "cv-writer changed from v2 to v3",
                    old: "v2",
                    new: "v3",
                  },
                ],
                text:
                  "Not started: the plan changed since you approved it. Nothing was reserved, " +
                  "given or started. HQ asks once per change.",
                remedy:
                  "Start the step yourself, or stop the mission: a running mission's plan can't be " +
                  "approved again.",
              }
            : null,
        runs: s.started ? [cvRun] : [],
      }),
      step("o1.review", "Review the application", {
        derived: "ready",
        can_start: true,
        why_not: null,
        held: s.held.has("o1.review"),
        hold_text: s.held.has("o1.review")
          ? "Ready, and waiting for you: you asked to be asked before it starts."
          : null,
        assignment: {
          specialist: { id: "application-reviewer", version: 1 },
          override: null,
          reason: "fits",
          alternates: [],
          notes: [],
        },
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
  await write(`**/api/missions/${ID}/steps/o1.review/hold`, (body) => {
    if (body.hold) s.held.add("o1.review");
    else s.held.delete("o1.review");
    return { step_id: "o1.review", held: !!body.hold };
  });
  await write(`**/api/missions/${ID}/steps/o1.cv/start`, () => {
    s.started = true;
    return {
      outcome: "started",
      reason: "",
      state: "running",
      session_key: KEY,
      run: null,
    };
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
    .screenshot({ path: `test-results/runner-${name}-${project}.png` });
}

test("under an approval the board says HQ starts the steps, and who started each run", async ({
  page,
}, info) => {
  const s: Server = { held: new Set(), refused: false, started: false };
  await stub(page, s);
  const board = await open(page);
  await expect(board.getByTestId("steps-under-approval")).toHaveText(
    "under your approval",
  );
  await expect(board.getByTestId("steps-runner")).toContainText(
    "HQ starts each agent step of revision 6 when it is ready",
  );
  await expect(
    board.getByTestId("step-o1.posting").getByTestId("step-run"),
  ).toContainText("started by HQ under your approval");
  await shot(page, "under-approval", info.project.name);
});

test("a launch the approval refused shows old → new, and the operator can still start it", async ({
  page,
}, info) => {
  const s: Server = { held: new Set(), refused: true, started: false };
  const writes = await stub(page, s);
  const board = await open(page);
  await expect(board.getByTestId("steps-refused")).toContainText(
    "o1.cv: Not started: the plan changed",
  );
  // A running mission's plan can't be approved again: the remedy says so (by the mission's state).
  await expect(board.getByTestId("steps-refused")).toContainText(
    "a running mission's plan can't be approved again",
  );
  const cv = board.getByTestId("step-o1.cv");
  await expect(cv.getByTestId("step-state")).toHaveText("refused");
  const line = cv.getByTestId("step-refusal");
  await expect(line).toContainText("v2");
  await expect(line).toContainText("v3");
  await expect(line).toContainText("specialist_version_changed");
  // The arrow is decoration: hidden from assistive tech, which reads "changed from … changed to".
  const arrow = line.locator('[aria-hidden="true"]');
  await expect(arrow).toHaveCount(1);
  await expect(arrow).toContainText("→");
  for (const words of ["changed from", "changed to"]) {
    const sr = line.getByText(words, { exact: true });
    await expect(sr).toHaveCount(1);
    await expect(sr).not.toHaveAttribute("aria-hidden", "true");
    const box = (await sr.boundingBox())!;
    expect(box.width <= 1 && box.height <= 1).toBe(true); // visually hidden, not display: none
  }
  await expect(line).toContainText("Nothing was reserved, given or started");
  await shot(page, "refused", info.project.name);
  const start = cv.getByRole("button", { name: "Start step" });
  expect((await start.boundingBox())!.height).toBeGreaterThanOrEqual(
    TOUCH_FLOOR,
  );
  await start.click();
  await expect(cv.getByTestId("step-state")).toHaveText("running");
  await expect(board.getByTestId("steps-refused")).toHaveCount(0);
  expect(writes).toEqual([
    {
      url: `/api/missions/${ID}/steps/o1.cv/start`,
      body: { revision: 6, expect_cwd: CWD },
    },
  ]);
});

test("ask me before this step starts: the operator's hold, sent as tapped", async ({
  page,
}, info) => {
  const s: Server = { held: new Set(), refused: false, started: false };
  const writes = await stub(page, s);
  const board = await open(page);
  const review = board.getByTestId("step-o1.review");
  const box = review.getByTestId("step-hold");
  await expect(box).not.toBeChecked();
  const label = review.getByText("Ask me before this step starts");
  expect((await label.boundingBox())!.height).toBeGreaterThanOrEqual(
    TOUCH_FLOOR,
  );
  await label.click();
  await expect(review.getByTestId("step-state")).toHaveText("held");
  await expect(review.getByTestId("step-held")).toContainText(
    "Ready, and waiting for you",
  );
  await expect(box).toBeChecked();
  await shot(page, "held", info.project.name);
  await box.click();
  await expect(box).not.toBeChecked();
  await expect(review.getByTestId("step-state")).toHaveText("ready");
  expect(writes).toEqual([
    { url: `/api/missions/${ID}/steps/o1.review/hold`, body: { hold: true } },
    { url: `/api/missions/${ID}/steps/o1.review/hold`, body: { hold: false } },
  ]);
  // A step that is running offers no hold: there is nothing left to hold.
  await expect(
    board.getByTestId("step-o1.posting").getByTestId("step-hold"),
  ).toHaveCount(0);
});

test("a step that has run already is the operator's to start again: the board says so", async ({
  page,
}, info) => {
  const s: Server = {
    held: new Set(),
    refused: false,
    started: false,
    ranOnce: true,
  };
  await stub(page, s);
  const board = await open(page);
  const posting = board.getByTestId("step-o1.posting");
  await expect(posting.getByTestId("step-runner-done")).toHaveText(RAN_TEXT);
  await expect(
    posting.getByRole("button", { name: "Start step" }),
  ).toBeVisible();
  await shot(page, "ran-once", info.project.name);
});
