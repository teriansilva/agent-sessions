import { expect, test } from "@playwright/test";

import { mockMissions } from "./mission-console";

// #795 — an escalation says why it escalated, and what you can do about it.
//
// The row appended "· below threshold" to EVERY escalated action. Three paths escalate and only
// one is the confidence gate — and that one is unreachable at any tier but `yolo`, where nothing
// reads `confidence_min`. So the fixture below is the exact live shape that made the claim
// absurd: `suggest`, a threshold of 0.70, and an escalation at conf 0.90 that the model raised
// itself.
//
// SCOPED TO THE MODEL-QUESTION CASE, deliberately. Since #877 those three paths write two
// states: `escalated` (the model asking — nothing to run, so the ✕ is its only control) and
// `escalated_low_confidence` (a real delivering verb, so Approve + Reject). This fixture is the
// FIRST, and its "only control is the ✕" assertion is true of that state and not of the other.
// The low-confidence row's controls are covered in `Orchestrator.test.tsx`; adding it here would
// need its own fixture rather than a reinterpretation of this one.
//
// Real-browser rather than jsdom for the second half: whether that single control is legible and
// keeps its per-project touch geometry with a label added are computed-layout facts. An emulator
// reports neither.

const NOW = Math.floor(Date.now() / 1000);

const ORCH_CONFIG = {
  enabled: true,
  autonomy: "suggest",
  allowed_verbs: ["continue"],
  auto_verbs_ceiling: ["continue"],
  confidence_min: 0.7,
  interval_minutes: 10,
  max_actions_per_pass: 4,
  proposal_ttl_minutes: 30,
  nudge_template: "Please continue.",
  notify: "escalations",
  configured: true,
  default_nudge_template: "Please continue.",
};

const RATIONALE = "This is a design call only you can make.";

const ACTION = {
  id: "act-1",
  state: "escalated",
  ts: NOW - 600,
  expires_at: NOW + 1800,
  tier: "suggest",
  session_id: "claude:aaa",
  engine: "claude",
  title: "Bail on PR#30 for same-cause",
  project: "infra",
  project_id: "p1",
  verb: "escalate",
  // Above the threshold, and escalated anyway — because the MODEL chose to, which is what
  // `escalation_reason` records and what the old copy could not distinguish.
  confidence: 0.9,
  rationale: RATIONALE,
  evidence: "recap",
  escalation_reason: "model",
};

const LIVE_CARD = {
  id: "claude:aaa",
  engine: "claude",
  title: "Bail on PR#30 for same-cause",
  cwd: "/home/u/infra",
  project: { kind: "project", id: "p1", name: "infra", color: "#ffb000" },
  state: "needs_you",
  live: false,
  last_activity: NOW - 720,
  last_mtime: NOW - 720,
  intervention_required: false,
  intervention_reason: "",
  ai_summary: "",
  synthesis: "",
  pending_action: ACTION,
};

// A SETTLED session: no controls at all, just what the orchestrator last did here. This is the
// other half of "no options" — the line used to print the ledger's own state name.
const SETTLED_CARD = {
  id: "claude:bbb",
  engine: "claude",
  title: "Awaiting Hermes re-review",
  cwd: "/home/u/docs",
  project: { kind: "project", id: "p2", name: "docs", color: "#ffb000" },
  state: "idle",
  live: false,
  last_activity: NOW - 900,
  last_mtime: NOW - 900,
  intervention_required: false,
  intervention_reason: "",
  ai_summary: "Waiting on review",
  synthesis: "",
  last_action: {
    id: "act-0",
    state: "expired",
    ts: NOW - 3600,
    tier: "suggest",
    session_id: "claude:bbb",
    engine: "claude",
    title: "Awaiting Hermes re-review",
    project: "docs",
    project_id: "p2",
    verb: "escalate",
    confidence: 0.8,
    rationale: "",
    evidence: "none",
  },
};

test.beforeEach(async ({ page }) => {
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
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [] } }),
  );
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [] } }),
  );
  await page.route(/\/api\/projects($|\?)/, (r) =>
    r.fulfill({ json: { projects: [] } }),
  );
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
        pending: [ACTION],
        feed: [],
        expired_now: 0,
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
        cards: [LIVE_CARD, SETTLED_CARD],
      },
    }),
  );
});

test("an escalation names its real cause, never the threshold it never consulted", async ({
  page,
}) => {
  await mockMissions(page);
    await page.goto("/mission");
  await expect(page.getByText(RATIONALE)).toBeVisible();

  const conf = page.getByText(/^conf 0\.90/);
  await expect(conf).toBeVisible();
  // The fix: the suffix comes from the server's `escalation_reason`, so it says what actually
  // happened — the model handed this back on purpose.
  await expect(conf).toHaveText(/needs your call/);
  // The bug: 0.90 is ABOVE the 0.70 threshold, and `suggest` never reads the threshold at all.
  // This assertion is the red one before the fix.
  await expect(conf).not.toHaveText(/below threshold/);
  await expect(page.getByText(/below threshold/)).toHaveCount(0);
});

test("the one control an escalation offers says what it does", async ({
  page,
}) => {
  await mockMissions(page);
    await page.goto("/mission");
  await expect(page.getByText(RATIONALE)).toBeVisible();

  // There is nothing to deliver, so there is no Approve — that part was already right.
  await expect(page.getByRole("button", { name: /approve/i })).toHaveCount(0);

  // ...which makes the ✕ the row's ONLY control, sitting beside the RECAP disclosure where a
  // bare glyph reads as "close that panel". It carries a visible label now.
  const dismiss = page.getByRole("button", {
    name: /dismiss this escalation/i,
  });
  await expect(dismiss).toBeVisible();
  await expect(dismiss).toHaveText(/dismiss/i);
});

test("the dismiss control keeps its geometry with the label added", async ({
  page,
}, testInfo) => {
  await mockMissions(page);
    await page.goto("/mission");
  await expect(page.getByText(RATIONALE)).toBeVisible();

  const dismiss = page.getByRole("button", {
    name: /dismiss this escalation/i,
  });
  const box = await dismiss.boundingBox();
  expect(box).not.toBeNull();

  // Per-project, and deliberately NOT one number: `.reject` is 32px on desktop and 44px under
  // `max-width: 800px`, shared with Approve. #795 does not restyle that split — it proves the
  // added label does not degrade it.
  const min = testInfo.project.name === "mobile" ? 44 : 32;
  expect(box!.height).toBeGreaterThanOrEqual(min);
  // The label rides the disclosure's head row (#781) rather than pushing the row taller — it
  // must not have wrapped the control onto a line of its own.
  expect(box!.height).toBeLessThan(min * 2);

  const recap = page.getByRole("button", { name: /show recap/i });
  const recapBox = await recap.boundingBox();
  expect(recapBox).not.toBeNull();

  // Same row: their vertical centres agree within a few pixels. Unchanged by #878 — the
  // untracked session block IS a card (it names and links its session), so `ActionRow` renders
  // there `embedded`, exactly as it did inside the grid's card.
  const centre = (b: { y: number; height: number }) => b.y + b.height / 2;
  expect(Math.abs(centre(box!) - centre(recapBox!))).toBeLessThan(6);
});

test("a settled action says what became of it, not which state it reached", async ({
  page,
}) => {
  await mockMissions(page);
  await page.goto("/mission");
  // Scoped to the pane: since #878 a session appears in BOTH the rail row and the view, which is
  // correct and makes an unscoped `getByText` ambiguous under strict mode. The assertion is
  // unchanged — only the region it is read from is now named.
  const pane = page.getByTestId("pane");
  await expect(pane.getByText("Awaiting Hermes re-review")).toBeVisible();

  // The other "no options" case: history, no controls. `EXPIRED` names a transition in a state
  // machine the operator never sees.
  await expect(pane.getByText(/no decision in time/i)).toBeVisible();
  await expect(page.getByText(/^expired$/i)).toHaveCount(0);
});
