import { expect, test, type Page } from "@playwright/test";

import { storeMotionChoice } from "./nightjarShell";
import {
  TURN,
  URL_PATH,
  addTool,
  addTurn,
  atBottom,
  complete,
  flush,
  gap,
  log,
  mockStreamingSession,
  openMessage,
  operatorTurn,
  paragraphs,
  snapshotOf,
  watchWords,
} from "./structuredStream";
import { TOUCH_FLOOR } from "./touchTarget";

// #1727: an API session's streaming reply arrives at the worker's flush cadence (through the
// events page, never a snapshot per flush), its newest words fade in with a caret riding the end,
// the follow-scroll eases, and a reader above the bottom gets Jump to latest counting new ROWS.
// The server is mocked (`structuredStream.ts`); desktop and mobile.

const EARLIER = "22222222-2222-4333-8444-555555555555";
const earlier = operatorTurn({
  turn_id: EARLIER,
  operation_id: EARLIER,
  state: "completed",
  text: "First question",
  reply: "First answer.",
  stream: undefined,
});

const liveReply = (page: Page) =>
  page.getByTestId("structured-turn").filter({ hasText: "Walk me through" }).getByTestId("structured-reply");

test.beforeEach(async ({ page }) => {
  await storeMotionChoice(page, "full");
});

test("streamed text arrives between snapshots; only the newest words fade, a caret rides the end, nothing above moves", async ({
  page,
}) => {
  const s = await mockStreamingSession(page, [earlier, operatorTurn({ reply: "Starting" })]);
  await page.goto(URL_PATH);
  await expect(liveReply(page)).toContainText("Starting");
  const words = await watchWords(page);
  // The snapshot from here on lags the journal: the words can only come from its events.
  s.frozen = snapshotOf(s);
  const above = await page.getByTestId("structured-turn").first().evaluate((el) => (el as HTMLElement).offsetTop);
  const replyTop = await liveReply(page).evaluate((el) => (el as HTMLElement).offsetTop);
  let shown = "Starting";
  for (const piece of [" the first", " step,", " then the second."]) {
    flush(s, piece);
    shown += piece;
    await expect(liveReply(page)).toContainText(shown);
  }
  await expect(liveReply(page)).toContainText("Starting the first step, then the second.");
  // The snapshot the pane can read never had those words: they came through the events.
  const held = (s.frozen!.turns as Record<string, unknown>[]).find((t) => t.turn_id === TURN)!;
  expect(held.reply).toBe("Starting");
  // Every flush reached the pane through the held events read, not a snapshot.
  expect(s.reads.some((r) => r.wait === "3")).toBe(true);
  expect(await words()).toBeGreaterThan(0);
  await expect(liveReply(page).locator(".nj-caret")).toHaveCount(1);
  // Nothing above the stream moved; the reply's own start did not either (rule 2).
  expect(await page.getByTestId("structured-turn").first().evaluate((el) => (el as HTMLElement).offsetTop)).toBe(above);
  expect(await liveReply(page).evaluate((el) => (el as HTMLElement).offsetTop)).toBe(replyTop);
  // The turn ends: the snapshot shows it, and the caret goes with the stream.
  s.frozen = null;
  complete(s);
  await expect(page.getByTestId("structured-turn").last()).toHaveAttribute("data-state", "completed");
  await expect(log(page).locator(".nj-caret")).toHaveCount(0);
  await expect(liveReply(page)).toContainText("Starting the first step, then the second.");
});

test("follow-scroll eases to the bottom in Full and never moves a reader above it", async ({ page }) => {
  const s = await mockStreamingSession(page, [operatorTurn({ reply: paragraphs(60) })]);
  await page.goto(URL_PATH);
  await expect(page.getByText("Progress paragraph 60.", { exact: true })).toBeAttached();
  await atBottom(page);
  // Sample the scroll position every frame while one flush adds a block of text.
  await log(page).evaluate((el) => {
    const w = window as unknown as { __tops: number[] };
    w.__tops = [];
    const sample = () => {
      w.__tops.push(el.scrollTop);
      if (w.__tops.length < 90) requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
  });
  const start = await log(page).evaluate((el) => el.scrollTop);
  flush(s, `\n\n${paragraphs(12, 61)}`);
  await expect(page.getByText("Progress paragraph 72.", { exact: true })).toBeAttached();
  await atBottom(page);
  await expect.poll(() => page.evaluate(() => (window as unknown as { __tops: number[] }).__tops.length)).toBe(90);
  const end = await log(page).evaluate((el) => el.scrollTop);
  const tops = await page.evaluate(() => (window as unknown as { __tops: number[] }).__tops);
  // Eased: the position passed through values between where it was and where it landed.
  expect(end).toBeGreaterThan(start + 100);
  expect(tops.filter((t) => t > start + 2 && t < end - 2).length).toBeGreaterThan(1);
  // A reader above the bottom is never moved, however the content grows.
  await log(page).evaluate((el) => { el.scrollTop -= 400; });
  await expect.poll(() => gap(page)).toBeGreaterThan(390);
  const reading = await log(page).evaluate((el) => el.scrollTop);
  flush(s, `\n\n${paragraphs(5, 73)}`);
  await expect(page.getByText("Progress paragraph 77.", { exact: true })).toBeAttached();
  await page.waitForTimeout(400);
  expect(await log(page).evaluate((el) => el.scrollTop)).toBe(reading);
});

test("Jump to latest counts new rows — never text flushes — and returns to following", async ({ page }) => {
  const s = await mockStreamingSession(page, [operatorTurn({ reply: paragraphs(60) })]);
  await page.goto(URL_PATH);
  await expect(page.getByText("Progress paragraph 60.", { exact: true })).toBeAttached();
  await atBottom(page);
  const jump = page.getByTestId("structured-jump");
  await expect(jump).toHaveCount(0);
  await log(page).evaluate((el) => { el.scrollTop -= 400; });
  await expect.poll(() => gap(page)).toBeGreaterThan(390);
  // What the reader is looking at: the paragraph nearest the middle of the log. A tool row added
  // ABOVE it (the turn's activity block) is absorbed by scroll anchoring, so its place on screen
  // is the reading position, not the raw scrollTop.
  const anchor = await log(page).evaluate((el) => {
    const mid = el.getBoundingClientRect().top + el.clientHeight / 2;
    const ps = [...el.querySelectorAll("p")].filter((p) => /^Progress paragraph \d+\.$/.test(p.textContent ?? ""));
    return ps.reduce((a, b) => (Math.abs(b.getBoundingClientRect().top - mid) < Math.abs(a.getBoundingClientRect().top - mid) ? b : a)).textContent!;
  });
  const at = async () => (await page.getByText(anchor, { exact: true }).boundingBox())!.y;
  const reading = await at();
  // Text flushes: new content below, but no new row.
  for (const i of [61, 62, 63]) flush(s, `\n\nProgress paragraph ${i}.`);
  await expect(page.getByText("Progress paragraph 63.", { exact: true })).toBeAttached();
  await expect(jump).toBeVisible();
  await expect(jump).toHaveText(/jump to latest/i);
  await expect(jump).not.toHaveText(/new/i);
  // Rows: a tool row, then a queued message — counted one each.
  addTool(s, "c1");
  await expect(jump).toHaveText(/1 new/i);
  addTurn(s, operatorTurn({ turn_id: EARLIER, operation_id: EARLIER, state: "queued", text: "Follow-up", reply: "", stream: undefined }));
  await expect(page.getByText("Follow-up", { exact: true })).toBeAttached();
  await expect(jump).toHaveText(/2 new/i);
  for (const i of [64, 65]) flush(s, `\n\nProgress paragraph ${i}.`);
  await expect(page.getByText("Progress paragraph 65.", { exact: true })).toBeAttached();
  await expect(jump).toHaveText(/2 new/i);
  // None of it moved what the reader reads.
  expect(Math.abs((await at()) - reading)).toBeLessThanOrEqual(1);
  const box = (await jump.boundingBox())!;
  expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await jump.click();
  await atBottom(page);
  await expect(jump).toHaveCount(0);
  // Following again: the next flush is followed.
  flush(s, "\n\nProgress paragraph 66.");
  await expect(page.getByText("Progress paragraph 66.", { exact: true })).toBeAttached();
  await atBottom(page);
});

test("a reconnect, a reset cursor and a retried turn mid-stream never duplicate text", async ({ page }) => {
  const s = await mockStreamingSession(page, [operatorTurn({ reply: "Alpha" })]);
  await page.goto(URL_PATH);
  await expect(liveReply(page)).toContainText("Alpha");
  flush(s, " beta");
  await expect(liveReply(page)).toContainText("Alpha beta");
  // The connection drops; the stream goes on meanwhile.
  s.failReads = 2;
  flush(s, " gamma");
  flush(s, " delta");
  await expect(liveReply(page)).toContainText("Alpha beta gamma delta", { timeout: 15_000 });
  await expect(liveReply(page)).toHaveText(/Alpha beta gamma delta$/);
  // The journal answers the pane's cursor with its reset (409) — and meanwhile the message was
  // retried as a NEW operation that streams on its own.
  const RETRY = "33333333-2222-4333-8444-555555555555";
  s.turns = [
    { ...s.turns[0], state: "failed", reason: "worker ended", stream: undefined },
    operatorTurn({ turn_id: RETRY, operation_id: RETRY, text: "Walk me through the change.", reply: "Again" }),
  ];
  s.revision += 2;
  s.resetReads = 1;
  await expect(page.getByTestId("structured-turn")).toHaveCount(2);
  flush(s, " from the top", RETRY);
  const retried = page.getByTestId("structured-turn").last().getByTestId("structured-reply");
  await expect(retried).toContainText("Again from the top");
  flush(s, " once more", RETRY);
  await expect(retried).toHaveText(/Again from the top once more$/);
  // The first attempt kept its own text, once; nothing spliced into the other.
  const first = page.getByTestId("structured-turn").first().getByTestId("structured-reply");
  await expect(first).toHaveText(/Alpha beta gamma delta$/);
  await expect(retried).not.toContainText("Alpha");
  expect(await log(page).evaluate((el) => el.textContent?.split("gamma").length)).toBe(2);
});

test("a new row arrives with the arrival motion; the first paint does not", async ({ page }) => {
  const s = await mockStreamingSession(page, [earlier, operatorTurn({ reply: "Working" })]);
  await page.goto(URL_PATH);
  await expect(liveReply(page)).toContainText("Working");
  await expect(log(page).locator(".nj-arrive")).toHaveCount(0);
  addTool(s, "c9");
  addTurn(s, operatorTurn({ turn_id: "44444444-2222-4333-8444-555555555555", operation_id: "44444444-2222-4333-8444-555555555555", state: "queued", text: "Arrives now", reply: "", stream: undefined }));
  const arrived = page.getByTestId("structured-turn").filter({ hasText: "Arrives now" });
  await expect(arrived).toHaveClass(/nj-arrive/);
  await expect(page.getByTestId("structured-tool").filter({ hasText: "c9" })).toHaveClass(/nj-arrive/);
  // The rows from the first paint never got it.
  await expect(page.getByTestId("structured-turn").first()).not.toHaveClass(/nj-arrive/);
  const names = await arrived.evaluate((el) => el.getAnimations().map((a) => (a as CSSAnimation).animationName));
  expect(names).toContain("nj-arrive");
});

for (const mode of ["Full", "Off"] as const)
test(`${mode}: a snapshot window that slides (oldest turn out, a same-height turn in) keeps a following reader at the bottom`, async ({
  page,
}) => {
  // Review of #1747: nothing grew, so nothing was written — then scroll anchoring moved the view
  // up by the removed turn's height, which read as the reader scrolling away.
  const id = (i: number) => `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`;
  const done = (i: number) =>
    operatorTurn({ turn_id: id(i), operation_id: id(i), state: "completed", text: "Same question", reply: "Same one-paragraph answer.", stream: undefined });
  const s = await mockStreamingSession(page, [...Array.from({ length: 30 }, (_, i) => done(i)), operatorTurn({ reply: "Working" })]);
  if (mode === "Off") await storeMotionChoice(page, "off");
  await page.goto(URL_PATH);
  await expect(liveReply(page)).toContainText("Working");
  await atBottom(page);
  const height = await log(page).evaluate((el) => el.scrollHeight);
  // Appended last, so every row above the bottom moves up by one turn: anchoring shifts the view.
  const reads = s.snapshots;
  s.turns = [...s.turns.slice(1), done(30)];
  s.revision += 1;
  // The slide alone, read and laid out (never together with the growth below, which would hide it).
  await expect.poll(() => s.snapshots).toBeGreaterThan(reads);
  await page.waitForTimeout(600);
  expect(await log(page).evaluate((el) => el.scrollHeight)).toBe(height); // the case: no growth
  await atBottom(page);
  await expect(page.getByTestId("structured-jump")).toHaveCount(0);
  // Still following: the next growth is followed.
  flush(s, `\n\n${paragraphs(6)}`);
  await expect(page.getByText("Progress paragraph 6.", { exact: true })).toBeAttached();
  await atBottom(page);
  await expect(page.getByTestId("structured-jump")).toHaveCount(0);
});

test("a reply streaming inside a fenced code block fades its newest words and carries the caret there (Hermes on #1747)", async ({
  page,
}) => {
  const s = await mockStreamingSession(page, [operatorTurn({ reply: "Run this:\n\n```bash\nnpm ci\n" })]);
  await page.goto(URL_PATH);
  const code = liveReply(page).getByTestId("md-code").locator("code");
  await expect(code).toHaveText("npm ci");
  await code.evaluate((el) => {
    const w = window as unknown as { __codeWords: string[] };
    w.__codeWords = [];
    new MutationObserver(() => {
      for (const n of el.querySelectorAll(".nj-word")) w.__codeWords.push(n.textContent ?? "");
    }).observe(el, { childList: true, subtree: true, characterData: true });
  });
  flush(s, "npm test -- --run\n");
  await expect(code).toHaveText("npm ci\nnpm test -- --run");
  const faded = await page.evaluate(() => (window as unknown as { __codeWords: string[] }).__codeWords);
  expect(faded.join("")).toContain("npm test");
  await expect(code.locator(".nj-caret")).toHaveCount(1);
});

test("a Codex-shaped turn: message one settled, a command, then message two streams word by word (#1727)", async ({
  page,
}) => {
  const first = "First message.";
  const s = await mockStreamingSession(page, [
    operatorTurn({
      reply: first,
      reply_breaks: [0],
      stream: undefined, // settled text only: nothing streams yet
      tools: [{ id: "c1", name: "commandExecution", outcome: "completed", summary: "npm test", at: first.length }],
    }),
  ]);
  s.holdReads = true; // reads are held until the journal moves, as the server's are
  await page.goto(URL_PATH);
  const reply = liveReply(page);
  await expect(reply).toContainText(first);
  // Message two opens: its first words are a text event the held snapshot cannot extend, so the
  // pane reads the snapshot that now shows the message after the first one.
  const before = s.snapshots;
  openMessage(s, "w0", "m2");
  await expect(reply).toContainText("w0");
  expect(s.snapshots).toBeGreaterThan(before);
  const rebased = s.snapshots;
  // From there every word reaches the pane at the flush cadence, through the events alone.
  const lags: number[] = [];
  for (const i of [1, 2, 3, 4, 5]) {
    const at = Date.now();
    flush(s, ` w${i}`, undefined, "m2");
    await expect(reply).toContainText(`w${i}`);
    lags.push(Date.now() - at);
    await page.waitForTimeout(150);
  }
  // Main read the next page a whole poll (1 s) after the rebase: the words arrived in a lump.
  expect(Math.max(...lags), `per-word lag ${lags.join(", ")} ms`).toBeLessThan(600);
  expect(s.snapshots).toBe(rebased);
  await expect(reply).toContainText("w0 w1 w2 w3 w4 w5");
  // Once each, and the command's row sits between the two messages.
  const text = await reply.innerText();
  expect(text.split("First message.").length).toBe(2);
  expect(text.split("w3").length).toBe(2);
  const tools = text.search(/commands & tools/i);
  expect(text.indexOf(first)).toBeLessThan(tools);
  expect(tools).toBeLessThan(text.indexOf("w0"));
  await expect(reply.locator(".nj-caret")).toHaveCount(1);
});
