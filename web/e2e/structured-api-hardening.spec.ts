import { expect, test, type Locator, type Page, type Route } from "@playwright/test";

import { mockRoster } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";

// #1725 (with #1420's fix): an API session degrades instead of dying. Real browser, desktop + mobile,
// against a small mocked server that holds the revision rule: a send at any revision but the journal's
// current one is refused 409 `revision_changed`, exactly as the worker's claim is. What a worker's
// last records do is move that revision after the pane last read it.

const ID = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
const ENGINE = "claude-api";
const OLD_TURN = "a0000000-0000-4000-8000-000000000000";
const ROUTE = new RegExp(`/api/structured/sessions/${ENGINE}(?::|%3A)${ID}(/[^?]+)?(\\?.*)?$`);
const NOW = Math.floor(Date.now() / 1000);
const D1 = "1".repeat(64);
const D2 = "2".repeat(64);
const DUNKNOWN = "3".repeat(64);
const TASK = { task_id: "bq7", description: "npm run dev", task_type: "local_bash", started_at: NOW - 240 };
const PYTEST = { task_id: "bq8", description: "pytest -x", task_type: "local_bash", started_at: NOW - 60 };
const CARGO = { pid: 48211, start: 9500, comm: "cargo", started_at: NOW - 3600 };

type Json = Record<string, unknown>;
type Inventory = {
  worker: string; digest: string | null; scan: "ok" | "unknown"; tasks: Json[]; processes: Json[];
  more: number; observed_at: number; overflow?: boolean;
};

const common = { reply: "", text_truncated: false, reply_truncated: false, tools_truncated: false,
  reason: null, tools: [] as Json[] };
const oldTurn = { ...common, turn_id: OLD_TURN, operation_id: OLD_TURN, state: "completed",
  text: "First.", reply: "Done first." };

function inventory(over: Partial<Inventory> = {}): Inventory {
  return { worker: "worker", digest: D1, scan: "ok", tasks: [TASK], processes: [], more: 0, observed_at: NOW, ...over };
}

/** The server. `revision` is the journal's; `shown` is what a read returns until the pane POSTs,
 *  the moment a worker's last records land after the pane's last read. */
async function serve(page: Page, opts: {
  theme?: string; revision?: number; shown?: number; turns?: Json[]; native?: Json; pending?: Json[];
} = {}) {
  const theme = opts.theme ?? "dark";
  await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
  await page.route("**/api/**", (r) => r.fulfill({ status: 404, json: { detail: "not mocked" } }));
  await page.route("**/api/config", (r) => r.fulfill({ json: {
    csrf: "test", terminal_backend: "ws", auth_mode: "none", theme, overview_expanded: [], projects_hidden: [],
  } }));
  await page.route("**/api/sessions**", (r) => r.fulfill({ json: {
    sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] },
  } }));
  await mockRoster(page);
  const server = {
    revision: opts.revision ?? 12,
    shown: opts.shown ?? null as number | null,
    turns: (opts.turns ?? [oldTurn]) as Json[],
    pending: (opts.pending ?? []) as Json[],
    native: { native_id: "native", worker: "worker" as string | null, background_active: false,
      stop_inventory: inventory({ tasks: [] }), background_tasks: [], worker_ends: [], ...opts.native } as Json,
    submits: [] as Json[],
    decisions: [] as Json[],
    stops: [] as Json[],
    /** Refuse the next submits with these answers before the revision rule applies. */
    refusals: [] as { status: number; json: Json }[],
    /** Answers to Stop, in order; the last repeats. Default: stopped. */
    stopAnswers: [] as ((body: Json) => { status: number; json: Json })[],
    /** Runs when Stop is accepted, before its answer (the journal's last records). */
    afterStop: () => {},
    /** Awaited before Stop's answer goes back. */
    beforeStopAnswer: async () => {},
    /** Runs as a send reaches the server, before it is checked. */
    beforeSubmit: () => {},
    /** Awaited before a refusal is answered. */
    beforeRefusal: async () => {},
    /** While set, reads answer with this older copy: a read that lost a race with a newer one. */
    frozen: null as Json | null,
    /** Reads served at the journal's own revision. */
    currentReads: 0,
    /** Answer this many snapshot reads with a 503. */
    failReads: 0,
    snapshot(): Json {
      const running = server.turns.find((t) => t.state === "running");
      return {
        session_key: `${ENGINE}:${ID}`,
        revision: server.shown ?? server.revision,
        event_cursor: server.shown ?? server.revision,
        cwd: "/home/u/demo",
        state: running ? "running" : "idle",
        active_turn: (running?.turn_id as string) ?? null,
        model_requested: null, files: true, images: true, omitted_turns: 0,
        pending_requests: server.pending, read_only: null,
        native: server.native, turns: server.turns,
      };
    },
    record(body: Json) {
      server.turns.push({ ...common, turn_id: body.operation_id, operation_id: body.operation_id,
        state: "completed", text: body.text, reply: `echo:${body.text}`, ts: NOW });
      server.revision += 2;
    },
  };
  await page.route(ROUTE, async (r: Route) => {
    const sub = r.request().url().match(ROUTE)?.[1] ?? "";
    if (sub === "/turns" && r.request().method() === "POST") {
      const body = r.request().postDataJSON() as Json;
      server.submits.push(body);
      server.shown = null; // from here the pane's next read sees the journal as it is
      server.beforeSubmit();
      const refused = server.refusals.shift();
      if (refused) return r.fulfill(refused);
      if (server.turns.some((t) => t.turn_id === body.operation_id)) {
        return r.fulfill({ status: 200, json: { state: "completed", turn_id: body.operation_id, revision: server.revision } });
      }
      if (body.expected_revision !== undefined && body.expected_revision !== server.revision) {
        await server.beforeRefusal();
        return r.fulfill({ status: 409, json: { detail: "native conversation revision changed",
          reason: "revision_changed", remedy: "retry" } });
      }
      server.record(body);
      return r.fulfill({ status: 200, json: { state: "completed", turn_id: body.operation_id, revision: server.revision } });
    }
    if (sub === "/stop") {
      const body = r.request().postDataJSON() as Json;
      server.stops.push(body);
      const next = server.stopAnswers[Math.min(server.stops.length - 1, server.stopAnswers.length - 1)];
      await server.beforeStopAnswer();
      const answer = next ? next(body) : { status: 200, json: { containment: "gone" } };
      if (answer.status === 200) {
        server.native.worker = null;
        server.native.stop_inventory = null;
        server.native.rollover_held = false;
        server.revision += 1;
        server.afterStop();
      }
      return r.fulfill(answer);
    }
    if (sub === "/decisions") {
      const body = r.request().postDataJSON() as Json;
      server.decisions.push(body);
      server.pending = server.pending.filter((q) => q.request_id !== body.request_id);
      server.revision += 2;
      return r.fulfill({ status: 200, json: { handoff: "sent", ...body } });
    }
    if (sub === "/events") {
      const at = (server.frozen?.revision as number | undefined) ?? server.shown ?? server.revision;
      return r.fulfill({ json: { revision: at, next_cursor: at, events: [] } });
    }
    if (!sub) {
      if (server.failReads > 0 && server.stops.length > 0) {
        server.failReads -= 1;
        return r.fulfill({ status: 503, json: { detail: "unavailable" } });
      }
      if (server.frozen) return r.fulfill({ json: server.frozen });
      if (server.shown === null) server.currentReads += 1;
      return r.fulfill({ json: server.snapshot() });
    }
    return r.fulfill({ status: 404, json: { detail: "not mocked" } });
  });
  return server;
}

async function open(page: Page) {
  await page.goto(`/s/${ENGINE}/${ID}`);
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  await expect(box(page)).toBeEnabled();
}

const box = (page: Page) => page.getByRole("textbox", { name: /^Message / });

async function send(page: Page, text: string) {
  await box(page).fill(text);
  await page.getByRole("button", { name: "Send", exact: true }).click();
}

const turn = (page: Page, text: string) => page.getByTestId("structured-turn").filter({ hasText: text });
const pendingEntry = (page: Page) => page.getByTestId("structured-pending");

async function pressStop(page: Page) {
  const pane = page.getByTestId("structured-pane");
  if ((page.viewportSize()?.width ?? 1200) <= 800) {
    await pane.getByTestId("head-actions-menu").click();
    await page.getByRole("menuitem", { name: "Stop" }).click();
  } else {
    await pane.getByRole("button", { name: "Stop the worker" }).click();
  }
}

/** WCAG contrast of an element's text against the first opaque background behind it. */
async function contrast(locator: Locator): Promise<number> {
  return locator.evaluate((el) => {
    // A colour mixed in CSS computes to `color(srgb r g b)`, its channels in 0–1.
    const rgb = (c: string) => (c.match(/[\d.]+/g) ?? []).map(Number)
      .map((v, i) => (c.startsWith("color(") && i < 3 ? v * 255 : v));
    const lum = ([r, g, b]: number[]) => {
      const f = (v: number) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
      return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
    };
    const fg = rgb(getComputedStyle(el).color);
    let node: Element | null = el;
    let bg = [255, 255, 255];
    while (node) {
      const c = rgb(getComputedStyle(node).backgroundColor);
      if (c.length === 3 || (c.length === 4 && c[3] === 1)) { bg = c.slice(0, 3); break; }
      node = node.parentElement;
    }
    const [a, b] = [lum(fg), lum(bg)].sort((x, y) => y - x);
    return (a + 0.05) / (b + 0.05);
  });
}

test.describe("#1420 a send after a worker exits", () => {
  test("its repro: the pane read 12, the journal is at 14: the message goes once more and lands", async ({ page }) => {
    const server = await serve(page, { revision: 14, shown: 12 });
    await open(page);
    await send(page, "the next message");
    await expect(turn(page, "the next message")).toBeVisible();
    await expect(pendingEntry(page)).toHaveCount(0);
    // Refused once at what it read, then again at what it re-read: the SAME operation, delivered once.
    expect(server.submits.map((s) => s.expected_revision)).toEqual([12, 14]);
    expect(server.submits[1].operation_id).toBe(server.submits[0].operation_id);
    expect(server.turns.filter((t) => t.text === "the next message")).toHaveLength(1);
  });

  test("stop: the worker's last records land after Stop's own read", async ({ page }) => {
    const server = await serve(page);
    await open(page);
    server.afterStop = () => { server.shown = server.revision; server.revision += 2; };
    await pressStop(page);
    await expect(page.getByTestId("structured-pane")).toBeVisible();
    await expect.poll(() => server.stops.length).toBe(1);
    await send(page, "after stop");
    await expect(turn(page, "after stop")).toBeVisible();
    await expect(pendingEntry(page)).toHaveCount(0);
    expect(server.submits.length).toBe(2);
    expect(server.submits[1].operation_id).toBe(server.submits[0].operation_id);
  });

  test("idle exit: the worker went away while the pane sat idle", async ({ page }) => {
    const server = await serve(page, { revision: 15, shown: 13 });
    await open(page);
    server.native.worker = null;
    await send(page, "after idle");
    await expect(turn(page, "after idle")).toBeVisible();
    expect(server.submits.map((s) => s.expected_revision)).toEqual([13, 15]);
  });

  test("crash: the worker's disconnect moved the revision with no turn running", async ({ page }) => {
    const server = await serve(page, { revision: 22, shown: 20 });
    await open(page);
    server.native.worker = null;
    await send(page, "after a crash");
    await expect(turn(page, "after a crash")).toBeVisible();
    expect(server.submits).toHaveLength(2);
    expect(server.turns.filter((t) => t.text === "after a crash")).toHaveLength(1);
  });

  test("a genuine conflict is surfaced, holds the queue, and is recovered by Retry under the same id", async ({ page }) => {
    // The pane reads the conversation at 12. Then a turn the agent ran for itself finishes: the
    // conversation really changed, however idle the session is now, and the send doesn't know yet.
    const server = await serve(page, { revision: 12 });
    await open(page);
    const notified = { ...common, turn_id: "b0000000-0000-4000-8000-000000000000",
      operation_id: "b0000000-0000-4000-8000-000000000000", state: "completed", text: "", reply: "Notified." };
    server.beforeSubmit = () => {
      server.beforeSubmit = () => {};
      server.turns = [...server.turns, notified];
      server.revision = 18;
    };
    await send(page, "answering what I saw");
    await box(page).fill("and another");
    await page.getByRole("button", { name: "Send", exact: true }).click();
    await expect(pendingEntry(page).first()).toHaveAttribute("data-phase", "refused");
    await expect(page.getByTestId("structured-pending-state").first()).toContainText("revision changed");
    await expect(page.getByTestId("structured-reply").filter({ hasText: "Notified." })).toBeVisible();
    // Refused once, never re-sent by itself, and what's behind it did not jump the queue.
    await expect.poll(() => server.submits.length).toBe(1);
    await expect(pendingEntry(page).nth(1)).toHaveAttribute("data-phase", "waiting");
    const operation = server.submits[0].operation_id;
    // The operator read it and chose: the same operation id, now at the current revision.
    await page.getByTestId("structured-remedy-action").click();
    await expect(turn(page, "answering what I saw")).toBeVisible();
    expect(server.submits[1].operation_id).toBe(operation);
    expect(server.submits[1].expected_revision).toBe(18);
    await expect(turn(page, "and another")).toBeVisible();
    expect(server.turns.filter((t) => t.text === "answering what I saw")).toHaveLength(1);
  });

  test("a read older than a poll already shown never sends over what the operator hasn't read", async ({ page }) => {
    // The send is at 12. A turn the agent ran for itself finishes, and a poll SHOWS it at 18. Then
    // the refusal's own re-read comes back older, from before that turn, and the pane rejects it:
    // it says the conversation is unchanged, and it isn't what the operator is looking at.
    const server = await serve(page, { revision: 12 });
    await open(page);
    const old = JSON.parse(JSON.stringify(server.snapshot())) as Json;
    const notified = { ...common, turn_id: "b0000000-0000-4000-8000-000000000000",
      operation_id: "b0000000-0000-4000-8000-000000000000", state: "completed", text: "", reply: "Notified." };
    server.beforeSubmit = () => {
      server.beforeSubmit = () => {};
      server.turns = [...server.turns, notified];
      server.revision = 18;
    };
    server.beforeRefusal = async () => {
      server.beforeRefusal = async () => {};
      await expect.poll(() => server.currentReads, { timeout: 15_000 }).toBeGreaterThan(0);
      await expect(page.getByTestId("structured-reply").filter({ hasText: "Notified." })).toBeVisible();
      server.frozen = old;
      setTimeout(() => { server.frozen = null; }, 1_500);
    };
    await send(page, "answering what I saw");
    await expect(pendingEntry(page)).toHaveAttribute("data-phase", "refused");
    await page.waitForTimeout(2_000);
    expect(server.submits).toHaveLength(1); // nothing went out by itself
    await expect(page.getByTestId("structured-reply").filter({ hasText: "Notified." })).toBeVisible();
    // The operator has read it, and chooses: the same operation, at what is shown.
    const operation = server.submits[0].operation_id;
    await page.getByTestId("structured-remedy-action").click();
    await expect(turn(page, "answering what I saw")).toBeVisible();
    expect(server.submits[1].operation_id).toBe(operation);
    expect(server.submits[1].expected_revision).toBe(18);
  });

  test("it goes again only once: a second move is the operator's to read", async ({ page }) => {
    const server = await serve(page, { revision: 14, shown: 12 });
    await open(page);
    server.refusals = [
      { status: 409, json: { detail: "native conversation revision changed", reason: "revision_changed", remedy: "retry" } },
      { status: 409, json: { detail: "native conversation revision changed", reason: "revision_changed", remedy: "retry" } },
    ];
    await send(page, "twice");
    await expect(pendingEntry(page)).toHaveAttribute("data-phase", "refused");
    expect(server.submits).toHaveLength(2);
    await page.waitForTimeout(500);
    expect(server.submits).toHaveLength(2);
    expect(server.submits[1].operation_id).toBe(server.submits[0].operation_id);
  });
});

test.describe("#1725 a refusal offers the action it names", () => {
  test.use({ actionTimeout: 20_000 });

  const unknownWorker = { status: 409, json: { detail: "a previous native worker's state is unknown; stop the session first", remedy: "stop_resume" } };

  test("Stop and resume with no background work stops and resends the same id, unasked", async ({ page }) => {
    const server = await serve(page);
    await open(page);
    server.refusals = [unknownWorker];
    await send(page, "go on");
    await expect(page.getByTestId("structured-remedy")).toHaveAttribute("data-remedy", "stop_resume");
    await expect(page.getByTestId("structured-remedy-warning")).toHaveCount(0);
    await page.getByTestId("structured-remedy-action").click();
    await expect(page.getByTestId("structured-stop-confirm")).toHaveCount(0);
    await expect(turn(page, "go on")).toBeVisible();
    expect(server.stops).toHaveLength(1);
    expect(server.submits).toHaveLength(2);
    expect(server.submits[1].operation_id).toBe(server.submits[0].operation_id);
  });

  test("another tab's turn during the Stop: it stays on Retry and is not posted", async ({ page }) => {
    const server = await serve(page);
    await open(page);
    server.refusals = [unknownWorker];
    await send(page, "go on");
    await expect(page.getByTestId("structured-remedy")).toHaveAttribute("data-remedy", "stop_resume");
    const other = { ...common, turn_id: "d0000000-0000-4000-8000-000000000000",
      operation_id: "d0000000-0000-4000-8000-000000000000", state: "completed", text: "From the other tab.",
      reply: "Answered there." };
    server.afterStop = () => { server.turns = [...server.turns, other]; server.revision += 4; };
    await page.getByTestId("structured-remedy-action").click();
    await expect.poll(() => server.stops.length).toBe(1);
    await expect(page.getByTestId("structured-reply").filter({ hasText: "Answered there." })).toBeVisible();
    await page.waitForTimeout(1_500);
    expect(server.submits).toHaveLength(1); // not posted over a turn the operator hasn't read
    await expect(pendingEntry(page)).toHaveAttribute("data-phase", "refused");
    await expect(page.getByTestId("structured-remedy")).toHaveAttribute("data-remedy", "retry");
    // Read, and chosen: the same operation, at what is shown.
    await page.getByTestId("structured-remedy-action").click();
    await expect(turn(page, "go on")).toBeVisible();
    expect(server.submits[1].operation_id).toBe(server.submits[0].operation_id);
    expect(server.submits[1].expected_revision).toBe(server.revision - 2);
    expect(server.stops).toHaveLength(1); // Retry stops nothing
  });

  test("a failed read after the Stop: it stays on Retry, whatever a later read brings", async ({ page }) => {
    const server = await serve(page);
    await open(page);
    server.refusals = [unknownWorker];
    await send(page, "go on");
    await expect(page.getByTestId("structured-remedy")).toHaveAttribute("data-remedy", "stop_resume");
    server.failReads = 1;
    await page.getByTestId("structured-remedy-action").click();
    await expect.poll(() => server.stops.length).toBe(1);
    await expect(page.getByTestId("structured-remedy")).toHaveAttribute("data-remedy", "retry");
    await page.waitForTimeout(1_500); // the reads have recovered by now
    expect(server.submits).toHaveLength(1);
    await page.getByTestId("structured-remedy-action").click();
    await expect(turn(page, "go on")).toBeVisible();
    expect(server.submits[1].operation_id).toBe(server.submits[0].operation_id);
  });

  test("a read after the Stop older than a poll already shown: it stays on Retry", async ({ page }) => {
    const server = await serve(page, { native: { background_active: true, stop_inventory: inventory(),
      background_tasks: [{ ...TASK, source: "engine", state: "running", updated_at: NOW }] } });
    await open(page);
    server.refusals = [unknownWorker];
    await send(page, "go on");
    await expect(page.getByTestId("structured-remedy")).toHaveAttribute("data-remedy", "stop_resume");
    const old = JSON.parse(JSON.stringify(server.snapshot())) as Json;
    const other = { ...common, turn_id: "d0000000-0000-4000-8000-000000000000",
      operation_id: "d0000000-0000-4000-8000-000000000000", state: "completed", text: "From the other tab.",
      reply: "Answered there." };
    server.beforeStopAnswer = async () => {
      server.turns = [...server.turns, other];
      server.revision += 4;
      await expect(page.getByTestId("structured-reply").filter({ hasText: "Answered there." })).toBeVisible();
      server.frozen = old;
      setTimeout(() => { server.frozen = null; }, 1_500);
    };
    await page.getByTestId("structured-remedy-action").click();
    await page.getByTestId("structured-stop-confirm-go").click();
    await expect.poll(() => server.stops.length).toBe(1);
    await expect(page.getByTestId("structured-remedy")).toHaveAttribute("data-remedy", "retry");
    await page.waitForTimeout(2_000);
    expect(server.submits).toHaveLength(1);
  });

  const bgRequest = (id = "s:perm-bg") => ({ request_id: id, turn_id: "c0000000-0000-4000-8000-000000000000", kind: "Bash",
    item_id: "toolu_bg", summary: "{}", payload: { input: { command: "ls" } }, payload_digest: "d".repeat(64),
    choices: ["approve", "reject"], complete: true, always: [], risk: null, operator_only: true, context: {},
    background_task: { task_id: "agent-bg", description: "test-runner sub-agent" } });
  const bgNative = { background_active: true,
    stop_inventory: inventory({ tasks: [{ task_id: "agent-bg", description: "test-runner sub-agent" }] }),
    background_tasks: [{ task_id: "agent-bg", description: "test-runner sub-agent", source: "engine", state: "running", started_at: NOW, updated_at: NOW }] };
  const retired = (request: ReturnType<typeof bgRequest>) =>
    ({ worker: "worker", turn_id: request.turn_id, request_id: request.request_id });

  async function refusedOver(page: Page, pending: Json[]) {
    const server = await serve(page, { pending, native: bgNative });
    await open(page);
    server.refusals = [unknownWorker];
    await send(page, "go on");
    await expect(page.getByTestId("structured-remedy")).toHaveAttribute("data-remedy", "stop_resume");
    return server;
  }

  async function stopAndResume(page: Page, server: Awaited<ReturnType<typeof serve>>) {
    await page.getByTestId("structured-remedy-action").click();
    await page.getByTestId("structured-stop-confirm-go").click();
    await expect.poll(() => server.stops.length).toBe(1);
  }

  test("a request truly ended by the Stop: it resumes", async ({ page }) => {
    const request = bgRequest();
    const server = await refusedOver(page, [request]);
    server.afterStop = () => { server.pending = []; server.native.ended_requests = [retired(request)]; server.revision += 2; };
    await stopAndResume(page, server);
    await expect(turn(page, "go on")).toBeVisible();
    expect(server.submits[1].operation_id).toBe(server.submits[0].operation_id);
  });

  test("another tab decided a request before the Stop: it stays on Retry", async ({ page }) => {
    const server = await refusedOver(page, [bgRequest()]);
    // Gone from what is pending, exactly as one a Stop retired, and the journal knows better.
    server.afterStop = () => { server.pending = []; server.native.ended_requests = []; server.revision += 2; };
    await stopAndResume(page, server);
    await expect(page.getByTestId("structured-remedy")).toHaveAttribute("data-remedy", "retry");
    await page.waitForTimeout(1_000);
    expect(server.submits).toHaveLength(1);
  });

  test("a request arrived on its own and the Stop retired it: it stays on Retry", async ({ page }) => {
    const server = await refusedOver(page, []);
    server.afterStop = () => { server.native.ended_requests = [retired(bgRequest())]; server.revision += 2; };
    await stopAndResume(page, server);
    await expect(page.getByTestId("structured-remedy")).toHaveAttribute("data-remedy", "retry");
    await page.waitForTimeout(1_000);
    expect(server.submits).toHaveLength(1);
  });

  test("a request arrived on its own and is still open after the Stop: it stays on Retry", async ({ page }) => {
    const server = await refusedOver(page, []);
    server.afterStop = () => { server.pending = [bgRequest("s:new")]; server.revision += 2; };
    await stopAndResume(page, server);
    await expect(page.getByTestId("structured-remedy")).toHaveAttribute("data-remedy", "retry");
    await page.waitForTimeout(1_000);
    expect(server.submits).toHaveLength(1);
  });

  test("with background work it warns first, asks, and Keep running changes nothing", async ({ page }) => {
    const server = await serve(page, { native: { stop_inventory: inventory(), background_active: true,
      background_tasks: [{ ...TASK, source: "engine", state: "running", updated_at: NOW }] } });
    await open(page);
    server.refusals = [unknownWorker];
    await send(page, "go on");
    await expect(page.getByTestId("structured-remedy-warning")).toContainText("Ends 1 background job: npm run dev");
    await page.getByTestId("structured-remedy-action").click();
    const dialog = page.getByTestId("structured-stop-confirm");
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("heading", { name: "Stop and resume?" })).toBeVisible();
    await dialog.getByRole("button", { name: "Keep running" }).click();
    await expect(dialog).toHaveCount(0);
    expect(server.stops).toHaveLength(0);
    expect(server.submits).toHaveLength(1);
    await expect(pendingEntry(page)).toHaveAttribute("data-phase", "refused");
    // Pressed again and confirmed: the exact digest it was shown, then the same id.
    await page.getByTestId("structured-remedy-action").click();
    await dialog.getByTestId("structured-stop-confirm-go").click();
    await expect(turn(page, "go on")).toBeVisible();
    expect(server.stops).toEqual([{ worker: "worker", digest: D1 }]);
    expect(server.submits[1].operation_id).toBe(server.submits[0].operation_id);
  });

  test("Retry and Resume with attachments are the buttons their remedies name", async ({ page }) => {
    const server = await serve(page);
    await open(page);
    server.refusals = [{ status: 409, json: { detail: "the conversation kept changing; retry", remedy: "retry" } }];
    await send(page, "again");
    await expect(page.getByTestId("structured-remedy-action")).toHaveText("Retry");
    await page.getByTestId("structured-remedy-action").click();
    await expect(turn(page, "again")).toBeVisible();
    expect(server.stops).toHaveLength(0);
    expect(server.submits[1].operation_id).toBe(server.submits[0].operation_id);

    server.refusals = [{ status: 409, json: { detail: "to attach documents, stop and resume this session", remedy: "resume_with_attachments" } }];
    await send(page, "with a file");
    await expect(page.getByTestId("structured-remedy-action")).toHaveText("Resume with attachments");
    await page.getByTestId("structured-remedy-action").click();
    await expect(turn(page, "with a file")).toBeVisible();
    expect(server.stops).toHaveLength(1);
  });

  test("a refusal naming no remedy offers none", async ({ page }) => {
    const server = await serve(page);
    await open(page);
    server.refusals = [{ status: 409, json: { detail: "this turn was not sent" } }];
    await send(page, "no way on");
    await expect(pendingEntry(page)).toHaveAttribute("data-phase", "refused");
    await expect(page.getByTestId("structured-remedy")).toHaveCount(0);
  });

  test("an async question's refused answer offers its remedy too", async ({ page }) => {
    const asked = { ...common, turn_id: "90000000-0000-4000-8000-000000000000",
      operation_id: "90000000-0000-4000-8000-000000000000", state: "completed", text: "Which?",
      reply: "Which one?", question_id: "q-1", questions: [{ title: "Which one?", options: ["A", "B"] }] };
    const server = await serve(page, { turns: [asked] });
    await open(page);
    server.refusals = [unknownWorker];
    await page.getByTestId("structured-question-option").first().click();
    await page.getByTestId("structured-question-send").click();
    const remedy = page.getByTestId("structured-question-remedy");
    await expect(remedy).toHaveText("Stop and resume");
    await remedy.click();
    await expect.poll(() => server.stops.length).toBe(1);
    expect(server.submits).toHaveLength(1); // the question is the operator's to read again
  });

  for (const theme of ["dark", "light"]) {
    test(`the remedy is keyboard-focusable in order, 44 px, and readable (${theme})`, async ({ page }) => {
      const server = await serve(page, { theme, native: { stop_inventory: inventory(), background_active: true,
        background_tasks: [{ ...TASK, source: "engine", state: "running", updated_at: NOW }] } });
      await open(page);
      server.refusals = [unknownWorker];
      await send(page, "keyboard");
      const action = page.getByTestId("structured-remedy-action");
      await expect(action).toBeVisible();
      await action.focus();
      await expect(action).toBeFocused();
      await page.keyboard.press("Tab");
      await expect(pendingEntry(page).getByRole("button", { name: "Edit" })).toBeFocused();
      await page.keyboard.press("Tab");
      await expect(pendingEntry(page).getByRole("button", { name: "Discard" })).toBeFocused();
      const box = await action.boundingBox();
      expect(box!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
      if ((page.viewportSize()?.width ?? 1200) <= 480) {
        const parent = await pendingEntry(page).boundingBox();
        expect(box!.width).toBeGreaterThan(parent!.width * 0.9); // full width on a phone
      }
      expect(await contrast(action)).toBeGreaterThanOrEqual(4.5);
      expect(await contrast(page.getByTestId("structured-remedy-warning"))).toBeGreaterThanOrEqual(4.5);
      await expect(page.locator("body")).not.toHaveCSS("overflow-x", "scroll");
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow).toBeLessThanOrEqual(0);
    });
  }
});

test.describe("#1725 a full conversation held by what still runs", () => {
  const held = (over: Partial<Inventory>) => ({ rollover_held: true, background_active: true,
    stop_inventory: inventory(over),
    background_tasks: ((over.tasks ?? [TASK]) as Json[]).map((t) => ({ ...t, source: "engine", state: "running", updated_at: NOW })) });
  const full = { status: 409, json: { detail: "this conversation's connection is full and can't start another turn until what's running finishes; nothing was sent", reason: "conversation_full", remedy: "stop_resume" } };

  test("an exact count: Send is refused with its remedy, Stop and resume recovers", async ({ page }) => {
    const server = await serve(page, { native: held({ tasks: [TASK, PYTEST], processes: [CARGO] }) });
    await open(page);
    const notice = page.getByTestId("structured-rollover-held");
    await expect(notice).toContainText("continues once 3 background jobs finish");
    await expect(notice).toContainText("Held");
    await expect(notice).not.toContainText("tracked");
    server.refusals = [full];
    await send(page, "after the jobs");
    await expect(pendingEntry(page)).toHaveAttribute("data-phase", "refused");
    await expect(page.getByTestId("structured-remedy-warning")).toContainText("Ends 3 background jobs: npm run dev, pytest -x, cargo · pid 48211");
    await page.getByTestId("structured-remedy-action").click();
    const dialog = page.getByTestId("structured-stop-confirm");
    await expect(dialog).toHaveAttribute("data-kind", "exact");
    await expect(dialog.getByTestId("structured-stop-untracked")).toHaveCount(0);
    await dialog.getByTestId("structured-stop-confirm-go").click();
    await expect(turn(page, "after the jobs")).toBeVisible();
    expect(server.stops).toEqual([{ worker: "worker", digest: D1 }]);
    expect(server.submits[1].operation_id).toBe(server.submits[0].operation_id);
    await expect(notice).toHaveCount(0);
  });

  test("more than is tracked: an honest count, a warning, and recovery only on confirmation", async ({ page }) => {
    const server = await serve(page, { native: held({ tasks: [TASK, PYTEST], processes: [CARGO], overflow: true }) });
    await open(page);
    const notice = page.getByTestId("structured-rollover-held");
    await expect(notice).toContainText("3 tracked jobs, plus more work that isn’t tracked");
    await expect(notice.getByText(/continues once 3 background jobs/)).toHaveCount(0);
    server.refusals = [full];
    await send(page, "later");
    await expect(page.getByTestId("structured-remedy-warning")).toContainText("plus more work that isn’t tracked");
    await page.getByTestId("structured-remedy-action").click();
    const dialog = page.getByTestId("structured-stop-confirm");
    await expect(dialog.getByTestId("structured-stop-untracked")).toHaveText(/Untracked background work may also end\./);
    await expect(dialog).toHaveAttribute("data-kind", "everything");
    expect(server.stops).toHaveLength(0);
    await dialog.getByTestId("structured-stop-confirm-go").click();
    await expect(turn(page, "later")).toBeVisible();
    expect(server.stops).toEqual([{ worker: "worker", everything: true }]);
  });

  test("a stale digest: the dialog refreshes and asks again, through the pane", async ({ page }) => {
    const server = await serve(page, { native: held({}) });
    await open(page);
    const fresh = inventory({ digest: D2, tasks: [TASK, PYTEST] });
    server.stopAnswers = [
      () => ({ status: 409, json: { detail: "changed", reason: "inventory_changed", inventory: fresh } }),
      () => ({ status: 200, json: { containment: "gone" } }),
    ];
    server.refusals = [full];
    await send(page, "stale");
    await page.getByTestId("structured-remedy-action").click();
    const dialog = page.getByTestId("structured-stop-confirm");
    await dialog.getByTestId("structured-stop-confirm-go").click();
    await expect(page.getByTestId("structured-stop-changed")).toBeVisible();
    expect(server.submits).toHaveLength(1); // nothing resumed on a Stop that did not happen
    await dialog.getByTestId("structured-stop-confirm-go").click();
    await expect(turn(page, "stale")).toBeVisible();
    expect(server.stops).toEqual([{ worker: "worker", digest: D1 }, { worker: "worker", digest: D2 }]);
  });

  test("a persistently unreadable scan: Stop everything, disclosed and confirmed, never an empty list", async ({ page }) => {
    const server = await serve(page, { native: held({ scan: "unknown", tasks: [], processes: [], digest: DUNKNOWN }) });
    await open(page);
    const notice = page.getByTestId("structured-rollover-held");
    await expect(notice).toContainText("work that isn’t tracked");
    server.refusals = [full];
    await send(page, "unknown");
    await expect(page.getByTestId("structured-remedy-warning")).toContainText("can’t be listed");
    await page.getByTestId("structured-remedy-action").click();
    const dialog = page.getByTestId("structured-stop-confirm");
    await expect(dialog).toHaveAttribute("data-scan", "unknown");
    await expect(dialog).toHaveAttribute("data-kind", "everything");
    await expect(page.getByTestId("structured-stop-unknown")).toBeVisible();
    await expect(page.getByTestId("structured-stop-everything-disclosure")).toBeVisible();
    await dialog.getByTestId("structured-stop-confirm-go").click();
    await expect(turn(page, "unknown")).toBeVisible();
    expect(server.stops).toEqual([{ worker: "worker", everything: true }]);
  });

  test("the notice's own Stop and resume stops, with nothing to resend", async ({ page }) => {
    const server = await serve(page, { native: held({}) });
    await open(page);
    const notice = page.getByTestId("structured-rollover-held");
    await notice.getByTestId("structured-rollover-stop").click();
    await page.getByTestId("structured-stop-confirm-go").click();
    await expect(notice).toHaveCount(0);
    expect(server.stops).toHaveLength(1);
    expect(server.submits).toHaveLength(0);
    const height = await page.getByTestId("structured-reply").first().boundingBox();
    expect(height).not.toBeNull();
  });
});

test.describe("#1725 what the strip carries (#1726)", () => {
  const strip = (page: Page) => page.getByTestId("structured-background-work");
  const notInLog = async (page: Page, id: string) =>
    expect(page.getByTestId("api-log").getByTestId(id)).toHaveCount(0);

  for (const theme of ["dark", "light"]) {
    test(`a held conversation is in the strip even with no job, and its Stop asks first (${theme})`, async ({ page }) => {
      const server = await serve(page, { theme, native: { rollover_held: true,
        stop_inventory: inventory({ tasks: [], processes: [] }), background_tasks: [] } });
      await open(page);
      const held = strip(page).getByTestId("structured-rollover-held");
      await expect(held).toContainText("Conversation full");
      await notInLog(page, "structured-rollover-held");
      expect(await contrast(held.getByText("Conversation full", { exact: false }))).toBeGreaterThanOrEqual(4.5);
      const stop = held.getByTestId("structured-rollover-stop");
      expect((await stop.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
      // It never takes the conversation's place: the log keeps more room than the strip.
      const [log, bar] = [await page.getByTestId("api-log").boundingBox(), await strip(page).boundingBox()];
      expect(bar!.height).toBeLessThan(log!.height);
      await stop.click();
      await expect.poll(() => server.stops.length).toBe(1); // nothing listed: Stop goes as ever
      await expect(held).toHaveCount(0);
    });
  }

  test("a held conversation with jobs: its Stop opens the dialog, and the jobs stay listed beside it", async ({ page }) => {
    const server = await serve(page, { native: { rollover_held: true, background_active: true,
      stop_inventory: inventory({ tasks: [TASK, PYTEST], overflow: true }),
      background_tasks: [TASK, PYTEST].map((t) => ({ ...t, source: "engine", state: "running", updated_at: NOW })) } });
    await open(page);
    await expect(strip(page).getByTestId("structured-rollover-held")).toContainText("2 tracked jobs, plus more work");
    await expect(page.getByTestId("structured-background-strip")).toContainText("2 agent tasks running");
    await strip(page).getByTestId("structured-rollover-stop").click();
    const dialog = page.getByTestId("structured-stop-confirm");
    await expect(dialog.getByTestId("structured-stop-untracked")).toBeVisible();
    await dialog.getByRole("button", { name: "Keep running" }).click();
    expect(server.stops).toHaveLength(0);
  });

  test("skipped frames and refused requests are in the strip, with nothing else running", async ({ page }) => {
    await serve(page, { native: { frames_skipped: 2, requests_refused: 1,
      stop_inventory: inventory({ tasks: [] }), background_tasks: [] } });
    await open(page);
    await expect(strip(page).getByTestId("structured-frames-skipped")).toContainText("2 frames");
    await expect(strip(page).getByTestId("structured-requests-refused")).toContainText("1 request");
    await notInLog(page, "structured-frames-skipped");
    await notInLog(page, "structured-requests-refused");
    await expect(page.getByTestId("structured-background-strip")).toHaveCount(0);
  });

  test("a waiting background request is pointed at from the strip; its card stays in the conversation", async ({ page }) => {
    const request = { request_id: "s:perm-bg", turn_id: "c0000000-0000-4000-8000-000000000000", kind: "Bash",
      item_id: "toolu_bg", summary: "{}", payload: { input: { command: "ls" } }, payload_digest: "d".repeat(64),
      choices: ["approve", "reject"], complete: true, always: [], risk: null, operator_only: true, context: {},
      background_task: { task_id: "agent-bg", description: "test-runner sub-agent" } };
    await serve(page, { pending: [request], native: { background_active: true,
      stop_inventory: inventory({ tasks: [{ task_id: "agent-bg", description: "test-runner sub-agent" }] }),
      background_tasks: [{ task_id: "agent-bg", description: "test-runner sub-agent", source: "engine", state: "running", started_at: NOW, updated_at: NOW }] } });
    await open(page);
    await expect(strip(page).getByTestId("structured-background-waiting")).toContainText("1 background request is waiting");
    await expect(page.getByTestId("api-log").getByTestId("structured-background-request")).toBeVisible();
  });
});

test.describe("#1725 a background task's request", () => {
  const request = {
    request_id: "s:perm-bg", turn_id: "c0000000-0000-4000-8000-000000000000", kind: "Bash",
    item_id: "toolu_bg", summary: JSON.stringify({ tool_name: "Bash", input: { command: "pytest -x tests/" } }),
    payload: { tool_name: "Bash", input: { command: "pytest -x tests/" }, background_task: { task_id: "agent-bg", description: "test-runner sub-agent" } },
    payload_digest: "d".repeat(64), choices: ["approve", "reject"], complete: true, always: [], risk: null,
    operator_only: true, context: {},
    background_task: { task_id: "agent-bg", description: "test-runner sub-agent" },
  };
  const live = { background_active: true, stop_inventory: inventory({ tasks: [{ task_id: "agent-bg", description: "test-runner sub-agent" }] }),
    background_tasks: [{ task_id: "agent-bg", description: "test-runner sub-agent", source: "engine", state: "running", started_at: NOW - 180, updated_at: NOW }] };

  for (const theme of ["light", "dark"]) {
    test(`shown under its origin, one-shot, focusable in order, 44 px (${theme})`, async ({ page }) => {
      const server = await serve(page, { theme, pending: [request], native: live });
      await open(page);
      const card = page.getByTestId("structured-background-request");
      await expect(card.getByTestId("structured-request-origin")).toContainText("test-runner sub-agent");
      await expect(card.getByTestId("structured-request-origin")).toContainText(/background task/i);
      await expect(card).toContainText("Requested by a background task, not your current turn.");
      await expect(card).toContainText("pytest -x tests/");
      const approve = card.getByRole("button", { name: "Approve once" });
      const decline = card.getByRole("button", { name: "Decline" });
      await expect(approve).toBeVisible();
      await expect(decline).toBeVisible();
      // One-shot only: never Always, and no turn to stop.
      await expect(card.getByTestId("structured-always")).toHaveCount(0);
      await expect(card.getByRole("button", { name: /Always/i })).toHaveCount(0);
      await expect(card.getByRole("button", { name: /stop the turn/i })).toHaveCount(0);
      await approve.focus();
      await page.keyboard.press("Tab");
      await expect(decline).toBeFocused();
      for (const button of [approve, decline]) {
        const size = await button.boundingBox();
        expect(size!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
      }
      expect(await contrast(card.getByTestId("structured-request-origin"))).toBeGreaterThanOrEqual(4.5);
      expect(await contrast(decline)).toBeGreaterThanOrEqual(4.5);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow).toBeLessThanOrEqual(0);
      expect(server.decisions).toHaveLength(0);
    });
  }

  test("stays decidable after the operator's turn ends, under its own turn id", async ({ page }) => {
    const running = { ...common, turn_id: "e0000000-0000-4000-8000-000000000000",
      operation_id: "e0000000-0000-4000-8000-000000000000", state: "running", text: "Update the docs." };
    const server = await serve(page, { pending: [request], native: live, turns: [oldTurn, running] });
    await open(page);
    const card = page.getByTestId("structured-background-request");
    await expect(card).toBeVisible();
    // Not inside the operator's turn: the request is the task's.
    await expect(turn(page, "Update the docs.").getByTestId("structured-background-request")).toHaveCount(0);
    // The operator's turn ends.
    server.turns = [oldTurn, { ...running, state: "completed", reply: "Done." }];
    server.revision += 2;
    await expect(page.getByTestId("structured-reply").filter({ hasText: "Done." })).toBeVisible();
    await expect(card).toBeVisible();
    await card.getByRole("button", { name: "Approve once" }).click();
    await expect.poll(() => server.decisions.length).toBe(1);
    expect(server.decisions[0]).toMatchObject({ request_id: "s:perm-bg", decision: "approve", turn_id: request.turn_id });
    expect(server.decisions[0]).not.toHaveProperty("grant");
    await expect(card).toHaveCount(0);
  });

  test("it holds nothing back: a message still goes while it waits", async ({ page }) => {
    const server = await serve(page, { pending: [request], native: live });
    await open(page);
    await send(page, "meanwhile");
    await expect(turn(page, "meanwhile")).toBeVisible();
    await expect(page.getByTestId("structured-background-request")).toBeVisible();
    expect(server.submits).toHaveLength(1);
  });

  test("a question it asks reads as what can be done: skip it, no turn to stop", async ({ page }) => {
    const question = { ...request, kind: "AskUserQuestion", choices: ["reject"], payload: {
      tool_name: "AskUserQuestion", input: { questions: [{ question: "Which one?", options: [{ label: "A" }] }] } } };
    await serve(page, { pending: [question], native: live });
    await open(page);
    const card = page.getByTestId("structured-background-request");
    await expect(card).toContainText("Skip it");
    await expect(card).not.toContainText(/stop the turn/i);
    await expect(card.getByRole("button", { name: "Skip question" })).toBeVisible();
  });

  test("declining goes as a decline, and a card whose request is gone goes with it", async ({ page }) => {
    const server = await serve(page, { pending: [request], native: live });
    await open(page);
    const card = page.getByTestId("structured-background-request");
    await card.getByRole("button", { name: "Decline" }).click();
    await expect.poll(() => server.decisions.length).toBe(1);
    expect(server.decisions[0]).toMatchObject({ decision: "reject" });
    await expect(card).toHaveCount(0);
  });
});

test.describe("#1725 a frame too large to read", () => {
  test("a turn's row and the count outside any turn are visible, never silence", async ({ page }) => {
    const tools = [{ id: "battlelab-skipped-1", name: "battlelab", outcome: "skipped",
      summary: "A frame too large to read (over 1 MiB) was skipped" }];
    const uncertain = { ...common, turn_id: "f0000000-0000-4000-8000-000000000000",
      operation_id: "f0000000-0000-4000-8000-000000000000", state: "uncertain", text: "Run it.",
      reason: "BattleLab couldn't read a frame this turn may have depended on (over 1 MiB), so its outcome isn't known",
      tools };
    await serve(page, { turns: [uncertain], native: { frames_skipped: 2, requests_refused: 1 } });
    await open(page);
    const row = turn(page, "Run it.");
    await expect(row).toContainText("outcome isn't known");
    await expect(row.getByText("A frame too large to read (over 1 MiB) was skipped")).toBeAttached();
    await expect(row.getByText("skipped", { exact: true })).toBeAttached();
    await expect(page.getByTestId("structured-frames-skipped")).toContainText("2 frames too large to read");
    await expect(page.getByTestId("structured-requests-refused")).toContainText("1 request from a background task");
  });
});
