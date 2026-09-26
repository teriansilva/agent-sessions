import { expect, type Locator, type Page, test, type WebSocketRoute } from "@playwright/test";
import { throttleIfAsked } from "./browserTiming";
import { mockRoster } from "./roster";

/** The map's window workspace (#208) — the acceptance matrix, in a real browser.
 *
 *  jsdom cannot see any of this: React Flow's pointer handling, xterm's own document-level
 *  listeners, real layout for the clamping rules, and — the one that matters most — the SOCKET
 *  lifecycle. Every teardown assertion here is made on the sockets rather than on the DOM being
 *  empty, because an overlay that empties while its WebSockets stay open is exactly the bug a
 *  DOM assertion would pass through. */

const now = Math.floor(Date.now() / 1000);
const KEYS = Array.from({ length: 9 }, (_, i) => `s${i + 1}`);
const sessions = KEYS.map((k, i) => ({
  id: `claude:${k}`,
  engine: "claude",
  uuid: k,
  short_uuid: k,
  cwd: "/home/u/proj",
  project: { kind: "project", id: "p1", name: "proj", color: "#ffb000" },
  last_mtime: now - i * 60,
  first_user_message: "",
  title: `Window session ${i + 1}`,
  sticky: false,
  archived: false,
  ai_summary: "",
}));

/** One WebSocket connection. Tracked per INSTANCE, not per session key: "one socket is still
 *  live" is not the same claim as "the right socket is still live". A test that only counts
 *  would pass while an orphaned map socket survived and the route's own had closed — which is
 *  the exact leak the full-screen case has to rule out. */
interface Conn {
  /** Connect order, from 0. */
  seq: number;
  key: string;
  closed: boolean;
}

interface SockLog {
  conns: Conn[];
  /** Per session: the input payloads the pane sent ({"t":"i","d":…}). */
  input: Record<string, string[]>;
  /** Per session: how many resize frames ({"t":"r",cols,rows}) it sent. */
  resize: Record<string, number>;
  /** Per session: every resize frame's grid, and which connection (`Conn.seq`) carried it. */
  frames: Record<string, { cols: number; rows: number; conn: number }[]>;
  /** The routed sockets, in connect order — a test closes one to force a reconnect. */
  routes: WebSocketRoute[];
}

/** Projects and engines CROSSED, so the two layouts partition the same sessions differently
 *  while producing the SAME node count (2 clusters + 4 chips either way).
 *
 *  Aligning them one-to-one (project alpha = all claude) does not work: the Agents layout then
 *  reproduces the Projects layout exactly, nothing moves, and the test passes against the very
 *  bug it is meant to catch. The mtimes are chosen so `r1` moves on BOTH axes — its cluster
 *  leads in Projects and trails in Agents, and inside the Agents cluster `r3` sorts ahead of it. */
const RELAYOUT = (() => {
  const base = sessions[0];
  const mk = (
    n: number,
    engine: string,
    proj: string,
    age: number,
  ) => ({
    ...base,
    id: `${engine}:r${n}`,
    engine,
    uuid: `r${n}`,
    short_uuid: `r${n}`,
    title: `Relayout ${engine} ${n}`,
    last_mtime: now - age,
    project: {
      kind: "project",
      id: proj,
      name: proj,
      color: proj === "alpha" ? "#ffb000" : "#3b9eff",
    },
  });
  return [
    mk(1, "claude", "alpha", 100),
    mk(2, "opencode", "alpha", 10), // alpha's newest → alpha leads in Projects
    mk(3, "claude", "beta", 50), // claude's newest → sorts ahead of r1 in Agents
    mk(4, "opencode", "beta", 200),
  ];
})();

/** The session list served to the map. Mutable so a test can make a session leave the map the
 *  way a real one does — the next `/api/sessions` after a refetch simply doesn't carry it. */
function payload(list: typeof sessions) {
  return {
    sessions: list,
    next_offset: null,
    total: list.length,
    facets: { projects: [], engines: [] },
  };
}

async function mockApp(
  page: Page,
  opts: {
    visible?: () => typeof sessions;
    /** Cluster toggle keys to open. Defaults to the single-project fixture's. */
    expanded?: string[];
    /** Project entities. Empty for fixtures that need the cluster count to come from the
     *  sessions alone — an empty entity still renders as a drag-target cluster (#447). */
    projects?: { id: string; name: string; color: string; archived: boolean }[];
  } = {},
): Promise<SockLog> {
  const log: SockLog = { conns: [], input: {}, resize: {}, frames: {}, routes: [] };
  const visible = opts.visible ?? (() => sessions);
  const expanded = opts.expanded ?? ["project:p1"];
  const projects = opts.projects ?? [
    { id: "p1", name: "proj", color: "#ffb000", archived: false },
  ];

  // Playwright matches routes in REVERSE registration order — catch-all first.
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/pulse/notifications**", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0 } }),
  );
  await page.route("**/api/**/draft", (r) =>
    r.fulfill({
      json: { id: "d", text: "", attachments: [], updated_at: null },
    }),
  );
  await page.route("**/api/projects**", (r) => r.fulfill({ json: { projects } }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/sessions?**", (r) => r.fulfill({ json: payload(visible()) }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: expanded,
        projects_hidden: [],
        project_names: {},
        compose_default: "collapsed",
      },
    }),
  );

  await page.routeWebSocket(/\/ws\/term\//, (ws) => {
    const key = decodeURIComponent(
      new URL(ws.url()).pathname.replace("/ws/term/", ""),
    );
    const conn: Conn = { seq: log.conns.length, key, closed: false };
    log.conns.push(conn);
    log.routes.push(ws);
    log.input[key] ??= [];
    log.resize[key] ??= 0;
    log.frames[key] ??= [];
    ws.onMessage((raw) => {
      if (typeof raw !== "string") return;
      try {
        const msg = JSON.parse(raw) as { t?: string; d?: string; cols?: number; rows?: number };
        if (msg.t === "i" && typeof msg.d === "string") log.input[key].push(msg.d);
        if (msg.t === "r") {
          log.resize[key] += 1;
          log.frames[key].push({ cols: msg.cols ?? 0, rows: msg.rows ?? 0, conn: conn.seq });
        }
      } catch {
        /* not a control frame we care about */
      }
    });
    ws.onClose(() => {
      conn.closed = true;
    });
    ws.send(JSON.stringify({ t: "role", role: "owner" }));
    ws.send(JSON.stringify({ t: "seq", n: 0 }));
    // Enough lines that the viewport has real scrollback to move independently.
    ws.send(
      Buffer.from(
        `\x1b[2J\x1b[H${Array.from({ length: 80 }, (_, i) => `line ${i} for ${key}\r\n`).join("")}`,
      ),
    );
  });
  return log;
}

const opened = (log: SockLog) => log.conns.map((c) => c.key);
const liveConns = (log: SockLog) => log.conns.filter((c) => !c.closed);
const live = (log: SockLog) => liveConns(log).length;

const chip = (page: Page, n: number): Locator =>
  page.locator(".tr-overview .tr-ov-chip", { hasText: `Window session ${n}` });

const win = (page: Page, n: number): Locator =>
  page.locator(`[data-session-window="claude:s${n}"]`);

async function rectOf(loc: Locator) {
  const box = await loc.boundingBox();
  if (!box) throw new Error("no box");
  return box;
}

/** Drag a window by its header. Used both to assert dragging and to park a window out of the
 *  chip grid so the next chip stays clickable. */
async function dragWindow(page: Page, n: number, to: { x: number; y: number }) {
  const head = win(page, n).locator("[data-window-head]");
  const box = await rectOf(head);
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(to.x, to.y, { steps: 12 });
  await page.mouse.up();
}

/** Park a window so a later chip click (or a click into the other window) is not intercepted.
 *  Two default-size windows cannot both be reachable in a 1280px shell — one covering the other
 *  is correct windowing, so the multi-window tests widen the viewport and park deliberately. */
async function parkAt(page: Page, n: number, x: number, y: number) {
  await dragWindow(page, n, { x, y });
}

/** The multi-window tests need room for two 720px windows side by side. */
async function wideDesktop(page: Page) {
  await page.setViewportSize({ width: 1920, height: 1200 });
}

async function openMap(page: Page) {
  await mockRoster(page); // the manifest-generated roster (#853 P4)
  await page.goto("/overview");
  await expect(chip(page, 1)).toBeVisible();
}

/** Open window 1 and let its opening fit settle. */
async function openFirstWindow(page: Page, log: SockLog) {
  await openMap(page);
  await chip(page, 1).click();
  await expect.poll(() => log.conns.length).toBe(1);
  await expect(win(page, 1).locator(".xterm-screen")).toBeVisible();
  await page.waitForTimeout(500);
}

/** One-clock resize tracing (#995), on the PAGE's `performance.now()`: every ResizeObserver
 *  callback that observed a window's terminal host, and every resize frame the page sent.
 *  Installed after `mockApp`, so it wraps the WebSocket class `routeWebSocket` placed in the page. */
async function traceResizes(page: Page) {
  await page.addInitScript(() => {
    const trace = { ro: [] as number[], frames: [] as [number, number, number][] };
    (window as unknown as { __resizeTrace: typeof trace }).__resizeTrace = trace;
    const RO = window.ResizeObserver;
    window.ResizeObserver = class extends RO {
      constructor(cb: ResizeObserverCallback) {
        super((entries, obs) => {
          const onHost = entries.some((e) => {
            const el = e.target as HTMLElement;
            return !!el.closest?.("[data-session-window]") && !!el.querySelector?.(".xterm");
          });
          if (onHost) trace.ro.push(performance.now());
          cb(entries, obs);
        });
      }
    };
    const send = WebSocket.prototype.send;
    WebSocket.prototype.send = function (data) {
      if (typeof data === "string" && data.includes('"t":"r"')) {
        const m = JSON.parse(data) as { cols: number; rows: number };
        trace.frames.push([performance.now(), m.cols, m.rows]);
      }
      return send.call(this, data);
    };
  });
}

type ResizeTrace = { ro: number[]; frames: [number, number, number][] };

const readTrace = (page: Page): Promise<ResizeTrace> =>
  page.evaluate(() => (window as unknown as { __resizeTrace: ResizeTrace }).__resizeTrace);

const resetTrace = (page: Page) =>
  page.evaluate(() => {
    const t = (window as unknown as { __resizeTrace: ResizeTrace }).__resizeTrace;
    t.ro.length = 0;
    t.frames.length = 0;
  });

/** Shrink by real keyboard events while the browser clock is paused. Each DOM layout and
 * observer delivery must land before advancing 16ms: slow rendering cannot split the burst. */
async function keyboardResizeBurst(page: Page, steps: number) {
  const grip = win(page, 1).locator("[data-window-resize]");
  await grip.focus();
  const width = Math.round((await rectOf(win(page, 1))).width);
  for (let i = 0; i < steps; i++) {
    const observed = (await readTrace(page)).ro.length;
    await grip.evaluate((el) => {
      el.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }));
    });
    await expect
      .poll(async () => Math.round((await rectOf(win(page, 1))).width))
      .toBe(width - (i + 1) * 10);
    await expect.poll(async () => (await readTrace(page)).ro.length).toBeGreaterThan(observed);
    await page.clock.runFor(16);
  }
}

/** Wait until a session has sent no resize frame for a while: past the trailing debounce. */
async function framesQuiet(log: SockLog, key: string) {
  await expect
    .poll(
      async () => {
        const n = log.frames[key].length;
        await new Promise((r) => setTimeout(r, 400));
        return log.frames[key].length === n;
      },
      { timeout: 15_000 },
    )
    .toBe(true);
}

/** The grid a FRESH fit produces, measured independently of the frames under test: the server drops
 *  the socket, the pane reconnects, and `refit(true)` sends its real size past the grid dedupe. */
async function freshFitGrid(log: SockLog, key: string): Promise<[number, number]> {
  const seq = log.conns.length;
  await log.routes[log.routes.length - 1].close({ code: 1001, reason: "e2e: re-measure" });
  await expect.poll(() => log.conns.length, { timeout: 15_000 }).toBe(seq + 1);
  await expect
    .poll(() => log.frames[key].filter((f) => f.conn === seq).length, { timeout: 15_000 })
    .toBeGreaterThan(0);
  const first = log.frames[key].filter((f) => f.conn === seq)[0];
  return [first.cols, first.rows];
}

test.describe("desktop workspace", () => {
  // Every test here opens real map windows with live terminal sockets — xterm mounts, repaint
  // waits, pointer and keyboard drives per window. On the shared runner under load they failed
  // as 30 s TIMEOUTS, never on an assertion (31 shard runs since Sep 23 — #1158 has the
  // inventory); the budget is the spec's, like the eight-xterm cap test's own 180 s below.
  test.describe.configure({ timeout: 90_000 });

  // eslint-disable-next-line no-empty-pattern -- Playwright requires the destructuring form
  test.beforeEach(async ({}, testInfo) => {
    test.skip(testInfo.project.name === "mobile", "desktop-only workspace (#208)");
  });

  test("a chip opens one window with one socket; the same chip focuses it without a second", async ({
    page,
  }) => {
    const log = await mockApp(page);
    await openMap(page);

    await chip(page, 1).click();
    await expect(win(page, 1)).toBeVisible();
    // The pane inside it is the real one: xterm rendered, and exactly one socket for it.
    await expect(win(page, 1).locator(".xterm-screen")).toBeVisible();
    await expect.poll(() => log.conns.length).toBe(1);
    expect(opened(log)).toEqual(["claude:s1"]);

    // Decision 2: a second open of the same session focuses the existing window. Two windows
    // in one tab would both be told `owner` — (fp, tab_id) cannot tell them apart — and would
    // fight over the pty width.
    await chip(page, 1).click();
    await expect(page.locator("[data-session-window]")).toHaveCount(1);
    await page.waitForTimeout(300);
    expect(opened(log)).toEqual(["claude:s1"]);
    await expect(page.locator("[data-window-readout]")).toContainText("1");
  });

  test("close tears the socket down; the other window stays live; Close all closes every socket", async ({
    page,
  }) => {
    const log = await mockApp(page);
    await wideDesktop(page);
    await openMap(page);

    await chip(page, 1).click();
    await parkAt(page, 1, 400, 900); // park low-left, so chip 2 stays clickable
    await chip(page, 2).click();
    await parkAt(page, 2, 1500, 900);
    await expect.poll(() => log.conns.length).toBe(2);
    expect(live(log)).toBe(2);

    await win(page, 2).locator("[data-window-close]").click();
    await expect(win(page, 2)).toHaveCount(0);
    await expect
      .poll(() => log.conns.filter((c) => c.closed).map((c) => c.key))
      .toEqual(["claude:s2"]);
    // The other window is untouched — closing one must not disturb its neighbour's socket.
    expect(live(log)).toBe(1);
    await expect(win(page, 1).locator(".xterm-screen")).toBeVisible();

    await page.locator("[data-window-close-all]").click();
    await expect(page.locator("[data-session-window]")).toHaveCount(0);
    // Asserted on the SOCKETS, not on the overlay being empty.
    await expect.poll(() => live(log)).toBe(0);
    await expect(page.locator("[data-window-close-all]")).toHaveCount(0);
  });

  test("full screen navigates to the route and leaves exactly one socket — the route's own", async ({
    page,
  }) => {
    const log = await mockApp(page);
    await wideDesktop(page);
    await openMap(page);

    await chip(page, 1).click();
    await parkAt(page, 1, 400, 900);
    await chip(page, 2).click();
    await parkAt(page, 2, 1500, 900);
    await expect.poll(() => log.conns.length).toBe(2);

    await win(page, 2).locator("[data-window-fullscreen]").click();
    await expect(page).toHaveURL(/\/s\/claude\/s2$/);
    await expect(page.locator("[data-session-window]")).toHaveCount(0);
    // Leaving /overview unmounts the workspace, so both windows' sockets close and only the
    // route's own remains. Asserted on the surviving CONNECTION, not on a count: "one socket is
    // live" would also hold if the orphaned map socket for s1 had survived while both of s2's
    // closed — which is the leak this case exists to rule out.
    await expect.poll(() => live(log)).toBe(1);
    const survivor = liveConns(log)[0];
    expect(survivor.key).toBe("claude:s2");
    // ...and it is the LAST connection opened — the route's own, not the map window's.
    expect(survivor.seq).toBe(log.conns.length - 1);
    // Every earlier connection is gone: the map's s1, and the map's own s2.
    expect(log.conns.filter((c) => c.seq < survivor.seq).every((c) => c.closed)).toBe(true);
    expect(opened(log).filter((k) => k === "claude:s2")).toHaveLength(2);
  });

  test("the cap is exactly 8: the 9th open mounts no window and no socket", async ({
    page,
  }) => {
    test.setTimeout(180_000); // eight live xterms is the point of this test, and it is not cheap
    await page.setViewportSize({ width: 1920, height: 1200 });
    const log = await mockApp(page);
    await openMap(page);

    for (let n = 1; n <= 8; n++) {
      await chip(page, n).click();
      await expect(win(page, n)).toBeVisible();
      // Park each window low so the chip grid above stays clickable.
      await dragWindow(page, n, { x: 300 + n * 40, y: 1150 });
    }
    await expect(page.locator("[data-session-window]")).toHaveCount(8);
    await expect(page.locator("[data-window-readout]")).toContainText("8");
    await expect.poll(() => log.conns.length).toBe(8);

    await chip(page, 9).click();
    await expect(page.locator("[data-window-notice]")).toBeVisible();
    await expect(page.locator("[data-session-window]")).toHaveCount(8);
    await expect(page.locator("[data-window-readout]")).toContainText("8");
    // The refusal is real: no ninth socket was opened.
    await page.waitForTimeout(400);
    expect(log.conns.length).toBe(8);
  });

  test("input lands only in the focused window", async ({ page }) => {
    const log = await mockApp(page);
    await wideDesktop(page);
    await openMap(page);

    await chip(page, 1).click();
    await parkAt(page, 1, 400, 900);
    await chip(page, 2).click();
    await parkAt(page, 2, 1500, 900);
    await expect.poll(() => log.conns.length).toBe(2);

    // Focus window 1 by pressing inside its terminal, then type.
    await win(page, 1).locator(".xterm-screen").click();
    await expect(win(page, 1)).toHaveAttribute("data-focused", "true");
    await page.keyboard.type("abc");
    await expect.poll(() => log.input["claude:s1"].join("")).toContain("abc");
    expect(log.input["claude:s2"].join("")).not.toContain("abc");

    // Now the other one: the keystrokes follow the focus, and do not double up.
    await win(page, 2).locator(".xterm-screen").click();
    await expect(win(page, 2)).toHaveAttribute("data-focused", "true");
    await page.keyboard.type("xyz");
    await expect.poll(() => log.input["claude:s2"].join("")).toContain("xyz");
    expect(log.input["claude:s1"].join("")).not.toContain("xyz");
  });

  test("two terminals select and scroll independently", async ({ page }) => {
    await mockApp(page);
    await wideDesktop(page);
    await openMap(page);

    await chip(page, 1).click();
    await parkAt(page, 1, 400, 900);
    await chip(page, 2).click();
    await parkAt(page, 2, 1500, 900);

    // Selection: drag across window 1's screen. xterm paints its selection as real DOM, so a
    // second pane picking it up would be visible here — this is the document-level listener
    // case, where each Terminal instance attaches its own mouseup/copy handlers.
    const screen1 = await rectOf(win(page, 1).locator(".xterm-screen"));
    await page.mouse.move(screen1.x + 20, screen1.y + 20);
    await page.mouse.down();
    await page.mouse.move(screen1.x + 220, screen1.y + 60, { steps: 8 });
    await page.mouse.up();
    await expect
      .poll(async () => win(page, 1).locator(".xterm-selection div").count())
      .toBeGreaterThan(0);
    expect(await win(page, 2).locator(".xterm-selection div").count()).toBe(0);

    // Scroll: a wheel over window 2 must move window 2's viewport and leave window 1's alone.
    const top = (l: Locator) =>
      l.locator(".xterm-viewport").evaluate((e) => e.scrollTop);
    const before1 = await top(win(page, 1));
    const before2 = await top(win(page, 2));
    expect(before2).toBeGreaterThan(0); // it opened at the tail of real scrollback
    const view2 = await rectOf(win(page, 2).locator(".xterm-viewport"));
    await page.mouse.move(view2.x + view2.width / 2, view2.y + view2.height / 2);
    await page.mouse.wheel(0, -600);
    await expect.poll(() => top(win(page, 2))).toBeLessThan(before2);
    expect(await top(win(page, 1))).toBe(before1);
  });

  test("moving a window emits no resize frames (#227/#349)", async ({ page }) => {
    const log = await mockApp(page);
    await openFirstWindow(page, log);
    const afterOpen = log.resize["claude:s1"];

    // A MOVE changes no dimension, so the agent must never see a resize at all.
    await dragWindow(page, 1, { x: 700, y: 500 });
    await page.waitForTimeout(500);
    expect(log.resize["claude:s1"]).toBe(afterOpen);
  });

  /* THE DEBOUNCE CONTRACT, AND WHY IT IS NOT "AT MOST THREE FRAMES PER DRAG" (#995).
   *
   * The refit behind a window is a TRAILING 120ms debounce (`Terminal.tsx` `refitSoon`): size
   * changes less than 120ms apart collapse into one refit; changes further apart may each get
   * their own, and that is correct. The old test drove 20 AWAITED `page.mouse.move` calls and
   * capped the whole drag at three frames — but each awaited move is a round trip through the
   * automation channel, measured at 110–315ms apart on the shared CI host even unthrottled. Every
   * such gap legitimately ended a debounce window, so the cap failed without any regression: a
   * one-clock trace of 12 drags found every frame sent ≥122ms after the last size change before it,
   * and frame counts that tracked the ≥120ms gaps exactly.
   *
   * So the collapsing contract is pinned where the cadence is the PAGE's frame rate: a keyboard
   * resize burst dispatched inside the page, one step per animation frame, asserted against the
   * debounce windows it actually produced. With the debounce bypassed it fails (one frame per grid
   * change, sent at once). The real pointer drag stays as a smoke test with no timing ceiling. */
  test("a resize burst settles into one trailing refit per debounce window, on the real grid (#227/#349/#995)", async ({
    page,
  }) => {
    await page.clock.install();
    const log = await mockApp(page);
    await traceResizes(page);
    await openFirstWindow(page, log);
    await throttleIfAsked(page);
    await page.clock.pauseAt(await page.evaluate(() => Date.now() + 1000));
    await page.clock.runFor(250);
    const widthBefore = Math.round((await rectOf(win(page, 1))).width);
    await resetTrace(page);

    // 720px → the 560px minimum, 10px a step: every step is a real size change, none is clamped.
    const STEPS = 16;
    await keyboardResizeBurst(page, STEPS);
    await page.clock.runFor(120);
    await framesQuiet(log, "claude:s1");
    expect(Math.round((await rectOf(win(page, 1))).width)).toBe(widthBefore - STEPS * 10);

    const tr = await readTrace(page);
    const gaps = tr.ro.slice(1).map((t, i) => t - tr.ro[i]);
    const windows = 1 + gaps.filter((g) => g >= 120).length;
    const shown = `observer gaps ${JSON.stringify(gaps.map(Math.round))}, frames ${JSON.stringify(
      tr.frames.map(([t, c, r]) => [Math.round(t), c, r]),
    )}`;
    // The controlled burst really occupies ONE debounce window, even on a slow runner.
    expect(tr.ro.length, shown).toBeGreaterThanOrEqual(STEPS);
    expect(windows, shown).toBe(1);
    // It refitted, at most once per debounce window — never once per size change…
    expect(tr.frames.length, shown).toBeGreaterThan(0);
    expect(tr.frames.length, shown).toBeLessThanOrEqual(windows);
    // …and every refit TRAILED: sent a debounce interval after the last size change before it.
    for (const [t] of tr.frames) {
      const lastChange = tr.ro.filter((x) => x <= t);
      expect(t - lastChange[lastChange.length - 1], shown).toBeGreaterThanOrEqual(100);
    }
    // It converged: the last frame is the grid a FRESH fit produces, measured outside the burst.
    const last = tr.frames[tr.frames.length - 1];
    await page.clock.resume();
    expect(await freshFitGrid(log, "claude:s1")).toEqual([last[1], last[2]]);
  });

  test("a real pointer resize refits and converges on the real grid — no timing ceiling (#995)", async ({
    page,
  }) => {
    const log = await mockApp(page);
    await openFirstWindow(page, log);
    await throttleIfAsked(page);
    const before = log.resize["claude:s1"];

    const grip = await rectOf(win(page, 1).locator("[data-window-resize]"));
    await page.mouse.move(grip.x + grip.width / 2, grip.y + grip.height / 2);
    await page.mouse.down();
    for (let i = 1; i <= 20; i++) {
      await page.mouse.move(
        grip.x + grip.width / 2 - i * 8,
        grip.y + grip.height / 2 - i * 4,
      );
    }
    await page.mouse.up();
    await framesQuiet(log, "claude:s1");

    // It refitted. How MANY frames is deliberately not asserted: the awaited moves above can land a
    // debounce interval apart on a loaded host, and each such gap legitimately ends a window (see
    // the burst test, which owns the collapsing contract).
    expect(log.resize["claude:s1"] - before).toBeGreaterThan(0);
    const ours = log.frames["claude:s1"].filter((f) => f.conn === 0);
    const last = ours[ours.length - 1];
    expect(await freshFitGrid(log, "claude:s1")).toEqual([last.cols, last.rows]);
  });

  test("the tether tracks its chip through pan and zoom, at a non-zero shell offset", async ({
    page,
  }) => {
    await mockApp(page);
    await openMap(page);
    await chip(page, 1).click();

    const layer = page.locator("[data-window-layer]");
    const layerBox = await rectOf(layer);
    // The assertion below is only meaningful because the overlay does NOT start at the
    // viewport origin — the sidebar and header push it right and down. A tether computed in
    // viewport coordinates would pass at 0,0 and fail here.
    expect(layerBox.x).toBeGreaterThan(100);
    expect(layerBox.y).toBeGreaterThan(0);

    const tetherStart = async () => {
      const d = await page.locator("[data-tether]").first().getAttribute("data-tether-d");
      const m = /^M (-?\d+) (-?\d+)/.exec(d ?? "");
      if (!m) throw new Error(`unparsable tether: ${d}`);
      return { x: Number(m[1]), y: Number(m[2]) };
    };
    const chipRight = async () => {
      const b = await rectOf(chip(page, 1));
      const l = await rectOf(layer);
      return { x: b.x + b.width - l.x, y: b.y + b.height / 2 - l.y };
    };
    const attached = async () => {
      const [t, c] = await Promise.all([tetherStart(), chipRight()]);
      expect(Math.abs(t.x - c.x)).toBeLessThanOrEqual(4);
      expect(Math.abs(t.y - c.y)).toBeLessThanOrEqual(4);
    };
    await attached();

    // Pan the canvas by dragging the background.
    await page.mouse.move(layerBox.x + 60, layerBox.y + layerBox.height - 60);
    await page.mouse.down();
    await page.mouse.move(layerBox.x + 200, layerBox.y + layerBox.height - 140, {
      steps: 10,
    });
    await page.mouse.up();
    await attached();

    // Zoom in with React Flow's own control.
    await page.getByRole("button", { name: /zoom in/i }).click();
    await page.waitForTimeout(400);
    await attached();
  });

  test("a collapsed chip re-anchors its tether to the cluster; a session that leaves the map keeps its window", async ({
    page,
  }) => {
    let visible = sessions;
    await mockApp(page, { visible: () => visible });
    await openMap(page);
    await chip(page, 1).click();
    await expect(page.locator('[data-tether="claude:s1"]')).toHaveCount(1);

    // Collapse the cluster: the chip is gone from the map, but the window is not — the tether
    // re-anchors to the cluster the chip folded into.
    await page.getByRole("button", { name: /collapse all/i }).click();
    await expect(chip(page, 1)).toHaveCount(0);
    await expect(win(page, 1)).toBeVisible();
    await expect(page.locator('[data-tether="claude:s1"]')).toHaveCount(1);
    const group = page.locator(".tr-ov-group").first();
    const [t, g, l] = [
      await page
        .locator('[data-tether="claude:s1"]')
        .getAttribute("data-tether-d"),
      await rectOf(group),
      await rectOf(page.locator("[data-window-layer]")),
    ];
    const m = /^M (-?\d+) (-?\d+)/.exec(t ?? "");
    expect(Math.abs(Number(m![1]) - (g.x + g.width - l.x))).toBeLessThanOrEqual(4);

    // Now make the session leave the map entirely (the shape an archive-from-elsewhere takes:
    // the next session list simply doesn't carry it). The window stays open and usable; only
    // the tether goes.
    visible = sessions.filter((s) => s.id !== "claude:s1");
    await page.getByRole("button", { name: /new project/i }).click();
    await page.getByLabel("Project name").fill("refetch");
    await page.getByRole("button", { name: "Create", exact: true }).click();
    await expect(page.locator('[data-tether="claude:s1"]')).toHaveCount(0);
    await expect(win(page, 1)).toBeVisible();
    await expect(win(page, 1).locator(".xterm-screen")).toBeVisible();
  });

  test("a rename reaches an open window's chrome, and a session leaving the map keeps its name", async ({
    page,
  }) => {
    let visible = sessions;
    await mockApp(page, { visible: () => visible });
    await openMap(page);
    await chip(page, 1).click();
    const head = win(page, 1).locator("[data-window-head]");
    await expect(head).toContainText("Window session 1");

    // Rename it elsewhere (the sidebar's rename, an AI title landing) — the next session list
    // carries the new name, and the window is still open.
    visible = sessions.map((s) =>
      s.id === "claude:s1" ? { ...s, title: "Renamed while open" } : s,
    );
    await page.getByRole("button", { name: /new project/i }).click();
    await page.getByLabel("Project name").fill("refetch");
    await page.getByRole("button", { name: "Create", exact: true }).click();
    await expect(head).toContainText("Renamed while open");

    // And when the session leaves the map entirely, the chrome keeps the last name it knew
    // rather than blanking — a filter never closes a window, and must not un-name one either.
    visible = sessions.filter((s) => s.id !== "claude:s1");
    await page.getByRole("button", { name: /new project/i }).click();
    await page.getByLabel("Project name").fill("refetch2");
    await page.getByRole("button", { name: "Create", exact: true }).click();
    await expect(page.locator('[data-tether="claude:s1"]')).toHaveCount(0);
    await expect(head).toContainText("Renamed while open");
  });

  test("a window cannot be dragged out of reach, and a shrinking viewport brings it back", async ({
    page,
  }) => {
    await mockApp(page);
    await openMap(page);
    await chip(page, 1).click();

    // Drag far past the bottom-right corner.
    await dragWindow(page, 1, { x: 5000, y: 5000 });
    const layer1 = await rectOf(page.locator("[data-window-layer]"));
    const w1 = await rectOf(win(page, 1));
    expect(w1.x + w1.width).toBeLessThanOrEqual(layer1.x + layer1.width + 1);
    expect(w1.y + w1.height).toBeLessThanOrEqual(layer1.y + layer1.height + 1);
    expect(w1.x).toBeGreaterThanOrEqual(layer1.x - 1);
    expect(w1.y).toBeGreaterThanOrEqual(layer1.y - 1);

    // Shrink the viewport under it: the window moves, it is never left unreachable.
    await page.setViewportSize({ width: 1000, height: 620 });
    await page.waitForTimeout(400);
    const layer2 = await rectOf(page.locator("[data-window-layer]"));
    const w2 = await rectOf(win(page, 1));
    expect(w2.x + w2.width).toBeLessThanOrEqual(layer2.x + layer2.width + 1);
    expect(w2.y + w2.height).toBeLessThanOrEqual(layer2.y + layer2.height + 1);
    // Its chrome bar is on screen, which is what makes it draggable again.
    await expect(win(page, 1).locator("[data-window-head]")).toBeVisible();
  });

  test("the chrome controls are named, keyboard-reachable, and never start a drag", async ({
    page,
  }) => {
    await mockApp(page);
    await openMap(page);
    await chip(page, 1).click();

    const w = win(page, 1);
    await expect(w.getByRole("button", { name: "Open full screen" })).toBeVisible();
    await expect(w.getByRole("button", { name: "Close window" })).toBeVisible();
    await expect(w.getByRole("button", { name: "Resize window" })).toBeVisible();

    // A press that lands on a control must not move the window: press, move, release away.
    const before = await rectOf(w);
    const close = await rectOf(w.locator("[data-window-close]"));
    await page.mouse.move(close.x + close.width / 2, close.y + close.height / 2);
    await page.mouse.down();
    await page.mouse.move(close.x + 120, close.y + 120, { steps: 8 });
    await page.mouse.up();
    const after = await rectOf(w);
    expect(after.x).toBe(before.x);
    expect(after.y).toBe(before.y);

    // The grip resizes from the keyboard too, so it is not a pointer-only control.
    await w.locator("[data-window-resize]").focus();
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("ArrowRight");
    await page.waitForTimeout(200);
    expect((await rectOf(w)).width).toBeGreaterThan(before.width);
  });

  test("drag-to-reassign still works in Projects layout", async ({ page }) => {
    await mockApp(page);
    await openMap(page);

    // Press+move on a chip is the reassign gesture and must not have become a window open.
    const c = await rectOf(chip(page, 1));
    await page.mouse.move(c.x + c.width / 2, c.y + c.height / 2);
    await page.mouse.down();
    await page.mouse.move(c.x + c.width / 2 + 140, c.y + c.height / 2 + 90, {
      steps: 10,
    });
    await page.mouse.up();
    await expect(page.locator("[data-session-window]")).toHaveCount(0);
    await expect(page.locator(".tr-ov-hint")).toBeVisible();
  });
  test("filtering the LAST chip off the map keeps the window and its socket alive", async ({
    page,
  }) => {
    // The map emptying is a state, not a reason to tear the workspace down. This is the extreme
    // of "a session leaves the map": it was the only one, so the canvas has nothing left to
    // draw — and an early return there unmounted the window layer with it, closing a live
    // session's socket over a layout change.
    const only = [sessions[0]];
    let visible = only;
    // No project ENTITIES either: an empty project still renders as a drag-target cluster
    // (#447), so with one the map never actually empties and this test would pass against the
    // very bug it exists to catch — it has to reach zero nodes to exercise that path.
    const log = await mockApp(page, { visible: () => visible, projects: [] });
    await openMap(page);
    await chip(page, 1).click();
    await expect(win(page, 1)).toBeVisible();
    await expect.poll(() => log.conns.length).toBe(1);
    const conn = log.conns[0];

    visible = [];
    await page.getByRole("button", { name: /new project/i }).click();
    await page.getByLabel("Project name").fill("empties the map");
    await page.getByRole("button", { name: "Create", exact: true }).click();

    await expect(page.locator(".tr-ov-chip")).toHaveCount(0);
    // The precondition this regression depends on: the map is genuinely empty.
    await expect(page.locator(".react-flow__node")).toHaveCount(0);
    // The window is still there, still connected, and still the SAME socket — not a remount.
    await expect(win(page, 1)).toBeVisible();
    await expect(win(page, 1).locator(".xterm-screen")).toBeVisible();
    await page.waitForTimeout(500);
    expect(conn.closed).toBe(false);
    expect(log.conns).toHaveLength(1);
    // ...and the way out is still reachable.
    await expect(page.locator("[data-window-close-all]")).toBeVisible();
  });

  test("a narrow desktop navigates instead of opening a window below the floor", async ({
    page,
  }) => {
    // 801px is "desktop" by the ≤800px breakpoint, but the expanded sidebar leaves ~460px of
    // map — under MIN_SIZE. Opening there would hand the agent a column count the floor exists
    // to prevent, so the chip does what it always did instead.
    await mockApp(page);
    await page.setViewportSize({ width: 801, height: 620 });
    await mockRoster(page); // the manifest-generated roster (#853 P4)
    await page.goto("/overview");
    await expect(chip(page, 1)).toBeVisible();
    const layer = page.locator("[data-window-layer]");
    await expect(layer).toHaveCount(0);

    await chip(page, 1).click();
    await expect(page).toHaveURL(/\/s\/claude\/s1$/);
    await expect(page.locator("[data-session-window]")).toHaveCount(0);
  });

  test("an open window survives a too-narrow map AND the mobile breakpoint", async ({
    page,
  }) => {
    // The mount gate and the open gate are deliberately different questions: an open window
    // must not be torn down (socket and all) because the operator shrank the window.
    const log = await mockApp(page);
    await openMap(page);
    await chip(page, 1).click();
    await expect.poll(() => log.conns.length).toBe(1);
    const conn = log.conns[0];

    await page.setViewportSize({ width: 801, height: 620 });
    await page.waitForTimeout(400);
    await expect(win(page, 1)).toBeVisible();
    expect(conn.closed).toBe(false);
    // Measured HERE, not after crossing to mobile: at 800px the sidebar becomes a drawer and
    // hands the map the full width back, so the window is wider there than at 801px with the
    // sidebar expanded. The squeezed state is the one that proves the intent survived.
    const narrow = await rectOf(win(page, 1));
    expect(narrow.width).toBeLessThan(720);

    // ...and across the mobile breakpoint itself (801 → 800), which is the same question one
    // pixel further on. The breakpoint decides whether a window may be OPENED; it is not a
    // reason to close one that already is.
    await page.setViewportSize({ width: 800, height: 620 });
    await page.waitForTimeout(400);
    await expect(win(page, 1)).toBeVisible();
    expect(conn.closed).toBe(false);
    expect(log.conns).toHaveLength(1); // not a teardown-and-reconnect either

    // Growing the map back restores the layout: the record kept the operator's intent, so
    // nothing was destroyed by passing through a box too small to honour it.
    await page.setViewportSize({ width: 1680, height: 1000 });
    await page.waitForTimeout(400);
    const wide = await rectOf(win(page, 1));
    expect(wide.width).toBeGreaterThan(narrow.width);
    expect(Math.round(wide.width)).toBe(720);
    // The SAME socket throughout: 1680 → 801 → 800 → 1680 without a single reconnect.
    expect(conn.closed).toBe(false);
    expect(log.conns).toHaveLength(1);
  });

  test("a same-count relayout moves the tether with its chip, without a wake-up pan", async ({
    page,
  }) => {
    // The bug this pins: subscribing the tether to `s.nodes.length` looks like "the graph
    // changed" and is not. Switching Projects → Agents here rebuilds the graph with the same
    // number of nodes, so nothing the layer subscribed to changed, and every tether stayed at
    // its old coordinates until an unrelated pan or zoom happened to wake it up.
    await mockApp(page, {
      visible: () => RELAYOUT,
      // Both layouts' clusters, so switching mode never collapses the chips out of the map...
      expanded: ["project:alpha", "project:beta", "agent:claude", "agent:opencode"],
      // ...and no entity list, so the Projects layout's cluster count comes from the sessions
      // alone. An extra empty project would add a cluster to one layout and not the other,
      // which is exactly the same-count property this test depends on.
      projects: [],
    });
    await page.setViewportSize({ width: 1680, height: 1000 });
    await mockRoster(page); // the manifest-generated roster (#853 P4)
    await page.goto("/overview");
    const chipA = page.locator(".tr-overview .tr-ov-chip", {
      hasText: "Relayout claude 1",
    });
    await expect(chipA).toBeVisible();
    await chipA.click();
    await expect(page.locator('[data-tether="claude:r1"]')).toHaveCount(1);

    const layer = page.locator("[data-window-layer]");
    const nodeCount = () => page.locator(".react-flow__node").count();
    const attached = async () => {
      const d = await page
        .locator('[data-tether="claude:r1"]')
        .getAttribute("data-tether-d");
      const m = /^M (-?\d+) (-?\d+)/.exec(d ?? "");
      expect(m, `unparsable tether: ${d}`).not.toBeNull();
      const c = await rectOf(chipA);
      const l = await rectOf(layer);
      expect(Math.abs(Number(m![1]) - (c.x + c.width - l.x))).toBeLessThanOrEqual(4);
      expect(Math.abs(Number(m![2]) - (c.y + c.height / 2 - l.y))).toBeLessThanOrEqual(4);
    };
    await attached();

    const before = await nodeCount();
    const chipBefore = await rectOf(chipA);
    await page.getByRole("radio", { name: /group by agents/i }).click();
    await expect(page.getByRole("radio", { name: /group by agents/i })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    await expect(chipA).toBeVisible();
    // The fixture has to keep exercising the same-count path — if a layout change ever makes
    // these differ, this test would silently stop being the regression it claims to be.
    expect(await nodeCount()).toBe(before);
    // ...and the chip really did move, or there would be nothing for the tether to get wrong.
    await expect
      .poll(async () => {
        const now = await rectOf(chipA);
        return Math.abs(now.x - chipBefore.x) + Math.abs(now.y - chipBefore.y);
      })
      .toBeGreaterThan(4);
    // No pan, no zoom, no nudge: the tether must already be on the chip.
    await attached();
  });

});

test("mobile keeps the full-screen route and mounts no overlay", async ({
page,
}, testInfo) => {
test.skip(testInfo.project.name !== "mobile", "the mobile half of the split");
await mockApp(page);
await mockRoster(page); // the manifest-generated roster (#853 P4)
await page.goto("/overview");
await expect(chip(page, 1)).toBeVisible();
await expect(page.locator("[data-window-layer]")).toHaveCount(0);

await chip(page, 1).click();
await expect(page).toHaveURL(/\/s\/claude\/s1$/);
await expect(page.locator("[data-session-window]")).toHaveCount(0);
});
