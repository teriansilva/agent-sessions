/** Mission Control polish, P2b (#967): the plan card's states, in a real browser.
 *
 *  A new mission plans itself. The server says how that stands (`plan_state`), and the card is one line
 *  for each answer: Planning…, Plan ready with Review plan, and Couldn't plan with Plan manually.
 *
 *  Every server here is a mutable object read on each request, so a state change happens on the
 *  "server" while nobody touches the page. That is the claim under test: the card moves from planning
 *  to ready on its own, and Begin follows.
 */
import { expect, test, type Page } from "@playwright/test";
import {
  MISSION,
  missionList,
  missionRow,
  mockMissions,
  openMissionRail,
} from "./mission-console";

const T = 1_700_000_000;
const ID = "msn_plan";
const TITLE = "Fix the open acme-app bugs, one PR each";
const INSTRUCTION = "Fix the flaky upload retry and open a PR with a regression test";

const PROJECTS = [
  { id: "p1", name: "acme-app", cwd: "/repo/acme-app" },
  { id: "p2", name: "acme-docs", cwd: "/repo/acme-docs" },
];
const ENGINES = [
  { id: "claude", label: "claude" },
  { id: "codex", label: "codex" },
];

function objective(key: string, title: string, gate: boolean) {
  return {
    mission_id: ID,
    key,
    ord: 0,
    title,
    probe: "forge_pr",
    probe_args: null,
    gate,
    state: "pending",
    met_at: null,
    observed: null,
    source: "model",
  };
}
const OBJECTIVES = [
  objective("branch", "A branch exists for the fix", false),
  objective("pr", "A PR is open for the fix", true),
];

function plan(over: Record<string, unknown> = {}) {
  return {
    plan_id: "pln_1",
    mission_id: ID,
    project_id: "p1",
    cwd: "/repo/acme-app",
    engine: "claude",
    engine_reason: "it is a python repo",
    brief: INSTRUCTION,
    created_at: T,
    project_options: PROJECTS,
    engine_options: ENGINES,
    ...over,
  };
}

type Server = Record<string, unknown> & { objectives: unknown[] };

/** Everything `/mission` reads, with ONE mission whose detail is `server` as it is at that moment. */
async function stub(page: Page, server: Server) {
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
    r.fulfill({
      json: {
        projects: PROJECTS.map((p) => ({ id: p.id, name: p.name, folders: [p.cwd] })),
      },
    }),
  );
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await page.route("**/api/templates**", (r) => r.fulfill({ json: { templates: [] } }));
  await mockMissions(page, {
    missions: () =>
      missionList([
        missionRow({
          id: ID,
          title: TITLE,
          project_id: "p1",
          state: server.state,
          plan_state: server.plan_state,
        }),
      ]),
  });
  await page.route(new RegExp(`/api/missions/${ID}(\\?.*)?$`), (r) =>
    r.fulfill({
      json: {
        ...MISSION,
        id: ID,
        title: TITLE,
        instruction: INSTRUCTION,
        project_id: "p1",
        events: [],
        events_next_seq: null,
        ...server,
      },
    }),
  );
  await page.route(`**/api/missions/${ID}/objectives`, (r) =>
    r.fulfill({ json: { objectives: server.objectives } }),
  );
}

async function selectMission(page: Page) {
  await openMissionRail(page);
  await page.getByTestId("rail-mission").filter({ hasText: TITLE }).first().click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByTestId("console-title")).toHaveText(TITLE);
}

/** The requests that would PLAN or LAUNCH. A card that moves on its own sends none of them. */
function planningRequests(page: Page) {
  const seen: string[] = [];
  page.on("request", (r) => {
    const path = new URL(r.url()).pathname;
    if (r.method() !== "GET" && /\/api\/missions\/[^/]+\/(plan|dispatch)$/.test(path))
      seen.push(`${r.method()} ${path}`);
  });
  return seen;
}

/** THE CARD SITS AT THE TOP OF THE MISSION BODY (#967 P2b review).
 *
 *  The thread is bottom-anchored like a chat. P1's card was tall enough to fill the pane, so nothing
 *  showed; folded to one line, the card dropped to the bottom with the thread and left hundreds of
 *  pixels of void above it. So: the card's top is within 24px of the Details band's bottom, and the
 *  thread's own content still sits at the bottom of the pane, below the card. Measured as boxes in the
 *  browser, at whatever viewport the project runs. */
async function expectCardUnderDetails(page: Page, label: string) {
  const band = await page.getByTestId("details-toggle").boundingBox();
  const card = await page.getByTestId("mission-plan-card").boundingBox();
  expect(band, `${label}: the Details band has a box`).not.toBeNull();
  expect(card, `${label}: the plan card has a box`).not.toBeNull();
  const gap = card!.y - (band!.y + band!.height);
  expect(gap, `${label}: space between the Details band and the plan card`).toBeGreaterThanOrEqual(-1);
  expect(gap, `${label}: space between the Details band and the plan card`).toBeLessThanOrEqual(24);
  // Scoped to the thread's own scroller: the Details timeline has an empty state with the same words.
  const paneEl = page.getByTestId("pane");
  const empty = paneEl.getByText("Nothing has happened yet.");
  await expect(empty, `${label}: the thread's empty state is shown`).toBeVisible();
  const eb = (await empty.boundingBox())!;
  const pane = (await paneEl.boundingBox())!;
  expect(eb.y, `${label}: the thread sits below the card`).toBeGreaterThanOrEqual(card!.y + card!.height);
  expect(
    pane.y + pane.height - (eb.y + eb.height),
    `${label}: the thread stays anchored to the bottom of the pane`,
  ).toBeLessThanOrEqual(48);
}

async function shot(page: Page, name: string, project: string) {
  await page.getByTestId("mission-plan-card").scrollIntoViewIfNeeded();
  await page.screenshot({ path: `test-results/p2b-${name}-${project}.png` });
}

test("a created mission goes planning → planned without anyone pressing a plan button, and Begin waits for the plan AND the objectives (#967)", async ({
  page,
}, info) => {
  test.setTimeout(90_000);
  const server: Server = {
    state: "draft",
    plan_state: "pending",
    plan_generation: 1,
    plan_detail: null,
    plan: null,
    objectives: [],
    objectives_state: "pending",
  };
  const sent = planningRequests(page);
  await stub(page, server);
  await page.goto("/mission");
  await selectMission(page);

  const card = page.getByTestId("mission-plan-card");
  const lead = page.getByTestId("mission-plan-lead");
  const begin = page.getByTestId("mission-begin");

  // PLANNING: a quiet line, no fields, and Begin disabled with that line as its reason.
  await expect(card).toHaveAttribute("data-plan-state", "planning");
  await expect(lead).toContainText("Planning…");
  await expect(lead).toContainText("A plan is still being prepared.");
  await expect(card.getByRole("combobox")).toHaveCount(0);
  await expect(begin).toBeDisabled();
  await expect(begin).toHaveAccessibleDescription(/A plan is still being prepared/);
  await expectCardUnderDetails(page, "planning");
  // "planning", no ellipsis: in a chip an ellipsis reads as truncation (#967 P2b review).
  await expect(page.getByTestId("mission-state")).toHaveText("planning");
  await shot(page, "planning", info.project.name);

  // THE OBJECTIVES SETTLE FIRST. The plan is still being prepared, so Begin still waits.
  //
  // Proven, not assumed (#984 review). "No empty-checklist line" proved nothing: the planning card
  // never draws one. So wait for the client to READ a detail with the objectives settled and planning
  // still pending, and for the settled checklist to be on screen (the Details band counts it), before
  // the planner is released below.
  const settledRead = page.waitForResponse(async (r) => {
    if (r.request().method() !== "GET") return false;
    if (!new RegExp(`/api/missions/${ID}(\\?.*)?$`).test(r.url())) return false;
    const body = (await r.json().catch(() => null)) as Record<string, unknown> | null;
    return body?.objectives_state === "done" && body?.plan_state === "pending";
  });
  server.objectives = OBJECTIVES;
  server.objectives_state = "done";
  await settledRead;
  await expect(page.getByTestId("details-toggle")).toContainText("0/2 objectives met", {
    timeout: 15_000,
  });
  await expect(page.getByTestId("mission-plan-lead")).toContainText("Planning…");
  await expect(begin).toBeDisabled();
  await expect(begin).toHaveAccessibleDescription(/A plan is still being prepared/);

  // …THEN THE PLANNER SETTLES, on the server, with nobody touching the page. Only the plan's own
  // short wait can bring this in: the objectives have stopped polling, and the detail cadence is 150s.
  server.state = "planned";
  server.plan_state = "ready";
  server.plan = plan();
  await expect(card).toHaveAttribute("data-plan-state", "ready", { timeout: 20_000 });
  await expect(lead).toHaveText("Plan ready — claude in acme-app · 2 objectives");
  await expect(page.getByTestId("mission-state")).toHaveText("planned");
  await expect(begin).toBeEnabled();
  // Folded to its line; Review plan unfolds the fields.
  await expect(page.getByTestId("mission-plan-brief")).toHaveCount(0);
  await page.getByTestId("mission-plan-review").click();
  await expect(page.getByTestId("mission-plan-brief")).toHaveValue(INSTRUCTION);
  await page.getByTestId("mission-plan-review").click();
  await expectCardUnderDetails(page, "ready, folded");
  await shot(page, "ready", info.project.name);

  expect(sent, "nothing asked for a plan or a launch").toEqual([]);
});

test("while a plan is being prepared Begin is disabled and the card says why, even over a previous plan (#967)", async ({
  page,
}) => {
  // A Plan again in flight: plan A is still stored, attempt 2 is pending, and DISPATCH answers 409.
  const server: Server = {
    state: "planned",
    plan_state: "pending",
    plan_generation: 2,
    plan_detail: null,
    plan: plan(),
    objectives: OBJECTIVES,
    objectives_state: "done",
  };
  const sent = planningRequests(page);
  await stub(page, server);
  await page.goto("/mission");
  await selectMission(page);

  const card = page.getByTestId("mission-plan-card");
  await expect(card).toHaveAttribute("data-plan-state", "planning");
  await expect(card).toContainText("A plan is still being prepared.");
  const begin = page.getByTestId("mission-begin");
  await expect(begin).toBeDisabled();
  await expect(begin).toHaveAttribute("aria-describedby", "mission-start-reason");
  await expect(page.locator("#mission-start-reason")).toContainText("A plan is still being prepared.");
  // Plan again is held too: the server would refuse a second attempt while one runs.
  await page.getByTestId("mission-overflow").click();
  await expect(page.getByTestId("mission-replan")).toBeDisabled();
  await expect(page.getByTestId("mission-replan")).toHaveText(/Planning…/);
  expect(sent).toEqual([]);
});

test("with NO AI endpoint the card says it couldn't plan; Plan manually saves a first plan with no plan_id, and Begin enables (#967)", async ({
  page,
}, info) => {
  test.setTimeout(90_000);
  const server: Server = {
    state: "draft",
    plan_state: "skipped",
    plan_generation: 1,
    plan_detail: "no AI endpoint is configured, so nothing can be planned",
    plan: null,
    plan_options: { project_options: PROJECTS, engine_options: ENGINES },
    objectives: OBJECTIVES,
    objectives_state: "done",
  };
  const bodies: Record<string, unknown>[] = [];
  let refuseNext = true;
  await stub(page, server);
  await page.route(`**/api/missions/${ID}/plan`, (r) => {
    if (r.request().method() !== "PATCH")
      return r.fulfill({ status: 500, json: { detail: "only a PATCH was expected" } });
    const body = r.request().postDataJSON() as Record<string, unknown>;
    bodies.push(body);
    if (refuseNext) {
      // A project archived since the options were read: the server's own 422, in its own words.
      refuseNext = false;
      return r.fulfill({ status: 422, json: { detail: "the chosen project is archived" } });
    }
    const chosen = PROJECTS.find((p) => p.id === body.project_id)!;
    server.plan = plan({
      plan_id: "pln_manual",
      project_id: chosen.id,
      cwd: chosen.cwd,
      engine: body.engine,
      engine_reason: "",
      brief: body.brief,
    });
    server.plan_state = "ready";
    server.plan_detail = null;
    server.state = "planned";
    delete server.plan_options;
    return r.fulfill({ json: server.plan });
  });
  await page.goto("/mission");
  await selectMission(page);

  const card = page.getByTestId("mission-plan-card");
  const begin = page.getByTestId("mission-begin");
  await expect(card).toHaveAttribute("data-plan-state", "unplanned");
  await expect(page.getByTestId("mission-plan-lead")).toHaveText(
    "Couldn't plan: no AI endpoint is configured",
  );
  await expect(begin).toBeDisabled();
  await expect(begin).toHaveAccessibleDescription(/Couldn't plan: no AI endpoint is configured/);
  await expectCardUnderDetails(page, "skipped");
  await shot(page, "skipped", info.project.name);

  await page.getByTestId("mission-plan-manually").click();
  const form = page.getByTestId("mission-plan-manual");
  await expect(form).toBeVisible();
  const project = page.getByTestId("mission-manual-project");
  const engine = page.getByTestId("mission-manual-engine");
  const brief = page.getByTestId("mission-manual-brief");
  const save = page.getByTestId("mission-manual-save");
  await expect(project).toBeFocused();
  // The mission's own project and instruction are where it starts; the agent is the operator's call.
  await expect(project).toHaveValue("p1");
  await expect(brief).toHaveValue(INSTRUCTION);
  await expect(save).toBeDisabled();

  await project.selectOption("p2");
  await engine.selectOption("codex");
  await brief.fill("Fix the docs build and open a PR");
  await expect(save).toBeEnabled();
  // Every control on the card holds the 44px floor.
  for (const el of [page.getByTestId("mission-plan-manually"), project, engine, save, page.getByTestId("mission-manual-cancel")]) {
    const b = await el.boundingBox();
    expect(b!.height, "44px floor").toBeGreaterThanOrEqual(44);
  }
  await shot(page, "skipped-plan-manually", info.project.name);

  // A REFUSAL is shown inline, in the server's words, and keeps what was chosen.
  await save.click();
  const error = card.getByTestId("mission-plan-error");
  await expect(error).toHaveText("the chosen project is archived");
  await expect(engine).toHaveValue("codex");
  await expect(begin).toBeDisabled();

  // …and a second save is accepted.
  await expect(save).toBeEnabled();
  await save.click();
  await expect.poll(() => bodies.length).toBe(2);
  for (const body of bodies) {
    // THE FIRST PLAN NAMES NO PLAN: there is none, and the server accepts this shape only then.
    expect(Object.keys(body).sort()).toEqual(["brief", "engine", "project_id"]);
  }
  expect(bodies[1]).toEqual({
    project_id: "p2",
    engine: "codex",
    brief: "Fix the docs build and open a PR",
  });

  await expect(card).toHaveAttribute("data-plan-state", "ready");
  await expect(form).toHaveCount(0);
  await expect(page.getByTestId("mission-plan-lead")).toHaveText(
    "Plan ready — codex in acme-docs · 2 objectives",
  );
  await expect(page.getByTestId("mission-state")).toHaveText("planned");
  await expect(begin).toBeEnabled();
});

test("Plan manually starts on the mission's OWN project past the planner's 40-project cap, and saves it (#984 review)", async ({
  page,
}) => {
  // The operator's picker is not the model's capped list. More than 40 options, the mission's own
  // project sorting LAST: the form starts on it, the save names it, and the edit picker shows it.
  const pad = (i: number) => String(i).padStart(2, "0");
  const many = Array.from({ length: 41 }, (_, i) => ({
    id: `p${pad(i)}`,
    name: `acme-${pad(i)}`,
    cwd: `/repo/acme-${pad(i)}`,
  }));
  const own = { id: "p_last", name: "zzz-last-sorting", cwd: "/repo/zzz-last-sorting" };
  const options = [...many, own];
  const server: Server = {
    state: "draft",
    project_id: own.id,
    cwd: own.cwd,
    plan_state: "skipped",
    plan_generation: 1,
    plan_detail: "no AI endpoint is configured, so nothing can be planned",
    plan: null,
    plan_options: { project_options: options, engine_options: ENGINES },
    objectives: OBJECTIVES,
    objectives_state: "done",
  };
  const bodies: Record<string, unknown>[] = [];
  await stub(page, server);
  await page.route(`**/api/missions/${ID}/plan`, (r) => {
    if (r.request().method() !== "PATCH")
      return r.fulfill({ status: 500, json: { detail: "only a PATCH was expected" } });
    const body = r.request().postDataJSON() as Record<string, unknown>;
    bodies.push(body);
    server.plan = plan({
      plan_id: "pln_own",
      project_id: own.id,
      cwd: own.cwd,
      engine: body.engine,
      engine_reason: "",
      brief: body.brief,
      project_options: options,
    });
    server.plan_state = "ready";
    server.plan_detail = null;
    server.state = "planned";
    delete server.plan_options;
    return r.fulfill({ json: server.plan });
  });
  await page.goto("/mission");
  await selectMission(page);

  await page.getByTestId("mission-plan-manually").click();
  const project = page.getByTestId("mission-manual-project");
  // Every option is offered (plus the "Choose a project…" placeholder), and the mission's own is selected.
  await expect(project.locator("option")).toHaveCount(options.length + 1);
  await expect(project).toHaveValue(own.id);
  await page.getByTestId("mission-manual-engine").selectOption("codex");
  await page.getByTestId("mission-manual-save").click();
  await expect.poll(() => bodies.length).toBe(1);
  expect(bodies[0]).toEqual({ project_id: own.id, engine: "codex", brief: INSTRUCTION });

  await expect(page.getByTestId("mission-plan-lead")).toHaveText(
    "Plan ready — codex in zzz-last-sorting · 2 objectives",
  );
  // …and the EDIT picker shows that project, not "Choose a project…".
  await page.getByTestId("mission-plan-review").click();
  await expect(page.getByTestId("mission-plan-project")).toHaveValue(own.id);
  await expect(page.getByTestId("mission-begin")).toBeEnabled();
});

test("a FAILED plan shows the reason from plan_detail and Plan manually; Plan again stays in ⋯; its thread events render as text (#967)", async ({
  page,
}, info) => {
  const detail = "the model call failed: endpoint returned HTTP 500";
  const server: Server = {
    state: "draft",
    plan_state: "failed",
    plan_generation: 1,
    plan_detail: detail,
    plan: null,
    plan_options: { project_options: PROJECTS, engine_options: ENGINES },
    objectives: OBJECTIVES,
    objectives_state: "done",
    // The two timeline kinds P2 added. P4 draws them compactly; until then they must render as text,
    // never crash the thread and never print their meta.
    events: [
      {
        seq: 2,
        mission_id: ID,
        at: T + 20,
        kind: "plan_edit",
        session_key: null,
        action_id: null,
        text: null,
        meta: { plan_id: "pln_secret_marker", changed: ["engine", "brief"] },
        settlement: null,
      },
      {
        seq: 1,
        mission_id: ID,
        at: T + 10,
        kind: "planning",
        session_key: null,
        action_id: null,
        text: `could not plan: ${detail}`,
        meta: { outcome: "failed", generation: 1 },
        settlement: null,
      },
    ],
  };
  await stub(page, server);
  await page.goto("/mission");
  await selectMission(page);

  const card = page.getByTestId("mission-plan-card");
  await expect(card).toHaveAttribute("data-plan-state", "unplanned");
  await expect(page.getByTestId("mission-plan-lead")).toHaveText(`Couldn't plan: ${detail}`);
  await expect(page.getByTestId("mission-plan-manually")).toBeVisible();
  await expect(page.getByTestId("mission-begin")).toBeDisabled();

  const events = page.getByTestId("thread-event");
  await expect(events.filter({ hasText: "plan edited" })).toContainText("Changed: engine, brief");
  await expect(events.filter({ hasText: "could not plan:" })).toHaveCount(1);
  const threadText = (await events.allTextContents()).join("\n");
  expect(threadText).not.toContain("pln_secret_marker");
  expect(threadText).not.toContain("{");
  await shot(page, "could-not-plan", info.project.name);

  await page.getByTestId("mission-overflow").click();
  await expect(page.getByTestId("mission-replan")).toHaveText("Plan again");
  await expect(page.getByTestId("mission-replan")).toBeEnabled();
});
