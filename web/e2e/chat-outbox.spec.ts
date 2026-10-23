import { expect, test, type Page, type Route } from "@playwright/test";
import { mockRoster } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";

// #1465 for the HTTP chat client (`apichat`, runtime `chat`, rendered by ChatPane): a sent message
// leaves the composer at once and shows as pending, with the same outbox contract as the native
// clients. The server is a mocked `chat_runtime`: `POST /api/chat/<sid>/messages {turn_id, text}`
// records a pending turn, deduplicates an exact repeat by `turn_id`, and REFUSES (409) a second
// turn while a reply runs — so a client that advanced its queue early would be refused here.

const A = "0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b";
const B = "0190a3b2-1c2d-7e3f-8a9b-000000000000";
const ROUTE = /\/api\/chat\/apichat(?::|%3A)([0-9a-f-]{36})(\/messages)?$/;

type Turn = Record<string, unknown>;
/** What the server does with one send. `record`: records it (202). `lost`: records it, then the
 *  response is lost. `abort`: lost before anything was recorded. `{status}`: refused. */
type Outcome = "record" | "lost" | "abort" | { status: number; detail: string };

function turn(over: Turn): Turn {
  const now = Date.now() / 1000;
  return { text: "hello", ts: now, status: "done", reason: null, reply: "hi", reply_ts: now,
    usage: null, truncated: false, dropped: 0, ...over };
}

async function app(page: Page, opts: { theme?: string; a?: Turn[] } = {}) {
  const theme = opts.theme ?? "dark";
  await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
  await page.route("**/api/**", (r) => r.fulfill({ status: 404, json: { detail: "not mocked" } }));
  await page.route("**/api/config", (r) => r.fulfill({ json: {
    csrf: "x", new_session_engines: ["apichat"], terminal_backend: "ws", auth_mode: "none",
    overview_expanded: [], projects_hidden: [], theme,
  } }));
  await page.route("**/api/sessions**", (r) => r.fulfill({ json: {
    sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] },
  } }));
  await page.route("**/api/agents/apichat/endpoint", (r) => r.fulfill({ json: {
    base_url: "https://llm.example.lan/v1", model: "qwen3-coder", api_key_set: true,
    context_window: 32768, max_output_tokens: 4096, request_timeout: null, configured: true, tools: "none",
  } }));
  await mockRoster(page, { overrides: { apichat: { present: true, supports_new: true } } });
  const conversations: Record<string, Turn[]> = {
    [A]: opts.a ?? [],
    [B]: [turn({ turn_id: "b0000000-0000-4000-8000-000000000000", text: "Other conversation.",
      reply: "Hello from the other conversation." })],
  };
  const fx = {
    conversations,
    /** Every send that reached the server, in order. */
    sends: [] as { conversation: string; body: { turn_id: string; text: string } }[],
    /** Answers to sends with a turn id the server already has (its dedup). */
    replays: 0,
    /** Every read fails (503) while set. */
    failReads: false,
    /** Another worker holds the conversation's fence: every send is refused BEFORE a turn is
     *  appended (`chat_runtime._begin`), while reads still show nothing in flight. */
    fenced: false,
    plan: [] as (Outcome | "hold")[],
    holds: new Map<number, (o: Outcome) => void>(),
    release(i: number, outcome: Outcome = "record") {
      const go = fx.holds.get(i);
      if (!go) throw new Error(`send ${i} is not held`);
      fx.holds.delete(i);
      go(outcome);
    },
    /** The running reply finishes. */
    finish(id = A) {
      for (const t of conversations[id]) if (t.status === "pending") Object.assign(t, { status: "done", reply: `Reply to: ${t.text}` });
    },
  };
  const inFlight = (id: string) =>
    conversations[id].find((t) => t.status === "pending" || t.status === "awaiting_approval");
  const answer = async (r: Route, id: string, body: { turn_id: string; text: string }, outcome: Outcome) => {
    if (outcome === "abort") return r.abort("failed");
    if (typeof outcome === "object") return r.fulfill({ status: outcome.status, json: { detail: outcome.detail } });
    // `chat_runtime._begin`: an exact repeat answers with the recorded turn (409 while pending).
    const existing = conversations[id].find((t) => t.turn_id === body.turn_id);
    if (existing) {
      fx.replays += 1;
      return existing.status === "pending"
        ? r.fulfill({ status: 409, json: { detail: "in_flight" } })
        : r.fulfill({ status: 200, json: { turn: existing } });
    }
    if (inFlight(id)) return r.fulfill({ status: 409, json: { detail: "another message is still waiting for its reply" } });
    conversations[id].push(turn({ turn_id: body.turn_id, text: body.text, status: "pending", reply: null, reply_ts: null }));
    if (outcome === "lost") return r.abort("failed");
    return r.fulfill({ status: 202, json: { turn: { turn_id: body.turn_id, status: "pending" } } });
  };
  await page.route(ROUTE, async (r) => {
    const [, id, messages] = r.request().url().match(ROUTE) ?? [];
    if (!conversations[id]) return r.fulfill({ status: 404, json: { detail: "no such conversation" } });
    if (messages && r.request().method() === "POST") {
      const body = r.request().postDataJSON() as { turn_id: string; text: string };
      const i = fx.sends.length;
      fx.sends.push({ conversation: id, body });
      if (fx.fenced) return r.fulfill({ status: 409, json: { detail: "in_flight — another worker owns this conversation" } });
      const plan = fx.plan.shift() ?? "record";
      if (plan !== "hold") return answer(r, id, body, plan);
      const outcome = await new Promise<Outcome>((go) => fx.holds.set(i, go));
      return answer(r, id, body, outcome).catch(() => {}); // a page that left may have closed it
    }
    if (fx.failReads) return r.fulfill({ status: 503, json: { detail: "unavailable" } });
    return r.fulfill({ json: { session_id: id, cwd: "/w", created_at: 0, revision: conversations[id].length,
      turns: conversations[id], in_flight: (inFlight(id)?.turn_id as string) ?? null } });
  });
  return fx;
}

const box = (page: Page) => page.getByLabel("Message the agent");
const pending = (page: Page) => page.getByTestId("structured-pending");
const turnRow = (page: Page, text: string) => page.getByTestId("chat-turn").filter({ hasText: text });

async function open(page: Page, id = A) {
  await page.goto(`/s/apichat/${id}`);
  await expect(page.getByTestId("chat-pane")).toBeVisible();
  await expect(box(page)).toBeEnabled();
}

async function send(page: Page, text: string) {
  await box(page).fill(text);
  await page.getByRole("button", { name: "Send", exact: true }).click();
}

async function goInApp(page: Page, path: string) {
  await page.evaluate((p) => {
    history.pushState(null, "", p);
    dispatchEvent(new PopStateEvent("popstate"));
  }, path);
}

test("two sends during a slow reply wait for it, then go one reply at a time, in order, never refused", async ({ page }, testInfo) => {
  const fx = await app(page, { a: [turn({ turn_id: "c0000000-0000-4000-8000-000000000000",
    text: "Check startup.", status: "pending", reply: null, reply_ts: null })] });
  await open(page);
  await expect(page.getByTestId("chat-waiting")).toBeVisible();
  await send(page, "Now look at the idle timeout too.");
  await expect(box(page)).toHaveValue(""); // left the composer at once
  await send(page, "And write the findings to notes.md.");
  await expect(pending(page)).toHaveCount(2);
  await expect(pending(page).first()).toContainText("Goes out when the current reply has finished.");
  await expect(pending(page).nth(1)).toContainText("Goes out after the message above.");
  await expect(page.getByTestId("structured-outbox-note")).toHaveText("Not sent yet — leaving this session discards it");
  await page.screenshot({ path: testInfo.outputPath("waiting.png") });
  await page.waitForTimeout(2_500); // a poll cycle while the reply runs: nothing goes
  expect(fx.sends).toHaveLength(0);
  fx.finish();
  await expect.poll(() => fx.sends.length).toBe(1);
  expect(fx.sends[0].body.text).toBe("Now look at the idle timeout too.");
  await expect(turnRow(page, "Now look at the idle timeout too.")).toHaveCount(1);
  await expect(pending(page)).toHaveCount(1); // its reply runs: the second waits again
  await page.waitForTimeout(2_500);
  expect(fx.sends).toHaveLength(1);
  fx.finish();
  await expect.poll(() => fx.sends.length).toBe(2);
  expect(fx.sends[1].body.text).toBe("And write the findings to notes.md.");
  expect(fx.sends[1].body.turn_id).not.toBe(fx.sends[0].body.turn_id);
  await expect(pending(page)).toHaveCount(0);
  await expect(page.getByTestId("chat-turn")).toHaveCount(3);
  await expect(page.locator('[data-phase="refused"]')).toHaveCount(0);
  await expect(page.getByTestId("chat-send-error")).toHaveCount(0);
});

test("refused → Edit puts the words back and releases the queue; the edit is a new turn after it", async ({ page }, testInfo) => {
  const fx = await app(page);
  fx.plan.push("hold");
  await open(page);
  await send(page, "Now look at the idle timeout too.");
  await send(page, "And write the findings to notes.md.");
  await expect.poll(() => fx.sends.length).toBe(1);
  fx.release(0, { status: 413, detail: "too long for this endpoint's context window" });
  const refused = pending(page).first();
  await expect(refused).toHaveAttribute("data-phase", "refused");
  await expect(refused).toContainText("Not sent: too long for this endpoint's context window");
  await expect(refused.getByRole("button", { name: "Retry" })).toHaveCount(0);
  await expect(pending(page).nth(1)).toHaveAttribute("data-phase", "waiting");
  await page.waitForTimeout(1_200);
  expect(fx.sends).toHaveLength(1); // the order holds behind the failure
  const edit = refused.getByRole("button", { name: "Edit" });
  const discard = refused.getByRole("button", { name: "Discard" });
  for (const b of [edit, discard]) expect((await b.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await edit.focus();
  await page.keyboard.press("Tab");
  await expect(discard).toBeFocused();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
  await page.screenshot({ path: testInfo.outputPath("refused.png") });
  await edit.click();
  await expect(box(page)).toHaveValue("Now look at the idle timeout too.");
  await expect(box(page)).toBeFocused();
  await expect.poll(() => fx.sends.length).toBe(2); // released: the waiting one goes
  expect(fx.sends[1].body.text).toBe("And write the findings to notes.md.");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect(pending(page)).toHaveCount(1);
  await expect(pending(page)).toContainText("Goes out when the current reply has finished.");
  fx.finish();
  await expect.poll(() => fx.sends.length).toBe(3);
  expect(fx.sends[2].body.text).toBe("Now look at the idle timeout too.");
  expect(fx.sends[2].body.turn_id).not.toBe(fx.sends[0].body.turn_id);
  await expect(pending(page)).toHaveCount(0);
});

test("unknown → no Edit; a Retry during the outage waits, and the first good read shows the turn once — nothing re-sent", async ({ page }, testInfo) => {
  const fx = await app(page);
  fx.plan.push("lost");
  await open(page);
  // The response is lost AFTER the server recorded it, and the reads right after fail too.
  let failing = true;
  await page.route(ROUTE, (r) => (r.request().method() === "GET" && failing
    ? r.fulfill({ status: 503, json: { detail: "unavailable" } }) : r.fallback()));
  await send(page, "Now look at the idle timeout too.");
  const entry = pending(page).first();
  await expect(entry).toHaveAttribute("data-phase", "unknown");
  await expect(entry).toContainText("May not have reached BattleLab. Retrying sends the same message; it won’t be duplicated.");
  await expect(entry.getByRole("button", { name: "Edit" })).toHaveCount(0);
  await expect(entry.getByRole("button", { name: "Discard — it may still arrive" })).toBeVisible();
  await expect(page.getByTestId("structured-outbox-note")).toHaveText(
    "Leaving drops this list; a message already on its way may still arrive");
  for (const b of [entry.getByRole("button", { name: "Retry" }), entry.getByRole("button", { name: "Discard — it may still arrive" })])
    expect((await b.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await page.screenshot({ path: testInfo.outputPath("unknown.png") });
  await entry.getByRole("button", { name: "Retry" }).click();
  // #1591: while reads fail the Retry waits in the outbox rather than posting into a dead link…
  await expect(entry).toHaveAttribute("data-phase", "waiting");
  expect(fx.sends.length).toBe(1);
  // …and the first good read shows the server already has the turn (only its response was lost):
  // the entry settles by the turn's id and nothing is re-sent — one turn, never two. (A Retry the
  // server answers with the same id is pinned in ChatPane.test.tsx's 409-in_flight case.)
  failing = false;
  await expect(pending(page)).toHaveCount(0, { timeout: 15_000 });
  expect(fx.sends.length).toBe(1);
  expect(fx.replays).toBe(0);
  await expect(turnRow(page, "Now look at the idle timeout too.")).toHaveCount(1);
  await expect(page.getByTestId("chat-turn")).toHaveCount(1);
});

test("a lost response is reconciled by the late read: no duplicate, nothing re-sent", async ({ page }) => {
  const fx = await app(page);
  fx.plan.push("lost");
  await open(page);
  await send(page, "Now look at the idle timeout too.");
  await expect(turnRow(page, "Now look at the idle timeout too.")).toHaveCount(1);
  await expect(pending(page)).toHaveCount(0);
  await page.waitForTimeout(2_500);
  expect(fx.sends).toHaveLength(1);
  await expect(page.getByTestId("chat-turn")).toHaveCount(1);
});

test("switch during a held POST: the recorded turn appears once, no stale row, the waiting entry is gone", async ({ page }) => {
  const fx = await app(page);
  fx.plan.push("hold");
  await open(page);
  await send(page, "Now look at the idle timeout too.");
  await send(page, "And write the findings to notes.md.");
  await expect.poll(() => fx.sends.length).toBe(1);
  await goInApp(page, `/s/apichat/${B}`);
  await expect(page.getByText("Hello from the other conversation.")).toBeVisible();
  fx.release(0); // the server records it after we left
  await page.waitForTimeout(1_200);
  await expect(box(page)).toHaveValue(""); // nothing of A's in B's composer…
  await expect(pending(page)).toHaveCount(0); // …or B's outbox
  fx.finish();
  await page.evaluate(() => history.back());
  await expect(turnRow(page, "Now look at the idle timeout too.")).toHaveCount(1);
  await expect(page.getByTestId("chat-turn")).toHaveCount(1);
  await expect(pending(page)).toHaveCount(0);
  await page.waitForTimeout(2_500);
  expect(fx.sends).toHaveLength(1); // the waiting entry went with the pane
});

test("beforeunload is armed only while an unrecorded entry exists", async ({ page }) => {
  const fx = await app(page);
  fx.plan.push("hold");
  await open(page);
  const armed = () => page.evaluate(() => {
    const ev = new Event("beforeunload", { cancelable: true });
    dispatchEvent(ev);
    return ev.defaultPrevented;
  });
  expect(await armed()).toBe(false);
  await send(page, "Now look at the idle timeout too.");
  await expect(pending(page)).toHaveCount(1);
  expect(await armed()).toBe(true);
  fx.release(0);
  await expect(pending(page)).toHaveCount(0);
  expect(await armed()).toBe(false);
});

test("a fence 409 before the turn is appended waits — never refused — although the read shows nothing in flight", async ({ page }) => {
  const fx = await app(page);
  await open(page);
  fx.fenced = true;
  await send(page, "Now look at the idle timeout too.");
  await expect.poll(() => fx.sends.length).toBe(1);
  await expect(pending(page)).toHaveAttribute("data-phase", "waiting");
  await expect(page.locator('[data-phase="refused"]')).toHaveCount(0);
  fx.fenced = false; // the other worker lets go
  await expect(pending(page)).toHaveCount(0);
  await expect(turnRow(page, "Now look at the idle timeout too.")).toHaveCount(1);
  expect(new Set(fx.sends.map((s) => s.body.turn_id)).size).toBe(1); // the same turn throughout
});

test("a busy 409 while reads fail stays bounded — no POST loop — and is delivered once both recover", async ({ page }) => {
  const fx = await app(page);
  await open(page); // the last good read: idle
  fx.fenced = true;
  fx.failReads = true;
  await send(page, "Now look at the idle timeout too.");
  await expect.poll(() => fx.sends.length).toBe(1);
  await page.waitForTimeout(6_000); // several backoff rounds with every read failing
  expect(fx.sends.length).toBe(1); // nothing re-POSTed against the stale idle read
  await expect(pending(page)).toHaveAttribute("data-phase", "waiting");
  fx.fenced = false;
  fx.failReads = false;
  await expect(pending(page)).toHaveCount(0, { timeout: 30_000 });
  await expect(turnRow(page, "Now look at the idle timeout too.")).toHaveCount(1);
  expect(fx.sends.length).toBe(2);
});
