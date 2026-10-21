import { expect, test, type Page } from "@playwright/test";

import {
  MISSION,
  missionList,
  missionRow,
  mockMissions,
} from "./mission-console";

/** `ActionRow` in the MISSION CONSOLE, in a real browser (#726 Phases 1–2, moved here in #1049).
 *
 *  These four were the pane-strip cases in `orchestrator.spec.ts`. #1049 removed the session
 *  pane's decision strip, but not `ActionRow` and not what it does: the mission console renders
 *  the same row for every decision on a session its mission holds (`MissionBody.decisions`), and
 *  since #1049 that is the ONLY place Approve exists. So the behaviour moved here rather than
 *  being deleted — a real click on a real button reaching the approve route, the stale-409
 *  wording, evidence pulled on expand, and the mobile tap target.
 *
 *  Network is fully mocked; the suite never talks to a backend or an AI endpoint.
 */

const NOW = Math.floor(Date.now() / 1000);
const UUID = "aaaaaaaa-0000-4000-8000-000000000001";
const UUID2 = "aaaaaaaa-0000-4000-8000-000000000002";
/** A server-shaped mission id (`msn_` + 32 hex), so the `?m=` deep link is honoured. */
const MID = `msn_${"1049".repeat(8)}`;

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

const CONTINUE_ACTION = {
  id: "act-continue",
  state: "proposed",
  projection: "actionable",
  can_approve: true,
  can_reject: true,
  ts: NOW,
  expires_at: NOW + 1800,
  tier: "suggest",
  session_id: `claude:${UUID}`,
  engine: "claude",
  title: "Kimi transcript adapter",
  project: "agent-sessions",
  project_id: "p1",
  verb: "continue",
  confidence: 0.86,
  rationale:
    "finished the adapter and stopped without running the tests it planned",
  evidence: "screen",
};

/** An escalation on a SECOND held session — one live action per session, as the ledger enforces.
 *  It never reaches a session, so it must not offer Approve. */
const ESCALATE_ACTION = {
  ...CONTINUE_ACTION,
  id: "act-escalate",
  state: "escalated",
  can_approve: false,
  session_id: `claude:${UUID2}`,
  title: "Relay session cap",
  project: "battlelab-cloud",
  verb: "escalate",
  confidence: 0.34,
  rationale: "asks which of two migration strategies to take — a design call",
  evidence: "none",
};

function card(action: typeof CONTINUE_ACTION | null, uuid = UUID) {
  return {
    id: `claude:${uuid}`,
    engine: "claude",
    title: action?.title ?? "Kimi transcript adapter",
    cwd: "/home/u/agent-sessions",
    project: { kind: "project", id: "p1", name: "agent-sessions", color: "#ffb000" },
    state: action ? "needs_you" : "idle",
    live: false,
    last_activity: NOW - 600,
    last_mtime: NOW - 600,
    intervention_required: false,
    ai_summary: "",
    synthesis: "",
    mission_id: MID,
    ...(action ? { pending_action: action } : {}),
  };
}

function overview(cards: unknown[]) {
  return {
    cache_version: 2,
    generated_at: NOW,
    window_days: 3,
    scan_depth: "slow",
    input_fingerprint: null,
    synthesis_skipped: false,
    banner: null,
    cards,
  };
}

/** Wire the app shell, then a mission holding `actions`' sessions, and open it by deep link. The
 *  overview re-reads WITHOUT the actions once `settled()` says so, as the server's would. */
async function openConsole(
  page: Page,
  actions: (typeof CONTINUE_ACTION)[],
  settled: () => boolean = () => false,
) {
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
      json: {
        sessions: [],
        next_offset: null,
        total: 0,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0 } }),
  );
  await page.route(/\/api\/pulse\/orchestrator$/, (r) =>
    r.fulfill({
      json: {
        config: ORCH_CONFIG,
        pending: settled() ? [] : actions,
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
      json: overview(
        actions.map((a) =>
          card(settled() ? null : a, a.session_id.split(":")[1]),
        ),
      ),
    }),
  );
  const keys = actions.map((a) => a.session_id);
  await mockMissions(page, {
    missions: missionList([
      missionRow({ id: MID, title: "Adapter work", session_keys: keys }),
    ]),
    mission: {
      ...MISSION,
      id: MID,
      title: "Adapter work",
      sessions: keys.map((k) => ({ session_key: k, removed_at: null })),
      events: [],
      events_next_seq: null,
    },
  });
  await page.goto(`/mission?m=${MID}`);
  await expect(page.getByTestId("console-title")).toHaveText("Adapter work");
}

test("approve delivers, and only a delivering verb offers the button", async ({
  page,
}) => {
  let approvedId: string | null = null;
  await page.route(/\/api\/pulse\/actions\/.*\/approve$/, async (r) => {
    approvedId = new URL(r.request().url()).pathname.split("/").at(-2) ?? null;
    await r.fulfill({ json: { ...CONTINUE_ACTION, state: "delivered" } });
  });
  await openConsole(page, [CONTINUE_ACTION, ESCALATE_ACTION]);

  // Wait on both rows, so a count of one Approve cannot be one row that simply has not arrived.
  await expect(page.getByText(CONTINUE_ACTION.rationale)).toBeVisible();
  await expect(page.getByText(ESCALATE_ACTION.rationale)).toBeVisible();

  // The escalation must NOT offer an approve button — it never reaches a session, and a button
  // implying otherwise would be a lie about what the system does.
  const approve = page.getByRole("button", { name: /^approve$/i });
  await expect(approve).toHaveCount(1);

  await approve.click();
  await expect.poll(() => approvedId).toBe("act-continue");
});

test("a stale 409 says nothing was sent, distinguishably from an error", async ({
  page,
}) => {
  // Producer-shaped: the approve route returns the SETTLED record with its `detail` on a stale
  // refusal (`routes/pulse.py`), so the row goes and the explanation is raised to the console.
  await page.route(/\/api\/pulse\/actions\/.*\/approve$/, (r) =>
    r.fulfill({
      status: 409,
      json: {
        ...CONTINUE_ACTION,
        state: "stale",
        detail: "the session's screen changed since this was proposed",
      },
    }),
  );
  await openConsole(page, [CONTINUE_ACTION]);

  await page.getByRole("button", { name: /^approve$/i }).click();
  // Compare-and-execute refused: the operator must be able to tell "nothing happened" from
  // "something broke", because the two call for completely different responses.
  await expect(
    page.getByText(/not sent — the session's screen changed/i),
  ).toBeVisible();
  await expect(page.getByText(/couldn.t complete that/i)).toHaveCount(0);
});

test("evidence is pulled from the server on expand, not shipped with the proposal", async ({
  page,
}) => {
  let evidenceCalls = 0;
  await page.route(/\/api\/pulse\/evidence\//, async (r) => {
    evidenceCalls += 1;
    await r.fulfill({
      json: {
        kind: "screen",
        text: "✓ parser complete\n› (idle 11m)",
        available: true,
      },
    });
  });
  await openConsole(page, [CONTINUE_ACTION]);

  await expect(page.getByRole("button", { name: /^approve$/i })).toBeVisible();
  // Nothing is fetched until the operator asks — the proposal carries a KIND, never content.
  expect(evidenceCalls).toBe(0);

  await page.getByRole("button", { name: /show live screen/i }).click();
  await expect(page.getByText(/parser complete/)).toBeVisible();
  expect(evidenceCalls).toBe(1);
});

test.describe("mobile", () => {
  test("approve is a 44px target and the page never scrolls sideways", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    let settled = false;
    await page.route(/\/api\/pulse\/actions\/.*\/approve$/, (r) => {
      settled = true;
      return r.fulfill({ json: { ...CONTINUE_ACTION, state: "delivered" } });
    });
    await openConsole(page, [CONTINUE_ACTION], () => settled);

    const approve = page.getByRole("button", { name: /^approve$/i });
    await expect(approve).toBeVisible();

    // 44px is the touch-target floor from the repo's design guidance.
    const box = await approve.boundingBox();
    expect(box!.height).toBeGreaterThanOrEqual(44);

    // The page must never scroll horizontally (#494) — long rationales and screen dumps wrap or
    // scroll inside their own block.
    const overflow = await page.evaluate(
      () =>
        document.documentElement.scrollWidth -
        document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(1);

    await approve.click();
    await expect(page.getByText(CONTINUE_ACTION.rationale)).toHaveCount(0);
    await expect(page.getByRole("button", { name: /^approve$/i })).toHaveCount(0);
  });
});

test("a model escalation says its number is confidence that the call is YOURS (#1060)", async ({
  page,
}) => {
  // `DEFAULT_ORCH_PROMPT` defines confidence as "right AND safe", and for an escalation the right
  // action is to ask — so 0.90 means "0.90 sure this decision is yours". Printed as
  // "conf 0.90 · needs your call" it read as confidence in an answer, the opposite claim. This is
  // the card the operator meets in the mission thread; it moved here when #1054 removed the pane's
  // decision strip, which is where this assertion used to live.
  const esc = {
    ...ESCALATE_ACTION,
    escalation_reason: "model",
    confidence: 0.9,
  } as typeof CONTINUE_ACTION;
  await openConsole(page, [esc]);
  await expect(page.getByText(ESCALATE_ACTION.rationale)).toBeVisible();

  const chip = page.getByText(/^needs your call · 0\.90 sure$/);
  await expect(chip).toBeVisible();
  await expect(chip).toHaveAttribute("aria-label", /0\.90 sure this decision is yours/);
  await expect(page.getByText(/^conf 0\.90/)).toHaveCount(0);
});

test("a session's own menu is answered from the card, and answering never attaches a viewer (#1060)", async ({
  page,
}) => {
  const sockets: string[] = [];
  page.on("websocket", (ws) => sockets.push(ws.url()));
  const esc = {
    ...ESCALATE_ACTION,
    escalation_reason: "model",
    confidence: 0.9,
    observed_prompt: {
      prompt_class: "choice",
      observed_at: NOW,
      menu: {
        engine: "claude",
        question: "Which migration strategy should I use?",
        options: [
          { n: 1, label: "Online, batched", selected: true },
          { n: 2, label: "Offline, in one transaction", selected: false },
        ],
      },
    },
  } as unknown as typeof CONTINUE_ACTION;
  let answered = false;
  let body: unknown = null;
  await page.route(/\/api\/pulse\/actions\/.*\/choose$/, async (r) => {
    body = r.request().postDataJSON();
    answered = true;
    await r.fulfill({
      json: {
        ...esc,
        state: "rejected",
        outcome: "answered_by_operator",
        choice: { id: "choose_1", verb: "choose", option: 2, state: "delivered" },
      },
    });
  });
  await openConsole(page, [esc], () => answered);
  const url = page.url();

  const menu = page.getByTestId("menu-options");
  await expect(menu).toContainText("Which migration strategy should I use?");
  const opts = menu.getByTestId("menu-option");
  await expect(opts).toHaveText(["1. Online, batched", "2. Offline, in one transaction"]);
  for (const i of [0, 1]) expect((await opts.nth(i).boundingBox())!.height).toBeGreaterThanOrEqual(44);

  // The first tap arms and sends nothing; the second sends the number AND the label shown.
  await opts.nth(1).click();
  await expect(opts.nth(1)).toHaveText("Send 2 · Offline, in one transaction");
  await expect(page.getByTestId("menu-armed")).toContainText("Types 2 into the session");
  expect(answered).toBe(false);
  await opts.nth(1).click();
  await expect.poll(() => body).toEqual({ option: 2, label: "Offline, in one transaction" });

  // The decision is settled and leaves the thread; the operator never left the console.
  await expect(page.getByText(ESCALATE_ACTION.rationale)).toHaveCount(0);
  expect(page.url()).toBe(url);
  // NO VIEWER: answering is a server-side write through the actuator, never a terminal socket.
  expect(sockets.filter((u) => u.includes("/ws"))).toEqual([]);
});

test("a refused answer says nothing was sent and keeps the decision (#1060)", async ({ page }) => {
  const esc = {
    ...ESCALATE_ACTION,
    observed_prompt: {
      prompt_class: "choice",
      observed_at: NOW,
      menu: {
        engine: "claude",
        question: "Proceed?",
        options: [
          { n: 1, label: "Yes", selected: true },
          { n: 2, label: "No", selected: false },
        ],
      },
    },
  } as unknown as typeof CONTINUE_ACTION;
  await page.route(/\/api\/pulse\/actions\/.*\/choose$/, (r) =>
    r.fulfill({
      status: 409,
      json: {
        detail:
          "nothing was sent: the session is no longer at that menu — open it to see what it is showing now",
      },
    }),
  );
  await openConsole(page, [esc]);
  const opts = page.getByTestId("menu-option");
  await opts.nth(0).click();
  await opts.nth(0).click();
  await expect(page.getByTestId("menu-note")).toContainText(
    "Not sent — nothing was sent: the session is no longer at that menu",
  );
  await expect(page.getByText(ESCALATE_ACTION.rationale)).toBeVisible();
  await expect(opts.nth(0)).toHaveText("1. Yes");
});
