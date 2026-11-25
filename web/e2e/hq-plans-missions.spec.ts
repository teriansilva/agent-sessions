/** HQ plans every mission (#2152), in a real browser, desktop and phone.
 *
 *  1. New mission → HQ: there is no New-mission form. Every entry opens HQ on a FRESH conversation
 *     (an existing conversation and a half-typed draft are untouched; `/mission/new` redirects);
 *     HQ asks for what is missing, then its draft card shows the flow it wrote (every step, who
 *     does it, skills and MINIMUM grade); nothing is created before the confirm; the mission opens
 *     with its Team; Approve plan.
 *  2. The plan card's states: no plan → Plan with HQ (bound to THAT mission), HQ unavailable →
 *     Settings → AI → HQ, and a legacy one-agent plan → read-only with its Begin.
 *
 *  The backend is mocked; every write the SPA sends is asserted.
 */
import { expect, test, type Page } from "@playwright/test";

import { openAsk, pickConversation } from "./askSidebar";
import { mockAskConversations, turnOf } from "./askStream";
import { MISSION, missionList, missionRow, mockMissions, openMissionRail } from "./mission-console";
import { TOUCH_FLOOR } from "./touchTarget";

const MID = "msn_" + "b".repeat(32);
const PID = "c".repeat(32);
const GOAL = "review the state of the MCP server and tell me what's missing";
const TITLE = "Review the MCP server";
const DIGEST = `sha256:${"9f3c41d0".repeat(8)}`;

const FLOW = {
  kind: "flow",
  source: "mission_local",
  title: "Review the MCP server",
  playbook: null,
  steps: [
    {
      title: "Assess the MCP server",
      brief: "Read the code, run its tests, list what is missing. Read-only.",
      actor: "agent",
      skills: ["code-reviewer"],
      min_grade: "Senior",
      rework: null,
    },
    {
      id: "report",
      title: "Write the findings down",
      brief: "",
      prompts: [],
      checklist: ["The report is in the project"],
      actor: "agent",
      skills: ["report-writer"],
      min_grade: "Specialist",
      memory: "none",
      outputs: [],
      distinct_from: ["Assess the MCP server"],
      rework: null,
    },
    { title: "Accept the review", brief: "", actor: "operator", skills: [], min_grade: "", rework: null },
  ],
};

function proposal(over: Record<string, unknown> = {}) {
  return {
    action_id: PID,
    kind: "operation",
    op: "mission.create_local",
    risk: "reversible",
    label: "Create a draft mission",
    verb: "Create draft",
    title: "Example Dev",
    text: `Creates a draft mission: "${TITLE}". Applies the flow HQ wrote for it, since no deployed procedure fits: the plan gets its steps and roles. Nothing runs until you approve the plan.`,
    needs: [],
    state: "pending",
    summary: "",
    reason: "",
    can_confirm: true,
    can_dismiss: true,
    content: true,
    ...over,
  };
}

const TEAM = {
  revision: 1,
  author_kind: "hq",
  team: {
    steps: [
      {
        id: "o1.assess",
        objective: "o1",
        title: "Assess the MCP server",
        actor: "agent",
        role: { skills: ["code-reviewer"], min_grade: "frontier" },
        distinct_from: [],
        assignable: true,
        assignment: null,
        unassigned: { summary: "not assembled yet", candidates: [] },
      },
      {
        id: "o1.accept",
        objective: "o1",
        title: "Accept the review",
        actor: "operator",
        role: null,
        distinct_from: [],
        assignable: false,
        assignment: null,
      },
    ],
    peak: 1,
    pool_max: 6,
    pool_note: null,
    pool_size: 3,
  },
};

function approval(approved: boolean) {
  const lines = {
    groups: [{ objective: "o1", project: "Example Dev", rows: [{ key: "procedure", text: "review (mission-local procedure)" }] }],
    bounds: "Up to 2 sessions in all, up to 1 at once (pool limit 6). One session per step run.",
    not_covered: "The floor still asks you, every time.",
    agent_steps: 2,
  };
  return {
    mission_id: MID,
    revision: 1,
    card: { ok: true, reasons: [], digest: DIGEST, ...lines },
    approval: approved
      ? {
          id: `apr-${"a".repeat(20)}`,
          revision: 1,
          digest: DIGEST,
          created_at: 1_763_000_000,
          revoked_at: null,
          revoked_reason: null,
          state: "in_force",
          reasons: [],
          agent_steps: 2,
          ...lines,
        }
      : null,
    consent: null,
    consent_offer: null,
    consent_days: { default: 7, max: 30 },
  };
}

type World = {
  configured?: boolean;
  /** The mission detail: `null` while it does not exist yet. */
  mission: Record<string, unknown> | null;
  approved: boolean;
};

async function stub(page: Page, w: World) {
  const writes: { url: string; body: Record<string, unknown> }[] = [];
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        auth_mode: "none",
        terminal_backend: "ws",
        pulse: { configured: w.configured ?? true },
        new_session_engines: ["claude"],
        onboarded: true,
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], total: 0, next_offset: null, facets: { projects: [], engines: [] } } }),
  );
  await page.route("**/api/projects**", (r) =>
    r.fulfill({ json: { projects: [{ id: "p1", name: "Example Dev", folders: ["/repo/example"] }] } }),
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
      missionList(w.mission ? [missionRow({ id: MID, title: TITLE, state: "draft" })] : []),
    team: TEAM,
  });
  await page.route(new RegExp(`/api/missions/${MID}(\\?.*)?$`), (r) =>
    w.mission
      ? r.fulfill({ json: { ...w.mission, events: [], events_next_seq: null } })
      : r.fulfill({ status: 404, json: { detail: "unknown mission" } }),
  );
  await page.route(`**/api/missions/${MID}/plan/approval`, (r) =>
    r.fulfill({ json: approval(w.approved) }),
  );
  await page.route(`**/api/missions/${MID}/plan/approve`, (r) => {
    writes.push({ url: "approve", body: r.request().postDataJSON() });
    w.approved = true;
    return r.fulfill({ json: { id: "apr", revision: 1, digest: DIGEST, created_at: 1, replayed: false } });
  });
  await page.route(`**/api/missions/${MID}/dispatch`, (r) => {
    writes.push({ url: "dispatch", body: r.request().postDataJSON() });
    return r.fulfill({ json: { state: "running", reason: "", session_key: "claude-api:a" } });
  });
  for (const tail of ["plan", "procedure"])
    await page.route(`**/api/missions/${MID}/${tail}`, (r) => {
      writes.push({ url: tail, body: r.request().postDataJSON() ?? {} });
      return r.fulfill({ status: 410, json: { detail: "HQ plans missions" } });
    });
  return writes;
}

/** A conversation the operator already had: New mission must never write into it. */
const EXIST = "ask_" + "e".repeat(32);

/** HQ's side, stubbed: a message without the goal AND the project gets HQ's question (no card);
 *  one with both gets the draft card. The card's content, and the tap. Every message is recorded
 *  with the conversation it went to. */
async function stubHq(page: Page) {
  const fake = await mockAskConversations(page, [
    {
      id: EXIST,
      created_at: 1,
      updated_at: 2,
      turns: [turnOf("what did the docs session do?", { answer: "It rewrote the install guide." })],
    },
  ]);
  const sent: (Record<string, unknown> & { cid: string })[] = [];
  await page.route("**/api/ask/conversations/*/message/stream", (r) => {
    const req = r.request().postDataJSON() as { turn_id: string; text: string };
    const id = /\/(ask_[0-9a-f]{32})\/message\/stream$/.exec(new URL(r.request().url()).pathname)![1];
    sent.push({ ...req, cid: id });
    const complete = /Example Dev/.test(req.text) && /MCP/.test(req.text);
    const turn = {
      ...turnOf(
        req.text,
        {
          answer: complete
            ? "None of Example Dev's flows is a review, so I wrote one. Confirm the card."
            : "Which project is it in, and what should the mission achieve?",
        },
        req.turn_id,
      ),
      proposals: complete ? [proposal()] : [],
      contact: "off",
    };
    const c = fake.conversations.find((x) => x.id === id)!;
    c.turns = [...c.turns, turn];
    return r.fulfill({
      status: 200,
      contentType: "application/x-ndjson",
      body: JSON.stringify({ type: "turn", status: 200, turn }) + "\n",
    });
  });
  await page.route(`**/api/ask/proposals/${PID}/content`, (r) => r.fulfill({ json: FLOW }));
  const decided: Record<string, unknown>[] = [];
  await page.route(`**/api/ask/proposals/${PID}/decide**`, (r) => {
    decided.push(r.request().postDataJSON() ?? {});
    const card = fake.conversations.flatMap((c) => c.turns).flatMap((t) => t.proposals ?? [])[0];
    Object.assign(card, {
      state: "done",
      summary: `Draft mission created (${TITLE}) with the procedure "Review the MCP server" as plan revision 1.`,
      can_confirm: false,
      can_dismiss: false,
      mission: { id: MID },
    });
    return r.fulfill({ status: 202, json: { proposal: { state: "deciding" } } });
  });
  return { sent, decided, fake };
}

/** Every mission create the BROWSER sends: there must be none (HQ's card runs it server-side). */
function watchCreates(page: Page): string[] {
  const creates: string[] = [];
  page.on("request", (req) => {
    if (req.method() === "POST" && new URL(req.url()).pathname === "/api/missions")
      creates.push(req.url());
  });
  return creates;
}

async function sendToHq(page: Page, text: string) {
  const thread = page.getByTestId("hq-thread");
  await thread.locator("textarea").fill(text);
  await thread.locator("textarea").press("Enter");
}

const DRAFT = { ...MISSION, id: MID, title: TITLE, state: "draft", plan: null, plan_revision: 1 };

test("New mission opens a FRESH HQ conversation; HQ asks what is missing; its card; Create draft; Team; Approve", async ({ page }, info) => {
  const w: World = { mission: null, approved: false };
  const writes = await stub(page, w);
  const hq = await stubHq(page);
  const creates = watchCreates(page);
  await page.goto("/mission");
  await expect(page.getByTestId("mission-landing")).toBeVisible();
  // There is no New-mission form any more: no goal box, no project picker.
  await expect(page.getByTestId("new-mission-instruction")).toHaveCount(0);
  await expect(page.getByTestId("new-mission-project")).toHaveCount(0);

  // An existing conversation, open in the HQ sidebar, with a half-typed message in its composer.
  const panel = await openAsk(page);
  await pickConversation(panel);
  await expect(panel.getByTestId("ask-turn")).toHaveCount(1);
  await panel.locator("textarea").fill("half-typed, not sent");
  await page.keyboard.press("Escape"); // close the sidebar (a drawer on a phone)
  await expect(panel).not.toHaveAttribute("data-open", "true");

  // A. New mission → HQ's page on a FRESH conversation: nothing sent, nothing created.
  const button = page.getByTestId("landing-new-mission");
  expect((await button.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await button.click();
  await expect(page).toHaveURL(/\/hq$/);
  const thread = page.getByTestId("hq-thread");
  await expect(thread).toBeVisible();
  await expect(thread.getByTestId("ask-turn")).toHaveCount(0);
  await expect(thread.locator("textarea")).toHaveValue("");
  expect(hq.sent).toEqual([]);

  // B. HQ asks for what is missing: no card yet, and still no mission.
  await sendToHq(page, "plan a new mission");
  await expect(thread.getByTestId("ask-turn")).toHaveCount(1);
  await expect(thread).toContainText("Which project is it in");
  await expect(page.getByTestId("ask-operation")).toHaveCount(0);
  await expect(page).toHaveURL(/\/hq\/c\/ask_/);
  const fresh = hq.sent[0].cid;
  expect(fresh).not.toBe(EXIST);

  // …then its draft card: the steps, who does each, min. grade.
  await sendToHq(page, `Example Dev: ${GOAL} (the MCP server)`);
  const card = page.getByTestId("ask-operation");
  await expect(card).toContainText("Create a draft mission");
  expect(hq.sent.map((m) => m.cid)).toEqual([fresh, fresh]);
  expect(hq.sent.every((m) => !("mission" in m))).toBe(true);
  await expect(card.getByTestId("ask-mission-flow-source")).toHaveText(/Written for this mission/i);
  const steps = card.getByTestId("ask-mission-flow-step");
  await expect(steps).toHaveCount(3);
  await expect(steps.nth(0)).toContainText("Assess the MCP server");
  await expect(steps.nth(0).getByTestId("ask-mission-flow-role")).toHaveText("code-reviewer · min. grade Senior");
  await expect(steps.nth(2).getByTestId("ask-mission-flow-role")).toHaveText("You");
  // Everything an agent will be told is on the card, whole (#2152 review).
  await expect(steps.nth(0).getByTestId("ask-mission-flow-brief")).toHaveText(
    "Read the code, run its tests, list what is missing. Read-only.",
  );
  await expect(steps.nth(1).getByTestId("ask-mission-flow-done")).toHaveText(
    "Done means: The report is in the project",
  );
  await expect(steps.nth(1).getByTestId("ask-mission-flow-distinct")).toHaveText(
    "Not the same specialist as: Assess the MCP server",
  );
  await page.screenshot({ path: `test-results/hq-plans-card-${info.project.name}.png`, fullPage: false });
  // C. Nothing is created before the confirm: no create from the browser, no mission listed.
  expect(creates).toEqual([]);
  expect(writes).toEqual([]);
  // The existing conversation was never written to, and keeps its one turn.
  expect(hq.fake.conversations.find((c) => c.id === EXIST)!.turns).toHaveLength(1);

  // Create draft: the tap is the only write (the server's card runner creates the draft).
  w.mission = DRAFT;
  const confirm = card.getByTestId("ask-operation-confirm");
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect.poll(() => hq.decided.length).toBe(1);
  expect(creates).toEqual([]);
  const open = page.getByTestId("ask-operation-mission");
  await expect(open).toBeVisible();
  await open.click();

  // D. The mission: HQ planned it, so the plan card steps aside; Team and Plan approval carry it.
  await expect(page.getByTestId("console-title")).toHaveText(TITLE);
  await expect(page.getByTestId("mission-approval")).toBeVisible();
  await expect(page.getByTestId("mission-plan-card")).toHaveCount(0);
  await expect(page.getByTestId("mission-begin")).toHaveCount(0);
  await expect(page.getByText("Assess the MCP server").first()).toBeVisible();
  const approve = page.getByTestId("mission-approval").getByRole("button", { name: "Approve plan" });
  await approve.click();
  await expect(page.getByTestId("mission-approval").getByTestId("approval-state")).toHaveText("APPROVED");
  expect(writes.map((x) => x.url)).toEqual(["approve"]);
  expect(writes[0].body).toMatchObject({ revision: 1, digest: DIGEST });

  // E. The draft the operator was typing in the sidebar's conversation is still there.
  const again = await openAsk(page);
  await expect(again.locator("textarea")).toHaveValue("half-typed, not sent");
  expect(creates).toEqual([]);
});

test("every New mission entry opens HQ fresh, and an old /mission/new link redirects there", async ({ page }) => {
  await stub(page, { mission: null, approved: false });
  const hq = await stubHq(page);
  const creates = watchCreates(page);
  // The rail's New mission.
  await page.goto("/mission");
  await openMissionRail(page);
  await page.getByTestId("rail-new-mission").click();
  await expect(page).toHaveURL(/\/hq$/);
  await expect(page.getByTestId("hq-thread").getByTestId("ask-turn")).toHaveCount(0);
  // The Dashboard's New menu.
  await page.goto("/dashboard");
  await page.getByRole("button", { name: "New" }).click();
  await expect(page.getByTestId("dashboard-new-mission")).toHaveAttribute("href", "/hq");
  await page.getByTestId("dashboard-new-mission").click();
  await expect(page).toHaveURL(/\/hq$/);
  await expect(page.getByTestId("hq-thread").getByTestId("ask-turn")).toHaveCount(0);
  // A bookmark of the retired New-mission page: redirected, never a form.
  await page.goto("/mission/new");
  await expect(page).toHaveURL(/\/hq$/);
  await expect(page.getByTestId("hq-thread")).toBeVisible();
  await expect(page.getByTestId("hq-thread").getByTestId("ask-turn")).toHaveCount(0);
  await expect(page.getByTestId("new-mission-instruction")).toHaveCount(0);
  expect(hq.sent).toEqual([]);
  expect(creates).toEqual([]);
});

test("a draft with no plan offers Plan with HQ, bound to THAT mission", async ({ page }, info) => {
  const writes = await stub(page, {
    mission: { ...DRAFT, plan_revision: null, plan_state: "skipped" },
    approved: false,
  });
  const hq = await stubHq(page);
  await page.goto(`/mission?m=${MID}`);
  const card = page.getByTestId("mission-plan-card");
  await expect(card).toHaveAttribute("data-plan-state", "unplanned");
  await expect(page.getByTestId("mission-plan-lead")).toHaveText("This mission has no plan");
  for (const gone of ["mission-begin", "mission-plan-manually", "mission-plan-engine", "mission-plan-review"])
    await expect(page.getByTestId(gone)).toHaveCount(0);
  const button = page.getByTestId("mission-plan-with-hq");
  expect((await button.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await card.screenshot({ path: `test-results/hq-plans-noplan-${info.project.name}.png` });
  await button.click();
  await expect(page).toHaveURL(/\/hq\/c\/ask_/);
  await expect.poll(() => hq.sent.length).toBe(1);
  expect(hq.sent[0]).toMatchObject({ text: `Plan this mission: ${TITLE}`, mission: MID });
  expect(hq.sent[0].cid).not.toBe(EXIST); // its own conversation, bound to THAT mission
  expect(writes).toEqual([]);
});

test("HQ unavailable: the card links to Settings → AI → HQ and keeps the draft", async ({ page }, info) => {
  await stub(page, {
    configured: false,
    mission: { ...DRAFT, plan_revision: null, plan_state: "skipped" },
    approved: false,
  });
  await page.goto(`/mission?m=${MID}`);
  const card = page.getByTestId("mission-plan-card");
  await expect(card).toHaveAttribute("data-plan-state", "hq-unavailable");
  await expect(page.getByTestId("mission-plan-lead")).toHaveText("HQ isn't set up to plan missions");
  await expect(page.getByTestId("mission-plan-why")).toContainText("The draft is kept");
  const link = page.getByTestId("mission-plan-hq-settings");
  expect((await link.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await card.screenshot({ path: `test-results/hq-plans-unavailable-${info.project.name}.png` });
  await link.click();
  await expect(page).toHaveURL(/\/settings\/ai-hq/);
});

test("a legacy one-agent plan is read-only and keeps its Begin", async ({ page }, info) => {
  const writes = await stub(page, {
    mission: {
      ...DRAFT,
      state: "planned",
      plan_revision: null,
      plan_state: "ready",
      objectives: [{ key: "pr", title: "A PR is open", gate: true, state: "unmet" }],
      objectives_state: "done",
      plan: {
        plan_id: "pln_1",
        mission_id: MID,
        project_id: "p1",
        cwd: "/repo/example",
        engine: "claude-api",
        engine_reason: "",
        brief: "Review the current state of the MCP server implementation.",
        created_at: 1,
        project_options: [{ id: "p1", name: "Example Dev", cwd: "/repo/example" }],
      },
    },
    approved: false,
  });
  await page.goto(`/mission?m=${MID}`);
  const card = page.getByTestId("mission-plan-card");
  await expect(card).toHaveAttribute("data-plan-state", "legacy");
  await expect(page.getByTestId("mission-plan-lead")).toContainText("One agent: claude-api in Example Dev");
  await expect(page.getByTestId("mission-plan-legacy")).toHaveText(/Legacy plan/i);
  await expect(card.locator("textarea, select")).toHaveCount(0);
  await card.screenshot({ path: `test-results/hq-plans-legacy-${info.project.name}.png` });
  const begin = page.getByTestId("mission-begin");
  await begin.click();
  await expect(page.getByTestId("mission-dispatch-confirm")).toContainText("/repo/example");
  await begin.click();
  await expect.poll(() => writes.map((x) => x.url)).toEqual(["dispatch"]);
  expect(writes[0].body).toMatchObject({ plan_id: "pln_1", expect_cwd: "/repo/example" });
});
