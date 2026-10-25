import { expect, type Page, test } from "@playwright/test";
import { mockRoster } from "./roster";

/** The map stays responsive with live windows (#1688) — two budgets, asserted as COUNTS.
 *
 *  Never wall-clock or fps: the shared runner has no GPU and a load that swings by 5×, so a
 *  timing budget would measure the host. What these count is the work itself:
 *
 *  1. Layout reads. Every output chunk ran the terminal's follow logic, which asked the DOM
 *     viewport `clientHeight` / `scrollHeight` / `scrollTop` a dozen times — and each ask after
 *     xterm dirtied the page is a full-document layout of the whole map. The follow logic now
 *     samples the viewport once per animation frame. xterm's own viewport never reads
 *     `clientHeight`, so that getter on `.xterm-viewport` is the follow logic's meter.
 *  2. Window renders on pan. The window layer re-renders every pan frame (its tethers
 *     re-project); a fresh rect object per render broke `SessionWindow`'s memo, so every frame
 *     re-rendered every window's terminal. A memo that bails out keeps its previous props
 *     object, so a NEW props object on a window's fiber is a render — read off the DOM node's
 *     React fiber, no test hook in the app. The positive case (a real bounds change) proves the
 *     meter sees renders at all, and that the memo does not swallow a resize. */

const WINDOWS = 4;
const now = Math.floor(Date.now() / 1000);
const sessions = Array.from({ length: 12 }, (_, i) => ({
  id: `claude:p${i}`,
  engine: "claude",
  uuid: `p${i}`,
  short_uuid: `p${i}`,
  cwd: "/home/u/proj",
  project: { kind: "project", id: "p1", name: "proj", color: "#ffb000" },
  last_mtime: now - i * 60,
  first_user_message: "",
  title: `Perf session ${i}`,
  sticky: false,
  archived: false,
  ai_summary: "",
}));

async function mockApp(page: Page, opts: { stream: boolean }) {
  // Playwright matches routes in REVERSE registration order — catch-all first.
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/pulse/notifications**", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0 } }),
  );
  await page.route("**/api/**/draft", (r) =>
    r.fulfill({ json: { id: "d", text: "", attachments: [], updated_at: null } }),
  );
  await page.route("**/api/projects**", (r) =>
    r.fulfill({
      json: { projects: [{ id: "p1", name: "proj", color: "#ffb000", archived: false }] },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/sessions?**", (r) =>
    r.fulfill({
      json: {
        sessions,
        next_offset: null,
        total: sessions.length,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: ["project:p1"],
        projects_hidden: [],
        project_names: {},
        compose_default: "collapsed",
      },
    }),
  );
  await page.routeWebSocket(/\/ws\/term\//, (ws) => {
    const key = decodeURIComponent(new URL(ws.url()).pathname.replace("/ws/term/", ""));
    ws.onMessage(() => {});
    ws.send(JSON.stringify({ t: "role", role: "owner" }));
    ws.send(JSON.stringify({ t: "seq", n: 0 }));
    ws.send(
      Buffer.from(
        `\x1b[2J\x1b[H${Array.from({ length: 120 }, (_, i) => `line ${i} for ${key}\r\n`).join("")}`,
      ),
    );
    if (!opts.stream) return;
    // A busy agent: a spinner rewrite every 10 ms and a new line every 40 ms. Faster than the
    // frame rate on ANY host, so per-chunk reads (the old shape: ~3–11 per chunk) exceed the
    // per-frame budget even at 60 fps, while per-frame sampling stays bounded by frames.
    let n = 0;
    const timer = setInterval(() => {
      n++;
      ws.send(
        Buffer.from(
          `\r\x1b[2K${"⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[n % 10]} Thinking… ${n}` +
            (n % 4 === 0 ? `\r\nstep ${n} done in ${key}\r\n` : ""),
        ),
      );
    }, 10);
    ws.onClose(() => clearInterval(timer));
  });
}

/** Restore WINDOWS windows from storage, side by side under the chips. */
async function seedWindows(page: Page) {
  await page.addInitScript((n) => {
    localStorage.setItem(
      "tr-overview-workspace",
      JSON.stringify(
        Array.from({ length: n }, (_, i) => ({
          key: `claude:p${i}`,
          engine: "claude",
          id: `p${i}`,
          title: `Perf session ${i}`,
          x: 20 + i * 400,
          y: 560,
          w: 580,
          h: 340,
          z: i + 1,
        })),
      ),
    );
  }, WINDOWS);
}

/** Count `clientHeight` reads on terminal viewports, and animation frames, while armed. */
async function meterViewportReads(page: Page) {
  await page.addInitScript(() => {
    const m = { on: false, reads: 0, frames: 0 };
    (window as unknown as { __vpMeter: typeof m }).__vpMeter = m;
    const d = Object.getOwnPropertyDescriptor(Element.prototype, "clientHeight");
    if (!d?.get) return;
    const get = d.get;
    Object.defineProperty(Element.prototype, "clientHeight", {
      ...d,
      get(this: Element) {
        if (m.on && this.classList?.contains("xterm-viewport")) m.reads++;
        return get.call(this);
      },
    });
    const tick = () => {
      if (m.on) m.frames++;
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
}

/** Every window's current props object, read off its root `<section>`'s React fiber. */
async function windowProps(page: Page): Promise<number> {
  return page.evaluate(() => {
    const w = window as unknown as { __seenProps?: WeakSet<object>; __propsNew?: number };
    w.__seenProps ??= new WeakSet();
    let fresh = 0;
    for (const el of document.querySelectorAll("[data-session-window]")) {
      const k = Object.keys(el).find((x) => x.startsWith("__reactFiber$"));
      if (!k) throw new Error("no React fiber on a window");
      type Fiber = { tag: number; return: Fiber | null; memoizedProps: object; alternate: Fiber | null };
      // The section is SessionWindow's root element: its nearest function-component owner
      // (SimpleMemo 15 / Memo 14 / Function 0) is the window.
      let f = (el as unknown as Record<string, Fiber>)[k].return;
      while (f && f.tag !== 15 && f.tag !== 14 && f.tag !== 0) f = f.return;
      if (!f) throw new Error("no window component above the section");
      for (const p of [f.memoizedProps, f.alternate?.memoizedProps]) {
        if (!p || w.__seenProps.has(p)) continue;
        w.__seenProps.add(p);
        fresh++;
      }
    }
    return fresh;
  });
}

async function openMap(page: Page) {
  await page.setViewportSize({ width: 1920, height: 1200 });
  await mockRoster(page);
  await page.goto("/overview");
  await expect(page.locator("[data-session-window]")).toHaveCount(WINDOWS);
  for (let i = 0; i < WINDOWS; i++)
    await expect(
      page.locator(`[data-session-window="claude:p${i}"] .xterm-rows`),
    ).toContainText(`claude:p${i}`); // in the replay AND in every streamed line
}

test.describe("map window performance budgets (#1688)", () => {
  // Four live xterms on a shared runner: the budget is the spec's, like overview-windows'.
  test.describe.configure({ timeout: 90_000 });

  // eslint-disable-next-line no-empty-pattern -- Playwright requires the destructuring form
  test.beforeEach(async ({}, testInfo) => {
    test.skip(testInfo.project.name === "mobile", "desktop-only workspace (#208)");
  });

  test("streaming windows sample their viewport about once a frame, not once per chunk", async ({
    page,
  }) => {
    await meterViewportReads(page);
    await mockApp(page, { stream: true });
    await seedWindows(page);
    await openMap(page);

    await page.evaluate(() => {
      const m = (window as unknown as { __vpMeter: { on: boolean; reads: number; frames: number } })
        .__vpMeter;
      m.reads = 0;
      m.frames = 0;
      m.on = true;
    });
    await page.waitForTimeout(2_000);
    const { reads, frames } = await page.evaluate(() => {
      const m = (window as unknown as { __vpMeter: { on: boolean; reads: number; frames: number } })
        .__vpMeter;
      m.on = false;
      return { reads: m.reads, frames: m.frames };
    });
    expect(frames).toBeGreaterThan(0);
    // The meter must have seen the follow logic run at all — a silent meter passes anything.
    expect(reads).toBeGreaterThan(0);
    // Per window per frame: one sample, plus a resample after the viewport's own scroll
    // event. Per chunk (the old shape) this was ~9 and grows as frames slow down.
    expect(reads / (frames * WINDOWS)).toBeLessThanOrEqual(3);
  });

  test("panning the map re-renders no window; a bounds change still does", async ({ page }) => {
    await mockApp(page, { stream: false });
    await seedWindows(page);
    await openMap(page);
    // Let the opening fit, the socket status and the first paint settle into their props.
    await expect.poll(async () => windowProps(page), { intervals: [500] }).toBe(0);

    const tether = () => page.locator("[data-tether]").first().getAttribute("data-tether-d");
    const before = await tether();
    const pane = await page.locator(".react-flow__pane").boundingBox();
    if (!pane) throw new Error("no pane");
    // Empty background above the windows, right of the chips.
    await page.mouse.move(pane.x + pane.width - 80, pane.y + 80);
    await page.mouse.down();
    await page.mouse.move(pane.x + pane.width - 380, pane.y + 140, { steps: 20 });
    await page.mouse.up();
    // The pan happened: the tethers re-projected...
    await expect.poll(tether).not.toBe(before);
    // ...and not one window rendered for it.
    expect(await windowProps(page)).toBe(0);

    // Positive case: a smaller map box is a real bounds change. `> 0` proves the meter sees a
    // render at all (the `bounds` prop alone changes); the geometry check below is what proves
    // the memo did not swallow the re-clamp of the window that no longer fits.
    await page.setViewportSize({ width: 1500, height: 1200 });
    await expect.poll(async () => windowProps(page)).toBeGreaterThan(0);
    const layer = await page.locator("[data-window-layer]").boundingBox();
    const last = await page.locator(`[data-session-window="claude:p${WINDOWS - 1}"]`).boundingBox();
    if (!layer || !last) throw new Error("no box");
    expect(last.x + last.width).toBeLessThanOrEqual(layer.x + layer.width + 1);
  });
});
