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

import { CONFIG, ORCH, SESSION, T, missionConsole, missionControlSettings } from "./mission-directions";
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

/** A console whose mission policy has AI-written directions ON, served the way the real routes do:
 *  `/api/config` carries both scopes with their revisions, and `PATCH /api/automation/{scope}`
 *  refuses a stale revision with 409 and otherwise merges and bumps it. Records every policy write
 *  and every `POST /api/prefs` (which Turn off must never use). */
async function turnOffServer(page: Page) {
  const session = { ...ORCH, revision: "session-1" };
  const state = {
    patches: [] as { url: string; body: { revision: string; policy: Record<string, unknown> } }[],
    legacy: [] as unknown[],
    unreadable: false,
    sessionBefore: { ...session },
    policies: {
      session,
      mission: {
        ...ORCH,
        autonomy: "yolo",
        auto_ai_directions: true,
        ai_direction_confidence_min: 0.95,
        revision: "mission-1",
      } as Record<string, unknown>,
    },
  };
  // The first config read happens inside `missionConsole` (it navigates), so the mission policy
  // it serves is this one, revision included.
  await missionConsole(page, {
    events: [autoEvent()],
    orchestrator: state.policies.mission,
  });
  // Registered after `missionConsole`, so these win for every later read and write.
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        ...CONFIG,
        automation: { version: 1, legacy_compatible: false, ...state.policies },
      },
    }),
  );
  await page.route("**/api/prefs", (r) => {
    state.legacy.push(r.request().postDataJSON());
    return r.fulfill({ json: {} });
  });
  await page.route("**/api/automation/*", (r) => {
    const url = new URL(r.request().url()).pathname;
    const scope = url.split("/").at(-1) as "session" | "mission";
    const body = r.request().postDataJSON();
    state.patches.push({ url, body });
    if (state.unreadable)
      return r.fulfill({ status: 503, json: { detail: "Policy unreadable" } });
    if (body.revision !== state.policies[scope].revision)
      return r.fulfill({ status: 409, json: { detail: "automation policy changed; reload before saving" } });
    const next = { ...state.policies[scope], ...body.policy, revision: `${scope}-${state.patches.length + 1}` };
    (state.policies as Record<string, unknown>)[scope] = next;
    return r.fulfill({ json: { version: 1, scope, policy: next } });
  });
  return state;
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

  test("on YOLO it is enabled, and turning it on saves the mission policy", async ({ page }) => {
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
    const copy = page.getByRole("region", { name: "Mission orchestration", exact: true }).getByTestId("orchestrator-yolo-copy");
    await expect(copy).toContainText(/HQ types text you wrote/i);
    await expect(copy).not.toContainText(/One exception/i);
  });

  test("with it on it names the exception instead", async ({ page }) => {
    await missionControlSettings(page, {
      autonomy: "yolo",
      auto_ai_directions: true,
      ai_direction_confidence_min: 0.95,
    });
    const copy = page.getByRole("region", { name: "Mission orchestration", exact: true }).getByTestId("orchestrator-yolo-copy");
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

  // #2179: Turn off writes the MISSION scope through the revisioned `PATCH /api/automation/mission`
  // — never the retired `POST /api/prefs {orchestrator}` bridge — with only the one field it turns
  // off, and the row keeps offering it until the server has accepted the write.
  test("Turn off patches the mission policy with the revision it read", async ({ page }) => {
    const f = await turnOffServer(page);
    await openMissionConversation(page);
    await page.getByTestId("thread-turn-off-auto").click();
    await expect.poll(() => f.patches.length).toBe(1);
    expect(f.patches[0]).toEqual({
      url: "/api/automation/mission",
      body: { revision: "mission-1", policy: { auto_ai_directions: false } },
    });
    // Accepted → the config is re-read and the mode is off, so the offer goes away.
    await expect(page.getByTestId("thread-turn-off-auto")).toHaveCount(0);
    expect(f.policies.mission.auto_ai_directions).toBe(false);
    expect(f.policies.mission.ai_direction_confidence_min).toBe(0.95); // other fields untouched
    expect(f.policies.session).toEqual(f.sessionBefore); // the session policy untouched
    expect(f.legacy).toEqual([]);
  });

  test("a policy changed elsewhere reloads, says so, and is not retried", async ({ page }) => {
    const f = await turnOffServer(page);
    await openMissionConversation(page);
    // Another tab raised the threshold after this page read the policy.
    f.policies.mission = { ...f.policies.mission, ai_direction_confidence_min: 0.97, revision: "mission-2" };
    await page.getByTestId("thread-turn-off-auto").click();
    await expect(page.getByTestId("thread-turn-off-error")).toContainText(
      "The mission policy changed elsewhere. Latest values loaded; review them and turn it off again.",
    );
    expect(f.patches).toHaveLength(1);
    expect(f.patches[0].body.revision).toBe("mission-1");
    // Still on, and still offered: nothing claims it is off before an accepted write.
    await expect(page.getByTestId("thread-turn-off-auto")).toBeEnabled();
    expect(f.policies.mission.auto_ai_directions).toBe(true);
    // A second tap uses the reloaded revision.
    await page.getByTestId("thread-turn-off-auto").click();
    await expect.poll(() => f.patches.length).toBe(2);
    expect(f.patches[1].body.revision).toBe("mission-2");
    await expect(page.getByTestId("thread-turn-off-auto")).toHaveCount(0);
    expect(f.legacy).toEqual([]);
  });

  test("a failed or unreadable policy write is visible and leaves the offer in place", async ({ page }) => {
    const f = await turnOffServer(page);
    await openMissionConversation(page);
    f.unreadable = true;
    await page.getByTestId("thread-turn-off-auto").click();
    await expect(page.getByTestId("thread-turn-off-error")).toContainText("Policy unreadable");
    await expect(page.getByTestId("thread-turn-off-auto")).toBeEnabled();
    expect(f.policies.mission.auto_ai_directions).toBe(true);
    expect(f.legacy).toEqual([]);
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
