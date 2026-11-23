/** HQ's running questions are tasks, in a real browser (#2103).
 *
 *  What a DOM emulator cannot prove: that a question sent while a turn runs really goes out as
 *  its own request and reads as queued, that Stop sits BESIDE Send as a 44px square, that the
 *  working mark and the strip it opens are reachable from another section of the app (a popover
 *  on a desktop, a bottom sheet with a scrim on a phone), that a long sheet scrolls on a short
 *  viewport with Close and Stop all still in reach, that the controls are 44px targets on a
 *  coarse pointer, that keyboard focus walks the strip in order and Escape gives it back, and
 *  that a notification's Open lands on the conversation. Runs on both projects.
 *
 *  The API is mocked by a small fake of the turn slot: the first question of a conversation is
 *  held running, the next ones are queued behind it (the fourth is refused), a settle or a Stop
 *  starts the oldest queued one, and `/api/hq/tasks` is derived from that state.
 */
import { expect, test, type Locator, type Page, type Route } from "@playwright/test";

import {
  askStopTurnPath,
  askWithdrawTurnPath,
  HQ_TASKS_API,
  HQ_TASKS_STOP_ALL_API,
} from "../src/lib/apiPaths";
import { hqConversationPath } from "../src/lib/routes";
import { openAsk } from "./askSidebar";
import { ASK_STREAM, mockAskConversations, type FakeAsk, type FakeTurn } from "./askStream";
import { mockNightjarShell } from "./nightjarShell";
import { TOUCH_FLOOR } from "./touchTarget";

const A = "ask_" + "a".repeat(32);
const B = "ask_" + "b".repeat(32);
const NOW = 1_700_000_100;

type Status = "pending" | "queued" | "done" | "failed" | "stopped" | "withdrawn" | "not_started";

interface Turn {
  turn_id: string;
  question: string;
  asked_at: number;
  started_at: number | null;
  settled_at: number | null;
  status: Status;
  answer: string | null;
  [key: string]: unknown;
}

function turn(question: string, status: Status, over: Partial<Turn> = {}): FakeTurn {
  const ran = status !== "queued" && status !== "withdrawn" && status !== "not_started";
  return {
    turn_id: crypto.randomUUID(),
    question,
    asked_at: NOW,
    started_at: ran ? NOW : null,
    settled_at: status === "pending" || status === "queued" ? null : NOW + 5,
    status,
    answer: status === "done" ? "An answer." : null,
    detail: null,
    code: null,
    matches: [],
    mission_matches: [],
    tools: [],
    steps:
      status === "pending"
        ? { count: 7, more: null, incomplete: false, live: { n: 8, tool: "session_status", at: NOW } }
        : { count: ran ? 3 : 0, more: null, incomplete: false },
    elapsed_ms: status === "pending" ? 42_000 : ran ? 9_000 : null,
    contact: "off",
    ...over,
  } as unknown as FakeTurn;
}

const turnsOf = (fake: FakeAsk, cid: string) =>
  fake.conversations.find((c) => c.id === cid)!.turns as unknown as Turn[];

interface Hq {
  fake: FakeAsk;
  /** The mutating requests the page made, by path. */
  posts: string[];
  /** Settle the running turn of `cid` (its held stream ends with it) and start the next. */
  settle: (cid: string, answer: string) => Promise<void>;
}

/** The shell, the conversations, the turn slot (running, queued, promoted) and the task list. */
async function mockHq(page: Page, seed: FakeAsk["conversations"] = []): Promise<Hq> {
  await mockNightjarShell(page);
  const fake = await mockAskConversations(page, seed);
  const hq: Hq = { fake, posts: [], settle: async () => {} };
  const streams = new Map<string, Route>();

  const end = async (cid: string, id: string, status: Status, answer: string | null) => {
    const turns = turnsOf(fake, cid);
    const t = turns.find((x) => x.turn_id === id)!;
    Object.assign(t, {
      status,
      answer,
      settled_at: NOW + 60,
      steps: { count: 3, more: null, incomplete: false },
      elapsed_ms: 58_000,
    });
    const held = streams.get(id);
    streams.delete(id);
    if (held)
      await held.fulfill({
        status: 200,
        contentType: "application/x-ndjson",
        body: JSON.stringify({ type: "turn", status: 200, turn: t }) + "\n",
      });
  };
  const promote = (cid: string) => {
    const next = turnsOf(fake, cid).find((t) => t.status === "queued");
    if (next) Object.assign(next, { status: "pending", started_at: NOW + 61, elapsed_ms: 1_000 });
  };
  hq.settle = async (cid, answer) => {
    const running = turnsOf(fake, cid).find((t) => t.status === "pending");
    if (!running) throw new Error("nothing is running");
    await end(cid, running.turn_id, "done", answer);
    promote(cid);
  };

  await page.route(ASK_STREAM, async (route) => {
    const req = route.request().postDataJSON() as { turn_id: string; text: string };
    const cid = /\/(ask_[0-9a-f]{32})\/message\/stream$/.exec(
      new URL(route.request().url()).pathname,
    )![1];
    hq.posts.push(`stream:${req.text}`);
    const turns = turnsOf(fake, cid);
    if (!turns.some((t) => t.status === "pending")) {
      turns.push(turn(req.text, "pending", { turn_id: req.turn_id }) as unknown as Turn);
      streams.set(req.turn_id, route); // held: it runs until the test settles or stops it
      return;
    }
    if (turns.filter((t) => t.status === "queued").length >= 3)
      return route.fulfill({
        status: 409,
        json: { detail: "queue full — withdraw a queued question or wait", queue_full: true },
      });
    const queued = turn(req.text, "queued", { turn_id: req.turn_id });
    turns.push(queued as unknown as Turn);
    await route.fulfill({
      status: 200,
      contentType: "application/x-ndjson",
      body: JSON.stringify({ type: "turn", status: 200, turn: queued }) + "\n",
    });
  });
  await page.route(/\/api\/ask\/conversations\/[^/]+\/turns\/[^/]+\/(stop|withdraw)$/, async (route) => {
    const path = new URL(route.request().url()).pathname;
    hq.posts.push(path);
    const [, cid, id, verb] = /conversations\/([^/]+)\/turns\/([^/]+)\/(stop|withdraw)$/.exec(path)!;
    const t = turnsOf(fake, cid).find((x) => x.turn_id === id);
    if (!t) return route.fulfill({ status: 404, json: { detail: "no such turn" } });
    if (verb === "withdraw") {
      const was = t.status === "queued";
      if (was) Object.assign(t, { status: "withdrawn", settled_at: NOW + 30 });
      return route.fulfill({ json: { turn: t, withdrawn: was, running: true } });
    }
    if (t.status === "pending") {
      await end(cid, id, "stopped", null);
      promote(cid); // Stop leaves the queue: the next one starts
    }
    return route.fulfill({ json: { turn: t, running: false } });
  });
  const list = () => {
    const tasks: Record<string, unknown>[] = [];
    const recent: Record<string, unknown>[] = [];
    for (const c of fake.conversations) {
      const turns = c.turns as unknown as Turn[];
      const title = turns[0]?.question ?? "";
      for (const t of turns) {
        const parked = t.status === "done" && Boolean(t.ask) && t === turns[turns.length - 1];
        const row = {
          cid: c.id,
          turn_id: t.turn_id,
          question: t.question.split("\n")[0],
          via: null,
          conversation: title,
          steps: (t.steps as { count: number }).count,
          step: t.status === "pending" ? "session_status" : null,
          stopping: false,
          asked_at: t.asked_at,
          started_at: t.started_at,
          ended_at: t.settled_at,
          elapsed_ms: t.elapsed_ms,
        };
        if (t.status === "pending") tasks.push({ ...row, state: "running" });
        else if (t.status === "queued") tasks.push({ ...row, state: "queued" });
        else if (parked) tasks.push({ ...row, state: "question" });
        else recent.push({ ...row, state: t.status === "done" ? "answered" : t.status });
      }
    }
    const n = (s: string) => tasks.filter((t) => t.state === s).length;
    return {
      tasks,
      recent: recent.slice(-6).reverse(),
      counts: { running: n("running"), queued: n("queued"), question: n("question") },
      queue_max: 3,
    };
  };
  await page.route(`**${HQ_TASKS_API}`, (route) => route.fulfill({ json: list() }));
  await page.route(`**${HQ_TASKS_STOP_ALL_API}`, async (route) => {
    hq.posts.push(HQ_TASKS_STOP_ALL_API);
    let stopped = 0;
    let withdrawn = 0;
    for (const c of fake.conversations) {
      const turns = c.turns as unknown as Turn[];
      for (const t of turns.filter((x) => x.status === "queued")) {
        Object.assign(t, { status: "withdrawn", settled_at: NOW + 30 });
        withdrawn += 1;
      }
      for (const t of turns.filter((x) => x.status === "pending")) {
        await end(c.id, t.turn_id, "stopped", null);
        stopped += 1;
      }
    }
    await route.fulfill({ json: { stopped, withdrawn, ...list() } });
  });
  return hq;
}

async function ask(panel: Locator, text: string) {
  await panel.getByTestId("composer-input").fill(text);
  await panel.getByTestId("composer-input").press("Enter");
}

const conv = (id: string, turns: FakeTurn[]) => ({ id, created_at: 1, updated_at: 2, turns });

/** One running and two queued in A, one parked on a question in B, and a few settled ones. */
function busySeed() {
  return [
    conv(A, [
      turn("What changed in the map since yesterday?", "done"),
      turn("How far are we with #1613 and who is working on it?", "pending"),
      turn("And which sessions are blocked on review right now?", "queued"),
      turn("Draft a status note for the release channel", "queued"),
    ]),
    conv(B, [
      turn("Archive old docs sessions", "done", {
        ask: {
          question_id: crypto.randomUUID(),
          question: "All of them, or only the merged ones?",
          options: ["All", "Only merged"],
          allow_text: false,
          answered: false,
        },
      }),
    ]),
  ];
}

const box = async (l: Locator) => (await l.boundingBox())!;

test("a question asked while one runs is queued, with Stop beside Send, and runs next", async ({
  page,
}) => {
  const hq = await mockHq(page);
  await page.goto("/");
  const panel = await openAsk(page);
  await ask(panel, "How far are we with #1613?");
  const stop = panel.getByTestId("composer-stop");
  const send = panel.getByTestId("composer-send");
  await expect(stop).toBeVisible();
  // Stop sits BESIDE Send (#2103): a square left of it on the same line, 44px both ways; Send
  // stays, disabled only because the box is empty.
  await expect(stop).toHaveAccessibleName("Stop HQ");
  await expect(send).toBeVisible();
  await expect(send).toBeDisabled();
  const [s, d] = [await box(stop), await box(send)];
  expect(s.x + s.width).toBeLessThanOrEqual(d.x + 0.5);
  expect(Math.abs(s.y + s.height / 2 - (d.y + d.height / 2))).toBeLessThan(1.5);
  expect(s.width).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  expect(s.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await expect(panel.getByTestId("composer-queue-line")).toHaveCount(0);

  await ask(panel, "And which sessions are blocked?");
  // Its own request, behind the running one: the Commander's bubble, dashed, marked Queued.
  const turns = panel.getByTestId("ask-turn");
  await expect(turns).toHaveCount(2);
  const bubble = turns.nth(1).getByRole("article", { name: "You — Queued" });
  await expect(bubble).toHaveText(/And which sessions are blocked\?/);
  await expect(bubble).toHaveCSS("border-top-style", "dashed");
  await expect(turns.nth(1).getByTestId("ask-queue-line")).toContainText("Queued");
  await expect(turns.nth(1).getByTestId("ask-withdraw")).toBeVisible();
  await expect(turns.nth(1).getByRole("article", { name: /^Answer from/ })).toHaveCount(0);
  await expect(panel.getByTestId("composer-queue-line")).toHaveText(
    "Queued behind the running question",
  );
  expect(hq.posts).toEqual(["stream:How far are we with #1613?", "stream:And which sessions are blocked?"]);
  // The first is still the one running, and the box is free for the next question.
  await expect(panel.getByTestId("ask-working")).toHaveCount(1);
  await expect(panel.getByTestId("composer-input")).toHaveValue("");
  await expect(panel.getByTestId("composer-input")).toBeEnabled();
  // Both are in this conversation's thread, so the sidebar's strip (other conversations' tasks
  // only) stays away: one question, one place. The shell's working mark still counts them.
  await expect(panel.getByTestId("hq-tasks")).toHaveCount(0);
  const mark = page.locator('.hud-topbar [data-testid="hq-tasks-mark"]');
  await expect(mark).toHaveAccessibleName("HQ tasks: 1 running, 1 queued");

  // The running question settles: the queued one runs next, by itself.
  await hq.settle(hq.fake.conversations[0].id, "It is nine commits in of eleven.");
  await expect(turns.nth(0).getByTestId("ask-answer")).toHaveText(
    "It is nine commits in of eleven.",
  );
  await expect(turns.nth(1).getByTestId("ask-working")).toBeVisible();
  await expect(turns.nth(1).getByTestId("ask-queue-line")).toHaveCount(0);
  await expect(panel.getByTestId("composer-queue-line")).toHaveCount(0);
  await expect(mark).toHaveAccessibleName("HQ tasks: 1 running");
  await hq.settle(hq.fake.conversations[0].id, "Two are blocked on review.");
  await expect(turns.nth(1).getByTestId("ask-answer")).toHaveText("Two are blocked on review.");
  await expect(panel.getByTestId("composer-stop")).toHaveCount(0);
  await expect(panel.getByTestId("hq-tasks")).toHaveCount(0);
  await expect(mark).toHaveCount(0); // nothing left to act on
});

test("Withdraw takes a queued question back; a full queue closes Send and says why", async ({
  page,
}) => {
  const hq = await mockHq(page);
  await page.goto("/");
  const panel = await openAsk(page);
  await ask(panel, "First question");
  await expect(panel.getByTestId("composer-stop")).toBeVisible();
  for (const q of ["Second", "Third", "Fourth"]) {
    await ask(panel, q);
    await expect(panel.getByRole("article", { name: "You — Queued" }).last()).toHaveText(
      new RegExp(q),
    );
  }
  // Three wait: Send is closed, in words, and Enter sends nothing.
  await panel.getByTestId("composer-input").fill("Fifth");
  await expect(panel.getByTestId("composer-send")).toBeDisabled();
  await expect(panel.getByTestId("composer-queue-line")).toHaveText(
    "Queue full — withdraw one or wait",
  );
  await panel.getByTestId("composer-input").press("Enter");
  await expect(panel.getByTestId("composer-input")).toHaveValue("Fifth");
  expect(hq.posts.filter((p) => p.startsWith("stream:"))).toHaveLength(4);

  // Withdraw the second: it names that turn, and a slot is free again.
  const cid = hq.fake.conversations[0].id;
  const second = turnsOf(hq.fake, cid)[1];
  // Only the thread offers its Withdraw: the sidebar's strip leaves this conversation out.
  await expect(panel.getByRole("button", { name: "Withdraw: Second" })).toHaveCount(1);
  const withdraw = panel.getByTestId("ask-turn").nth(1).getByTestId("ask-withdraw");
  await expect(withdraw).toHaveAccessibleName("Withdraw: Second");
  expect((await box(withdraw)).height).toBeGreaterThanOrEqual(
    (await page.evaluate(() => matchMedia("(pointer: coarse), (max-width: 800px)").matches))
      ? TOUCH_FLOOR
      : 32,
  );
  await withdraw.click();
  expect(hq.posts).toContain(askWithdrawTurnPath(cid, second.turn_id));
  const line = panel.getByTestId("ask-turn").nth(1).getByTestId("ask-queue-line");
  await expect(line).toHaveText("Withdrawn");
  await expect(panel.getByTestId("composer-queue-line")).toHaveText(
    "Queued behind the running question",
  );
  await expect(panel.getByTestId("composer-send")).toBeEnabled();
  await panel.getByTestId("composer-input").press("Enter");
  await expect(panel.getByRole("article", { name: "You — Queued" }).last()).toHaveText(/Fifth/);
  // The running turn was never touched.
  expect(hq.posts.some((p) => p.endsWith("/stop"))).toBe(false);
  await expect(panel.getByTestId("ask-working")).toHaveCount(1);
});

test("Stop all stops the running question and withdraws the queue, from the strip", async ({
  page,
}) => {
  const hq = await mockHq(page);
  await page.goto("/");
  let panel = await openAsk(page);
  await ask(panel, "First question");
  await expect(panel.getByTestId("composer-stop")).toBeVisible();
  await ask(panel, "Second");
  await ask(panel, "Third");
  await expect(panel.getByTestId("ask-queue-line")).toHaveCount(2);
  // All three are this conversation's: the sidebar lists none of them. The working mark's strip
  // lists every conversation's, with Stop all.
  await expect(panel.getByTestId("hq-tasks")).toHaveCount(0);
  await panel.getByTestId("ask-close").click();
  const mark = page.locator('.hud-topbar [data-testid="hq-tasks-mark"]');
  await mark.click();
  const strip = page.getByTestId("hq-tasks");
  await expect(strip.getByTestId("hq-tasks-counts")).toHaveText("1 running · 2 queued");
  await expect(strip.getByTestId("hq-task")).toHaveCount(3);
  const stopAll = strip.getByTestId("hq-tasks-stop-all");
  await expect(stopAll).toHaveText("Stop all");
  await stopAll.click();
  expect(hq.posts).toContain(HQ_TASKS_STOP_ALL_API);
  await expect(strip.getByTestId("hq-tasks-empty")).toHaveText("Nothing running");
  await page.keyboard.press("Escape");
  await expect(strip).toHaveCount(0);
  panel = await openAsk(page);
  // Everything ended: the first stopped, the others withdrawn; nothing runs, Send is back alone.
  await expect(panel.getByTestId("ask-process-tally")).toHaveText(/Stopped after/);
  const lines = panel.getByTestId("ask-queue-line");
  await expect(lines).toHaveCount(2);
  await expect(lines.nth(0)).toHaveText("Withdrawn");
  await expect(lines.nth(1)).toHaveText("Withdrawn");
  await expect(panel.getByTestId("composer-stop")).toHaveCount(0);
  await expect(panel.getByTestId("composer-queue-line")).toHaveCount(0);
  await expect(panel.getByTestId("hq-tasks")).toHaveCount(0);
  await expect(page.getByTestId("hq-tasks-mark")).toHaveCount(0);
});

test("the sidebar's strip lists only the other conversations' tasks, never the open one's", async ({
  page,
}) => {
  await mockHq(page, busySeed());
  await page.goto("/");
  const panel = await openAsk(page);
  const strip = panel.getByTestId("hq-tasks");
  // A new conversation is open: every conversation's tasks are someone else's.
  await expect(strip.getByTestId("hq-tasks-counts")).toHaveText("1 running · 2 queued · 1 question");
  await expect(strip.getByTestId("hq-task")).toHaveCount(4);

  // Open A: its running turn and its queue are in the thread, with Stop and Withdraw; the strip
  // above keeps only B's question, and no Stop all (it would reach A's hidden turns too).
  await panel.getByTestId("ask-switcher").click();
  await panel.getByTestId("ask-switcher-item").filter({ hasText: "What changed in the map" }).click();
  await expect(panel.getByTestId("ask-working")).toHaveCount(1);
  await expect(panel.getByTestId("ask-withdraw")).toHaveCount(2);
  await expect(strip.getByTestId("hq-tasks-counts")).toHaveText("1 question");
  await expect(strip.getByTestId("hq-task")).toHaveCount(1);
  await expect(strip.getByTestId("hq-task")).toHaveAttribute("data-state", "question");
  await expect(strip.getByTestId("hq-task-stop")).toHaveCount(0);
  await expect(strip.getByTestId("hq-tasks-stop-all")).toHaveCount(0);
  // Each Withdraw exists once, in the thread.
  await expect(
    panel.getByRole("button", { name: /^Withdraw: And which sessions are blocked/ }),
  ).toHaveCount(1);

  // Open B: A's three are elsewhere again, and Stop all reaches exactly them.
  await panel.getByTestId("ask-switcher").click();
  await panel.getByTestId("ask-switcher-item").filter({ hasText: "Archive old docs" }).click();
  await expect(strip.getByTestId("hq-tasks-counts")).toHaveText("1 running · 2 queued");
  await expect(strip.getByTestId("hq-task")).toHaveCount(3);
  await expect(strip.getByTestId("hq-tasks-stop-all")).toBeVisible();
});

test("the working mark opens the strip from another section: rows, 44px targets, keyboard order", async ({
  page,
}) => {
  const hq = await mockHq(page, busySeed());
  await page.goto("/dashboard");
  const phone = await page.evaluate(() => matchMedia("(max-width: 800px)").matches);
  const mark = page.locator('.hud-topbar [data-testid="hq-tasks-mark"]');
  // Its own control, left of the HQ icon, saying what it counts; the "?" marks the parked one.
  await expect(mark).toHaveAccessibleName("HQ tasks: 1 running, 2 queued, 1 has a question");
  await expect(mark).toContainText("4");
  await expect(mark.getByTestId("hq-tasks-mark-question")).toHaveText("?");
  const toggle = page.locator('.hud-topbar [data-testid="ask-toggle"]');
  const [m, t] = [await box(mark), await box(toggle)];
  expect(m.x + m.width).toBeLessThanOrEqual(t.x + 0.5);
  expect(m.height).toBeGreaterThanOrEqual(phone ? TOUCH_FLOOR : 34 - 0.01);
  await expect(mark).toHaveAttribute("aria-expanded", "false");

  await mark.focus();
  await page.keyboard.press("Enter");
  await expect(mark).toHaveAttribute("aria-expanded", "true");
  const surface = page.getByTestId(phone ? "hq-tasks-sheet" : "hq-tasks-popover");
  await expect(surface).toBeVisible();
  await expect(page.getByTestId("hq-tasks-scrim")).toHaveCount(phone ? 1 : 0);
  const strip = surface.getByTestId("hq-tasks");
  await expect(strip.getByTestId("hq-tasks-counts")).toHaveText("1 running · 2 queued · 1 question");

  // Uniform rows: the state and tally, the question's first line, the conversation's name.
  const rows = strip.getByTestId("hq-task");
  await expect(rows).toHaveCount(4);
  await expect(rows.nth(0)).toHaveAttribute("data-state", "running");
  await expect(rows.nth(0)).toContainText("Reading a session's status…");
  await expect(rows.nth(0)).toContainText(/7 steps · \d+ s/);
  await expect(rows.nth(0)).toContainText("How far are we with #1613 and who is working on it?");
  await expect(rows.nth(0)).toContainText("What changed in the map since yesterday?");
  await expect(rows.nth(0).locator(".nj-acquire")).toBeVisible();
  await expect(rows.nth(1)).toContainText("Queued");
  await expect(rows.nth(3)).toHaveAttribute("data-state", "question");
  await expect(rows.nth(3)).toContainText("HQ has a question");
  // A parked task has nothing to stop: it links to its conversation.
  await expect(rows.nth(3).getByRole("button")).toHaveCount(0);
  await expect(rows.nth(3).getByRole("link", { name: /^Answer in/ })).toHaveAttribute(
    "href",
    hqConversationPath(B),
  );
  // One line each, never wider than the strip.
  const overflow = await strip.evaluate((el) => el.scrollWidth - el.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  // Recent rows are real links with a visible Open.
  const recent = strip.getByTestId("hq-task-recent");
  await expect(recent).toHaveCount(1);
  await expect(recent.first()).toContainText("Answered");
  const open = recent.first().getByRole("link", { name: /^Open / });
  await expect(open).toBeVisible();
  await expect(open).toHaveAttribute("href", hqConversationPath(A));

  // Every control is a 44px target where a finger is the pointer.
  if (await page.evaluate(() => matchMedia("(pointer: coarse), (max-width: 800px)").matches)) {
    const targets = [
      strip.getByTestId("hq-tasks-stop-all"),
      strip.getByTestId("hq-task-stop"),
      strip.getByTestId("hq-task-withdraw").first(),
      strip.getByTestId("hq-task-answer"),
      open,
      strip.getByTestId("hq-tasks-open-hq"),
      ...(phone ? [strip.getByTestId("hq-tasks-close")] : []),
    ];
    for (const target of targets)
      expect((await box(target)).height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }

  // Keyboard order: Stop all, (Close), then each row's action top to bottom, then Open HQ.
  const order = [
    "hq-tasks-stop-all",
    ...(phone ? ["hq-tasks-close"] : []),
    "hq-task-stop",
    "hq-task-withdraw",
    "hq-task-withdraw",
    "hq-task-answer",
    "hq-task-open",
    "hq-tasks-open-hq",
  ];
  await strip.getByTestId(phone ? "hq-tasks-close" : "hq-tasks-stop-all").focus();
  if (phone) await page.keyboard.press("Shift+Tab");
  const seen: (string | null)[] = [];
  for (let i = 0; i < order.length; i += 1) {
    seen.push(await page.evaluate(() => document.activeElement?.getAttribute("data-testid") ?? null));
    await page.keyboard.press("Tab");
  }
  expect(seen).toEqual(order);

  // Escape closes it and gives focus back to the mark; the HQ icon was never touched.
  await page.keyboard.press("Escape");
  await expect(surface).toHaveCount(0);
  await expect(mark).toBeFocused();
  await expect(toggle).toHaveAttribute("aria-expanded", "false");

  // Stop names the running turn; the list follows.
  await mark.click();
  await surface.getByTestId("hq-task-stop").click();
  const running = turnsOf(hq.fake, A)[1];
  expect(hq.posts).toContain(askStopTurnPath(A, running.turn_id));
  await expect(strip.getByTestId("hq-tasks-counts")).toHaveText("1 running · 1 queued · 1 question");
  // Open HQ goes to HQ's page and closes the strip; the page's list carries the same strip.
  await surface.getByTestId("hq-tasks-open-hq").click();
  await expect(page).toHaveURL(/\/hq$/);
  await expect(surface).toHaveCount(0);
  if (!phone) {
    const inList = page.getByTestId("hq-list").getByTestId("hq-tasks");
    await expect(inList).toHaveAttribute("data-host", "page");
    await expect(inList.getByTestId("hq-task")).toHaveCount(3);
  }
});

test("on a short phone the sheet scrolls, and Close and Stop all stay in reach", async ({ page }) => {
  // Many conversations, each with a running question and one queued behind it.
  const many = Array.from({ length: 7 }, (_, i) =>
    conv("ask_" + String(i + 1).repeat(32), [
      turn(`Conversation ${i + 1}: a long first question that names the whole release train`, "pending"),
      turn(`Conversation ${i + 1}: and a follow-up waiting behind it`, "queued"),
    ]),
  );
  await mockHq(page, many);
  await page.setViewportSize({ width: 320, height: 600 });
  await page.goto("/dashboard");
  const mark = page.locator('.hud-topbar [data-testid="hq-tasks-mark"]');
  await expect(mark).toHaveAccessibleName("HQ tasks: 7 running, 7 queued");
  await mark.click();
  const sheet = page.getByTestId("hq-tasks-sheet");
  await expect(sheet).toBeVisible();
  await expect(sheet.getByTestId("hq-task")).toHaveCount(14);
  const metrics = () =>
    sheet.evaluate((el) => ({
      scroll: el.scrollHeight,
      client: el.clientHeight,
      top: el.scrollTop,
      wide: el.scrollWidth - el.clientWidth,
      bottom: el.getBoundingClientRect().bottom,
    }));
  // Once it has slid up (the box is read after the arrival's transform has ended).
  await expect.poll(async () => (await metrics()).bottom).toBeLessThanOrEqual(600 + 0.5);
  const before = await metrics();
  // Taller than the viewport allows: it scrolls inside itself, never sideways, and stays on screen.
  expect(before.scroll).toBeGreaterThan(before.client + 200);
  expect(before.client).toBeLessThanOrEqual(600 * 0.85 + 1);
  expect(before.bottom).toBeLessThanOrEqual(600 + 0.5);
  expect(before.wide).toBeLessThanOrEqual(0);

  // Scroll to the last row: it is reachable, and the head has not gone anywhere.
  const last = sheet.getByTestId("hq-task").last();
  await last.scrollIntoViewIfNeeded();
  await expect(last).toBeInViewport();
  expect((await metrics()).top).toBeGreaterThan(200);
  const inView = async (l: Locator) => {
    const b = await box(l);
    const s = await box(sheet);
    return b.y >= s.y - 0.5 && b.y + b.height <= s.y + s.height + 0.5 && b.height >= TOUCH_FLOOR;
  };
  const stopAll = sheet.getByTestId("hq-tasks-stop-all");
  const close = sheet.getByTestId("hq-tasks-close");
  expect(await inView(stopAll)).toBe(true);
  expect(await inView(close)).toBe(true);
  // …and they are really pressable there: nothing of the list lies over them.
  await stopAll.click({ trial: true });
  await close.click();
  await expect(sheet).toHaveCount(0);
  await expect(mark).toBeFocused();
});

test("a notification about a settled question opens its conversation", async ({ page }) => {
  await mockHq(page, [
    conv(B, [turn("Archive old docs sessions", "done", { answer: "Archived eleven sessions." })]),
  ]);
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({
      json: {
        notifications: [
          {
            id: "n1",
            ts: NOW,
            read: false,
            title: "HQ answered",
            project: "",
            reason: "Archive old docs sessions",
            session_id: "",
            engine: "hq",
            action_id: `hq-turn:${B}:t1`,
            hq_conversation: B,
          },
        ],
        unread: 1,
        uncertain: 0,
        settled: [],
      },
    }),
  );
  await page.goto("/dashboard");
  await page.getByRole("button", { name: /Notifications/ }).click();
  const bell = page.getByRole("dialog", { name: "Notifications" });
  const row = bell.getByRole("listitem").filter({ hasText: "HQ answered" });
  // The existing row, unchanged: a fixed title, the conversation's name, no answer text.
  await expect(row).toContainText("Archive old docs sessions");
  await expect(row).not.toContainText("Archived eleven sessions.");
  const open = row.getByRole("link", { name: "Open" });
  await expect(open).toHaveAttribute("href", hqConversationPath(B));
  await open.click();
  await expect(page).toHaveURL(new RegExp(`${hqConversationPath(B)}$`));
  await expect(page.getByTestId("hq-thread").getByTestId("ask-answer")).toHaveText(
    "Archived eleven sessions.",
  );
});
