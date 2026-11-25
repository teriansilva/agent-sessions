/** The session pane renders NO decision strip, even with a decision pending for that session (#1049).
 *
 * This replaces `session-decisions.spec.ts`, and it is the one assertion worth keeping from it.
 *
 * WHY THE STRIP WENT. It offered an Approve at the moment it was least likely to work. Opening
 * the pane attaches a viewer, and when the viewer's width differs from the one the proposal was
 * judged at, the agent repaints: `actuator.screen_matches` then sees a changed screen fingerprint
 * and the approval is refused as stale — permanently, not for a cooldown. The `operator_approval`
 * exemption (#969) only covers the attached half, not the repaint. So the control sat on the one
 * screen whose opening could invalidate it.
 *
 * WHAT THIS DOES NOT FIX. The same path still exists from the mission console: its `ActionRow`
 * offers **Open session** beside Approve, and the bell and push deep-link to the pane, so an
 * operator who looks first and approves second can still be refused. That is the delivery guard's
 * problem, owned by #973, and pinned as an expected failure in
 * `tests/test_console_approve_after_viewing.py`. Removing the strip does not make the
 * Approve reliable. (Measured on the author's install at the time: the ledger's pending actions
 * mostly ended `expired` or `observed`, with only a handful `stale` — the strip was barely used,
 * not provably broken on every use.)
 *
 * This test is a GUARD, not a feature: a lot of machinery pointed at `session-decisions`, and
 * re-adding the mount would quietly bring back a control on the screen most likely to invalidate it.
 * A mission-HELD decision still renders in its mission's thread, which `pulse-unified.spec.ts` and
 * `mission-console-decisions.spec.ts` pin.
 */
import { expect, test, type Page } from "@playwright/test";
import { setupBench } from "./terminal/harness";

const ENGINE = "claude";
const UUID = "dddddddd-1111-2222-3333-444444444444";
const KEY = `${ENGINE}:${UUID}`;

/** Producer-shaped, exactly as `GET /api/pulse/orchestrator` returns it since #959 review 4805 —
 *  a fixture without the operator projection would prove nothing about the real surface. */
function pendingAction() {
  return {
    id: "act_mine",
    state: "proposed",
    projection: "actionable",
    can_approve: true,
    can_reject: true,
    ts: 1_700_000_000,
    tier: "suggest",
    session_id: KEY,
    engine: ENGINE,
    title: "Fix the flaky upload retry",
    project: "demo",
    project_id: "",
    verb: "nudge",
    confidence: 0.8,
    rationale: "The agent asked whether to run the mobile spec.",
    evidence: "screen",
    answer: "yes — run the mobile compose spec, then push",
  };
}

async function openPaneWithAPendingDecision(page: Page) {
  await setupBench(page, {
    sessions: [{ engine: ENGINE, uuid: UUID, title: "Fix the flaky upload retry" }],
  });
  let approved = 0;
  await page.route(/\/api\/pulse\/orchestrator$/, (r) =>
    r.fulfill({
      json: {
        config: {},
        pending: [pendingAction()],
        feed: [],
        expired_now: 0,
        delivering_verbs: ["nudge", "continue"],
      },
    }),
  );
  // Any approval at all is a failure here: there is no control left to issue one.
  await page.route(/\/api\/pulse\/actions\/[^/]+\/approve$/, (r) => {
    approved += 1;
    return r.fulfill({ json: { ...pendingAction(), state: "delivered" } });
  });
  await page.goto(`/s/${ENGINE}/${UUID}`);
  return () => approved;
}

test("the pane shows no decision strip, and offers no Approve, for a decision about itself", async ({
  page,
}) => {
  const approvals = await openPaneWithAPendingDecision(page);

  // The terminal itself is up — otherwise this would pass on a pane that failed to render at all,
  // which is exactly how an absence assertion lies. `.xterm-rows` is what the other terminal
  // specs anchor on.
  await expect(page.locator(".xterm-rows")).toBeVisible();

  await expect(page.getByTestId("session-decisions")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^approve$/i })).toHaveCount(0);

  // The strip polled `/api/pulse/orchestrator` on mount and on a 60 s timer. Nothing here should
  // ever reach the approve route, however long the page sits.
  await page.waitForTimeout(500);
  expect(approvals()).toBe(0);
});
