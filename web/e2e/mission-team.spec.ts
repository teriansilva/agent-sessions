/** The mission's team (#1950, #1613 P5), in a real browser, desktop and phone.
 *
 *  Assemble, read the reasons and alternates, try the review on Sonnet (disabled with its reason in
 *  the list, and refused by the server when the list was stale), and accept a legal override. The
 *  backend is mocked; every write the SPA sends is asserted, and the client decides nothing: each
 *  reason, note and refusal on screen is the server's own text.
 */
import { expect, test, type Page, type Request } from "@playwright/test";
import { MISSION, missionList, missionRow, mockMissions, openMissionRail } from "./mission-console";
import { TOUCH_FLOOR } from "./touchTarget";

const ID = "msn_team";
const TITLE = "Apply: Data analyst, Example GmbH";
const ENGINE = "claude-api";

type Assign = {
  specialist: { id: string; version: number };
  override: null | { engine: string; model: string; effort: string | null };
  reason: string;
  alternates: { specialist: { id: string; version: number }; reason: string }[];
  notes: string[];
};

const ROLE = {
  posting: { skills: ["career-archive"], min_grade: "fast" },
  cv: { skills: ["cv-writing"], min_grade: "strong" },
  review: { skills: ["application-review"], min_grade: "frontier" },
  record: { skills: ["career-archive"], min_grade: "fast" },
} as const;

function step(id: string, title: string, role: (typeof ROLE)[keyof typeof ROLE] | null) {
  return {
    id: `o1.${id}`,
    objective: "o1",
    title,
    actor: role ? "agent" : "operator",
    role,
    distinct_from: id === "review" ? [{ step: "o1.cv", constraint: "specialist" }] : [],
    assignable: !!role,
  };
}

const MODEL_GRADE: Record<string, string> = { opus: "frontier", sonnet: "strong", haiku: "fast" };
const SPEC_MODEL: Record<string, string> = {
  "career-archivist": "sonnet",
  "cv-writer": "sonnet",
  "application-reviewer": "opus",
};

/** The mutable "server": the plan revision and the stored assignments. */
type Server = { revision: number; assigned: Record<string, Assign>; refuseNext: boolean };

function view(s: Server) {
  const steps = [
    step("intake", "Intake", null),
    step("posting", "Read the posting", ROLE.posting),
    step("cv", "Tailor the CV", ROLE.cv),
    step("review", "Review the application", ROLE.review),
    step("record", "Record the application", ROLE.record),
  ].map((st) => {
    if (!st.assignable) return { ...st, assignment: null };
    const a = s.assigned[st.id];
    if (!a) {
      return {
        ...st,
        assignment: null,
        unassigned: { summary: "no qualifying specialist: needs a specialist", candidates: [] },
      };
    }
    const model = a.override?.model ?? SPEC_MODEL[a.specialist.id];
    return {
      ...st,
      assignment: a,
      derived: {
        title: a.specialist.id,
        effective: { engine: ENGINE, model, effort: a.override?.effort ?? null },
        grade: MODEL_GRADE[model],
        ok: true,
        reasons: [],
        notes: [{ code: "usage_unknown", text: "remaining usage unknown; the launch cap still applies" }],
      },
    };
  });
  return {
    revision: s.revision,
    author_kind: "operator",
    team: { steps, peak: 1, pool_max: 6, pool_note: null, pool_size: 3 },
  };
}

const ASSEMBLED: Record<string, Assign> = {
  "o1.posting": pick("career-archivist", "only specialist with career-archive; Specialist ≥ Intern"),
  "o1.cv": pick("cv-writer", "only specialist with cv-writing; Specialist ≥ Specialist"),
  "o1.review": {
    ...pick(
      "application-reviewer",
      "only specialist with application-review; Senior ≥ Senior; distinct from o1.cv",
    ),
    alternates: [
      {
        specialist: { id: "review-lead", version: 2 },
        reason: "also qualifies (Senior ≥ Senior); same grade and free capacity, ordered after by id",
      },
    ],
  },
  "o1.record": pick("career-archivist", "only specialist with career-archive; Specialist ≥ Intern"),
};

function pick(id: string, reason: string): Assign {
  return { specialist: { id, version: 1 }, override: null, reason, alternates: [], notes: [] };
}

const SONNET_REFUSAL = "sonnet is Specialist, below Senior, which application-review needs";

function choices(s: Server, stepId: string, sonnetOk: boolean) {
  const need = { "o1.review": "frontier", "o1.cv": "strong" }[stepId] ?? "fast";
  const rank: Record<string, number> = { fast: 1, strong: 2, frontier: 3 };
  const models = ["opus", "sonnet", "haiku"].map((model, index) => {
    const g = MODEL_GRADE[model];
    const ok = rank[g] >= rank[need] || (model === "sonnet" && sonnetOk);
    const label = { fast: "Intern", strong: "Specialist", frontier: "Senior" }[g];
    const needL = { fast: "Intern", strong: "Specialist", frontier: "Senior" }[need];
    return {
      index,
      engine: ENGINE,
      label: "Claude — API",
      model,
      effort: null,
      grade: g,
      ok,
      reasons: ok
        ? []
        : [{ code: "grade_below_min", text: `${model} is ${label}, below ${needL}, which ${stepId === "o1.review" ? "application-review" : "cv-writing"} needs` }],
    };
  });
  return {
    revision: s.revision,
    step: stepId,
    candidates: {
      listing: `sha256:${"a".repeat(32)}`,
      choices: [
        {
          index: 0,
          specialist: s.assigned[stepId]?.specialist ?? { id: "application-reviewer", version: 1 },
          title: "x",
          engine: ENGINE,
          model: "opus",
          effort: null,
          grade: "frontier",
          ok: true,
          reasons: [],
        },
      ],
    },
    overrides: { listing: `sha256:${"b".repeat(32)}`, choices: models },
  };
}

async function stub(page: Page, s: Server, opts: { sonnetOk?: boolean } = {}) {
  const writes: Record<string, unknown>[] = [];
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
  await page.route("**/api/projects**", (r) => r.fulfill({ json: { projects: [] } }));
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await page.route("**/api/templates**", (r) => r.fulfill({ json: { templates: [] } }));
  await mockMissions(page, {
    missions: () => missionList([missionRow({ id: ID, title: TITLE, state: "planned" })]),
  });
  await page.route(new RegExp(`/api/missions/${ID}(\\?.*)?$`), (r) =>
    r.fulfill({
      json: { ...MISSION, id: ID, title: TITLE, state: "planned", events: [], events_next_seq: null },
    }),
  );
  await page.route(`**/api/missions/${ID}/plan/team`, (r) => r.fulfill({ json: view(s) }));
  await page.route(`**/api/missions/${ID}/plan/team/choices**`, (r) => {
    const stepId = new URL(r.request().url()).searchParams.get("step") ?? "";
    return r.fulfill({ json: choices(s, stepId, !!opts.sonnetOk) });
  });
  await page.route(`**/api/missions/${ID}/plan/ops`, (r) => {
    const body = r.request().postDataJSON() as Record<string, unknown>;
    writes.push(body);
    if (body.expected_revision !== s.revision) {
      return r.fulfill({ status: 409, json: { detail: "the plan changed", latest_revision: s.revision } });
    }
    if (body.op === "assemble") {
      s.assigned = { ...ASSEMBLED };
    } else if (body.op === "set_override") {
      const model = ["opus", "sonnet", "haiku"][body.index as number];
      if (model === "sonnet" && body.step === "o1.review") {
        // The list the client held said sonnet was fine; the server re-checks and refuses.
        return r.fulfill({
          status: 422,
          json: { detail: SONNET_REFUSAL, code: "grade_below_min", reasons: [] },
        });
      }
      const a = s.assigned[body.step as string];
      s.assigned[body.step as string] = {
        ...a,
        override: { engine: ENGINE, model, effort: null },
        reason: `override for this mission: ${model} on ${ENGINE}, Intern ≥ Intern; ${a.specialist.id} itself runs on sonnet`,
      };
    }
    s.revision += 1;
    return r.fulfill({ json: { revision: s.revision, replayed: false } });
  });
  return writes;
}

async function open(page: Page) {
  await page.goto("/mission");
  await openMissionRail(page);
  await page.getByTestId("rail-mission").filter({ hasText: TITLE }).first().click();
  await expect(page.getByTestId("console-title")).toHaveText(TITLE);
  const team = page.getByTestId("mission-team");
  await expect(team).toBeVisible();
  return team;
}

function row(page: Page, id: string) {
  return page.getByTestId(`team-step-o1.${id}`);
}

/** Evidence for the PR: the card as it renders (`test-results/` is not committed). */
async function shot(page: Page, name: string, project: string) {
  const vp = page.viewportSize();
  if (vp) await page.setViewportSize({ width: vp.width, height: 2400 });
  await page.getByTestId("mission-team").scrollIntoViewIfNeeded();
  await page.getByTestId("mission-team").screenshot({ path: `test-results/team-${name}-${project}.png` });
}

test("assemble, then read each pick's reason and alternates", async ({ page }, info) => {
  const s: Server = { revision: 1, assigned: {}, refuseNext: false };
  const writes = await stub(page, s);
  const team = await open(page);
  await expect(row(page, "review").getByTestId("team-unassigned")).toBeVisible();
  const assemble = team.getByRole("button", { name: "Assemble" });
  expect((await assemble.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await assemble.click();
  await expect(row(page, "review")).toContainText("application-reviewer");
  expect(writes).toHaveLength(1);
  expect(writes[0]).toMatchObject({ op: "assemble", expected_revision: 1 });
  expect(String(writes[0].operation_id)).toMatch(/^[0-9a-f-]{36}$/);
  // The server's reason, verbatim, and the alternates behind their expander.
  await expect(row(page, "review")).toContainText(
    "only specialist with application-review; Senior ≥ Senior; distinct from o1.cv",
  );
  await row(page, "review").getByText("Alternates (1)").click();
  await expect(row(page, "review")).toContainText("review-lead");
  await expect(row(page, "review")).toContainText("same grade and free capacity, ordered after by id");
  // Operator steps get no specialist and no Change.
  await expect(row(page, "intake").getByRole("button")).toHaveCount(0);
  await expect(team.getByText("revision 2")).toBeVisible();
  await shot(page, "assembled", info.project.name);
});

test("the review on Sonnet: disabled with its reason, and refused when the list was stale", async ({
  page,
}, info) => {
  const s: Server = { revision: 2, assigned: { ...ASSEMBLED }, refuseNext: false };
  const writes = await stub(page, s);
  await open(page);
  const review = row(page, "review");
  await review.getByRole("button", { name: "Change" }).click();
  const picker = review.getByTestId("team-picker");
  const sonnet = picker.getByRole("radio", { name: /claude-api · sonnet/ });
  await expect(sonnet).toBeDisabled();
  await expect(picker).toContainText(SONNET_REFUSAL);
  expect(writes).toHaveLength(0);
  await shot(page, "picker", info.project.name);

  // A list read before a remap offered it; the server re-checks and refuses. Nothing is written.
  await page.unrouteAll({ behavior: "ignoreErrors" });
  const writes2 = await stub(page, s, { sonnetOk: true });
  await page.reload();
  await openMissionRail(page);
  await page.getByTestId("rail-mission").filter({ hasText: TITLE }).first().click();
  await row(page, "review").getByRole("button", { name: "Change" }).click();
  const radio = row(page, "review").getByRole("radio", { name: /claude-api · sonnet/ });
  await expect(radio).toBeEnabled();
  await radio.check();
  await row(page, "review").getByRole("button", { name: "Use this" }).click();
  const alert = row(page, "review").getByTestId("team-refusal");
  await expect(alert).toBeVisible();
  await expect(alert).toContainText(SONNET_REFUSAL);
  await expect(alert).toContainText("Nothing was written");
  await expect(alert).toContainText("grade_below_min");
  expect(writes2).toHaveLength(1);
  expect(writes2[0]).toMatchObject({ op: "set_override", step: "o1.review", index: 1 });
  expect(s.revision).toBe(2);
  // The review keeps its team.
  await expect(row(page, "review")).toContainText("claude-api · opus");
  await shot(page, "refused", info.project.name);
});

test("a legal override: the record step on haiku", async ({ page }) => {
  const s: Server = { revision: 2, assigned: { ...ASSEMBLED }, refuseNext: false };
  const writes = await stub(page, s);
  await open(page);
  const record = row(page, "record");
  const change = record.getByRole("button", { name: "Change" });
  expect((await change.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await change.click();
  const haiku = record.getByRole("radio", { name: /claude-api · haiku/ });
  await expect(haiku).toBeEnabled();
  const opt = haiku.locator("xpath=ancestor::li[1]");
  expect((await opt.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  // The label is the hit area, not just the 16px radio (independent review on #1985).
  const label = opt.locator("label");
  expect((await label.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await haiku.check();
  const sent = page.waitForRequest((r: Request) => r.url().endsWith("/plan/ops"));
  await record.getByRole("button", { name: "Use this" }).click();
  await sent;
  expect(writes[0]).toMatchObject({
    op: "set_override",
    step: "o1.record",
    index: 2,
    listing: `sha256:${"b".repeat(32)}`,
    expected_revision: 2,
  });
  await expect(record).toContainText("OVERRIDE · THIS MISSION");
  await expect(record).toContainText("claude-api · haiku");
  await expect(record).toContainText("Intern ≥ Intern");
  await expect(record.getByTestId("team-picker")).toHaveCount(0);
});
