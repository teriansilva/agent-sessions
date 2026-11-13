/** Plan approval (#1951, #1613 P6 slice A), in a real browser, desktop and phone.
 *
 *  Approve a plan (the revision and digest the card showed are what is sent), see a stale binding
 *  refused with old → new, see grade drift, and give then revoke a standing consent. The backend is
 *  mocked; every write the SPA sends is asserted, and every line on screen is the server's text.
 */
import { expect, test, type Page } from "@playwright/test";
import { MISSION, missionList, missionRow, mockMissions, openMissionRail } from "./mission-console";
import { TOUCH_FLOOR } from "./touchTarget";

const ID = "msn_appr";
const TITLE = "Apply: Data analyst, Example GmbH";
const DIGEST = `sha256:${"9f3c41d0".repeat(8)}`;
const SCOPE = `sha256:${"4be0c2f9".repeat(8)}`;
const AID = `apr-${"a".repeat(20)}`;
const CID = `con-${"c".repeat(20)}`;

const LINES = {
  groups: [
    {
      objective: "o1",
      project: "p-synthetic01",
      rows: [
        { key: "procedure", text: "application (mission-local procedure)" },
        {
          key: "o1.cv",
          text: "cv-writer v1 · claude-api · sonnet · Specialist (needs Specialist)",
          bound: "up to 1 + 3",
        },
        {
          key: "o1.review",
          text: "application-reviewer v1 · claude-api · opus · Senior (needs Senior)",
          bound: "up to 1 + 3",
        },
        {
          key: "equipment",
          text: "p-synthetic01: secret GITHUB_TOKEN (this project), project memory. Nothing from another project.",
          bound: "2 items",
        },
      ],
    },
  ],
  bounds: "Up to 16 sessions in all, up to 2 at once (pool limit 6). One session per step run.",
  not_covered:
    "The floor (anything irreversible, outward, money or credentials) still asks you, every time. The playbook's own gates (approve, submit) still wait for you. HQ never approves a gate, a tool request or a floor item.",
  agent_steps: 7,
};

type Reason = { code: string; text: string; step?: string; old?: unknown; new?: unknown };
type Server = {
  approved: boolean;
  stale: Reason[];
  cardReasons: Reason[];
  consent: null | "active" | "revoked";
};

function view(s: Server) {
  const approval = s.approved
    ? {
        id: AID,
        revision: 3,
        digest: DIGEST,
        created_at: 1_763_000_000,
        revoked_at: null,
        revoked_reason: null,
        state: s.stale.length ? "stale" : "in_force",
        reasons: s.stale,
        agent_steps: 7,
        ...(s.stale.length ? {} : LINES),
      }
    : null;
  return {
    mission_id: ID,
    revision: 3,
    card: { ok: s.cardReasons.length === 0, reasons: s.cardReasons, digest: DIGEST, ...LINES },
    approval,
    consent: s.consent
      ? {
          id: CID,
          approval_id: AID,
          lines: ["Launch plan revision 3 of “Apply”, exactly as approved (digest 9f3c41d0…)."],
          scope_digest: SCOPE,
          expires_at: 1_763_600_000,
          created_at: 1_763_000_100,
          revoked_at: s.consent === "revoked" ? 1_763_000_200 : null,
          state: s.consent,
          reasons:
            s.consent === "revoked"
              ? [{ code: "approval_revoked", text: "this standing consent was revoked" }]
              : [],
        }
      : null,
    consent_offer:
      s.approved && !s.stale.length
        ? {
            approval_id: AID,
            approval_digest: DIGEST,
            digest: SCOPE,
            lines: [
              "Launch plan revision 3 of “Apply”, exactly as approved (digest 9f3c41d0…).",
              "Only the 7 agent steps listed, on their pinned specialists and models.",
              "Any change to the plan, playbook, specialists or equipment voids this consent.",
            ],
          }
        : null,
    consent_days: { default: 7, max: 30 },
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
  await page.route(`**/api/missions/${ID}/plan/approval`, (r) => r.fulfill({ json: view(s) }));
  const write = (path: string, apply: (body: Record<string, unknown>) => unknown) =>
    page.route(`**/api/missions/${ID}/plan/${path}`, (r) => {
      const body = r.request().postDataJSON() as Record<string, unknown>;
      writes.push({ url: path, body });
      return r.fulfill({ json: apply(body) });
    });
  await write("approve", () => {
    s.approved = true;
    return { id: AID, revision: 3, digest: DIGEST, created_at: 1, replayed: false };
  });
  await write("approval/revoke", () => ({ id: AID, revoked_at: 2 }));
  await write("consent", () => {
    s.consent = "active";
    return { id: CID, approval_id: AID, expires_at: 3, created_at: 1, replayed: false };
  });
  await write("consent/revoke", () => {
    s.consent = "revoked";
    return { id: CID, revoked_at: 2 };
  });
  return writes;
}

async function open(page: Page) {
  await page.goto("/mission");
  await openMissionRail(page);
  await page.getByTestId("rail-mission").filter({ hasText: TITLE }).first().click();
  await expect(page.getByTestId("console-title")).toHaveText(TITLE);
  const card = page.getByTestId("mission-approval");
  await expect(card).toBeVisible();
  return card;
}

async function shot(page: Page, name: string, project: string) {
  const vp = page.viewportSize();
  if (vp) await page.setViewportSize({ width: vp.width, height: 2400 });
  await page.getByTestId("mission-approval").scrollIntoViewIfNeeded();
  await page
    .getByTestId("mission-approval")
    .screenshot({ path: `test-results/approval-${name}-${project}.png` });
}

test("approve the plan the card showed", async ({ page }, info) => {
  const s: Server = { approved: false, stale: [], cardReasons: [], consent: null };
  const writes = await stub(page, s);
  const card = await open(page);
  await expect(card).toContainText("revision 3 · not approved");
  await expect(card).toContainText("cv-writer v1 · claude-api · sonnet · Specialist (needs Specialist)");
  await expect(card).toContainText("up to 1 + 3");
  await expect(card.getByTestId("approval-not-covered")).toContainText(
    "The playbook's own gates (approve, submit) still wait for you",
  );
  const approve = card.getByRole("button", { name: "Approve plan" });
  expect((await approve.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await shot(page, "card", info.project.name);
  await approve.click();
  await expect(card.getByTestId("approval-state")).toHaveText("APPROVED");
  await expect(card).toContainText("All 7 agent steps still match what you approved");
  expect(writes).toHaveLength(1);
  expect(writes[0].url).toBe("approve");
  expect(writes[0].body).toMatchObject({ revision: 3, digest: DIGEST });
  expect(String(writes[0].body.operation_id)).toMatch(/^[0-9a-f-]{36}$/);
  expect(Object.keys(writes[0].body).sort()).toEqual(["digest", "operation_id", "revision"]);
  await shot(page, "approved", info.project.name);
});

test("a stale binding is shown old → new and nothing is remapped", async ({ page }, info) => {
  const s: Server = {
    approved: true,
    stale: [
      {
        code: "specialist_version_changed",
        text: "cv-writer changed from v1 to v2",
        step: "o1.cv",
        old: "v1",
        new: "v2",
      },
      {
        code: "equipment_changed",
        text: "project p-synthetic01's equipment changed: added secret STRIPE_KEY (this project)",
      },
    ],
    // The card re-checks what approving NOW would pin: the plan still names cv-writer v1.
    cardReasons: [
      {
        code: "specialist_version_changed",
        text: "cv-writer changed from v1 to v2",
        step: "o1.cv",
        old: "v1",
        new: "v2",
      },
    ],
    consent: null,
  };
  const writes = await stub(page, s);
  const card = await open(page);
  await expect(card.getByTestId("approval-state")).toHaveText("STALE");
  const stale = card.getByTestId("approval-stale");
  await expect(stale).toContainText("PLAN CHANGED");
  await expect(stale).toContainText("cv-writer changed from v1 to v2");
  await expect(stale).toContainText("specialist_version_changed");
  await expect(stale.locator("s")).toHaveText("v1");
  await expect(stale).toContainText("added secret STRIPE_KEY");
  // Standing consent is not offered on a stale approval.
  await expect(card.getByRole("button", { name: "Standing consent" })).toHaveCount(0);
  // Nothing is remapped: the new version is not approvable until the team names it.
  await expect(card.getByRole("button", { name: "Approve again" })).toBeDisabled();
  await expect(card.getByTestId("approval-blocked")).toContainText("change the team above");
  expect(writes).toHaveLength(0);
  await shot(page, "stale", info.project.name);
});

test("grade drift: refused until the model qualifies again", async ({ page }, info) => {
  const drift = {
    code: "grade_below_min",
    text: "sonnet is Intern, below Specialist, which cv-writing needs",
    step: "o1.cv",
    old: "strong",
    new: "fast",
  };
  const s: Server = { approved: true, stale: [drift], cardReasons: [drift], consent: null };
  await stub(page, s);
  const card = await open(page);
  await expect(card.getByTestId("approval-stale")).toContainText("BELOW GRADE");
  await expect(card).toContainText("sonnet is Intern, below Specialist, which cv-writing needs");
  await expect(card.getByRole("button", { name: "Approve again" })).toBeDisabled();
  await expect(card.getByRole("link", { name: "Settings → Agents" }).first()).toBeVisible();
  await shot(page, "grade", info.project.name);
});

test("give a standing consent, then revoke it", async ({ page }, info) => {
  const s: Server = { approved: true, stale: [], cardReasons: [], consent: null };
  const writes = await stub(page, s);
  const card = await open(page);
  await card.getByRole("button", { name: "Standing consent" }).click();
  const offer = card.getByTestId("consent-offer");
  await expect(offer).toContainText("Only the 7 agent steps listed");
  const select = offer.getByLabel("Expires after");
  expect((await select.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await expect(select.locator("option")).toHaveText(["1 day", "3 days", "7 days", "14 days", "30 days"]);
  await select.selectOption("14");
  await shot(page, "consent-offer", info.project.name);
  await offer.getByRole("button", { name: "Give consent" }).click();
  const state = card.getByTestId("consent-state");
  await expect(state).toContainText("ACTIVE");
  expect(writes[0]).toEqual({
    url: "consent",
    body: {
      approval_id: AID,
      scope_digest: SCOPE,
      days: 14,
      operation_id: expect.stringMatching(/^[0-9a-f-]{36}$/),
    },
  });
  const revoke = state.getByRole("button", { name: "Revoke consent" });
  expect((await revoke.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await revoke.click();
  await expect(state).toContainText("REVOKED");
  await expect(state).toContainText("approval_revoked");
  expect(writes[1]).toEqual({ url: "consent/revoke", body: { consent_id: CID } });
  await shot(page, "consent-revoked", info.project.name);
});
