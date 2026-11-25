import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { TOUCH_FLOOR } from "./touchTarget";
import { expect, test, type Locator, type Page } from "@playwright/test";
import { ACTIVE, ID, mockStructuredGuidance } from "./structuredGuidanceFixture";

// A long autonomous turn stays readable (#1643): its older loaded messages fold behind one
// disclosure, the newest three (a finished turn's final reply among them) and an open question
// stay out, and history this view never loaded is said to be not loaded — never counted as
// folded. The turns come from `long-turn.json`, which `tests/test_long_turn_fixture.py` proves is
// exactly what the server sends for a 200-message turn. Red on origin/main: no fold, no note —
// the wall of progress notes the operator reported.
type Json = Record<string, unknown>;
const FIXTURE = JSON.parse(
  readFileSync(resolve(process.cwd(), "e2e/fixtures/long-turn.json"), "utf8"),
) as Record<
  "long" | "clipped" | "agent" | "starved" | "starved_agent" | "starved_single" | "starved_single_agent",
  Json
>;
const update = (n: number) => `Update ${String(n).padStart(3, "0")}:`;

async function open(page: Page, turn: Json, theme = "dark", running = true) {
  const { state } = await mockStructuredGuidance(page, "codex-api", "steer", theme);
  state.state = running ? "running" : "idle";
  state.active_turn = running ? ACTIVE : (null as unknown as string);
  state.turns = [{
    ...turn, turn_id: ACTIVE, ...(turn.kind === "agent" ? {} : { operation_id: ACTIVE, text: "Implement it." }),
    state: running ? "running" : "completed",
  }];
  await page.goto(`/s/codex-api/${ID}`);
  return state;
}

const top = async (l: Locator) => (await l.boundingBox())!.y;

for (const theme of ["dark", "light"]) {
  test(`${theme}: a 200-message turn folds its older loaded messages and says what is not loaded`, async ({ page }, testInfo) => {
    await open(page, FIXTURE.long, theme);
    const reply = page.getByTestId("structured-reply");
    await expect(reply.getByTestId("structured-not-loaded")).toHaveText(
      "… 184 earlier updates not loaded — this view holds only the newest part of this turn",
    );
    const fold = reply.getByTestId("structured-fold");
    await expect(fold.locator(":scope > summary")).toHaveText("13 earlier updates");
    expect((await fold.locator(":scope > summary").boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    // The newest three are out; the older loaded ones are inside, closed.
    for (const n of [197, 198, 199]) await expect(reply.getByText(update(n))).toBeVisible();
    await expect(fold.getByText(update(184))).toBeHidden();
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
    await reply.getByTestId("structured-not-loaded").scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath(`fold-closed-${theme}.png`) });
    // Expanded: every loaded message, in order, with the calls made after each.
    await fold.locator(":scope > summary").click();
    const ys = [];
    for (let n = 184; n <= 199; n++) {
      const note = reply.getByText(update(n));
      await expect(note).toBeVisible();
      ys.push(await top(note));
    }
    expect(ys).toEqual([...ys].sort((a, b) => a - b));
    await expect(fold.getByTestId("structured-activity")).toHaveCount(13);
    await fold.locator(":scope > summary").scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath(`fold-open-${theme}.png`) });
  });
}

test("a finished agent-run turn keeps its final reply out of the fold", async ({ page }) => {
  await open(page, FIXTURE.agent, "dark", false);
  const turn = page.getByTestId("structured-agent-turn");
  const fold = turn.getByTestId("structured-fold");
  await expect(fold.locator(":scope > summary")).toHaveText("9 earlier updates");
  await expect(turn.getByText(update(11))).toBeVisible(); // the final reply
  expect(await fold.getByText(update(11)).count()).toBe(0);
  await expect(turn.getByTestId("structured-not-loaded")).toHaveCount(0); // nothing was cut
});

test("an open question is never inside the fold", async ({ page }) => {
  const state = await open(page, FIXTURE.long);
  state.turns[0] = {
    ...state.turns[0], question_id: "call-1",
    questions: [{ title: "Native wording or literal translations?", options: ["Native", "Literal"] }],
  };
  await page.reload();
  const question = page.getByTestId("structured-question");
  await expect(question).toBeVisible();
  const fold = page.getByTestId("structured-fold");
  expect(await fold.getByTestId("structured-question").count()).toBe(0);
  expect(await top(question)).toBeGreaterThan(await top(fold));
});

test("a newest message whose start was not loaded says so", async ({ page }) => {
  await open(page, FIXTURE.clipped);
  const reply = page.getByTestId("structured-reply");
  await expect(reply.getByTestId("structured-not-loaded")).toHaveText(
    "… 1 earlier update not loaded — this view holds only the newest part of this turn",
  );
  await expect(reply.getByTestId("structured-partial-start")).toHaveText("… start of this message not loaded");
  await expect(reply.getByTestId("structured-fold")).toHaveCount(0);
  await expect(reply.getByText("End of report.")).toBeVisible();
});

test("an older server's turn without boundaries renders unfolded, as before", async ({ page }) => {
  const turn = { ...FIXTURE.long };
  delete turn.reply_breaks;
  delete turn.reply_omitted;
  delete turn.first_message_partial;
  await open(page, turn);
  const reply = page.getByTestId("structured-reply");
  await expect(reply.getByTestId("structured-fold")).toHaveCount(0);
  await expect(reply.getByTestId("structured-not-loaded")).toHaveCount(0);
  await expect(reply.getByText(update(184))).toBeVisible();
});

// Hermes on #1651: the fold must not hide what the folded activity says. The newest calls ran
// after message 8, then four more notes came: those calls, a failed one and a risky one are all
// inside the closed fold, so its summary carries "Working", "Failed activity", "Risky activity".
function signalTurn(kind: "operator" | "agent") {
  const notes = Array.from({ length: 12 }, (_, i) => `Note ${String(i).padStart(2, "0")}.`);
  const reply = notes.join("\n\n");
  const at = (i: number) => reply.indexOf(notes[i + 1]);
  const call = (id: string, i: number, extra: Json = {}) =>
    ({ id, name: "Bash", summary: `echo ${id}`, outcome: "completed", at: at(i), ...extra });
  return {
    kind, reply, reply_ts: null, reply_truncated: false, tools_truncated: false,
    reply_breaks: notes.map((n) => reply.indexOf(n)), first_message_partial: false, reply_omitted: null,
    tools: [
      call("broke", 1, { outcome: "failed" }),
      call("force-push", 2, { risk: { level: "risky", reasons: ["rewrites remote history"] } }),
      call("newest", 7, { outcome: "running" }),
    ],
    ts: Date.parse("2026-10-24T03:00:00Z") / 1000,
  } as Json;
}

test("a closed fold says the newest calls are working and what failed or was risky", async ({ page }) => {
  await open(page, signalTurn("operator"));
  const summary = page.getByTestId("structured-fold").locator(":scope > summary");
  await expect(summary).toHaveText("9 earlier updates · Working · Failed activity · Risky activity");
  // Nothing outside the fold claims the turn is working on calls it no longer shows.
  await expect(page.getByTestId("structured-reply").getByText("Note 11.")).toBeVisible();
});

test("an agent-run turn's fold says it is working on its own and what was risky", async ({ page }) => {
  await open(page, signalTurn("agent"));
  const summary = page.getByTestId("structured-agent-turn").getByTestId("structured-fold").locator(":scope > summary");
  await expect(summary).toHaveText("9 earlier updates · Working on its own · Risky activity");
});

// Hermes on #1651: newer turns spent the shared snapshot budget, so this turn's reply came with no
// text at all — only `reply_omitted`. It still says what was not loaded, for both turn kinds,
// instead of reading as a turn with no reply. Both shapes are server-built (`long-turn.json`).
test("a turn the shared budget left no text for still says what was not loaded", async ({ page }) => {
  await open(page, FIXTURE.starved, "dark", false);
  const reply = page.getByTestId("structured-reply");
  await expect(reply.getByTestId("structured-not-loaded")).toHaveText(
    "… 2 earlier updates not loaded — this view holds only the newest part of this turn",
  );
  await expect(reply.getByTestId("structured-fold")).toHaveCount(0);
});

test("an agent-run turn the shared budget left no text for still says so", async ({ page }) => {
  await open(page, FIXTURE.starved_agent, "dark", false);
  await expect(page.getByTestId("structured-agent-turn").getByTestId("structured-not-loaded")).toHaveText(
    "… 2 earlier updates not loaded — this view holds only the newest part of this turn",
  );
});

// Hermes on #1651: a ONE-message reply with no budget left is counted too, for both turn kinds.
for (const [name, testId] of [
  ["starved_single", "structured-reply"],
  ["starved_single_agent", "structured-agent-turn"],
] as const) {
  test(`${name}: a single-message reply with no budget left still says it was not loaded`, async ({ page }) => {
    await open(page, FIXTURE[name], "dark", false);
    await expect(page.getByTestId(testId).getByTestId("structured-not-loaded")).toHaveText(
      "… 1 earlier update not loaded — this view holds only the newest part of this turn",
    );
  });
}
