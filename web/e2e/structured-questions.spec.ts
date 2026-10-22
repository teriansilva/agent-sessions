import { expect, test, type Page, type Route } from "@playwright/test";
import { ACTIVE, ID, mockStructuredGuidance } from "./structuredGuidanceFixture";
import { TOUCH_FLOOR } from "./touchTarget";

// #1463: an API agent's question shows its options. Codex's async question (it keeps working)
// is answered by the operator's next message; the buttons compose and send that message.
type Json = Record<string, unknown>;

const ASKED = {
  title: "Should project restrictions also prevent users' agents from reading other projects' files?",
  options: ["Enforce project isolation (Recommended)", "UI/API restrictions for trusted teammates only"],
};
const PROSE = `${ASKED.title}\n\n- ${ASKED.options[0]}\n- ${ASKED.options[1]}`;
const REFUSED = "that question was already answered or a newer one was asked";

type Conversation = { turns: Json[]; revision: number };

/** The server's rule (`native_journal._question_open`): a replay observes the original; an answer
 *  is admitted only while the question it names (`reply_to`) is the newest one and no operator
 *  message follows it. Anything else is recorded as an ordinary message. */
function serve(state: Conversation, body: Json, turn: Json = {}): "replay" | "admitted" | "refused" {
  if (state.turns.some((t) => t.turn_id === body.operation_id)) return "replay";
  if (body.reply_to !== undefined) {
    let open: unknown = null;
    for (let i = state.turns.length - 1; i >= 0; i--) {
      const t = state.turns[i];
      if (t.questions) { open = t.question_id; break; }
      if (t.kind !== "agent") break;
    }
    if (open !== body.reply_to) return "refused";
  }
  state.turns.push({
    ...state.turns[0], turn_id: body.operation_id, operation_id: body.operation_id, state: "queued",
    text: body.text, reply: "", questions: undefined, question_id: undefined, tools: [], ...turn,
  });
  state.revision++;
  return "admitted";
}
const refuse = (r: Route) => r.fulfill({ status: 409, json: { detail: REFUSED } });

async function asked(page: Page, theme = "dark", questions: Json[] = [ASKED], handoff = false) {
  const fixture = await mockStructuredGuidance(page, "codex-api", "steer", theme);
  const { state } = fixture;
  state.state = "running";
  state.turns = [{
    ...state.turns[0], text: "Plan multi user in battlelab.", reply: PROSE, questions, question_id: "call-1",
  }];
  const submits: Json[] = [];
  let reply: "ok" | "lost" | "refused" = "ok";
  await page.route(new RegExp(`/api/structured/sessions/codex-api(?::|%3A)${ID}/turns$`), (r) => {
    const body = r.request().postDataJSON() as Json;
    submits.push(body);
    if (reply === "refused") return r.fulfill({ status: 422, json: { detail: "the session is read only" } });
    if (serve(state, body) === "refused") return refuse(r);
    if (reply === "lost") return r.abort("failed");
    return r.fulfill({ status: 202, json: { state: "queued" } });
  });
  await page.goto(`/s/codex-api/${ID}`);
  if (handoff) await page.getByRole("button", { name: "Open here instead" }).click();
  await expect(page.getByTestId("structured-reply")).toBeVisible();
  return { ...fixture, submits, reply: (r: typeof reply) => { reply = r; } };
}

const block = (page: Page) => page.getByTestId("structured-question");
const options = (page: Page) => page.getByTestId("structured-question-option");
const send = (page: Page) => page.getByTestId("structured-question-send");

for (const theme of ["dark", "light"]) {
  test(`#1463: Codex's async question shows its options, and Send answer sends the chosen one (${theme})`, async ({ page }, testInfo) => {
    const { submits, state } = await asked(page, theme);
    await expect(block(page)).toBeVisible();
    await expect(block(page)).toContainText("keeps working meanwhile");
    await expect(options(page)).toHaveCount(2);
    for (const option of await options(page).all()) {
      expect((await option.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR); // design §8
    }
    await expect(send(page)).toBeDisabled();
    await page.screenshot({ path: testInfo.outputPath(`question-open-${theme}.png`), fullPage: true });
    // An unsent draft in the composer is the operator's: the answer neither sends nor clears it.
    const composer = page.getByRole("textbox").last();
    await composer.fill("Half-written thought");
    // Keyboard reaches the options.
    await options(page).first().locator("input").focus();
    await expect(options(page).first().locator("input")).toBeFocused();
    await options(page).first().click();
    await expect(block(page)).toContainText(`“${ASKED.options[0]}”`);
    await page.screenshot({ path: testInfo.outputPath(`question-picked-${theme}.png`), fullPage: true });
    await send(page).click();
    await expect.poll(() => submits.length).toBe(1);
    expect(submits[0].text).toBe(ASKED.options[0]);
    expect(typeof submits[0].operation_id).toBe("string");
    // It names the question it answers, so the server can refuse it once that question closed.
    expect(submits[0].reply_to).toBe("call-1");
    // Codex is still working: the answer queues like any message, and the buttons go away.
    expect(submits[0].expected_revision).toBeUndefined();
    await expect(block(page)).toHaveCount(0);
    await expect(page.getByTestId("structured-turn").last()).toContainText(ASKED.options[0]);
    expect(state.turns).toHaveLength(2);
    await expect(composer).toHaveValue("Half-written thought");
  });
}

test("#1463: several questions — Send waits for every answer and sends one line per question", async ({ page }) => {
  const channel = { title: "Which channel ships it first?", options: ["main", "stable"] };
  const { submits } = await asked(page, "dark", [ASKED, channel]);
  await expect(options(page)).toHaveCount(4);
  await options(page).nth(1).click();
  await expect(send(page)).toBeDisabled();
  await expect(block(page)).toContainText("1 of 2 answered");
  await options(page).nth(3).click();
  await send(page).click();
  await expect.poll(() => submits.length).toBe(1);
  expect(submits[0].text).toBe(`${ASKED.title}: ${ASKED.options[1]}\n${channel.title}: stable`);
});

test("#1463: a message sent after the question closes it; prose alone is never a menu", async ({ page }) => {
  const { state } = await asked(page);
  await expect(block(page)).toBeVisible();
  state.turns.push({ ...state.turns[0], turn_id: "33333333-2222-4333-8444-555555555555",
    operation_id: "33333333-2222-4333-8444-555555555555", state: "queued",
    text: "Isolation, please.", reply: "", questions: undefined, tools: [] });
  state.revision++;
  await expect(block(page)).toHaveCount(0);
  // The question's own words stay in the reply.
  await expect(page.getByTestId("structured-reply").first()).toContainText(ASKED.options[0]);
  // A reply that only LISTS options, with no structured question, offers no buttons.
  state.turns = [{ ...state.turns[0], questions: undefined, turn_id: ACTIVE }];
  state.revision++;
  await expect(page.getByTestId("structured-reply")).toContainText(ASKED.options[1]);
  await expect(block(page)).toHaveCount(0);
});

test("#1463: a recorded answer whose response was lost is reconciled, and a later question with the same option gets its own message", async ({ page }) => {
  const { submits, state } = await asked(page);
  const route = new RegExp(`/api/structured/sessions/codex-api(?::|%3A)${ID}/turns$`);
  let lose = true;
  await page.route(route, (r) => {
    const body = r.request().postDataJSON() as Json;
    submits.push(body);
    if (serve(state, body, { state: "running" }) === "refused") return refuse(r);
    // Recorded, but the first response never comes back and the read-back right after fails.
    if (lose) return r.abort("failed");
    return r.fulfill({ status: 202, json: { state: "queued" } });
  });
  let failReads = 0;
  const snapshotRoute = new RegExp(`/api/structured/sessions/codex-api(?::|%3A)${ID}$`);
  await page.route(snapshotRoute, (r) => (failReads-- > 0 ? r.abort("failed") : r.fallback()));
  await options(page).first().click();
  failReads = 1;
  await send(page).click();
  await expect.poll(() => submits.length).toBe(1);
  // The next good read shows the recorded message: the hold clears, the question closes.
  await expect(block(page)).toHaveCount(0);
  lose = false;
  // Codex asks again, offering the same option. Answering it is a NEW message.
  const first = submits[0].operation_id as string;
  const answered = state.turns.find((t) => t.turn_id === first)!;
  answered.state = "completed";
  answered.reply = "Noted. One more:";
  answered.questions = [ASKED];
  answered.question_id = "call-3";
  state.revision++;
  await expect(block(page)).toBeVisible();
  await options(page).first().click();
  await send(page).click();
  await expect.poll(() => submits.length).toBe(2);
  expect(submits[1].operation_id).not.toBe(first);
  expect(submits[1].text).toBe(ASKED.options[0]);
  expect(submits[1].reply_to).toBe("call-3");
});

const B = { title: "Which channel ships it first?", options: ["main", "stable"] };
const B_TURN = "44444444-2222-4333-8444-555555555555";
const askB = (state: { turns: Json[]; revision: number }) => {
  state.turns.push({ kind: "agent", turn_id: B_TURN, state: "completed", reply: "Also:",
    reply_truncated: false, tools: [], tools_truncated: false, questions: [B], question_id: "call-2" });
  state.revision++;
};

test("#1463: an answer recorded before a newer question is reconciled, and the newer question stays open", async ({ page }) => {
  const { submits, state } = await asked(page);
  const route = new RegExp(`/api/structured/sessions/codex-api(?::|%3A)${ID}/turns$`);
  await page.route(route, (r) => {
    const body = r.request().postDataJSON() as Json;
    submits.push(body);
    serve(state, body, { state: "running" });
    // Recorded — then the response and the read-back right after it are lost.
    return r.abort("failed");
  });
  let failReads = 1;
  await page.route(new RegExp(`/api/structured/sessions/codex-api(?::|%3A)${ID}$`),
    (r) => (failReads-- > 0 ? r.abort("failed") : r.fallback()));
  await options(page).first().click();
  await send(page).click();
  await expect.poll(() => submits.length).toBe(1);
  askB(state); // B comes after A's recorded answer
  await expect(block(page)).toContainText(B.title);
  // Reconciled: no hold, no notice, no Retry — B is simply open and answerable.
  await expect(page.getByTestId("structured-question-status")).toHaveCount(0);
  await expect(page.getByTestId("structured-question-retry")).toHaveCount(0);
  await options(page).first().click();
  await expect(send(page)).toBeEnabled();
  expect(submits).toHaveLength(1);
});

test("#1463: a refused answer is not retried — the pane says so, and a new pick is a new message", async ({ page }) => {
  const { submits, state, reply } = await asked(page);
  reply("refused");
  await options(page).first().click();
  await send(page).click();
  // Authoritative 4xx refusal: nothing recorded on the server, no Retry, and choices stay free.
  await expect(page.getByTestId("structured-question-status")).toContainText("Not sent: the session is read only");
  await expect(page.getByTestId("structured-question-retry")).toHaveCount(0);
  // Codex keeps working and asks again — in the same turn, the identical question.
  state.turns[0].reply = `${PROSE}\n\nAsking again:`;
  state.revision++;
  await expect(page.getByTestId("structured-reply").first()).toContainText("Asking again");
  reply("ok");
  await options(page).nth(1).click();
  await send(page).click();
  await expect.poll(() => submits.length).toBe(2);
  expect(new Set(submits.map((b) => b.operation_id)).size).toBe(2); // a refusal is final
  expect(submits[1].text).toBe(ASKED.options[1]);
  await expect(block(page)).toHaveCount(0);
});

test("#1463: a lost answer's Retry resends the identical operation; the choices are frozen meanwhile and the composer stays the operator's", async ({ page }) => {
  const { submits, state } = await asked(page);
  let lost = true;
  await page.route(new RegExp(`/api/structured/sessions/codex-api(?::|%3A)${ID}/turns$`), (r) => {
    const body = r.request().postDataJSON() as Json;
    submits.push(body);
    if (lost) return r.abort("failed"); // never reached the server
    return serve(state, body) === "refused" ? refuse(r) : r.fulfill({ status: 202, json: { state: "queued" } });
  });
  await options(page).first().click();
  await send(page).click();
  await expect(page.getByTestId("structured-question-status")).toContainText(
    `“${ASKED.options[0]}” may not have reached BattleLab. Retrying sends the same message.`,
  );
  // Frozen: a retry can't send a different answer, and a fresh pick can't race the first.
  await expect(options(page).nth(1).locator("input")).toBeDisabled();
  await expect(send(page)).toBeDisabled();
  // The composer is not fenced: a typed message is the operator's own answer.
  const composer = page.getByRole("textbox").last();
  await composer.fill("Half-written thought");
  await expect(page.getByRole("button", { name: /^Send$/ })).toBeEnabled();
  lost = false;
  await page.getByTestId("structured-question-retry").click();
  await expect.poll(() => submits.length).toBe(2);
  expect(submits[1]).toEqual(submits[0]); // the identical body: id, text and question
  await expect(block(page)).toHaveCount(0);
  expect(state.turns.filter((t) => t.text === ASKED.options[0])).toHaveLength(1);
  await expect(composer).toHaveValue("Half-written thought");
});

test("#1463: Retrying an answer the server already recorded replays it — one message, never two", async ({ page }) => {
  const { submits, state } = await asked(page);
  await page.route(new RegExp(`/api/structured/sessions/codex-api(?::|%3A)${ID}/turns$`), (r) => {
    const body = r.request().postDataJSON() as Json;
    submits.push(body);
    const outcome = serve(state, body, { state: "running" });
    if (outcome === "refused") return refuse(r);
    // The first is recorded but its response is lost; the retry's replay comes back.
    return outcome === "admitted" ? r.abort("failed") : r.fulfill({ status: 202, json: { state: "running" } });
  });
  // The conversation can't be read meanwhile, so the pane can't see that it landed.
  let readsDown = true;
  await page.route(new RegExp(`/api/structured/sessions/codex-api(?::|%3A)${ID}$`),
    (r) => (readsDown ? r.abort("failed") : r.fallback()));
  await options(page).first().click();
  await send(page).click();
  await expect(page.getByTestId("structured-question-retry")).toBeVisible();
  await page.getByTestId("structured-question-retry").click();
  await expect.poll(() => submits.length).toBe(2);
  expect(submits[1]).toEqual(submits[0]);
  readsDown = false;
  state.revision++;
  await expect(block(page)).toHaveCount(0);
  expect(state.turns.filter((t) => t.text === ASKED.options[0])).toHaveLength(1);
  await expect(page.getByTestId("structured-turn")).toHaveCount(2);
});

test("#1463: a newer question retires a lost answer's Retry; its own answer names the newer question", async ({ page }) => {
  const { submits, state } = await asked(page);
  let lost = true;
  await page.route(new RegExp(`/api/structured/sessions/codex-api(?::|%3A)${ID}/turns$`), (r) => {
    const body = r.request().postDataJSON() as Json;
    submits.push(body);
    if (lost) return r.abort("failed");
    return serve(state, body) === "refused" ? refuse(r) : r.fulfill({ status: 202, json: { state: "queued" } });
  });
  await options(page).first().click();
  await send(page).click();
  await expect(page.getByTestId("structured-question-retry")).toBeVisible();
  lost = false;
  askB(state);
  await expect(block(page)).toContainText(B.title);
  // The old answer can no longer be admitted (the server would refuse it): nothing to retry.
  await expect(page.getByTestId("structured-question-retry")).toHaveCount(0);
  await expect(page.getByTestId("structured-question-status")).toHaveCount(0);
  await options(page).nth(1).click();
  await send(page).click();
  await expect.poll(() => submits.length).toBe(2);
  expect(submits[1].reply_to).toBe("call-2");
  expect(submits[1].text).toBe(B.options[1]);
  expect(submits[1].operation_id).not.toBe(submits[0].operation_id);
  await expect(block(page)).toHaveCount(0);
  expect(state.turns.filter((t) => t.text === ASKED.options[0])).toHaveLength(0);
});

test("#1463: a second tab's answer to the same question is refused by the server — one message", async ({ page }) => {
  const first = await asked(page);
  await options(page).first().click();
  await send(page).click();
  await expect.poll(() => first.submits.length).toBe(1);
  await expect(block(page)).toHaveCount(0);

  // The second tab still shows the question open (its read predates the first answer).
  const secondPage = await page.context().newPage();
  await asked(secondPage, "dark", [ASKED], true);
  const posts: Json[] = [];
  await secondPage.route(new RegExp(`/api/structured/sessions/codex-api(?::|%3A)${ID}/turns$`), (r) => {
    const body = r.request().postDataJSON() as Json;
    posts.push(body);
    return serve(first.state, body) === "refused" ? refuse(r) : r.fulfill({ status: 202, json: { state: "queued" } });
  });
  await expect(block(secondPage)).toContainText(ASKED.title);
  await options(secondPage).nth(1).click();
  await send(secondPage).click();
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0].reply_to).toBe("call-1");
  await expect(secondPage.getByTestId("structured-question-status")).toContainText(`Not sent: ${REFUSED}`);
  await expect(secondPage.getByTestId("structured-question-retry")).toHaveCount(0);
  const answers = first.state.turns.filter((t) => ASKED.options.includes(t.text as string));
  expect(answers.map((t) => t.text)).toEqual([ASKED.options[0]]);
});

// #1463 Phase 2: Claude's AskUserQuestion is a held callback. It is answered (the chosen labels or
// the operator's own words), skipped or stopped — never approved — with Skip permissions on or off.
const TURN = "11111111-2222-4333-8444-555555555555";
const ASK = {
  question: "Which colour scheme should the public share page use?",
  header: "Theme",
  options: [
    { label: "Follow the visitor's system", description: "Light or dark from prefers-color-scheme" },
    { label: "Always dark", description: "Matches the operator's app" },
    { label: "Always light", description: "Best for printing" },
  ],
  multiSelect: false,
};

function askRequest(questions: Json[] = [ASK], choices = ["answer", "reject", "cancel"]): Json {
  return {
    request_id: "s:perm-7", turn_id: TURN, kind: "AskUserQuestion", item_id: "toolu_7",
    payload_digest: "a".repeat(64), choices, complete: true, always: [], risk: null,
    summary: JSON.stringify({ tool_name: "AskUserQuestion", input: { questions } }),
    payload: { tool_name: "AskUserQuestion", input: { questions }, requires_user_interaction: true },
  };
}

async function claudeAsks(page: Page, opts: { bypass?: boolean; theme?: string; request?: Json } = {}) {
  const fixture = await mockStructuredGuidance(page, "claude-api", "interrupt", opts.theme ?? "dark");
  const { state } = fixture;
  Object.assign(state, { state: "awaiting_approval", bypass: opts.bypass ?? true });
  state.turns = [{ ...state.turns[0], turn_id: TURN, operation_id: TURN, state: "awaiting_approval",
    text: "Pick a colour scheme for the share page.", reply: "", attachments: [], tools: [] }];
  state.pending_requests = [opts.request ?? askRequest()];
  const decisions: Json[] = [];
  let reply: "ok" | "lost" = "ok";
  await page.route(new RegExp(`/api/structured/sessions/claude-api(?::|%3A)${ID}/decisions$`), (r) => {
    const body = r.request().postDataJSON() as Json;
    decisions.push(body);
    if (reply === "lost") return r.abort("failed");
    state.pending_requests = [];
    state.revision++;
    return r.fulfill({ json: { decision: body.decision, handoff: "sent" } });
  });
  await page.goto(`/s/claude-api/${ID}`);
  return { ...fixture, decisions, reply: (r: typeof reply) => { reply = r; } };
}

const ask = (page: Page) => page.getByTestId("structured-ask");
const askOptions = (page: Page) => page.getByTestId("structured-ask-option");
const askSend = (page: Page) => page.getByTestId("structured-ask-send");

for (const bypass of [true, false]) {
  for (const theme of ["dark", "light"]) {
    test(`#1463: Claude's question shows its options and Send answer answers it (${bypass ? "skip permissions" : "guarded"}, ${theme})`, async ({ page }, testInfo) => {
      const { decisions } = await claudeAsks(page, { bypass, theme });
      await expect(ask(page)).toBeVisible();
      await expect(ask(page)).toContainText("waiting for your answer");
      await expect(ask(page)).toContainText("Theme");
      await expect(askOptions(page)).toHaveCount(3);
      await expect(ask(page)).toContainText("Matches the operator's app");
      // A question is never offered as an approval.
      await expect(page.getByRole("button", { name: /allow|approve/i })).toHaveCount(0);
      for (const option of await askOptions(page).all()) {
        expect((await option.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
      }
      await askOptions(page).nth(1).click();
      await page.screenshot({ path: testInfo.outputPath(`ask-${bypass ? "skip" : "guarded"}-${theme}.png`), fullPage: true });
      await askSend(page).click();
      await expect.poll(() => decisions.length).toBe(1);
      expect(decisions[0]).toMatchObject({
        turn_id: TURN, request_id: "s:perm-7", decision: "answer",
        answers: [{ selected: ["Always dark"] }],
      });
      expect(decisions[0].grant).toBeUndefined();
      await expect(ask(page)).toHaveCount(0);
    });
  }
}

test("#1463: own words replace the options; Skip question declines with no answers", async ({ page }) => {
  const { decisions, state } = await claudeAsks(page);
  await askOptions(page).first().click();
  await page.getByTestId("structured-ask-other").fill("Dark, with a light print stylesheet");
  await askSend(page).click();
  await expect.poll(() => decisions.length).toBe(1);
  expect(decisions[0].answers).toEqual([{ other: "Dark, with a light print stylesheet" }]);
  // The next question is a new callback: its own request id.
  state.pending_requests = [{ ...askRequest(), request_id: "s:perm-8" }];
  state.revision++;
  await expect(ask(page)).toBeVisible();
  await page.getByRole("button", { name: "Skip question" }).click();
  await expect.poll(() => decisions.length).toBe(2);
  expect(decisions[1].decision).toBe("reject");
  expect(decisions[1].answers).toBeUndefined();
});

test("#1463: several questions and a multi-select — Send waits for every answer", async ({ page }) => {
  const scope = { question: "Which surfaces should the release cover?", header: "Scope", multiSelect: true,
    options: [{ label: "Web app", description: "" }, { label: "Docs site", description: "" }, { label: "Landing page", description: "" }] };
  const channel = { question: "Which channel ships it first?", header: "Channel", multiSelect: false,
    options: [{ label: "main", description: "" }, { label: "stable", description: "" }] };
  const { decisions } = await claudeAsks(page, { request: askRequest([scope, channel]) });
  await expect(askOptions(page)).toHaveCount(5);
  await askOptions(page).nth(2).click();
  await askOptions(page).nth(0).click();
  await expect(askSend(page)).toBeDisabled();
  await expect(ask(page)).toContainText("1 of 2 answered");
  await expect(ask(page)).toContainText("Answer “Channel” to send.");
  await askOptions(page).nth(4).click();
  await askSend(page).click();
  await expect.poll(() => decisions.length).toBe(1);
  expect(decisions[0].answers).toEqual([{ selected: ["Landing page", "Web app"] }, { selected: ["stable"] }]);
});

test("#1463: an uncertain answer freezes the choices; Retry resends the identical decision", async ({ page }) => {
  const { decisions, reply } = await claudeAsks(page);
  reply("lost");
  await askOptions(page).nth(1).click();
  await askSend(page).click();
  await expect(page.getByTestId("structured-deciding")).toContainText("may not have reached BattleLab");
  // Frozen: no other option, no own words, no second decision can be picked now.
  await expect(askOptions(page).nth(2).locator("input")).toBeDisabled();
  await expect(page.getByTestId("structured-ask-other")).toBeDisabled();
  reply("ok");
  await page.getByTestId("structured-ask-retry").click();
  await expect.poll(() => decisions.length).toBe(2);
  expect(decisions[1]).toEqual(decisions[0]);
  expect(decisions[1].decision_id).toBe(decisions[0].decision_id);
  await expect(ask(page)).toHaveCount(0);
});

test("#1463: an answerable question shows the complete request under Details (Hermes #6477)", async ({ page }) => {
  const request = askRequest();
  (request.payload as Json).decision_reason = "Picked for the signed release only";
  await claudeAsks(page, { request });
  const details = page.getByTestId("structured-ask-details");
  await expect(ask(page)).toBeVisible();
  await details.getByText("Details").click();
  // Everything the answer goes back with, not just the question rows.
  await expect(details).toContainText("Picked for the signed release only");
  await expect(details).toContainText("requires_user_interaction");
  await expect(details).toContainText(ASK.options[2].description);
});

test("#1463: a question with no readable questions still gets the explanation, not raw JSON (Hermes #6473)", async ({ page }) => {
  const request = askRequest([], ["reject", "cancel"]);
  (request.payload as Json).input = {};
  request.summary = JSON.stringify({ tool_name: "AskUserQuestion", input: {} });
  await claudeAsks(page, { request });
  const card = page.getByTestId("structured-request");
  await expect(page.getByTestId("structured-question-preview")).toContainText("can’t offer it as choices");
  await expect(card).not.toContainText("tool_name");
  await expect(card.getByRole("button", { name: "Skip question" })).toBeVisible();
  await expect(card.getByRole("button", { name: /allow|approve/i })).toHaveCount(0);
});

const FALLBACK = {
  question: "During the A2 downtime, should the gateway fall back to another model?",
  header: "Fallback",
  options: [
    { label: "No fallback (Recommended)", description: `${"Callers get errors for ~60 min. ".repeat(18)}Nothing to revert.` },
    { label: "Fallback to the row deployment", description: "Puts extra load on production." },
  ],
  multiSelect: false,
};

/** The /turns route for a Claude session: records each message once per operation id. */
async function claudeTurns(page: Page, state: { turns: Json[]; revision: number }, lose = { first: false }) {
  const posts: Json[] = [];
  await page.route(new RegExp(`/api/structured/sessions/claude-api(?::|%3A)${ID}/turns$`), (r) => {
    const body = r.request().postDataJSON() as Json;
    posts.push(body);
    if (lose.first) {
      lose.first = false;
      return r.abort("failed"); // never reached the server
    }
    const existing = state.turns.find((t) => t.turn_id === body.operation_id);
    // As the server does: an exact replay is observed; another text under the same id conflicts.
    if (existing && existing.text !== body.text) {
      return r.fulfill({ status: 409, json: { detail: "operation id already used for a different request" } });
    }
    if (!existing) {
      state.turns.push({ ...state.turns[0], turn_id: body.operation_id, operation_id: body.operation_id,
        state: "queued", text: body.text, reply: "", tools: [] });
      state.revision++;
    }
    return r.fulfill({ status: 202, json: { state: "queued" } });
  });
  return posts;
}

test("#1463: a question from a session that can't take exact answers is still answered — as a message, then the question stops", async ({ page }) => {
  // A worker from before exact answers (or a set Claude can't take back exactly) offers only
  // skip/stop. The card still offers the options; the picks go as the next message.
  const { decisions, state } = await claudeAsks(page, { request: askRequest([ASK, FALLBACK], ["reject", "cancel"]) });
  const posts = await claudeTurns(page, state);
  await expect(ask(page)).toContainText("waiting for your answer");
  await expect(askOptions(page)).toHaveCount(5);
  // Long text is shown whole: nothing answered on is cut.
  await expect(ask(page)).toContainText("Nothing to revert.");
  const composer = page.getByRole("textbox").last();
  await composer.fill("Half-written thought");
  await askOptions(page).nth(1).click();
  await askOptions(page).nth(3).click();
  await expect(page.getByRole("button", { name: /allow|approve/i })).toHaveCount(0);
  await askSend(page).click();
  await expect.poll(() => decisions.length).toBe(1);
  expect(posts).toHaveLength(1);
  expect(posts[0].text).toBe(
    `My answers to your questions:\n- ${ASK.question}\n  → Always dark\n- ${FALLBACK.question}\n  → No fallback (Recommended)`,
  );
  expect(posts[0].reply_to).toBeUndefined();
  // Then the pending question is stopped, so Claude reads the message next.
  expect(decisions[0]).toMatchObject({ request_id: "s:perm-7", decision: "cancel" });
  expect(decisions[0].answers).toBeUndefined();
  await expect(ask(page)).toHaveCount(0);
  await expect(composer).toHaveValue("Half-written thought");
});

test("#1463: an answer already sent is never sent twice — a lost stop, a reload, then only the stop is left (Hermes #6495)", async ({ page }) => {
  const { decisions, state, reply } = await claudeAsks(page, { request: askRequest([ASK], ["reject", "cancel"]) });
  const posts = await claudeTurns(page, state);
  await askOptions(page).nth(1).click();
  reply("lost"); // the message lands; the stop's response is lost
  await askSend(page).click();
  await expect.poll(() => decisions.length).toBe(1);
  expect(posts).toHaveLength(1);
  reply("ok");
  await page.reload();
  // The question is still pending, but its answer is in the conversation: no new send.
  await expect(page.getByTestId("structured-ask-message-status")).toContainText("Your answers are sent");
  await expect(askSend(page)).toBeDisabled();
  await page.getByTestId("structured-ask-stop").click();
  await expect.poll(() => decisions.length).toBe(2);
  expect(decisions[1].decision).toBe("cancel");
  await expect(ask(page)).toHaveCount(0);
  expect(posts).toHaveLength(1);
  expect(state.turns.filter((t) => t.text === posts[0].text)).toHaveLength(1);
});

test("#1463: a queued answer can't be removed out from under its question; Stop the question settles it", async ({ page }) => {
  const { decisions, state, reply } = await claudeAsks(page, { request: askRequest([ASK], ["reject", "cancel"]) });
  const posts = await claudeTurns(page, state);
  await askOptions(page).nth(1).click();
  reply("lost");
  await askSend(page).click();
  await expect.poll(() => decisions.length).toBe(1);
  reply("ok");
  await page.reload();
  await expect(page.getByTestId("structured-ask-message-status")).toContainText("Your answers are sent");
  // The answer is visibly queued, without Remove: removing it would strand the question.
  const answer = page.getByTestId("structured-turn").filter({ hasText: "My answers to your questions" });
  await expect(answer).toBeVisible();
  await expect(answer.getByRole("button", { name: "Remove this queued message" })).toHaveCount(0);
  expect(posts).toHaveLength(1);
});

test("#1463: an answer the server refused and never recorded is sent again under a fresh id", async ({ page }) => {
  const { decisions, state } = await claudeAsks(page, { request: askRequest([ASK], ["reject", "cancel"]) });
  const posts: Json[] = [];
  let refuseFirst = true;
  await page.route(new RegExp(`/api/structured/sessions/claude-api(?::|%3A)${ID}/turns$`), (r) => {
    const body = r.request().postDataJSON() as Json;
    posts.push(body);
    if (refuseFirst) {
      // e.g. the id was burned by an earlier, removed answer: refused, never in the conversation.
      refuseFirst = false;
      return r.fulfill({ status: 409, json: { detail: "this turn was not sent: the agent is busy with another turn" } });
    }
    state.turns.push({ ...state.turns[0], turn_id: body.operation_id, operation_id: body.operation_id,
      state: "queued", text: body.text, reply: "", tools: [] });
    state.revision++;
    return r.fulfill({ status: 202, json: { state: "queued" } });
  });
  await askOptions(page).nth(1).click();
  await askSend(page).click();
  await expect(page.getByTestId("structured-ask-message-status")).toContainText("Not sent");
  expect(decisions).toHaveLength(0);
  await askSend(page).click();
  await expect.poll(() => decisions.length).toBe(1);
  expect(posts).toHaveLength(2);
  expect(posts[1].operation_id).not.toBe(posts[0].operation_id);
  expect(posts[1].text).toBe(posts[0].text);
  expect(decisions[0].decision).toBe("cancel");
});

test("#1463: an answer sent under a fallback id is still recognised after a reload — never sent twice", async ({ page }) => {
  const { decisions, state, reply } = await claudeAsks(page, { request: askRequest([ASK], ["reject", "cancel"]) });
  const posts: Json[] = [];
  let refuseFirst = true;
  await page.route(new RegExp(`/api/structured/sessions/claude-api(?::|%3A)${ID}/turns$`), (r) => {
    const body = r.request().postDataJSON() as Json;
    posts.push(body);
    if (refuseFirst) {
      refuseFirst = false;
      return r.fulfill({ status: 409, json: { detail: "this turn was not sent: the agent is busy with another turn" } });
    }
    if (!state.turns.some((t) => t.turn_id === body.operation_id)) {
      state.turns.push({ ...state.turns[0], turn_id: body.operation_id, operation_id: body.operation_id,
        state: "queued", text: body.text, reply: "", tools: [] });
      state.revision++;
    }
    return r.fulfill({ status: 202, json: { state: "queued" } });
  });
  await askOptions(page).nth(1).click();
  await askSend(page).click();
  await expect(page.getByTestId("structured-ask-message-status")).toContainText("Not sent");
  reply("lost"); // the fallback-id answer lands; the stop's response is lost
  await askSend(page).click();
  await expect.poll(() => decisions.length).toBe(1);
  reply("ok");
  await page.reload();
  await expect(page.getByTestId("structured-ask-message-status")).toContainText("Your answers are sent");
  await expect(askSend(page)).toBeDisabled();
  await page.getByTestId("structured-ask-stop").click();
  await expect.poll(() => decisions.length).toBe(2);
  expect(posts).toHaveLength(2);
  expect(state.turns.filter((t) => t.text === posts[1].text)).toHaveLength(1);
});

test("#1463: a refusal whose read-back fails keeps the same id — no fresh id without proof (Hermes #6498)", async ({ page }) => {
  const { decisions } = await claudeAsks(page, { request: askRequest([ASK], ["reject", "cancel"]) });
  const posts: Json[] = [];
  let readsDown = false;
  await page.route(new RegExp(`/api/structured/sessions/claude-api(?::|%3A)${ID}/turns$`), (r) => {
    posts.push(r.request().postDataJSON() as Json);
    // The read-back after this refusal fails — and only from here on, so no earlier poll does.
    readsDown = true;
    // Recorded elsewhere under this id (another tab, other picks): the server refuses ours.
    return r.fulfill({ status: 409, json: { detail: "operation id already used for a different request" } });
  });
  await page.route(new RegExp(`/api/structured/sessions/claude-api(?::|%3A)${ID}$`),
    (r) => (readsDown ? r.abort("failed") : r.fallback()));
  await askOptions(page).nth(1).click();
  await askSend(page).click();
  await expect(page.getByTestId("structured-ask-message-status")).toContainText("Not sent");
  await askSend(page).click();
  await expect.poll(() => posts.length).toBe(2);
  // The read never proved the id unused, so it is NOT replaced: the server still dedupes.
  expect(posts[1].operation_id).toBe(posts[0].operation_id);
  expect(decisions).toHaveLength(0);
});

test("#1463: a second tab with OTHER picks is told another answer was sent — never that its own was (Hermes #6498)", async ({ page }) => {
  const first = await claudeAsks(page, { request: askRequest([ASK], ["reject", "cancel"]) });
  const posts = await claudeTurns(page, first.state);
  first.reply("lost"); // tab A's stop doesn't land, so the question is still pending
  await askOptions(page).nth(1).click();
  await askSend(page).click();
  await expect.poll(() => first.decisions.length).toBe(1);
  const other = await page.context().newPage();
  // The other tab's read predates A's answer; its next read (after the 409) sees it.
  const second = await claudeAsks(other, { request: askRequest([ASK], ["reject", "cancel"]) });
  const otherPosts: Json[] = [];
  await other.route(new RegExp(`/api/structured/sessions/claude-api(?::|%3A)${ID}/turns$`), (r) => {
    const body = r.request().postDataJSON() as Json;
    otherPosts.push(body);
    const a = first.state.turns.find((t) => t.turn_id === body.operation_id);
    if (a && !second.state.turns.some((t) => t.turn_id === a.turn_id)) {
      second.state.turns.push(a);
      second.state.revision++;
    }
    return a && a.text !== body.text
      ? r.fulfill({ status: 409, json: { detail: "operation id already used for a different request" } })
      : r.fulfill({ status: 202, json: { state: "queued" } });
  });
  await other.getByRole("button", { name: "Open here instead" }).click();
  await askOptions(other).nth(2).click();
  await askSend(other).click();
  await expect.poll(() => otherPosts.length).toBe(1);
  expect(otherPosts[0].operation_id).toBe(posts[0].operation_id); // the same operation
  await expect(other.getByTestId("structured-ask-message-status")).toContainText("Another answer was already sent, not yours");
  await expect(other.getByTestId("structured-ask-message-status")).toContainText("Always dark");
  // The other tab never stopped the question on the strength of someone else's answer.
  expect(second.decisions).toHaveLength(0);
  await expect(other.getByTestId("structured-ask-stop")).toBeVisible();
  expect(first.state.turns.filter((t) => String(t.text ?? "").startsWith("My answers"))).toHaveLength(1);
});

test("#1463: Skip question still skips a message-mode question — nothing is sent as a message", async ({ page }) => {
  const { decisions, state } = await claudeAsks(page, { request: askRequest([ASK, ASK], ["reject", "cancel"]) });
  const posts = await claudeTurns(page, state);
  await ask(page).getByRole("button", { name: "Skip question" }).click();
  await expect.poll(() => decisions.length).toBe(1);
  expect(decisions[0].decision).toBe("reject");
  expect(posts).toHaveLength(0);
});

test("#1463: a lost message-mode answer is retried as the identical message, and the question stops only after it lands", async ({ page }) => {
  const { decisions, state } = await claudeAsks(page, { request: askRequest([ASK, ASK], ["reject", "cancel"]) });
  const posts = await claudeTurns(page, state, { first: true });
  // Repeated questions can't be keyed for Claude, but they can be answered as a message.
  await expect(askOptions(page)).toHaveCount(6);
  await askOptions(page).nth(0).click();
  await askOptions(page).nth(4).click();
  await askSend(page).click();
  await expect(page.getByTestId("structured-ask-message-status")).toContainText("may not have reached BattleLab");
  expect(decisions).toHaveLength(0); // the question is NOT stopped while its answer is unsent
  await expect(askOptions(page).first().locator("input")).toBeDisabled();
  await page.getByTestId("structured-ask-retry").click();
  await expect.poll(() => decisions.length).toBe(1);
  expect(posts).toHaveLength(2);
  expect(posts[1]).toEqual(posts[0]);
  expect(decisions[0].decision).toBe("cancel");
  expect(state.turns.filter((t) => typeof t.text === "string" && (t.text as string).startsWith("My answers"))).toHaveLength(1);
});
