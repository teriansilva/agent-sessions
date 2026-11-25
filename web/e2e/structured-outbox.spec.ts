import { expect, test, type Page, type Route } from "@playwright/test";
import { mockRoster } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";

// #1465: a sent API-session message leaves the composer at once and shows as pending in the
// conversation — the native structured clients (the HTTP chat client: `chat-outbox.spec.ts`).
// Real browser, desktop + mobile: the server is a small mocked state machine whose
// `POST …/turns` can be HELD (route not yet fulfilled), refused, lost after recording, or answered
// after the snapshot already shows the turn. Every case asserts what the operator sees AND what
// went over the wire (operation ids, order, revision), so nothing is duplicated, lost or reordered.

const A = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
const B = "6c1e3d2f-9a4b-4d8e-8f32-7b5c4d3e2f10";
const ENGINE = "codex-api";
const ROUTE = new RegExp(`/api/structured/sessions/${ENGINE}(?::|%3A)([0-9a-f-]{36})(/[^?]+)?(\\?.*)?$`);

type Json = Record<string, unknown>;
/** What the server does with one submit. `record`: records it and answers 202. `lost`: records it,
 *  then the response is lost. `abort`: lost before anything was recorded. `{status}`: refused. */
type Outcome = "record" | "lost" | "abort" | { status: number; detail: string };

const common = { reply: "", text_truncated: false, reply_truncated: false, tools_truncated: false,
  reason: null, tools: [] as Json[] };

function session(id: string, turns: Json[] = []) {
  const running = turns.find((t) => t.state === "running");
  return {
    session_key: `${ENGINE}:${id}`, revision: 4, get event_cursor(): number { return this.revision; },
    cwd: "/home/u/demo", state: running ? "running" : "idle", active_turn: (running?.turn_id as string) ?? null,
    model_requested: null, files: true, images: true, omitted_turns: 0, pending_requests: [] as Json[],
    read_only: null,
    native: { native_id: "native", worker: "worker", background_active: false },
    turns,
  };
}
type Session = ReturnType<typeof session>;

async function app(page: Page, opts: { theme?: string; a?: Json[]; b?: Json[] } = {}) {
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
  let uploads = 0;
  await page.route("**/api/upload", (r) => {
    uploads += 1;
    const stored = `20261020-0900${String(uploads).padStart(2, "0")}-screenshot.png`;
    return r.fulfill({ json: { path: `/h/.agent-sessions/uploads/${stored}`, name: "screenshot.png", stored } });
  });
  const sessions: Record<string, Session> = {
    [A]: session(A, opts.a),
    [B]: session(B, opts.b ?? [{ ...common, turn_id: "b0000000-0000-4000-8000-000000000000",
      operation_id: "b0000000-0000-4000-8000-000000000000", state: "completed", text: "Other session.",
      reply: "Hello from the other session." }]),
  };
  const fx = {
    sessions,
    /** Every submit that reached the server, in order: `{session, body}`. */
    submits: [] as { session: string; body: Json }[],
    /** Per-submit plan, consumed in order; `hold` waits for `release(i, outcome)`. */
    plan: [] as (Outcome | "hold")[],
    holds: new Map<number, (o: Outcome) => void>(),
    failReads: false,
    release(i: number, outcome: Outcome = "record") {
      const go = fx.holds.get(i);
      if (!go) throw new Error(`submit ${i} is not held`);
      fx.holds.delete(i);
      go(outcome);
    },
    /** The server records an operator turn (idempotent on its id). */
    record(id: string, body: Json) {
      const s = sessions[id];
      if (s.turns.some((t) => t.turn_id === body.operation_id)) return;
      const busy = s.active_turn !== null;
      s.turns.push({ ...common, turn_id: body.operation_id, operation_id: body.operation_id, text: body.text,
        attachments: ((body.attachments as string[] | undefined) ?? []).map((stored) => ({
          stored, name: "screenshot.png", mime: "image/png" })),
        state: busy ? "queued" : "running", ts: Date.now() / 1000 });
      if (!busy) {
        s.state = "running";
        s.active_turn = body.operation_id as string;
      }
      s.revision++;
    },
  };
  const answer = async (r: Route, id: string, body: Json, outcome: Outcome) => {
    if (outcome === "record" || outcome === "lost") fx.record(id, body);
    // As `submit_turn` answers: the recorded turn plus the journal revision read after recording.
    if (outcome === "record") return r.fulfill({ status: 202, json: { state: "queued", turn_id: body.operation_id,
      revision: sessions[id].revision } });
    if (outcome === "lost" || outcome === "abort") return r.abort("failed");
    return r.fulfill({ status: outcome.status, json: { detail: outcome.detail } });
  };
  await page.route(ROUTE, async (r) => {
    const [, id, sub = ""] = r.request().url().match(ROUTE) ?? [];
    const s = sessions[id];
    if (!s) return r.fulfill({ status: 404, json: { detail: "no such session" } });
    if (sub === "/turns" && r.request().method() === "POST") {
      const body = r.request().postDataJSON() as Json;
      const i = fx.submits.length;
      fx.submits.push({ session: id, body });
      const plan = fx.plan.shift() ?? "record";
      if (plan !== "hold") return answer(r, id, body, plan);
      const outcome = await new Promise<Outcome>((go) => fx.holds.set(i, go));
      return answer(r, id, body, outcome).catch(() => {}); // a page that left may have closed it
    }
    if (fx.failReads) return r.fulfill({ status: 503, json: { detail: "unavailable" } });
    if (sub === "/events") return r.fulfill({ json: { revision: s.revision, next_cursor: s.revision, events: [] } });
    if (!sub) return r.fulfill({ json: s });
    return r.fulfill({ status: 404, json: { detail: "not mocked" } });
  });
  return fx;
}

const box = (page: Page) => page.getByRole("textbox", { name: /^Message / });
const pending = (page: Page) => page.getByTestId("structured-pending");
const turnRow = (page: Page, text: string) => page.getByTestId("structured-turn").filter({ hasText: text });

async function open(page: Page, id = A) {
  await page.goto(`/s/${ENGINE}/${id}`);
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  await expect(box(page)).toBeEnabled();
}

async function send(page: Page, text: string) {
  await box(page).fill(text);
  await page.getByRole("button", { name: "Send", exact: true }).click();
}

/** In-app navigation (no reload): the router's own history, as a sidebar link does. */
async function goInApp(page: Page, path: string) {
  await page.evaluate((p) => {
    history.pushState(null, "", p);
    dispatchEvent(new PopStateEvent("popstate"));
  }, path);
}

async function beforeUnloadArmed(page: Page) {
  return page.evaluate(() => {
    const ev = new Event("beforeunload", { cancelable: true });
    dispatchEvent(ev);
    return ev.defaultPrevented;
  });
}

test("a held POST shows the message pending and the composer empty at once", async ({ page }, testInfo) => {
  const fx = await app(page);
  fx.plan.push("hold");
  await open(page);
  await send(page, "Now look at the idle timeout too.");
  await expect(box(page)).toHaveValue("");
  await expect(pending(page)).toHaveCount(1);
  await expect(pending(page)).toHaveAttribute("data-phase", "sending");
  await expect(pending(page)).toContainText("Sending to BattleLab…");
  await expect(page.getByTestId("structured-outbox-note")).toHaveText(
    "Leaving drops this list; a message already on its way may still arrive");
  await expect.poll(() => fx.submits.length).toBe(1);
  // The composer is free: the next message can be written right away.
  await box(page).fill("And write the findings to notes.md.");
  await expect(page.getByRole("button", { name: "Send", exact: true })).toBeEnabled();
  await page.screenshot({ path: testInfo.outputPath("sending.png") });
  fx.release(0);
  await expect(pending(page)).toHaveCount(0);
  await expect(turnRow(page, "Now look at the idle timeout too.")).toHaveCount(1);
});

test("a second send waits and goes out after the first; the recorded turns replace them, no duplicate", async ({ page }, testInfo) => {
  const fx = await app(page);
  fx.plan.push("hold");
  await open(page);
  await send(page, "Now look at the idle timeout too.");
  await send(page, "And write the findings to notes.md.");
  await expect(pending(page)).toHaveCount(2);
  await expect(pending(page).nth(1)).toHaveAttribute("data-phase", "waiting");
  await expect(pending(page).nth(1)).toContainText("Goes out after the message above.");
  await expect(pending(page).nth(1)).toContainText(/waiting to send · 2nd/i);
  await page.screenshot({ path: testInfo.outputPath("waiting.png") });
  await page.waitForTimeout(1_200); // a poll cycle: the second must not go while the first is out
  expect(fx.submits).toHaveLength(1);
  fx.release(0);
  await expect.poll(() => fx.submits.length).toBe(2);
  expect(fx.submits.map((s) => s.body.text)).toEqual(["Now look at the idle timeout too.", "And write the findings to notes.md."]);
  expect(fx.submits[1].body.operation_id).not.toBe(fx.submits[0].body.operation_id);
  await expect(pending(page)).toHaveCount(0);
  await expect(turnRow(page, "Now look at the idle timeout too.")).toHaveCount(1);
  await expect(turnRow(page, "And write the findings to notes.md.")).toHaveCount(1);
  await expect(page.getByTestId("structured-turn")).toHaveCount(2);
  await expect(turnRow(page, "And write the findings to notes.md.")).toContainText("1st in queue");
});

test("a refused queued picture message: Edit restores its text and picture, and releases the queue", async ({ page }, testInfo) => {
  const fx = await app(page);
  fx.plan.push("hold", { status: 409, detail: "the session is read only" });
  await open(page);
  await send(page, "Now look at the idle timeout too.");
  await page.getByTestId("structured-file-input").setInputFiles({
    name: "screenshot.png", mimeType: "image/png", buffer: Buffer.from([0x89, 0x50, 0x4e, 0x47]) });
  await expect(page.getByTestId("structured-attachment")).toHaveCount(1);
  await send(page, "Compare it with this screenshot.");
  await expect(page.getByTestId("structured-attachment")).toHaveCount(0); // left with the message
  await send(page, "Then write the findings to notes.md.");
  await expect(pending(page)).toHaveCount(3);
  await expect(pending(page).nth(1).getByTestId("structured-pending-attachment")).toContainText("screenshot.png");
  fx.release(0);
  const refused = pending(page).first();
  await expect(refused).toHaveAttribute("data-phase", "refused");
  await expect(refused).toContainText("Not sent: the session is read only");
  await expect(refused.getByRole("button", { name: "Retry" })).toHaveCount(0);
  // The order holds behind the failure: the third waits.
  await page.waitForTimeout(1_200);
  expect(fx.submits).toHaveLength(2);
  expect(fx.submits[1].body.attachments).toEqual(["20261020-090001-screenshot.png"]);
  await page.screenshot({ path: testInfo.outputPath("refused.png") });
  await refused.getByRole("button", { name: "Edit" }).click();
  await expect(box(page)).toHaveValue("Compare it with this screenshot.");
  await expect(box(page)).toBeFocused();
  await expect(page.getByTestId("structured-attachment")).toHaveCount(1);
  await expect(page.getByTestId("structured-attachment")).toContainText("screenshot.png");
  await page.screenshot({ path: testInfo.outputPath("after-edit.png") });
  // Edit released the queue: the waiting message goes next…
  await expect.poll(() => fx.submits.length).toBe(3);
  expect(fx.submits[2].body.text).toBe("Then write the findings to notes.md.");
  // …and the edited one goes out after it, as a NEW message.
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => fx.submits.length).toBe(4);
  expect(fx.submits[3].body).toMatchObject({ text: "Compare it with this screenshot.",
    attachments: ["20261020-090001-screenshot.png"] });
  expect(fx.submits[3].body.operation_id).not.toBe(fx.submits[1].body.operation_id);
  await expect(pending(page)).toHaveCount(0);
  await expect(page.getByTestId("structured-turn")).toHaveCount(3);
});

test("unknown → no Edit; Retry resends the same operation_id", async ({ page }, testInfo) => {
  const fx = await app(page);
  fx.plan.push("abort");
  await open(page);
  await send(page, "And write the findings to notes.md.");
  const entry = pending(page).first();
  await expect(entry).toHaveAttribute("data-phase", "unknown");
  await expect(entry).toContainText("May not have reached BattleLab. Retrying sends the same message; it won’t be duplicated.");
  await expect(entry.getByRole("button", { name: "Edit" })).toHaveCount(0);
  await expect(entry.getByRole("button", { name: "Discard — it may still arrive" })).toBeVisible();
  expect(await beforeUnloadArmed(page)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("unknown.png") });
  await entry.getByRole("button", { name: "Retry" }).click();
  await expect.poll(() => fx.submits.length).toBe(2);
  expect(fx.submits[1].body.operation_id).toBe(fx.submits[0].body.operation_id);
  expect(fx.submits[1].body.text).toBe(fx.submits[0].body.text);
  await expect(pending(page)).toHaveCount(0);
  await expect(page.getByTestId("structured-turn")).toHaveCount(1);
});

test("a lost response is reconciled by the late snapshot: no duplicate, nothing re-sent", async ({ page }) => {
  const fx = await app(page);
  fx.plan.push("lost");
  await open(page);
  fx.failReads = true; // the response AND the reads right after it are lost
  await send(page, "Now look at the idle timeout too.");
  await expect(pending(page).first()).toHaveAttribute("data-phase", "unknown");
  await expect(pending(page).first().getByRole("button", { name: "Edit" })).toHaveCount(0);
  fx.failReads = false; // the conversation is readable again, and shows the turn
  await expect(pending(page)).toHaveCount(0);
  await expect(turnRow(page, "Now look at the idle timeout too.")).toHaveCount(1);
  await expect(page.getByTestId("structured-turn")).toHaveCount(1);
  expect(fx.submits).toHaveLength(1);
});

test("snapshot before the POST answers: the entry is dropped once and the late answer is ignored", async ({ page }) => {
  const fx = await app(page);
  fx.plan.push("hold", "hold");
  await open(page);
  await send(page, "Now look at the idle timeout too.");
  await send(page, "And write the findings to notes.md.");
  await expect.poll(() => fx.submits.length).toBe(1);
  fx.record(A, fx.submits[0].body); // recorded; the response is still on its way
  await expect(turnRow(page, "Now look at the idle timeout too.")).toHaveCount(1);
  await expect(pending(page)).toHaveCount(1); // the head's turn replaced it; the next one goes
  await expect.poll(() => fx.submits.length).toBe(2);
  await expect(pending(page)).toHaveAttribute("data-phase", "sending");
  fx.release(0, { status: 409, detail: "turn id already used" }); // late, and refused
  await page.waitForTimeout(1_200);
  await expect(page.locator('[data-phase="refused"]')).toHaveCount(0);
  await expect(pending(page)).toHaveCount(1);
  await expect(pending(page)).toContainText("And write the findings to notes.md.");
  await expect(turnRow(page, "Now look at the idle timeout too.")).toHaveCount(1);
  fx.release(1);
  await expect(pending(page)).toHaveCount(0);
  await expect(page.getByTestId("structured-turn")).toHaveCount(2);
  expect(fx.submits).toHaveLength(2);
});

test("a failed snapshot read neither resurrects nor drops an entry", async ({ page }) => {
  const fx = await app(page);
  fx.plan.push("record", "abort");
  await open(page);
  await send(page, "Now look at the idle timeout too.");
  await expect(turnRow(page, "Now look at the idle timeout too.")).toHaveCount(1);
  await send(page, "And write the findings to notes.md.");
  await expect(pending(page)).toHaveAttribute("data-phase", "unknown");
  fx.failReads = true; // every read fails from here
  await page.waitForTimeout(3_000); // several failing poll cycles
  await expect(pending(page)).toHaveCount(1); // not dropped…
  await expect(pending(page)).toHaveAttribute("data-phase", "unknown");
  await expect(pending(page)).toContainText("And write the findings to notes.md.");
  // …and the settled one never comes back as pending.
  await expect(pending(page).filter({ hasText: "Now look at the idle timeout too." })).toHaveCount(0);
  await expect(turnRow(page, "Now look at the idle timeout too.")).toHaveCount(1);
  expect(fx.submits).toHaveLength(2);
});

test("native: two sends are both recorded, in order, queued server-side", async ({ page }) => {
  const busy = { ...common, turn_id: "d0000000-0000-4000-8000-000000000000",
    operation_id: "d0000000-0000-4000-8000-000000000000", state: "running", text: "Check startup." };
  const fx = await app(page, { a: [busy] });
  await open(page);
  await send(page, "Now look at the idle timeout too.");
  await send(page, "And write the findings to notes.md.");
  await expect.poll(() => fx.submits.length).toBe(2);
  expect(fx.submits.map((s) => s.body.text)).toEqual(["Now look at the idle timeout too.", "And write the findings to notes.md."]);
  expect(fx.submits.map((s) => s.body.expected_revision)).toEqual([undefined, undefined]);
  await expect(pending(page)).toHaveCount(0);
  await expect(turnRow(page, "Now look at the idle timeout too.")).toContainText("1st in queue");
  await expect(turnRow(page, "And write the findings to notes.md.")).toContainText("2nd in queue");
});

test("session switch with a waiting entry: it is discarded with the pane, nothing is posted for it", async ({ page }) => {
  const fx = await app(page);
  fx.plan.push({ status: 409, detail: "the session is read only" });
  await open(page);
  await send(page, "Now look at the idle timeout too.");
  await expect(pending(page).first()).toHaveAttribute("data-phase", "refused");
  await send(page, "And write the findings to notes.md.");
  await expect(pending(page).nth(1)).toHaveAttribute("data-phase", "waiting");
  await expect(page.getByTestId("structured-outbox-note")).toHaveText("Not sent yet — leaving this session discards it");
  await goInApp(page, `/s/${ENGINE}/${B}`);
  await expect(page.getByText("Hello from the other session.")).toBeVisible();
  await expect(pending(page)).toHaveCount(0);
  expect(await beforeUnloadArmed(page)).toBe(false);
  await page.waitForTimeout(1_500);
  expect(fx.submits).toHaveLength(1);
  await page.evaluate(() => history.back());
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  await expect(page.getByText("Hello from the other session.")).toHaveCount(0);
  await expect(page.getByTestId("structured-empty")).toBeVisible(); // only what the server recorded: nothing
  await expect(pending(page)).toHaveCount(0);
  await page.waitForTimeout(1_200);
  expect(fx.submits).toHaveLength(1);
});

test("session switch during a held POST: the recorded turn appears once, no stale row, the waiting entry is gone", async ({ page }) => {
  const fx = await app(page);
  fx.plan.push("hold");
  await open(page);
  await send(page, "Now look at the idle timeout too.");
  await send(page, "And write the findings to notes.md.");
  await expect.poll(() => fx.submits.length).toBe(1);
  await goInApp(page, `/s/${ENGINE}/${B}`);
  await expect(page.getByText("Hello from the other session.")).toBeVisible();
  fx.release(0); // the server records it after we left
  await page.waitForTimeout(1_200);
  // Nothing of A's leaked into B's composer or outbox.
  await expect(box(page)).toHaveValue("");
  await expect(pending(page)).toHaveCount(0);
  await page.evaluate(() => history.back());
  await expect(turnRow(page, "Now look at the idle timeout too.")).toHaveCount(1);
  await expect(pending(page)).toHaveCount(0);
  await expect(page.getByTestId("structured-turn")).toHaveCount(1);
  await page.waitForTimeout(1_200);
  expect(fx.submits).toHaveLength(1); // the waiting entry went with the pane
});

test("beforeunload is armed only while an unrecorded entry exists", async ({ page }) => {
  const fx = await app(page);
  fx.plan.push("hold");
  await open(page);
  expect(await beforeUnloadArmed(page)).toBe(false);
  await send(page, "Now look at the idle timeout too.");
  await expect(pending(page)).toHaveCount(1);
  expect(await beforeUnloadArmed(page)).toBe(true);
  fx.release(0);
  await expect(pending(page)).toHaveCount(0);
  expect(await beforeUnloadArmed(page)).toBe(false);
  // The real prompt: a held message asks before the tab closes.
  fx.plan.push("hold");
  await send(page, "And write the findings to notes.md.");
  await expect(pending(page)).toHaveCount(1);
  const dialog = page.waitForEvent("dialog");
  void page.close({ runBeforeUnload: true });
  const d = await dialog;
  expect(d.type()).toBe("beforeunload");
  await d.dismiss();
});

/** WCAG contrast of an element's text against the first opaque background behind it. */
async function contrast(page: Page, selector: string) {
  return page.locator(selector).first().evaluate((el) => {
    // Any CSS colour syntax (rgb, oklch, color(srgb …)) → sRGB bytes + alpha, through a canvas.
    const ctx = document.createElement("canvas").getContext("2d", { willReadFrequently: true })!;
    const rgb = (c: string) => {
      ctx.clearRect(0, 0, 1, 1);
      ctx.fillStyle = c;
      ctx.fillRect(0, 0, 1, 1);
      const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data;
      return [r, g, b, a / 255];
    };
    const lum = ([r, g, b]: number[]) => {
      const f = (v: number) => {
        const s = v / 255;
        return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
      };
      return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
    };
    let bg: number[] = [];
    for (let n: Element | null = el; n; n = n.parentElement) {
      const c = rgb(getComputedStyle(n).backgroundColor);
      if (c[3] === 1) {
        bg = c.slice(0, 3);
        break;
      }
    }
    if (!bg.length) bg = rgb(getComputedStyle(document.body).backgroundColor).slice(0, 3);
    const [a, b] = [lum(rgb(getComputedStyle(el).color).slice(0, 3)), lum(bg)];
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  });
}

for (const theme of ["dark", "light"]) {
  test(`pending actions: ≥44px hit areas, focus order, contrast, no overflow (${theme})`, async ({ page }, testInfo) => {
    const fx = await app(page, { theme });
    fx.plan.push({ status: 409, detail: "the session is read only" });
    await open(page);
    await send(page, "Now look at the idle timeout too.");
    const refused = pending(page).first();
    await expect(refused).toHaveAttribute("data-phase", "refused");
    const edit = refused.getByRole("button", { name: "Edit" });
    const discard = refused.getByRole("button", { name: "Discard" });
    const mobile = testInfo.project.name === "mobile";
    const pane = (await page.getByTestId("api-log").boundingBox())!;
    for (const b of [edit, discard]) {
      const r = (await b.boundingBox())!;
      expect(r.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
      expect(r.width).toBeGreaterThanOrEqual(TOUCH_FLOOR);
      if (mobile) expect(r.width).toBeGreaterThan(pane.width * 0.6); // full width on a phone
    }
    await edit.focus();
    await page.keyboard.press("Tab");
    await expect(discard).toBeFocused();
    expect(await contrast(page, '[data-phase="refused"] [data-testid="structured-pending-state"]')).toBeGreaterThanOrEqual(4.5);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
    await page.screenshot({ path: testInfo.outputPath(`refused-${theme}.png`) });
    // The unknown state: Retry first, then the honest Discard.
    await discard.click();
    await expect(box(page)).toBeFocused();
    fx.plan.push("abort");
    await send(page, "And write the findings to notes.md.");
    const unknown = pending(page).first();
    await expect(unknown).toHaveAttribute("data-phase", "unknown");
    const retry = unknown.getByRole("button", { name: "Retry" });
    const drop = unknown.getByRole("button", { name: "Discard — it may still arrive" });
    for (const b of [retry, drop]) expect((await b.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    await retry.focus();
    await page.keyboard.press("Tab");
    await expect(drop).toBeFocused();
    expect(await contrast(page, '[data-phase="unknown"] [data-testid="structured-pending-state"]')).toBeGreaterThanOrEqual(4.5);
    expect(await contrast(page, '[data-testid="structured-outbox-note"]')).toBeGreaterThanOrEqual(4.5);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
    await page.screenshot({ path: testInfo.outputPath(`unknown-${theme}.png`) });
  });
}

test("an accepted head whose turn aged out of the snapshot is settled by its 2xx receipt; the queue moves on", async ({ page }) => {
  const fx = await app(page);
  await open(page);
  fx.failReads = true; // every read fails right after the 2xx
  await send(page, "Now look at the idle timeout too.");
  await send(page, "And write the findings to notes.md.");
  await expect.poll(() => fx.submits.length).toBe(1);
  await expect(pending(page).first()).toContainText("Recorded by BattleLab");
  // Meanwhile the head completed and 60 more turns pushed it out of the bounded window.
  const s = fx.sessions[A];
  s.turns = [];
  s.state = "idle";
  s.active_turn = null;
  s.revision += 60;
  fx.failReads = false;
  await expect.poll(() => fx.submits.length).toBe(2);
  expect(fx.submits[1].body.text).toBe("And write the findings to notes.md.");
  await expect(pending(page).filter({ hasText: "Now look at the idle timeout too." })).toHaveCount(0);
  await expect(pending(page)).toHaveCount(0);
});
