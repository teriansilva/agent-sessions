import { expect, test, type Page } from "@playwright/test";

import { MISSION, missionList, missionRow, mockMissions } from "./mission-console";

/** An agent's TOOL-PERMISSION dialog on the mission page, in a real browser (#1213).
 *
 *  The incident: an opencode mission sat on "Permission required · # Shell command · $ git log …"
 *  and the mission said only "the agent is waiting on a decision only you can make". The decision
 *  card must show the dialog itself — who asks, which tool, the whole command — and answer it with
 *  one button per option, arm then send, on desktop and on a real touch phone: the command wraps
 *  inside the card (no horizontal overflow), every option is a 44px target on mobile, and a send
 *  reaches `/choose` with the option number and the label shown.
 *
 *  Network is fully mocked; the suite never talks to a backend.
 */

const NOW = Math.floor(Date.now() / 1000);
const UUID = "ses_Perm1213E2E";
const SID = `opencode:${UUID}`;
const MID = `msn_${"1213".repeat(8)}`;
const COMMAND =
  "$ git log --oneline -15 && git log --all -i --grep=mission --oneline && find src/agent_sessions -name '*.py' -newer /workspace/infra/.mission-attempt-9a945fd36df192fe4aeabb52d6a79224/last-run.stamp";

const ORCH_CONFIG = {
  enabled: true,
  autonomy: "suggest",
  allowed_verbs: ["continue"],
  auto_verbs_ceiling: ["continue"],
  confidence_min: 0.75,
  interval_minutes: 10,
  max_actions_per_pass: 4,
  proposal_ttl_minutes: 30,
  nudge_template: "Please continue.",
  notify: "escalations",
  configured: true,
  default_nudge_template: "Please continue.",
};

const PERMISSION = {
  engine: "opencode",
  kind: "permission",
  parser: "opencode-permission",
  heading: "Permission required",
  title: "# Shell command",
  detail: COMMAND,
  question: "Allow this?",
  options: [
    { n: 1, label: "Allow once", selected: true, persistent: false },
    { n: 2, label: "Allow always", selected: false, persistent: true },
    { n: 3, label: "Reject", selected: false, persistent: false },
  ],
};

const ESCALATION = {
  id: "act-perm",
  state: "escalated",
  projection: "actionable",
  can_approve: false,
  can_reject: true,
  ts: NOW,
  expires_at: NOW + 1800,
  tier: "suggest",
  session_id: SID,
  engine: "opencode",
  title: "Run a mission feature test",
  project: "infra",
  project_id: "p1",
  verb: "escalate",
  confidence: 1,
  escalation_reason: "permission",
  rationale: `opencode asks permission — # Shell command: ${COMMAND} (Allow once · Allow always · Reject)`,
  evidence: "none",
  mission_id: MID,
  observed_prompt: {
    prompt_class: "confirm",
    menu: null,
    permission: PERMISSION,
    fingerprint: "f",
    observed_at: NOW,
  },
};

async function openMission(page: Page, settled: () => boolean) {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        pulse: {
          auto_enabled: false,
          interval_minutes: 30,
          window_days: 3,
          scan_depth: "slow",
          configured: true,
        },
        orchestrator: ORCH_CONFIG,
      },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0 } }),
  );
  await page.route(/\/api\/pulse\/orchestrator$/, (r) =>
    r.fulfill({
      json: {
        config: ORCH_CONFIG,
        pending: settled() ? [] : [ESCALATION],
        feed: [],
        expired_now: 0,
        delivering_verbs: ["continue", "choose", "answer"],
        running: [],
        last: {},
      },
    }),
  );
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: {
        cache_version: 2,
        generated_at: NOW,
        window_days: 3,
        scan_depth: "slow",
        input_fingerprint: null,
        synthesis_skipped: false,
        banner: null,
        cards: [
          {
            id: SID,
            engine: "opencode",
            title: "Run a mission feature test",
            cwd: "/workspace/infra",
            project: { kind: "project", id: "p1", name: "infra", color: "#ffb000" },
            state: settled() ? "idle" : "needs_you",
            live: true,
            last_activity: NOW - 240,
            last_mtime: NOW - 240,
            intervention_required: false,
            ai_summary: "",
            synthesis: "",
            mission_id: MID,
            ...(settled() ? {} : { pending_action: ESCALATION }),
          },
        ],
      },
    }),
  );
  await mockMissions(page, {
    missions: missionList([
      missionRow({ id: MID, title: "Run a mission feature test", session_keys: [SID] }),
    ]),
    mission: {
      ...MISSION,
      id: MID,
      title: "Run a mission feature test",
      sessions: [{ session_key: SID, removed_at: null }],
      events: [],
      events_next_seq: null,
    },
  });
  await page.goto(`/mission?m=${MID}`);
  await expect(page.getByTestId("console-title")).toHaveText("Run a mission feature test");
}

test("the mission shows the permission dialog itself and answers it", async ({
  page,
}, testInfo) => {
  let body: unknown = null;
  let answered = false;
  await page.route(/\/api\/pulse\/actions\/act-perm\/choose$/, async (r) => {
    body = r.request().postDataJSON();
    answered = true;
    await r.fulfill({
      json: {
        ...ESCALATION,
        state: "rejected",
        choice: { id: "choose-1", state: "delivered", verb: "choose", option: 3 },
      },
    });
  });
  await openMission(page, () => answered);

  const card = page.getByTestId("permission-card");
  await expect(card).toBeVisible();
  await expect(card).toContainText("opencode asks permission");
  await expect(card).toContainText("# Shell command");
  const detail = page.getByTestId("permission-detail");
  await expect(detail).toHaveText(COMMAND);

  // The whole command WRAPS inside the card — nothing clipped, nothing scrolling sideways.
  const fits = await card.evaluate((el) => {
    const pre = el.querySelector("[data-testid=permission-detail]") as HTMLElement;
    const box = el.getBoundingClientRect();
    const p = pre.getBoundingClientRect();
    return {
      preOverflow: pre.scrollWidth - pre.clientWidth,
      inside: p.left >= box.left - 0.5 && p.right <= box.right + 0.5,
      pageOverflow: document.documentElement.scrollWidth - window.innerWidth,
    };
  });
  expect(fits.preOverflow).toBeLessThanOrEqual(0);
  expect(fits.inside).toBe(true);
  expect(fits.pageOverflow).toBeLessThanOrEqual(0);

  const options = page.getByTestId("permission-option");
  await expect(options).toHaveText(["Allow once", "Allow always", "Reject"]);
  if (testInfo.project.name === "mobile") {
    for (const b of await options.all()) {
      const bb = await b.boundingBox();
      expect(bb?.height ?? 0).toBeGreaterThanOrEqual(44);
    }
  }

  // "Allow always" warns before it can be sent.
  await options.nth(1).click();
  await expect(page.getByTestId("permission-warning")).toBeVisible();
  expect(answered).toBe(false);

  // Arm Reject, then send it: the number and the label shown, nothing else.
  await options.nth(2).click();
  await expect(options.nth(2)).toHaveText("Send · Reject");
  await expect(page.getByTestId("permission-warning")).toHaveCount(0);
  await options.nth(2).click();
  await expect.poll(() => body).toEqual({ option: 3, label: "Reject" });
});

test("a refusal says nothing was sent", async ({ page }) => {
  await page.route(/\/api\/pulse\/actions\/act-perm\/choose$/, (r) =>
    r.fulfill({
      status: 409,
      json: {
        detail:
          "nothing was sent: the session is showing a different permission prompt now (the command or its options changed) — open it to see what it asks",
      },
    }),
  );
  await openMission(page, () => false);
  const once = page.getByTestId("permission-option").first();
  await once.click();
  await once.click();
  await expect(page.getByTestId("permission-note")).toContainText(
    "Not sent — nothing was sent: the session is showing a different permission prompt now",
  );
  await expect(page.getByTestId("permission-option")).toHaveCount(3);
});
