import { expect, test } from "@playwright/test";

// Real-browser checks for Pulse orchestration (#726 Phases 1–2). Network is fully mocked —
// the suite never talks to a backend or an AI endpoint.
//
// jsdom can't prove any of these: the approve button is a real tap target on a real emulated
// phone, the evidence disclosure is real layout, and the stale (409) path is the one an
// operator will actually hit — the session moved on between the proposal and the tap, so
// nothing was written. A green unit test on a broken tap is exactly the failure mode the
// workflow's UI rule exists to stop.

const NOW = Math.floor(Date.now() / 1000);

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
  prompt: "system prompt",
  notify: "escalations",
  configured: true,
  default_prompt: "system prompt",
  default_nudge_template: "Please continue.",
};

const CONTINUE_ACTION = {
  id: "act-continue",
  state: "proposed",
  ts: NOW,
  expires_at: NOW + 1800,
  tier: "suggest",
  session_id: "claude:aaaaaaaa-0000-4000-8000-000000000001",
  engine: "claude",
  title: "Kimi transcript adapter",
  project: "agent-sessions",
  project_id: "p1",
  verb: "continue",
  confidence: 0.86,
  rationale: "finished the adapter and stopped without running the tests it planned",
  evidence: "screen",
};

const ESCALATE_ACTION = {
  ...CONTINUE_ACTION,
  id: "act-escalate",
  state: "escalated",
  session_id: "codex:bbbbbbbb-0000-4000-8000-000000000002",
  engine: "codex",
  title: "Relay session cap",
  project: "battlelab-cloud",
  verb: "escalate",
  confidence: 0.34,
  rationale: "asks which of two migration strategies to take — a design call",
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        pulse: { auto_enabled: false, interval_minutes: 30, window_days: 3, scan_depth: "fast", configured: true },
        orchestrator: ORCH_CONFIG,
      },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } } }),
  );
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: {
        cache_version: 2,
        generated_at: null,
        window_days: 3,
        scan_depth: "fast",
        input_fingerprint: null,
        synthesis_skipped: false,
        banner: null,
        cards: [],
      },
    }),
  );
});

function mockOrchestrator(
  page: import("@playwright/test").Page,
  pending: unknown[],
  feed: unknown[] = [],
) {
  // `feed` defaults to empty on purpose: an action present in BOTH lists renders twice, and a
  // spec that then matches "the approve button" is asserting against an accident.
  return page.route(/\/api\/pulse\/orchestrator$/, (r) =>
    r.fulfill({ json: { config: ORCH_CONFIG, pending, feed, expired_now: 0, running: [], last: {} } }),
  );
}

test("approve delivers, and only a delivering verb offers the button", async ({ page }) => {
  await mockOrchestrator(page, [CONTINUE_ACTION, ESCALATE_ACTION]);
  let approvedId: string | null = null;
  await page.route(/\/api\/pulse\/actions\/.*\/approve$/, async (r) => {
    approvedId = new URL(r.request().url()).pathname.split("/").at(-2) ?? null;
    await r.fulfill({ json: { ...CONTINUE_ACTION, state: "delivered" } });
  });

  await page.goto("/pulse");
  await expect(page.getByRole("heading", { name: /needs a decision/i })).toBeVisible();

  // The escalation must NOT offer an approve button — it never reaches a session, and a
  // button implying otherwise would be a lie about what the system does.
  const approve = page.getByRole("button", { name: /^approve$/i });
  await expect(approve).toHaveCount(1);

  await approve.click();
  await expect.poll(() => approvedId).toBe("act-continue");
});

test("a stale 409 says nothing was sent, distinguishably from an error", async ({ page }) => {
  await mockOrchestrator(page, [CONTINUE_ACTION]);
  await page.route(/\/api\/pulse\/actions\/.*\/approve$/, (r) =>
    r.fulfill({
      status: 409,
      json: { detail: "the session's screen changed since this was proposed" },
    }),
  );

  await page.goto("/pulse");
  await page.getByRole("button", { name: /^approve$/i }).click();
  // Compare-and-execute refused: the operator must be able to tell "nothing happened" from
  // "something broke", because the two call for completely different responses.
  await expect(page.getByText(/not sent — the session's screen changed/i)).toBeVisible();
});

test("evidence is pulled from the server on expand, not shipped with the proposal", async ({
  page,
}) => {
  await mockOrchestrator(page, [CONTINUE_ACTION]);
  let evidenceCalls = 0;
  await page.route(/\/api\/pulse\/evidence\//, async (r) => {
    evidenceCalls += 1;
    await r.fulfill({
      json: { kind: "screen", text: "✓ parser complete\n› (idle 11m)", available: true },
    });
  });

  await page.goto("/pulse");
  await expect(page.getByRole("button", { name: /^approve$/i })).toBeVisible();
  // Nothing is fetched until the operator asks — the proposal carries a KIND, never content.
  expect(evidenceCalls).toBe(0);

  await page.getByRole("button", { name: /show live screen/i }).click();
  await expect(page.getByText(/parser complete/)).toBeVisible();
  expect(evidenceCalls).toBe(1);
});

test("the autonomy strip shows the ceiling, not just the tier", async ({ page }) => {
  await mockOrchestrator(page, []);
  await page.goto("/pulse");
  // "YOLO" alone reads as "does everything"; the copy has to say what it can actually send.
  await expect(page.getByText(/acts on its own:/i)).toContainText("continue");
  await expect(page.getByText(/everything else always waits for you/i)).toBeVisible();
});

test.describe("mobile", () => {
  test("approve is a full-width 44px target and the page never scrolls sideways", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await mockOrchestrator(page, [CONTINUE_ACTION]);
    await page.route(/\/api\/pulse\/actions\/.*\/approve$/, (r) =>
      r.fulfill({ json: { ...CONTINUE_ACTION, state: "delivered" } }),
    );

    await page.goto("/pulse");
    const approve = page.getByRole("button", { name: /^approve$/i });
    await expect(approve).toBeVisible();

    // 44px is the touch-target floor from the repo's design guidance.
    const box = await approve.boundingBox();
    expect(box!.height).toBeGreaterThanOrEqual(44);

    // Pulse must never scroll horizontally (#494) — long rationales and screen dumps wrap or
    // scroll inside their own block.
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(1);

    await approve.click();
    await expect(page.getByRole("button", { name: /^approve$/i })).toHaveCount(0);
  });
});
