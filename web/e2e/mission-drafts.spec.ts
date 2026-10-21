/** AI-drafted directions, in a real browser (#983 P3).
 *
 * A draft is model-authored text, so the card is only ever a proposal: dashed, labelled, the text
 * verbatim, and three ways out: Send as written, Edit, Dismiss. What a browser can say that jsdom
 * cannot: whether the frame is really dashed, whether the text survives the page byte for byte
 * (line breaks and doubled spaces included) on the card AND in the composer Edit fills, where focus
 * lands, what the page actually POSTs, and whether the card leaves when the server has closed the
 * draft. The console's timers are stopped, so a card can only leave because the page acted on an
 * answer, never because a background poll ran. Mocks are producer-shaped:
 * `mission_supervisor.propose_draft` for the record, `routes/pulse._operator_projection` for its
 * controls, and the relay route's `replaces_draft` answers.
 */
import { expect, test, type Locator, type Page } from "@playwright/test";

import { SESSION, T, deferred, missionConsole } from "./mission-directions";
import { openMissionConversation, openMissionDetails } from "./mission-console";

/** A line break and a doubled space, so a page that collapsed whitespace would fail. */
const DRAFT_TEXT =
  "The reviewer asked for a regression test for the retry backoff.\nAdd one next to the existing upload tests,  run it, and push.";
const EDITED = `${DRAFT_TEXT}\nThen say when the checks are green.`;
/** A second draft for the same session, proposed once the first has been replaced. */
const DRAFT_B = "Re-run the flaky upload test twenty times and report how many fail.";
const HINT =
  "Never sent on its own: it waits for your tap. Edit opens it in this session's message box, under Context, where you send it as your own message.";
/** Several lines more than the draft (and than the field's four-row minimum), so a field that grows
 *  with its content is visibly taller at every width, and still under its height cap. */
const GROWN = [
  DRAFT_TEXT,
  "Then run the whole upload suite.",
  "Then run the retry tests again.",
  "Then push.",
  "Then wait for the checks.",
  "Then say when they are green.",
  "Then stop.",
].join("\n");

function draftAction(over: Record<string, unknown> = {}) {
  return {
    id: "act_draft",
    state: "proposed",
    projection: "actionable",
    can_approve: true,
    can_reject: true,
    ts: T - 90,
    expires_at: T + 1800,
    tier: "suggest",
    session_id: SESSION,
    engine: "claude",
    title: "An AI-drafted direction is waiting for your tap",
    project: "agent-sessions",
    project_id: "p1",
    verb: "draft_direction",
    confidence: 0,
    rationale: "",
    evidence: "none",
    source: "supervisor",
    mission_id: "msn_1",
    objective_key: "review",
    objective_episode: 1,
    objective_incarnation: "inc_1",
    objective_title: "A reviewer approved the PR",
    draft: DRAFT_TEXT,
    announced: false,
    ...over,
  };
}

/** Stop every long timer before the app loads, so nothing re-reads the overview on its own. */
async function noPolls(page: Page) {
  await page.addInitScript(() => {
    const real = window.setInterval.bind(window);
    window.setInterval = ((fn: TimerHandler, ms?: number, ...rest: unknown[]) =>
      (ms ?? 0) >= 10_000 ? 0 : real(fn, ms, ...rest)) as typeof window.setInterval;
  });
}

interface DraftServer {
  pending: Record<string, unknown> | null;
  approvals: string[];
  rejects: string[];
  relays: Record<string, unknown>[];
  relayAnswer: { status: number; json: Record<string, unknown> };
}

function overview(pending: Record<string, unknown> | null) {
  return {
    cache_version: 1,
    generated_at: T - 60,
    window_days: 3,
    scan_depth: "fast",
    input_fingerprint: "fp",
    synthesis_skipped: false,
    cards: [
      {
        id: SESSION,
        engine: "claude",
        title: "Fix the flaky upload retry",
        cwd: "/repo",
        project: { kind: "project", id: "p1", name: "agent-sessions" },
        last_activity: T - 120,
        ai_summary: "",
        intervention_required: false,
        intervention_reason: "",
        live: true,
        state: pending ? "needs_you" : "in_flight",
        synthesis: null,
        mission_id: "msn_1",
        ...(pending ? { pending_action: pending } : {}),
      },
    ],
  };
}

/** The mission console with one pending draft (or `pending`), answering as the server would. The
 *  overview is re-read from `server.pending`, which only the approve, reject and relay handlers
 *  clear, exactly when the real server would have closed the draft. */
async function draftConsole(
  page: Page,
  opts: {
    pending?: Record<string, unknown>;
    /** The roster in order. The draft is always for SESSION; putting it last puts its message box
     *  below the fold of the Context column, so "scrolled into view" has something to prove. */
    sessions?: string[];
  } = {},
): Promise<DraftServer> {
  await noPolls(page);
  const server: DraftServer = {
    pending: opts.pending ?? draftAction(),
    approvals: [],
    rejects: [],
    relays: [],
    relayAnswer: {
      status: 200,
      json: {
        action_id: "relay_1",
        state: "delivered",
        detail: "",
        session_key: SESSION,
        draft_replaced: true,
        replaced_draft: "act_draft",
      },
    },
  };
  await missionConsole(page, {
    pending: server.pending,
    context: {
      id: "msn_1",
      project_id: "",
      cwd: "/repo",
      sessions: (opts.sessions ?? [SESSION]).map((session_key) => ({
        session_key,
        removed_at: null,
      })),
      git: null,
      git_error: null,
    },
  });
  // Registered after the console's own routes, so these win (newest route first).
  await page.route(/\/api\/pulse$/, (r) => r.fulfill({ json: overview(server.pending) }));
  await page.route(/\/api\/pulse\/actions\/[^/]+\/approve$/, (r) => {
    server.approvals.push(/actions\/([^/]+)\/approve/.exec(r.request().url())![1]);
    const sent = { ...draftAction(), state: "delivered", delivered_text: DRAFT_TEXT };
    server.pending = null;
    return r.fulfill({ json: { ...sent, projection: "settled", can_approve: false, can_reject: false } });
  });
  await page.route(/\/api\/pulse\/actions\/[^/]+\/reject$/, (r) => {
    server.rejects.push(/actions\/([^/]+)\/reject/.exec(r.request().url())![1]);
    server.pending = null;
    return r.fulfill({
      json: { ...draftAction(), state: "rejected", projection: "settled", can_approve: false, can_reject: false },
    });
  });
  await page.route("**/api/missions/*/relay", (r) => {
    server.relays.push(r.request().postDataJSON() as Record<string, unknown>);
    if (server.relayAnswer.json.draft_replaced === true) server.pending = null;
    return r.fulfill(server.relayAnswer);
  });
  return server;
}

/** No wording on the card may claim, or offer, a send without a tap. */
async function neverAutoSent(card: Locator) {
  await expect(card).not.toContainText(
    /automatically|auto-sent|sent on its own(?!:)|AI-written directions|confidence|Nudged/i,
  );
  await expect(card.getByTestId("draft-waits")).toHaveText("waits for your tap");
}

function composer(page: Page) {
  return page.locator(`[data-testid="mission-screen"][data-session="${SESSION}"]:visible`);
}

// --- the card in the mission console ---------------------------------------------------------------

test("D5 / B2: an AI draft is a dashed proposal showing exactly what it will type, and Send as written approves it", async ({
  page,
}) => {
  const server = await draftConsole(page);
  await openMissionConversation(page);
  const card = page.getByTestId("draft-card");
  await expect(card).toBeVisible();

  expect(await page.getByTestId("draft-row").evaluate((el) => getComputedStyle(el).borderTopStyle)).toBe(
    "dashed",
  );
  await expect(card.getByText("AI-drafted direction", { exact: true })).toBeVisible();
  await expect(card.getByTestId("draft-objective")).toHaveText("A reviewer approved the PR");
  // BYTE FOR BYTE: the DOM text, not a whitespace-normalised match.
  expect(await card.getByTestId("draft-text").evaluate((el) => el.textContent)).toBe(DRAFT_TEXT);
  await expect(card.getByTestId("draft-hint")).toHaveText(HINT);
  await expect(card.getByRole("button")).toHaveText(["Send as written", "Edit", "Dismiss"]);
  await neverAutoSent(card);

  // Send never takes focus on its own; keyboard order is Send, Edit, Dismiss.
  await expect(card.getByTestId("draft-send")).not.toBeFocused();
  await card.getByTestId("draft-send").focus();
  await page.keyboard.press("Tab");
  await expect(card.getByTestId("draft-edit")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(card.getByTestId("draft-dismiss")).toBeFocused();

  await card.getByTestId("draft-send").click();
  await expect.poll(() => server.approvals).toEqual(["act_draft"]);
  await expect(page.getByTestId("draft-card")).toHaveCount(0);
  expect(server.relays).toEqual([]);
  expect(server.rejects).toEqual([]);
});

test("D5: Edit opens the draft in its session's composer, and sending it replaces the draft as your own message; the card goes", async ({
  page,
}) => {
  // The draft's session is the LAST of three, so its message box starts below the Context column's
  // fold (the ≤1399px band is capped at 40% of the height), and only scrolling can show it.
  const server = await draftConsole(page, { sessions: ["claude:bbb", "claude:ccc", SESSION] });
  await openMissionConversation(page);
  await page.getByTestId("draft-edit").click();

  const block = composer(page);
  const input = block.getByTestId("relay-input");
  await expect(input).toBeFocused();
  expect(await input.inputValue()).toBe(DRAFT_TEXT);
  await expect(block.getByTestId("relay-draft-edit")).toBeVisible();

  // ON SCREEN, inside the Context column's own scroll (and the ≤1399px band's clip), not merely
  // focused somewhere below the fold.
  await expect(input).toBeInViewport({ ratio: 0.9 });

  // STACKED in the narrow Context column: the note, a full-width field, then Cancel and Send on
  // one row below it.
  const blockBox = (await block.boundingBox())!;
  const note = (await block.getByTestId("relay-draft-edit").boundingBox())!;
  const field = (await input.boundingBox())!;
  const cancel = (await block.getByTestId("relay-draft-cancel").boundingBox())!;
  const send = (await block.getByTestId("relay-send").boundingBox())!;
  expect(field.width, "the field is not full width").toBeGreaterThanOrEqual(0.9 * blockBox.width);
  expect(note.y + note.height, "the note is not above the field").toBeLessThanOrEqual(field.y + 1);
  expect(send.y, "Send is not below the field").toBeGreaterThanOrEqual(field.y + field.height - 1);
  expect(cancel.y, "Cancel is not below the field").toBeGreaterThanOrEqual(field.y + field.height - 1);
  expect(Math.abs(cancel.y - send.y), "Cancel and Send are not one row").toBeLessThanOrEqual(2);
  expect(cancel.x, "Cancel is not before Send").toBeLessThan(send.x);

  // …and the field grows with what is in it.
  await input.fill(GROWN);
  await expect
    .poll(async () => (await input.boundingBox())!.height, { message: "the field did not grow" })
    .toBeGreaterThan(field.height + 20);

  // Edit settles nothing: the draft is still a waiting proposal.
  await expect(page.getByTestId("draft-card")).toHaveCount(1);
  expect(server.rejects).toEqual([]);

  await input.fill(EDITED);
  await block.getByTestId("relay-send").click();
  await expect
    .poll(() => server.relays)
    .toEqual([{ session_key: SESSION, text: EDITED, replaces_draft: "act_draft" }]);
  await expect(page.getByTestId("draft-card")).toHaveCount(0);
  await expect(block.getByTestId("relay-note")).toHaveText("Sent.");
  await expect(block.getByTestId("relay-draft-edit")).toHaveCount(0);
  expect(server.approvals).toEqual([]);
  expect(server.rejects).toEqual([]);
});

test("D5: the NEXT draft for the same session opens in the composer too, with no remount", async ({
  page,
}) => {
  // The Edit generation must not restart when a successful replacement clears the current draft:
  // the composer is still mounted and would ignore a second Edit that reuses the first's number
  // (review 4887, finding 4).
  const server = await draftConsole(page);
  const next = draftAction({ id: "act_draft_b", draft: DRAFT_B });
  await page.route("**/api/missions/*/relay", (r) => {
    server.relays.push(r.request().postDataJSON() as Record<string, unknown>);
    server.pending = next; // the server closed A and has already proposed B
    return r.fulfill({
      json: {
        action_id: "relay_1",
        state: "delivered",
        detail: "",
        session_key: SESSION,
        draft_replaced: true,
        replaced_draft: "act_draft",
      },
    });
  });
  await openMissionConversation(page);
  const block = composer(page);

  await page.getByTestId("draft-edit").click();
  await block.getByTestId("relay-send").click();
  await expect.poll(() => server.relays.length).toBe(1);

  // B arrives on the console's re-read, and Edit must arm the composer with it.
  await expect(page.getByTestId("draft-text")).toHaveText(DRAFT_B);
  await page.getByTestId("draft-edit").click();
  await expect(block.getByTestId("relay-input")).toHaveValue(DRAFT_B);
  await expect(block.getByTestId("relay-draft-edit")).toBeVisible();
  await block.getByTestId("relay-send").click();
  await expect.poll(() => server.relays.length).toBe(2);
  expect(server.relays[1]).toEqual({
    session_key: SESSION,
    text: DRAFT_B,
    replaces_draft: "act_draft_b",
  });
});

test("D5: a delivered ordinary relay does not erase a draft adopted while it was in flight", async ({
  page,
}) => {
  // Edit can arm the composer while an ordinary message is still out. That message's own answer
  // must not empty the box the operator has just had filled (review 4887, finding 3).
  const server = await draftConsole(page);
  const gate = deferred();
  await page.route("**/api/missions/*/relay", async (r) => {
    server.relays.push(r.request().postDataJSON() as Record<string, unknown>);
    await gate.promise;
    return r.fulfill({
      json: { action_id: "relay_1", state: "delivered", detail: "", session_key: SESSION },
    });
  });
  // The message box lives under Context, which Edit opens on its own; an ordinary message means
  // opening it first.
  await openMissionDetails(page, "context");
  const block = composer(page);
  await block.getByTestId("relay-input").fill("are you there?");
  await block.getByTestId("relay-send").click();
  await expect.poll(() => server.relays.length).toBe(1);

  await page.getByTestId("draft-edit").click();
  await expect(block.getByTestId("relay-input")).toHaveValue(DRAFT_TEXT);

  gate.release();
  await expect(block.getByTestId("relay-note")).toHaveText("Sent.");
  await expect(block.getByTestId("relay-input")).toHaveValue(DRAFT_TEXT);
  await expect(block.getByTestId("relay-draft-edit")).toBeVisible();
  await block.getByTestId("relay-send").click();
  await expect.poll(() => server.relays.length).toBe(2);
  expect(server.relays[1]).toEqual({
    session_key: SESSION,
    text: DRAFT_TEXT,
    replaces_draft: "act_draft",
  });
});

test("D5: an edit that lost the race to its draft sends nothing, keeps the text, and says why", async ({
  page,
}) => {
  const server = await draftConsole(page);
  const refusal =
    "nothing was sent: the AI's draft is already delivered, so your edit did not replace it";
  server.relayAnswer = {
    status: 409,
    json: { detail: refusal, draft_replaced: false, draft_state: "delivered" },
  };
  await openMissionConversation(page);
  await page.getByTestId("draft-edit").click();
  const block = composer(page);
  const input = block.getByTestId("relay-input");
  await input.fill(EDITED);
  await block.getByTestId("relay-send").click();

  await expect(block.getByTestId("relay-note")).toHaveText(`Not sent — ${refusal}.`);
  expect(await input.inputValue()).toBe(EDITED);
  await expect(block.getByTestId("relay-draft-edit")).toHaveCount(0);
  expect(server.relays).toHaveLength(1);
});

test("D5: Dismiss rejects the draft, and nothing is typed", async ({ page }) => {
  const server = await draftConsole(page);
  await openMissionConversation(page);
  await page.getByTestId("draft-dismiss").click();
  await expect.poll(() => server.rejects).toEqual(["act_draft"]);
  await expect(page.getByTestId("draft-card")).toHaveCount(0);
  expect(server.approvals).toEqual([]);
  expect(server.relays).toEqual([]);
});

test("D5: the card never shows a sent-on-its-own state, even for a draft the server reports in flight", async ({
  page,
}) => {
  await draftConsole(page, {
    pending: draftAction({ state: "approved", projection: "in_flight_revocable", can_approve: false }),
  });
  await openMissionConversation(page);
  const card = page.getByTestId("draft-card");
  await expect(card).toBeVisible();
  await neverAutoSent(card);
  await expect(card.getByTestId("draft-send")).toHaveCount(0);
  await expect(card.getByRole("button")).toHaveText(["Edit", "Dismiss"]);
});

// --- the session's own pane ------------------------------------------------------------------------

test("B2: on a phone the card's Send as written, Edit and Dismiss are at least 44px tall", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "the 44px floor is a phone measurement");
  await draftConsole(page);
  await openMissionConversation(page);
  const card = page.getByTestId("draft-card");
  for (const id of ["draft-send", "draft-edit", "draft-dismiss"]) {
    const box = await card.getByTestId(id).boundingBox();
    expect(box, id).not.toBeNull();
    expect(box!.height, id).toBeGreaterThanOrEqual(44);
  }
});
