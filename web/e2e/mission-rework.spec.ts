/** Bounded rework on the running board (#1951 §4, frames 8 and 8b of
 *  `docs/mockups/plan-approval-1951-b/`), in a real browser, desktop and phone. A reopened step
 *  says which rework round it runs under; past the bound the review step says the rounds are used
 *  and the mission asks ONE question, with the server's own options. Server text only.
 *
 *  The harness has the same shape as mission-runner.spec.ts; the backend is mocked.
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

const ID = "msn_rework";
const TITLE = "Apply: Data analyst, Example GmbH";
const KEY = "claude-api:9a41c0de-1111-4111-8111-2c7b2c7b2c7b";
const CWD = "/home/op/work/applications";
const QUESTION = {
  seq: 41,
  question:
    "o1.review asked for changes a 4th time. The plan covers 3 rework rounds (o1.review → " +
    "o1.cv), so nothing was started and o1.cv was not sent back. Waive the review, stop following " +
    "it up, or keep it open: you can still start o1.review again yourself.",
  objective: "o1--review_ok-0123456789",
  episode: 4,
  options: [
    {
      label: "Keep it open: I'll decide",
      action: "note_answer",
      consequence:
        "Records your answer. Nothing else changes, so this may be asked again.",
      settling: false,
    },
    {
      label: "The review is not needed",
      action: "waive_objective",
      consequence:
        "Marks this objective NOT REQUIRED. The mission can finish without it.",
      settling: true,
    },
    {
      label: "Stop following it up",
      action: "stand_down_objective",
      consequence: "Stops following up on this objective. It stays unmet.",
      settling: true,
    },
  ],
};

type Server = {
  held: Set<string>;
  refused: boolean;
  started: boolean;
  exhausted?: boolean;
};

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
      step("o1.posting", "Read the posting", {
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
              }
            : null,
        rework: {
          round: 2,
          max_rounds: 3,
          exhausted: false,
          text: "rework 2 of 3",
        },
        runs: s.started ? [cvRun] : [],
      }),
      step("o1.review", "Review the application", {
        rework: s.exhausted
          ? {
              round: 4,
              max_rounds: 3,
              exhausted: true,
              text: "rework used: 3 of 3; you were asked what next",
            }
          : null,
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
        question: s.exhausted ? QUESTION : null,
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
    .screenshot({ path: `test-results/rework-${name}-${project}.png` });
}

test("a reopened step says which rework round it runs under", async ({
  page,
}, info) => {
  const s: Server = { held: new Set(), refused: false, started: true };
  await stub(page, s);
  const board = await open(page);
  const cv = board.getByTestId("step-o1.cv");
  await expect(cv.getByTestId("step-rework")).toHaveText("rework 2 of 3");
  await expect(
    board.getByTestId("step-o1.review").getByTestId("step-rework"),
  ).toHaveCount(0);
  await shot(page, "round", info.project.name);
});

test("past the bound: the review says the rounds are used, and ONE question asks what next", async ({
  page,
}, info) => {
  const s: Server = {
    held: new Set(),
    refused: false,
    started: true,
    exhausted: true,
  };
  await stub(page, s);
  const board = await open(page);
  await expect(
    board.getByTestId("step-o1.review").getByTestId("step-rework"),
  ).toHaveText("rework used: 3 of 3; you were asked what next");
  const q = page.getByTestId("mission-question");
  await expect(q).toHaveCount(1);
  await expect(q).toContainText("a 4th time");
  await expect(q).toContainText("covers 3 rework rounds");
  const opts = q.getByTestId("mission-question-option");
  await expect(opts).toHaveCount(3);
  for (let i = 0; i < 3; i++) {
    expect((await opts.nth(i).boundingBox())!.height).toBeGreaterThanOrEqual(
      TOUCH_FLOOR,
    );
  }
  await shot(page, "exhausted", info.project.name);
});
