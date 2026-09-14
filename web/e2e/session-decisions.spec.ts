/** A pending mission-control decision renders in its SESSION's pane (#948 P3).
 *
 * The "Sessions without a mission" view in the Missions section used to be the only surface for a
 * decision about a session no mission held. That view is gone, so the decision comes to the session
 * — which is where the bell already links. Only THIS session's pending actions show, and approving
 * one settles it through the existing route.
 *
 * The fixtures are PRODUCER-SHAPED: `GET /api/pulse/orchestrator`'s `pending` rows carry the
 * operator projection (`projection` / `can_approve` / `can_reject`), as the route returns them since
 * #959 review 4805. A fixture without them tests `ActionRow`'s legacy fallback, not this surface.
 */
import { expect, test, type Page } from "@playwright/test";
import { setupBench } from "./terminal/harness";

const ENGINE = "claude";
const UUID = "dddddddd-1111-2222-3333-444444444444";
const KEY = `${ENGINE}:${UUID}`;
const OTHER_KEY = "claude:eeeeeeee-1111-2222-3333-444444444444";

function action(id: string, sessionId: string, rationale: string, over: Record<string, unknown> = {}) {
  return {
    id,
    state: "proposed",
    projection: "actionable",
    can_approve: true,
    can_reject: true,
    ts: 1_700_000_000,
    tier: "suggest",
    session_id: sessionId,
    engine: ENGINE,
    title: "Fix the flaky upload retry",
    project: "demo",
    project_id: "",
    verb: "nudge",
    confidence: 0.8,
    rationale,
    evidence: "screen",
    answer: "yes — run the mobile compose spec, then push",
    ...over,
  };
}

type Action = ReturnType<typeof action>;

async function setup(page: Page, initial?: Action[]) {
  await setupBench(page, { sessions: [{ engine: ENGINE, uuid: UUID, title: "Fix the flaky upload retry" }] });
  let pending = initial ?? [
    action("act_mine", KEY, "The agent asked whether to run the mobile spec."),
    action("act_other", OTHER_KEY, "Someone else's decision."),
  ];
  const approvals: string[] = [];
  await page.route(/\/api\/pulse\/orchestrator$/, (r) =>
    r.fulfill({
      json: { config: {}, pending, feed: [], expired_now: 0, delivering_verbs: ["nudge", "continue"] },
    }),
  );
  await page.route(/\/api\/pulse\/actions\/[^/]+\/approve$/, (r) => {
    const id = /actions\/([^/]+)\/approve/.exec(r.request().url())![1];
    approvals.push(id);
    const settled = { ...pending.find((a) => a.id === id)!, state: "delivered" };
    pending = pending.filter((a) => a.id !== id);
    return r.fulfill({ json: settled });
  });
  return { approvals };
}

/** The strip's item for the action whose state line reads `state`. */
function itemInState(page: Page, state: string) {
  return page
    .getByTestId("session-decisions")
    .getByTestId("session-state")
    .filter({ hasText: new RegExp(`^${state}$`) })
    .locator("xpath=..");
}

test("the session pane shows its own pending decision, and only its own", async ({ page }) => {
  await setup(page);
  await page.goto(`/s/${ENGINE}/${UUID}`);
  const strip = page.getByTestId("session-decisions");
  await expect(strip).toBeVisible();
  await expect(strip).toContainText("The agent asked whether to run the mobile spec.");
  await expect(strip).not.toContainText("Someone else's decision.");
});

test("approving from the pane settles the decision through the existing route", async ({ page }) => {
  const { approvals } = await setup(page);
  await page.goto(`/s/${ENGINE}/${UUID}`);
  const strip = page.getByTestId("session-decisions");
  await strip.getByRole("button", { name: /approve/i }).first().click();
  await expect.poll(() => approvals).toEqual(["act_mine"]);
  await expect(page.getByTestId("session-decisions")).toHaveCount(0);
});

test("the controls are the server's projection: Approve on a low-confidence escalation, not on an approved action", async ({
  page,
}) => {
  // #959 review 4805, finding 1. The low-confidence escalation keeps its real `continue` verb and
  // the server honours approving it; the `approved` action is already approved, so Approve is
  // withdrawn and only Reject remains. Both rows' states alone would have said the opposite.
  await setup(page, [
    action("act_low", KEY, "Confidence fell below the threshold.", {
      state: "escalated_low_confidence",
      verb: "continue",
      confidence: 0.4,
    }),
    action("act_done", KEY, "Already approved, not yet delivered.", {
      state: "approved",
      projection: "in_flight_revocable",
      can_approve: false,
      verb: "continue",
    }),
  ]);
  await page.goto(`/s/${ENGINE}/${UUID}`);

  const low = itemInState(page, "escalated_low_confidence");
  await expect(low).toContainText("Confidence fell below the threshold.");
  await expect(low.getByRole("button", { name: "Approve" })).toBeVisible();
  await expect(low.getByRole("button", { name: "Reject this action" })).toBeVisible();

  const done = itemInState(page, "approved");
  await expect(done).toContainText("Already approved, not yet delivered.");
  await expect(done.getByRole("button", { name: "Reject this action" })).toBeVisible();
  await expect(done.getByRole("button", { name: /approve/i })).toHaveCount(0);
});

test("a refused LAST decision says nothing was sent, even when the re-read fails", async ({ page }) => {
  // #959 review 4805, finding 2. A 409 carries the settled record, which removes the row; when it
  // was the session's only pending action the strip used to unmount and take the explanation with
  // it — indistinguishable from a successful delivery.
  await setupBench(page, { sessions: [{ engine: ENGINE, uuid: UUID, title: "Fix the flaky upload retry" }] });
  const mine = action("act_mine", KEY, "The agent asked whether to run the mobile spec.");
  let approveTapped = false;
  await page.route(/\/api\/pulse\/orchestrator$/, (r) =>
    approveTapped
      ? r.fulfill({ status: 500, json: { detail: "ledger unreadable" } })
      : r.fulfill({
          json: {
            config: {},
            pending: [mine, action("act_other", OTHER_KEY, "Someone else's decision.")],
            feed: [],
            expired_now: 0,
            delivering_verbs: ["nudge"],
          },
        }),
  );
  let orchestratorReadsAfterTap = 0;
  page.on("request", (req) => {
    if (approveTapped && /\/api\/pulse\/orchestrator$/.test(req.url())) orchestratorReadsAfterTap += 1;
  });
  await page.route(/\/api\/pulse\/actions\/[^/]+\/approve$/, (r) => {
    approveTapped = true;
    // The server's compare-and-execute verdict: the SETTLED record plus its explanation.
    return r.fulfill({
      status: 409,
      json: {
        ...mine,
        state: "stale",
        projection: "settled",
        can_approve: false,
        can_reject: false,
        detail: "the session moved on before this was approved",
      },
    });
  });

  await page.goto(`/s/${ENGINE}/${UUID}`);
  const strip = page.getByTestId("session-decisions");
  await strip.getByRole("button", { name: "Approve" }).click();
  // The settle announces a resolution, which re-reads — and that read fails.
  await expect.poll(() => orchestratorReadsAfterTap).toBeGreaterThan(0);

  const status = strip.getByRole("status");
  await expect(status).toHaveText("Not sent — the session moved on before this was approved");
  await expect(strip.getByRole("button", { name: /approve/i })).toHaveCount(0);
  await expect(strip.getByTestId("session-state")).toHaveCount(0);
});
