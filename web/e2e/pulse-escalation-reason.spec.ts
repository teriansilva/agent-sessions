import { expect, test, type Page } from "@playwright/test";

import { setupBench } from "./terminal/harness";

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
// WHERE THE ROW RENDERS (#948 P3): a decision for a session no mission holds used to sit in the
// "Sessions without a mission" view under /mission. That view is gone, and the decision renders in
// the SESSION's own pane (`session-decisions`) — `ActionRow` embedded, exactly as it was inside
// the old card. Every assertion below is unchanged; only the page it is read from moved.
//
// Real-browser rather than jsdom for the second half: whether that single control is legible and
// keeps its per-project touch geometry with a label added are computed-layout facts. An emulator
// reports neither.

const NOW = Math.floor(Date.now() / 1000);

const UUID = "aaaaaaaa-0000-4000-8000-000000000795";

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
  session_id: `claude:${UUID}`,
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

test.beforeEach(async ({ page }) => {
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
});

/** The session's pane, with its pending decision strip on screen. */
async function openPane(page: Page) {
  await setupBench(page, {
    sessions: [{ engine: "claude", uuid: UUID, title: ACTION.title }],
  });
  await page.goto(`/s/claude/${UUID}`);
  const strip = page.getByTestId("session-decisions");
  await expect(strip.getByText(RATIONALE)).toBeVisible();
  return strip;
}

test("an escalation names its real cause, never the threshold it never consulted", async ({
  page,
}) => {
  const strip = await openPane(page);

  const conf = strip.getByText(/^conf 0\.90/);
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
  const strip = await openPane(page);

  // There is nothing to deliver, so there is no Approve — that part was already right.
  await expect(strip.getByRole("button", { name: /approve/i })).toHaveCount(0);

  // ...which makes the ✕ the row's ONLY control, sitting beside the RECAP disclosure where a
  // bare glyph reads as "close that panel". It carries a visible label now.
  const dismiss = strip.getByRole("button", {
    name: /dismiss this escalation/i,
  });
  await expect(dismiss).toBeVisible();
  await expect(dismiss).toHaveText(/dismiss/i);
});

test("the dismiss control keeps its geometry with the label added", async ({
  page,
}, testInfo) => {
  const strip = await openPane(page);

  const dismiss = strip.getByRole("button", {
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

  const recap = strip.getByRole("button", { name: /show recap/i });
  const recapBox = await recap.boundingBox();
  expect(recapBox).not.toBeNull();

  // Same row: their vertical centres agree within a few pixels. The session pane renders
  // `ActionRow` `embedded` (#948 P3), exactly as the grid's card and the untracked block did.
  const centre = (b: { y: number; height: number }) => b.y + b.height / 2;
  expect(Math.abs(centre(box!) - centre(recapBox!))).toBeLessThan(6);
});

// REMOVED (#948 P3): "a settled action says what became of it, not which state it reached". It
// read the history line an untracked session's block rendered from the card's `last_action`. That
// block went with the "Sessions without a mission" view, and no surface renders `last_action` any
// more; the plain-words outcome it pinned ("no decision in time", never `expired`) is still pinned
// on `actionOutcome` in `src/lib/orchestratorAction.test.ts`.
