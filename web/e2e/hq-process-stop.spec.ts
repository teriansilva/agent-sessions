/** HQ's process row and Stop, in a real browser (#2076).
 *
 *  What a DOM emulator cannot prove: that Stop really sits in the composer's foot row beside
 *  Send (#2103; it took Send's place until then) and is a 44px target on a coarse pointer, that a step line is one too, that Esc reaches HQ only when nothing over it
 *  owns the key, that the row and its bar hold still outside Full motion, that a long list scrolls
 *  with the thread (never inside itself) with the composer still docked, and that keyboard focus
 *  reaches the row and Stop in order. Runs on both projects (desktop and phone).
 *
 *  The API is mocked. A question's stream is HELD open until the test stops it, as a long turn
 *  is; a turn already running when HQ opens is read back with the server's live totals.
 */
import { expect, test, type Page, type Route } from "@playwright/test";

import { askStopTurnPath } from "../src/lib/apiPaths";
import { openAsk, pickConversation } from "./askSidebar";
import { mockAskConversations, type FakeAsk, type FakeTurn } from "./askStream";
import { mockNightjarShell, storeMotionChoice } from "./nightjarShell";
import { TOUCH_FLOOR } from "./touchTarget";

const CID = "ask_" + "c".repeat(32);

interface Step {
  name: string;
  outcome: "ok" | "refused";
  n: number;
  ms: number;
  count?: number;
  error?: string;
  sent?: string;
  effect?: boolean;
  args?: Record<string, string>;
}

function steps(n: number): Step[] {
  return Array.from({ length: n }, (_, i) =>
    i % 5 === 4
      ? {
          name: "session_status",
          outcome: "refused",
          n: i + 1,
          ms: 100,
          error: "unknown_key",
          args: { key: "claude:4be1a09c-0000-4000-8000-000000000000" },
        }
      : {
          name: i % 2 ? "session_status" : "find_sessions",
          outcome: "ok",
          n: i + 1,
          ms: 800,
          count: 12,
          args: i % 2
            ? { key: "claude:00000000-0000-4000-8000-00000000000" + (i % 10) }
            : { query: "the memory review panel and everything around it" },
        },
  );
}

/** A turn with the process fields the server adds (#2076); the fake's own type predates them. */
function turn(over: Record<string, unknown>): FakeTurn {
  return {
    turn_id: crypto.randomUUID(),
    question: "How far are we with #1613 and who is working on it?",
    asked_at: 1_700_000_000,
    status: "done",
    answer: null,
    detail: null,
    code: null,
    matches: [],
    mission_matches: [],
    tools: [],
    contact: "off",
    ...over,
  } as unknown as FakeTurn;
}

const stopped = (turnId: string, question: string, done: Step[]) =>
  turn({
    turn_id: turnId,
    question,
    status: "stopped",
    tools: done,
    steps: { count: done.length, more: null, incomplete: false },
    elapsed_ms: 58_000,
  });

const running = (done: Step[]) =>
  turn({
    status: "pending",
    tools: done,
    steps: {
      count: done.length,
      more: null,
      incomplete: false,
      live: { n: done.length + 1, tool: "session_status", at: 1_700_000_050 },
    },
    elapsed_ms: 42_000,
  });

interface Held {
  fake: FakeAsk;
  /** Stop requests the page made, by path. */
  stops: string[];
}

/** The shell, the conversation routes, a stream that stays open, and the stop route: stopping
 *  settles the held turn as `stopped` with `done` steps and ends its stream with the same turn. */
async function mockHq(page: Page, seed: FakeTurn[] = [], done: Step[] = steps(3)): Promise<Held> {
  await mockNightjarShell(page);
  const fake = await mockAskConversations(
    page,
    seed.length ? [{ id: CID, created_at: 1, updated_at: 2, turns: seed }] : [],
  );
  const held: Held = { fake, stops: [] };
  let stream: { route: Route; turnId: string; text: string } | null = null;
  await page.route("**/api/ask/conversations/*/message/stream", (route) => {
    const req = route.request().postDataJSON() as { turn_id: string; text: string };
    stream = { route, turnId: req.turn_id, text: req.text }; // held: answered only by a Stop
  });
  await page.route(/\/api\/ask\/conversations\/[^/]+\/turns\/[^/]+\/stop$/, async (route) => {
    const path = new URL(route.request().url()).pathname;
    held.stops.push(path);
    const [, cid, turnId] = /conversations\/([^/]+)\/turns\/([^/]+)\/stop$/.exec(path)!;
    const conv = fake.conversations.find((c) => c.id === cid);
    const was = conv?.turns.find((t) => t.turn_id === turnId);
    const settled = stopped(turnId, was?.question ?? stream?.text ?? "", done);
    if (conv) conv.turns = [...conv.turns.filter((t) => t.turn_id !== turnId), settled];
    await route.fulfill({ json: { turn: settled, running: false } });
    if (stream?.turnId === turnId) {
      await stream.route.fulfill({
        status: 200,
        contentType: "application/x-ndjson",
        body: JSON.stringify({ type: "turn", status: 200, turn: settled }) + "\n",
      });
      stream = null;
    }
  });
  return held;
}

async function ask(page: Page, text = "How far are we with #1613?") {
  const panel = await openAsk(page);
  await panel.getByTestId("composer-input").fill(text);
  await panel.getByTestId("composer-input").press("Enter");
  await expect(panel.getByTestId("composer-stop")).toBeVisible();
  return panel;
}

async function openRunning(page: Page, done: Step[]) {
  const held = await mockHq(page, [running(done)], done);
  await page.goto("/");
  const panel = await openAsk(page);
  await pickConversation(panel);
  await expect(panel.getByTestId("ask-working")).toBeVisible();
  return { panel, held };
}

test("Stop sits beside Send, stops the turn it names, and the process row opens", async ({
  page,
}) => {
  const held = await mockHq(page);
  await page.goto("/");
  const first = await openAsk(page);
  /** A control's place in the composer box: its size and its insets from the box's corner. */
  const slot = async (id: string) => {
    const form = (await first.getByTestId("ask-form").boundingBox())!;
    const b = (await first.getByTestId(id).boundingBox())!;
    return {
      right: Math.round(form.x + form.width - (b.x + b.width)),
      // Its centre line in the foot row: Stop is 44px on every pointer, Send 36px on a mouse.
      centre: Math.round(form.y + form.height - (b.y + b.height / 2)),
    };
  };
  const send = await slot("composer-send");
  const panel = await ask(page);

  const stop = panel.getByTestId("composer-stop");
  await expect(stop).toHaveAccessibleName("Stop HQ");
  // #2103: Send KEEPS its slot (a question sent now is queued behind the running one), and
  // Stop is a 44px square to its left, on the same centre line of the foot row.
  await expect(panel.getByTestId("composer-send")).toBeVisible();
  expect(await slot("composer-send")).toEqual(send);
  const beside = await slot("composer-stop");
  expect(beside.centre).toBe(send.centre);
  expect(beside.right).toBeGreaterThan(send.right);
  const stopBox = (await stop.boundingBox())!;
  expect(stopBox.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  expect(stopBox.width).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  // The field stays usable while HQ works.
  await expect(panel.getByTestId("composer-input")).toBeEnabled();
  // The running row: ONE collapsed <details>, its bar inside the summary.
  const row = panel.getByTestId("ask-working");
  await expect(row).not.toHaveAttribute("open", "");
  await expect(row.getByRole("status")).toContainText("Reading your question…");
  await expect(row.locator("summary .nj-acquire")).toBeVisible();
  await expect(row.getByTestId("ask-process-tally")).toContainText(/^0 steps · \d+ s$/);

  await stop.click();
  expect(held.stops).toHaveLength(1);
  const created = held.fake.conversations[0];
  expect(held.stops[0]).toBe(askStopTurnPath(created.id, created.turns[0].turn_id));

  const done = panel.getByTestId("ask-process");
  await expect(done).toHaveAttribute("open", "");
  await expect(done.getByTestId("ask-process-tally")).toHaveText("Stopped after 3 steps");
  await expect(done.getByTestId("ask-process-step")).toHaveCount(3);
  await expect(done.getByTestId("ask-process-step").first()).toContainText(
    "Searching your sessions and missions",
  );
  await expect(done.locator(".nj-acquire")).toHaveCount(0);
  await expect(panel.getByTestId("ask-stopped-line")).toHaveText(
    "Stopped by you. Nothing was sent.",
  );
  await expect(panel.getByTestId("ask-answer")).toHaveCount(0);
  await expect(panel.getByTestId("ask-error")).toHaveCount(0);
  // Send is back, and it stays stopped across a reload of the conversation.
  await expect(panel.getByTestId("composer-send")).toBeVisible();
  await expect(panel.getByTestId("composer-stop")).toHaveCount(0);
  // The row toggles like any <details>.
  await done.getByTestId("ask-process-summary").click();
  await expect(done).not.toHaveAttribute("open", "");
});

test("Esc stops only when no dialog is open and no IME composition is active", async ({ page }) => {
  const held = await mockHq(page);
  await page.goto("/");
  const panel = await ask(page);
  const input = panel.getByTestId("composer-input");

  // An Esc that belongs to an IME composition is the composition's own.
  await input.evaluate((el) => {
    el.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Escape", isComposing: true, bubbles: true, cancelable: true }),
    );
  });
  await page.waitForTimeout(150);
  expect(held.stops).toHaveLength(0);
  await expect(page.getByTestId("ask-sidebar")).toHaveAttribute("data-open", "true");

  // A dialog over HQ takes Esc first: it closes, the turn runs on.
  await panel.getByTestId("ask-delete").click();
  const dialog = page.getByRole("dialog", { name: /delete/i });
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await page.waitForTimeout(150);
  expect(held.stops).toHaveLength(0);
  await expect(panel.getByTestId("composer-stop")).toBeVisible();

  // With nothing over it, Esc in HQ stops the turn — and never closes HQ instead.
  await input.focus();
  await page.keyboard.press("Escape");
  await expect(panel.getByTestId("ask-process")).toHaveAttribute("data-state", "stopped");
  expect(held.stops).toHaveLength(1);
  await expect(page.getByTestId("ask-sidebar")).toHaveAttribute("data-open", "true");
});

test("the Esc hint shows for a keyboard only", async ({ page }, info) => {
  await mockHq(page);
  await page.goto("/");
  const panel = await ask(page);
  const hint = panel.getByTestId("composer-stop-hint");
  if (info.project.name === "mobile") await expect(hint).toBeHidden();
  else await expect(hint).toHaveText("Esc to stop");
});

for (const mode of ["reduced", "off", "full"] as const) {
  test(`the row and its bar ${mode === "full" ? "move in Full" : `hold still in ${mode}`} motion`, async ({
    page,
  }) => {
    await storeMotionChoice(page, mode);
    const { panel } = await openRunning(page, steps(4));
    const row = panel.getByTestId("ask-working");
    const names = await row.evaluate((el) => ({
      bar: getComputedStyle(el.querySelector(".nj-acquire")!, "::after").animationName,
      caret: getComputedStyle(el.querySelector('[data-testid="ask-step"]')!, "::after")
        .animationName,
    }));
    if (mode === "full") {
      expect(names.bar).not.toBe("none");
      expect(names.caret).not.toBe("none");
    } else {
      expect(names).toEqual({ bar: "none", caret: "none" });
      // Nothing in the row is running an animation, opened or closed.
      await row.getByTestId("ask-process-summary").click();
      await expect(row).toHaveAttribute("open", "");
      const moving = await row.evaluate(
        (el) => el.getAnimations({ subtree: true }).filter((a) => a.playState === "running").length,
      );
      expect(moving).toBe(0);
      // The bar still shows (a half-filled track says "working").
      await expect(row.locator(".nj-acquire")).toBeVisible();
    }
  });
}

test("Stop, the row and each step line are at least 44px", async ({ page }) => {
  const { panel } = await openRunning(page, steps(6));
  const row = panel.getByTestId("ask-working");
  const summary = row.getByTestId("ask-process-summary");
  expect((await summary.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  const stop = (await panel.getByTestId("composer-stop").boundingBox())!;
  expect(stop.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  expect(stop.width).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await summary.click();
  const lines = row.getByTestId("ask-process-step");
  await expect(lines).toHaveCount(6);
  for (const box of await lines.evaluateAll((els) => els.map((e) => e.getBoundingClientRect().height)))
    expect(box).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  // The live step is marked, at the same floor, and nothing overflows the row sideways.
  const live = row.getByTestId("ask-process-live");
  await expect(live).toContainText("Running");
  expect((await live.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  expect(await row.evaluate((el) => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(0);
  // A refusal reads as one, in words.
  await expect(lines.nth(4)).toContainText("refused: unknown key");
});

test("with a long process the last step and the composer stay reachable by scrolling", async ({
  page,
}) => {
  const done = steps(200);
  const settled = turn({
    status: "done",
    answer: "Commander, #1613 is three fifths done.",
    tools: done,
    steps: {
      count: 1_440,
      more: { count: 1_240, ms: 1, tools: { session_status: 1_240 }, refused: {} },
      incomplete: false,
    },
    elapsed_ms: 842_000,
  });
  await mockHq(page, [settled]);
  await page.goto("/");
  const panel = await openAsk(page);
  await pickConversation(panel);
  const row = panel.getByTestId("ask-process");
  await expect(row.getByTestId("ask-process-tally")).toHaveText("1,440 steps · 14 m 02 s");
  await row.getByTestId("ask-process-summary").click();
  await expect(row.getByTestId("ask-process-step")).toHaveCount(200);

  // The list never scrolls on its own: the THREAD does.
  const list = row.getByTestId("ask-process-steps");
  const own = await list.evaluate((el) => ({
    overflow: getComputedStyle(el).overflowY,
    clipped: el.scrollHeight - el.clientHeight,
  }));
  expect(own.overflow).toBe("visible");
  expect(own.clipped).toBeLessThanOrEqual(1);
  const pane = panel.getByTestId("ask-pane");
  expect(await pane.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true);

  // The last step, the closing line and the answer are reached by scrolling the thread…
  const last = row.getByTestId("ask-process-step").last();
  await last.scrollIntoViewIfNeeded();
  await expect(last).toBeInViewport();
  await expect(last).toContainText("200");
  const more = row.getByTestId("ask-process-more");
  await more.scrollIntoViewIfNeeded();
  await expect(more).toBeInViewport();
  await expect(more).toContainText("… and 1,240 more lookups");
  const answer = panel.getByTestId("ask-answer");
  await answer.scrollIntoViewIfNeeded();
  await expect(answer).toBeInViewport();
  // …and the composer never left: it is docked under the thread, in view and usable.
  const input = panel.getByTestId("composer-input");
  await expect(input).toBeInViewport();
  await input.fill("and who is on #1691?");
  await expect(panel.getByTestId("composer-send")).toBeEnabled();
  await expect(panel.getByTestId("composer-send")).toBeInViewport();
  // Scrolled back to the top, the composer is still there.
  await pane.evaluate((el) => el.scrollTo(0, 0));
  await expect(input).toBeInViewport();
});

test("keyboard focus reaches the row, then Stop, in order — and both answer the keyboard", async ({
  page,
}) => {
  const { panel, held } = await openRunning(page, steps(2));
  const summary = panel.getByTestId("ask-process-summary");
  await summary.focus();
  await expect(summary).toBeFocused();
  // The summary is a real control: Enter opens the row.
  await page.keyboard.press("Enter");
  await expect(panel.getByTestId("ask-working")).toHaveAttribute("open", "");

  // Tabbing on from the row reaches the composer's field, then Stop — never Stop first.
  const seen: string[] = [];
  for (let i = 0; i < 12 && seen[seen.length - 1] !== "composer-stop"; i++) {
    await page.keyboard.press("Tab");
    seen.push(
      await page.evaluate(() => document.activeElement?.getAttribute("data-testid") ?? ""),
    );
  }
  expect(seen).toContain("composer-input");
  expect(seen[seen.length - 1]).toBe("composer-stop");
  expect(seen.indexOf("composer-input")).toBeLessThan(seen.indexOf("composer-stop"));
  await expect(panel.getByTestId("composer-stop")).toBeFocused();

  await page.keyboard.press("Enter");
  await expect(panel.getByTestId("ask-process")).toHaveAttribute("data-state", "stopped");
  expect(held.stops).toHaveLength(1);
});
