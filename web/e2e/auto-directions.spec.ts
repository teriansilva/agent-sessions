/** Autonomous AI-written directions, in a real browser (#983 P4).
 *
 * This is the one mode in which a model authors the bytes typed into a permission-bypassed agent,
 * so the browser has to prove the things jsdom cannot: that the toggle is genuinely un-clickable
 * off YOLO (not merely styled as such), that the honest-limits warning is ON THE PAGE rather than
 * in a tooltip nobody opens, what the page actually POSTs, and that the copy on the draft card and
 * in the YOLO paragraph tracks the pref instead of describing the default.
 *
 * Mocks are producer-shaped: the orchestrator block as `prefs.public_orchestrator` serves it, and
 * the thread event as `missions.ensure_delivered_nudge_event` writes it for an autonomous send.
 */
import { expect, test, type Page } from "@playwright/test";

import { SESSION, T, missionConsole, missionControlSettings } from "./mission-directions";
import { openMissionConversation } from "./mission-console";

const TYPED =
  "The reviewer asked for a regression test for the retry backoff.\nAdd one next to the existing upload tests, run it, and push.";

/** A delivered thread event for an AUTONOMOUS send, exactly as the server records one. */
function autoEvent(over: Record<string, unknown> = {}) {
  return {
    seq: 4,
    mission_id: "msn_1",
    at: T - 30,
    kind: "action",
    session_key: SESSION,
    action_id: "act_auto",
    text: TYPED,
    meta: {
      source: "supervisor",
      objective_key: "review",
      episode: 1,
      delivered: true,
      text_source: "ai_auto",
      auto: true,
      confidence: 0.97,
      digest: null,
      stage: "delivered",
      ...over,
    },
    settlement: null,
  };
}

/** A draft still waiting, as `propose_draft` mints it and the pulse route projects it. */
function draftAction(over: Record<string, unknown> = {}) {
  return {
    id: "act_draft",
    state: "proposed",
    projection: "actionable",
    can_approve: true,
    can_reject: true,
    ts: T - 90,
    expires_at: T + 1800,
    tier: "yolo",
    session_id: SESSION,
    engine: "claude",
    title: "An AI-drafted direction is waiting for your tap",
    project: "agent-sessions",
    project_id: "p1",
    verb: "draft_direction",
    confidence: 0.4,
    rationale: "",
    evidence: "none",
    source: "supervisor",
    mission_id: "msn_1",
    objective_key: "review",
    objective_episode: 1,
    objective_incarnation: "inc_1",
    objective_title: "A reviewer approved the PR",
    draft: TYPED,
    announced: false,
    ...over,
  };
}

/** Record every prefs save the page makes, winning over any route registered earlier. */
async function recordPrefs(page: Page) {
  const saves: Record<string, unknown>[] = [];
  await page.route("**/api/prefs", (r) => {
    const body = r.request().postDataJSON() as {
      orchestrator?: Record<string, unknown>;
    } | null;
    saves.push(body?.orchestrator ?? {});
    return r.fulfill({ json: {} });
  });
  return saves;
}

test.describe("the settings toggle", () => {
  test("is disabled off YOLO, and says so beside the warning", async ({ page }) => {
    await missionControlSettings(page, { autonomy: "suggest", auto_ai_directions: false });
    const toggle = page.getByTestId("auto-ai-toggle");
    await expect(toggle).toBeVisible();
    await expect(toggle).toBeDisabled();
    await expect(page.getByTestId("auto-ai-tier-note")).toContainText(/YOLO/);
    // The warning is ON THE PAGE, not behind a hover: these are the honest limits.
    const warn = page.getByTestId("auto-ai-warning");
    await expect(warn).toBeVisible();
    await expect(warn).toContainText(/not a safety check/i);
    await expect(warn).toContainText(/untrusted/i);
    await expect(warn).toContainText(/[Nn]obody reads the text/);
    // The announcement is part of the mitigation the risk was accepted on, so the fact that it
    // ignores a turned-down `notify` has to be visible HERE, where the operator opts in.
    await expect(warn).toContainText(/announced, even if you have notifications turned down/i);
    // …and with the mode off there is no threshold to set.
    await expect(page.getByTestId("auto-ai-threshold")).toHaveCount(0);
  });

  test("on YOLO it is enabled, and turning it on POSTs the pref", async ({ page }) => {
    const saves = await missionControlSettings(page, {
      autonomy: "yolo",
      auto_ai_directions: false,
    });
    const toggle = page.getByTestId("auto-ai-toggle");
    await expect(toggle).toBeEnabled();
    await expect(toggle).not.toBeChecked();
    // `click`, not `check`: the box is CONTROLLED by the saved block, so it only flips once the
    // save round-trips. `check()` asserts the new state the instant it clicks and would fail on
    // the round trip rather than on the behaviour. What matters is what the page SAVED…
    await toggle.click();
    await expect.poll(() => saves.length).toBeGreaterThan(0);
    expect(saves.at(-1)).toEqual({ auto_ai_directions: true });
    // …and that the panel then shows the state the server confirmed, which retries properly.
    await expect(toggle).toBeChecked();
    await expect(page.getByTestId("auto-ai-threshold")).toBeVisible();
  });

  test("with it on, the threshold offers the approved floor and no less", async ({ page }) => {
    await missionControlSettings(page, { autonomy: "yolo", auto_ai_directions: true });
    const slider = page.getByTestId("auto-ai-threshold");
    await expect(slider).toBeVisible();
    // The FLOOR is the approved 0.90 — the control must not be able to ask for less.
    await expect(slider).toHaveAttribute("min", "0.9");
    await expect(slider).toHaveAttribute("max", "1");
    await expect(page.getByTestId("auto-ai-threshold-value")).toHaveText("0.90");
  });
});

test.describe("the YOLO paragraph tells the truth about what is typed", () => {
  test("with the mode off it is P3's copy, claiming only your own words are sent", async ({
    page,
  }) => {
    await missionControlSettings(page, { autonomy: "yolo", auto_ai_directions: false });
    const copy = page.getByTestId("orchestrator-yolo-copy");
    await expect(copy).toContainText(/only ever types text/i);
    await expect(copy).not.toContainText(/One exception/i);
  });

  test("with it on it names the exception instead", async ({ page }) => {
    await missionControlSettings(page, {
      autonomy: "yolo",
      auto_ai_directions: true,
      ai_direction_confidence_min: 0.95,
    });
    const copy = page.getByTestId("orchestrator-yolo-copy");
    await expect(copy).toContainText(/One exception, which you turned on/i);
    await expect(copy).toContainText("0.95");
    await expect(copy).toContainText(/nobody reading it first/i);
  });
});

test.describe("an autonomously sent direction in the thread", () => {
  test("is labelled, shows its exact text and confidence, and offers Turn off", async ({
    page,
  }) => {
    await missionConsole(page, {
      events: [autoEvent()],
      orchestrator: { autonomy: "yolo", auto_ai_directions: true },
    });
    await openMissionConversation(page);
    const row = page.getByTestId("thread-nudged");
    await expect(row).toBeVisible();
    await expect(row).toHaveAttribute("data-source", "ai_auto");
    await expect(row).toContainText("AI-written · sent automatically");
    await expect(page.getByTestId("thread-nudged-confidence")).toHaveText("confidence 0.97");
    // The exact bytes, behind Show text — verbatim, line break and all.
    await page.getByTestId("thread-nudged-toggle").click();
    await expect(page.getByTestId("thread-nudged-text")).toHaveText(TYPED);
  });

  test("Turn off posts the pref that switches the mode off", async ({ page }) => {
    await missionConsole(page, {
      events: [autoEvent()],
      orchestrator: { autonomy: "yolo", auto_ai_directions: true },
    });
    const saves = await recordPrefs(page);
    await openMissionConversation(page);
    await page.getByTestId("thread-turn-off-auto").click();
    await expect.poll(() => saves.length).toBeGreaterThan(0);
    expect(saves.at(-1)).toEqual({ auto_ai_directions: false });
  });

  test("a row from a mode already switched off offers no Turn off", async ({ page }) => {
    await missionConsole(page, {
      events: [autoEvent()],
      orchestrator: { autonomy: "yolo", auto_ai_directions: false },
    });
    await openMissionConversation(page);
    // The row still reads as an unreviewed send — it is history, drawn from the RECORDED source.
    await expect(page.getByTestId("thread-nudged")).toHaveAttribute("data-source", "ai_auto");
    await expect(page.getByTestId("thread-turn-off-auto")).toHaveCount(0);
  });
});

test.describe("the drafted-direction card's promise follows the pref", () => {
  test("with the mode off it keeps P3's wording exactly", async ({ page }) => {
    await missionConsole(page, {
      pending: draftAction(),
      orchestrator: { autonomy: "yolo", auto_ai_directions: false },
    });
    await openMissionConversation(page);
    await expect(page.getByTestId("draft-hint")).toContainText(
      "Never sent on its own: it waits for your tap.",
    );
    await expect(page.getByTestId("draft-waits")).toHaveText("waits for your tap");
  });

  test("with it on it says a draft at the threshold goes on its own", async ({ page }) => {
    await missionConsole(page, {
      pending: draftAction(),
      orchestrator: {
        autonomy: "yolo",
        auto_ai_directions: true,
        ai_direction_confidence_min: 0.95,
      },
    });
    await openMissionConversation(page);
    const hint = page.getByTestId("draft-hint");
    await expect(hint).not.toContainText("Never sent on its own");
    await expect(hint).toContainText("0.95 or higher");
    await expect(hint).toContainText(/nobody reading it first/i);
    // This draft is BELOW the threshold, so it is still the proposal card, with its three ways out.
    await expect(page.getByTestId("draft-waits")).toHaveText("sent on its own at 0.95+");
    await expect(page.getByTestId("draft-send")).toBeVisible();
    await expect(page.getByTestId("draft-dismiss")).toBeVisible();
    await expect(page.getByTestId("draft-text")).toHaveText(TYPED);
  });
});
