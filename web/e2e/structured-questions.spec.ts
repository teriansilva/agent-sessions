import { expect, test, type Page, type Route } from "@playwright/test";
import { ACTIVE, ID, mockStructuredGuidance } from "./structuredGuidanceFixture";

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
      expect((await option.boundingBox())!.height).toBeGreaterThanOrEqual(44); // design §8
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
