import { expect, test, type Page, type Route } from "@playwright/test";
import { mockRoster } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";

// #1538: a queued message proven never sent says why, in the down status colour, and offers
// Send again — a NEW operation with the same words and attachments, pending at once through the
// composer's outbox (#1465). A missing picture is asked about, never dropped. Send now controls,
// uncertain handoffs and delivered turns never offer it. Real browser, desktop + mobile; the server
// is a small mocked state machine and every case checks what went over the wire.

const A = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
const ENGINE = "claude-api";
const ROUTE = new RegExp(`/api/structured/sessions/${ENGINE}(?::|%3A)([0-9a-f-]{36})(/[^?]+)?(\\?.*)?$`);
const PIC = "20261020-114424-screenshot.png";
const NOT_SENT = "a0000000-0000-4000-8000-000000000001";

/** The id the server derives for this message's Send again (`native_journal.retry_id`). */
const RETRY = "9d4b6c2a-3f1e-5b7d-8a90-1c2d3e4f5a6b";

type Json = Record<string, unknown>;
/** What the server does with one submit: record it, refuse it, or hold it until released. */
type Outcome = "record" | "hold" | { status: number; detail: string }
  /** Claimed, then refused before stdin: recorded as a not-sent turn with its own derived retry. */
  | { refusedAfterClaim: string };

const common = { reply: "", text_truncated: false, reply_truncated: false, tools_truncated: false,
  reason: null, tools: [] as Json[], attachments: [] as Json[] };

const earlier = { ...common, turn_id: "a0000000-0000-4000-8000-000000000000",
  operation_id: "a0000000-0000-4000-8000-000000000000", state: "completed",
  text: "Is parallel startup stable?", reply: "Parallel startup looks fine; the slow part is the worker relaunch.",
  ts: 1792560684 };

function notSent(over: Json = {}): Json {
  return { ...common, turn_id: NOT_SENT, operation_id: NOT_SENT, state: "failed",
    text: "Will this also improve? What leads to this?", ts: 1792575864,
    reason: "not sent: the session ended while this message waited — nothing was delivered",
    not_sent: { code: "worker_ended", retry: RETRY }, ...over };
}

/** One server, shared by every page (tab) attached to it. Like the journal it records an operation
 *  id once: a repeat is a replay, never a second delivery. `retried` is derived from what it holds. */
function server(turns: Json[]) {
  const s = {
    session_key: `${ENGINE}:${A}`, revision: 9, get event_cursor(): number { return this.revision; },
    cwd: "/home/u/battlelab", state: "idle", active_turn: null as string | null,
    model_requested: null, files: true, images: true, omitted_turns: 0, pending_requests: [] as Json[],
    read_only: null, native: { native_id: "native", worker: "worker", background_active: false },
    turns,
  };
  return {
    s,
    submits: [] as Json[],
    plan: [] as Outcome[],
    holds: [] as (() => void)[],
    /** How many times the agent was handed each operation id. */
    deliveries: new Map<string, number>(),
    view() {
      const byId = new Map(s.turns.map((t) => [t.turn_id as string, t]));
      return { ...s, event_cursor: s.revision, turns: s.turns.map((t) => {
        const ns = t.not_sent as Json | undefined;
        if (!ns) return t;
        const attempt = byId.get(ns.retry as string);
        return { ...t, not_sent: { ...ns, retry_recorded: !!attempt, retried: !!attempt && !attempt.not_sent } };
      }) };
    },
    /** A claim the worker refused before stdin: in the conversation, never delivered. */
    refuse(body: Json, next: string) {
      const id = body.operation_id as string;
      if (s.turns.some((t) => t.turn_id === id)) return;
      s.turns.push({ ...common, turn_id: id, operation_id: id, text: body.text, state: "failed",
        reason: "not sent: BattleLab could not hand this message to the agent — nothing was delivered",
        not_sent: { code: "unexpected", retry: next }, ts: Date.now() / 1000 });
      s.revision++;
    },
    record(body: Json) {
      const id = body.operation_id as string;
      if (s.turns.some((t) => t.turn_id === id)) return; // a replay observes, never re-delivers
      this.deliveries.set(id, (this.deliveries.get(id) ?? 0) + 1);
      s.turns.push({ ...common, turn_id: id, operation_id: id, text: body.text,
        attachments: ((body.attachments as string[] | undefined) ?? []).map((stored) => ({
          stored, name: "screenshot.png", mime: "image/png" })),
        state: "running", ts: Date.now() / 1000 });
      s.state = "running";
      s.active_turn = id;
      s.revision++;
    },
  };
}
type Server = ReturnType<typeof server>;

async function attach(page: Page, srv: Server, opts: { theme?: string; pictures?: boolean } = {}) {
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
  // The picture read-back (the thumbnail, and Send again's check). Gone: a 404.
  const png = Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
    "base64",
  );
  await page.route("**/api/uploads/**", (r) => opts.pictures === false
    ? r.fulfill({ status: 404, json: { detail: "not found" } })
    : r.fulfill({ status: 200, contentType: "image/png", body: png }));
  await page.route(ROUTE, async (r: Route) => {
    const [, id, sub = ""] = r.request().url().match(ROUTE) ?? [];
    if (id !== A) return r.fulfill({ status: 404, json: { detail: "no such session" } });
    if (sub === "/turns" && r.request().method() === "POST") {
      const body = r.request().postDataJSON() as Json;
      srv.submits.push(body);
      const plan = srv.plan.shift() ?? "record";
      if (plan === "hold") await new Promise<void>((go) => srv.holds.push(go));
      else if (typeof plan === "object" && "refusedAfterClaim" in plan) {
        srv.refuse(body, plan.refusedAfterClaim);
        return r.fulfill({ status: 409, json: { detail: "this turn was not sent" } });
      } else if (plan !== "record") return r.fulfill({ status: plan.status, json: { detail: plan.detail } });
      srv.record(body);
      return r.fulfill({ status: 202, json: { state: "running", turn_id: body.operation_id, revision: srv.s.revision } })
        .catch(() => {}); // a page that reloaded closed it
    }
    if (sub === "/events") return r.fulfill({ json: { revision: srv.s.revision, next_cursor: srv.s.revision, events: [] } });
    if (!sub) return r.fulfill({ json: srv.view() });
    return r.fulfill({ status: 404, json: { detail: "not mocked" } });
  });
}

async function app(page: Page, turns: Json[], opts: { theme?: string; pictures?: boolean } = {}) {
  const srv = server(turns);
  await attach(page, srv, opts);
  return srv;
}

const row = (page: Page) => page.getByTestId("structured-not-sent");
const box = (page: Page) => page.getByRole("textbox", { name: /^Message / });
const again = (page: Page) => row(page).getByRole("button", { name: "Send again" });

async function open(page: Page) {
  await page.goto(`/s/${ENGINE}/${A}`);
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  await expect(box(page)).toBeEnabled();
}

for (const theme of ["dark", "light"]) {
  test(`the not-sent row says why and Send again posts its derived id, pending at once (${theme})`, async ({ page }, testInfo) => {
    const fx = await app(page, [earlier, notSent({ attachments: [{ stored: PIC, name: "screenshot.png", mime: "image/png" }] })], { theme });
    fx.plan.push("hold");
    await open(page);
    await expect(row(page)).toContainText("Not sent");
    await expect(row(page)).toContainText("the session ended while this message waited — nothing was delivered.");
    const discard = row(page).getByRole("button", { name: "Discard" });
    // Touch-sized actions; full width on a phone.
    for (const button of [again(page), discard]) {
      const b = await button.boundingBox();
      expect(b?.height ?? 0).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    }
    if (testInfo.project.name === "mobile") {
      const [a, d, r] = await Promise.all([again(page).boundingBox(), discard.boundingBox(), row(page).boundingBox()]);
      expect(a!.width).toBeGreaterThan(r!.width * 0.9);
      expect(d!.y).toBeGreaterThan(a!.y);
    } else {
      // Keyboard order: Send again, then Discard.
      await again(page).focus();
      await page.keyboard.press("Tab");
      await expect(discard).toBeFocused();
    }
    await page.screenshot({ path: testInfo.outputPath("not-sent.png") });
    await again(page).click();
    // Pending at once, before the server answered; the original says it is on its way.
    const pending = page.getByTestId("structured-pending");
    await expect(pending).toHaveCount(1);
    await expect(pending).toContainText("Will this also improve? What leads to this?");
    await expect(pending.getByTestId("structured-pending-attachment")).toContainText("screenshot.png");
    await expect(row(page)).toContainText("Sending again…");
    await expect(row(page).getByRole("button")).toHaveCount(0);
    await expect.poll(() => fx.submits.length).toBe(1);
    const body = fx.submits[0];
    expect(body.operation_id).toBe(RETRY);
    expect(body.text).toBe("Will this also improve? What leads to this?");
    expect(body.attachments).toEqual([PIC]);
    await page.screenshot({ path: testInfo.outputPath("sending-again.png") });
    fx.holds.splice(0).forEach((go) => go());
    await expect(page.getByTestId("structured-pending")).toHaveCount(0);
    // The server holds it: the original is history, "Sent again below", no actions.
    await expect(row(page)).toHaveAttribute("data-settled", "again");
    await expect(row(page)).toContainText("Sent again below.");
    await expect(row(page).getByRole("button")).toHaveCount(0);
    await expect(page.getByTestId("structured-turn").filter({ hasText: "Will this also improve?" })).toHaveCount(2);
    await page.screenshot({ path: testInfo.outputPath("sent-again.png") });
    await page.reload();
    await expect(row(page)).toHaveAttribute("data-settled", "again");
    await expect(page.getByRole("button", { name: "Send again" })).toHaveCount(0);
    expect(fx.submits).toHaveLength(1);
  });
}

test("a reload while the retry's POST is held leaves Send again; the same id is delivered once", async ({ page }) => {
  const fx = await app(page, [earlier, notSent()]);
  fx.plan.push("hold");
  await open(page);
  await again(page).click();
  await expect(row(page)).toContainText("Sending again…");
  await expect.poll(() => fx.submits.length).toBe(1);
  await page.reload(); // the held POST has recorded nothing yet
  await expect(again(page)).toBeEnabled();
  await again(page).click();
  await expect.poll(() => fx.submits.length).toBe(2);
  fx.holds.splice(0).forEach((go) => go()); // the first, held one lands late
  await expect(row(page)).toHaveAttribute("data-settled", "again");
  expect(fx.submits.map((b) => b.operation_id)).toEqual([RETRY, RETRY]);
  expect(fx.deliveries.get(RETRY)).toBe(1);
});

test("a reload while the retry waits behind an earlier message leaves Send again", async ({ page }) => {
  const fx = await app(page, [earlier, notSent()]);
  fx.plan.push("hold");
  await open(page);
  await box(page).fill("First, a different question.");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => fx.submits.length).toBe(1);
  await again(page).click();
  await expect(page.getByTestId("structured-pending").nth(1)).toHaveAttribute("data-phase", "waiting");
  await expect(row(page)).toContainText("Sending again…");
  await page.reload();
  await expect(again(page)).toBeEnabled();
  expect(fx.submits.map((b) => b.operation_id)).not.toContain(RETRY);
  fx.holds.splice(0).forEach((go) => go());
});

test("a refused retry leaves Send again, and it goes again under the same id", async ({ page }) => {
  const fx = await app(page, [earlier, notSent()]);
  fx.plan.push({ status: 409, detail: "the session is read only" });
  await open(page);
  await again(page).click();
  await expect(page.getByTestId("structured-pending")).toHaveAttribute("data-phase", "refused");
  await expect(again(page)).toBeEnabled();
  await page.reload();
  await expect(again(page)).toBeEnabled();
  await again(page).click();
  await expect(row(page)).toHaveAttribute("data-settled", "again");
  expect(fx.submits.map((b) => b.operation_id)).toEqual([RETRY, RETRY]);
  expect(fx.deliveries.get(RETRY)).toBe(1);
});

test("two tabs pressing Send again at once deliver it once", async ({ context }) => {
  const srv = server([earlier, notSent()]);
  const [one, two] = [await context.newPage(), await context.newPage()];
  for (const page of [one, two]) await attach(page, srv);
  srv.plan.push("hold", "hold");
  await Promise.all([open(one), open(two)]);
  await Promise.all([again(one).click(), again(two).click()]);
  await expect.poll(() => srv.submits.length).toBe(2);
  srv.holds.splice(0).forEach((go) => go());
  for (const page of [one, two]) await expect(row(page)).toHaveAttribute("data-settled", "again");
  expect(srv.submits.map((b) => b.operation_id)).toEqual([RETRY, RETRY]);
  expect(srv.deliveries.get(RETRY)).toBe(1);
  await expect(one.getByTestId("structured-turn").filter({ hasText: "Will this also improve?" })).toHaveCount(2);
});

test("a missing picture is asked about, never dropped quietly", async ({ page }, testInfo) => {
  const fx = await app(page, [earlier, notSent({ attachments: [{ stored: PIC, name: "screenshot.png", mime: "image/png" }] })],
    { pictures: false });
  await open(page);
  await row(page).getByRole("button", { name: "Send again" }).click();
  const prompt = page.getByTestId("structured-not-sent-missing");
  await expect(prompt).toContainText("A picture is no longer available — send without it?");
  await page.screenshot({ path: testInfo.outputPath("missing-picture.png") });
  await page.waitForTimeout(500);
  expect(fx.submits).toHaveLength(0);
  await expect(page.getByTestId("structured-pending")).toHaveCount(0);
  await prompt.getByRole("button", { name: "Send without it" }).click();
  await expect.poll(() => fx.submits.length).toBe(1);
  expect(fx.submits[0].attachments).toBeUndefined();
  expect(fx.submits[0].text).toBe("Will this also improve? What leads to this?");
  await expect(row(page).getByRole("button")).toHaveCount(0);
});

test("no Send again on a delivered, uncertain or refused-Send-now turn, or one the agent refused", async ({ page }) => {
  const id = (n: number) => `c0000000-0000-4000-8000-00000000000${n}`;
  const fx = await app(page, [
    { ...earlier },
    { ...common, turn_id: id(1), operation_id: id(1), state: "uncertain", text: "Uncertain one",
      reason: "the native worker ended before this turn settled" },
    // The agent refused it after the handoff: no `not_sent`, nothing proves it unsent.
    { ...common, turn_id: id(2), operation_id: id(2), state: "failed", text: "Refused by the agent",
      reason: "the agent refused this turn" },
    { ...common, turn_id: id(3), operation_id: id(3), state: "queued", text: "Still queued",
      delivery: "interrupt_failed", delivery_reason: "the agent refused interruption" },
  ]);
  await open(page);
  await expect(page.getByTestId("structured-turn")).toHaveCount(4);
  await expect(page.getByTestId("structured-not-sent")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Send again" })).toHaveCount(0);
  expect(fx.submits).toHaveLength(0);
});

test("a retry refused after its claim is recovered at its own row, under its own id, once", async ({ page }, testInfo) => {
  const SECOND = "4c7d2e1f-8a9b-5c0d-9e1f-2a3b4c5d6e7f";
  const fx = await app(page, [earlier, notSent()]);
  fx.plan.push({ refusedAfterClaim: SECOND });
  await open(page);
  await again(page).click();
  await expect(row(page)).toHaveCount(2);
  // The original is history (its retry exists); the failed retry is the one that offers more.
  await expect(row(page).first()).toHaveAttribute("data-settled", "again");
  await expect(row(page).first().getByRole("button")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Send again" })).toHaveCount(1);
  await expect(row(page).nth(1).getByRole("button", { name: "Send again" })).toBeEnabled();
  await expect(page.getByTestId("structured-pending")).toHaveCount(0); // the conversation has it
  await page.screenshot({ path: testInfo.outputPath("retry-refused.png") });
  await page.reload();
  await expect(page.getByRole("button", { name: "Send again" })).toHaveCount(1);
  await row(page).nth(1).getByRole("button", { name: "Send again" }).click();
  await expect(row(page).nth(1)).toHaveAttribute("data-settled", "again");
  await expect(page.getByRole("button", { name: "Send again" })).toHaveCount(0);
  expect(fx.submits.map((b) => b.operation_id)).toEqual([RETRY, SECOND]);
  expect(fx.deliveries.get(SECOND)).toBe(1);
  expect(fx.deliveries.has(RETRY)).toBe(false);
});
