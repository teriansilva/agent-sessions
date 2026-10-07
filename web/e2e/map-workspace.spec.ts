import { expect, type Locator, type Page, test } from "@playwright/test";
import { mockRoster } from "./roster";

/** The map as a workspace (#936) — the parts of it a DOM emulator structurally cannot see.
 *
 *  Four of the five cases here fail in jsdom for the same reason they exist:
 *  - the blank-map drag defect is React Flow's own `visibility: hidden` on an unmeasured node,
 *    which needs real layout and a real ResizeObserver;
 *  - "leave the map and come back" is a socket lifecycle assertion, not a DOM one;
 *  - the reload cases need a real `localStorage` surviving a real navigation;
 *  - the sidebar interception has to prove that a REAL click on a real `<a href>` did not
 *    navigate, which is exactly what a synthetic event cannot tell you.
 */

const now = Math.floor(Date.now() / 1000);
const KEYS = ["s1", "s2", "s3", "s4"];
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

/** The id the mock engine reconciles a placeholder launch to. */
const RECONCILED = "opencode:ses_9f2b";

/** Session keys the app asked `GET /api/sessions/{key}` for, in order. */
const looked: string[] = [];

interface Conn {
  seq: number;
  key: string;
  closed: boolean;
  /** The `new=1` launch flag, read off the URL: a RESTORE must never carry it. */
  launched: boolean;
}

async function mockApp(
  page: Page,
  opts: {
    engines?: string[];
    /** Fired when the mock sends the `{"t":"id"}` reconcile frame (#1037). */
    onReconcileFrame?: () => void;
    /** Also send a second `id` frame that still names the placeholder, 250 ms later — the
     *  negative control for the reconcile guard. */
    placeholderFrameToo?: boolean;
  } = {},
): Promise<Conn[]> {
  const conns: Conn[] = [];
  looked.length = 0;
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/pulse/notifications**", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0 } }),
  );
  await page.route("**/api/**/draft", (r) =>
    r.fulfill({ json: { id: "d", text: "", attachments: [], updated_at: null } }),
  );
  await page.route("**/api/projects**", (r) =>
    r.fulfill({
      json: {
        projects: [
          {
            id: "p1",
            name: "proj",
            color: "#ffb000",
            archived: false,
            default_folder: "/home/u/proj",
          },
        ],
      },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await mockRoster(page);
  // The single-session lookup a pane makes for its own row (#867). Which key it names is the
  // observable proof of finding 1: a converged window must ask for the RECONCILED id, not the
  // `new-<uuid>` it still transports on.
  await page.route(/\/api\/sessions\/[^?]+$/, (r) => {
    const m = /\/api\/sessions\/([^?]+)$/.exec(new URL(r.request().url()).pathname);
    if (m) looked.push(decodeURIComponent(m[1]));
    return r.fulfill({ status: 404, json: { detail: "not found" } });
  });
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
        new_session_engines: opts.engines ?? [],
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
    const u = new URL(ws.url());
    const key = decodeURIComponent(u.pathname.replace("/ws/term/", ""));
    const conn: Conn = {
      seq: conns.length,
      key,
      closed: false,
      launched: u.searchParams.get("new") === "1",
    };
    conns.push(conn);
    ws.onClose(() => {
      conn.closed = true;
    });
    ws.send(JSON.stringify({ t: "role", role: "owner" }));
    ws.send(JSON.stringify({ t: "seq", n: 0 }));
    // The reconcile frame (#127): an engine that mints its own id answers a `new-<uuid>` launch
    // with the real one. Four of six engines do this, so a window opened from the new-session
    // flow is the normal case, not an edge one.
    if (key.startsWith("opencode:new-")) {
      ws.send(JSON.stringify({ t: "id", sid: RECONCILED }));
      opts.onReconcileFrame?.();
      // #1037 negative control: a frame that still names a placeholder must not make the map
      // refetch (the guard is defensive — the real server never converges to one).
      if (opts.placeholderFrameToo)
        setTimeout(() => {
          ws.send(JSON.stringify({ t: "id", sid: `${key}` }));
        }, 250);
    }
    ws.send(Buffer.from(`\x1b[2J\x1b[Hready ${key}\r\n`));
  });
  return conns;
}

const chip = (page: Page, n: number): Locator =>
  page.locator(".tr-overview .tr-ov-chip", { hasText: `Window session ${n}` });
const win = (page: Page, n: number): Locator =>
  page.locator(`[data-session-window="claude:s${n}"]`);
const sidebarRow = (page: Page, n: number): Locator =>
  page.locator(`.sidebar a[href="/s/claude/s${n}"]`);

async function box(loc: Locator) {
  const b = await loc.boundingBox();
  if (!b) throw new Error("no box");
  return b;
}

/** Drag a window by its header, leaving the pointer DOWN when `hold` is set — the mid-gesture
 *  state is the whole point of the blank-map case. */
async function dragWindow(
  page: Page,
  n: number,
  to: { x: number; y: number },
  hold = false,
) {
  const b = await box(win(page, n).locator("[data-window-head]"));
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
  await page.mouse.down();
  await page.mouse.move(to.x, to.y, { steps: 14 });
  if (!hold) await page.mouse.up();
}

async function openMap(page: Page) {
  await page.goto("/overview");
  await expect(chip(page, 1)).toBeVisible();
}

test.describe("map workspace", () => {
  // Every test here drives real windows over a mounted React Flow map — dozens of awaited pointer
  // moves, clicks and re-measures. On the shared runner under load they failed as TIMEOUTS
  // (`mouse.move`, `locator.click`), never on an assertion; the budget is the spec's, not a test's.
  test.describe.configure({ timeout: 90_000 });

  // eslint-disable-next-line no-empty-pattern -- Playwright requires the destructuring form
  test.beforeEach(async ({}, testInfo) => {
    test.skip(
      testInfo.project.name !== "desktop",
      "the workspace is desktop-only by design",
    );
  });

  test("the map stays VISIBLE while a window is dragged", async ({ page }) => {
    // The #936 P0 defect, red before the fix: dragging a window dispatched `rect` on every
    // pointer move → `ws.windows` got a new identity → so did `openIds` → `buildOverview` rebuilt
    // the whole node array. React Flow reads `measured` off the USER node (`adoptUserNodes`), so a
    // brand-new object arrives unmeasured and `NodeWrapper` renders it `visibility: hidden` until
    // its ResizeObserver lands. One update flickers; a CONTINUOUS drag re-orphans the
    // measurements every frame, so the map was blank for the length of the gesture.
    await page.setViewportSize({ width: 1920, height: 1200 });
    await mockApp(page);
    await openMap(page);
    await chip(page, 1).click();
    await expect(win(page, 1)).toBeVisible();
    const node = page.locator('.react-flow__node[data-id="claude:s3"]');
    await expect(node).toHaveCSS("visibility", "visible");

    // Sample the node's COMPUTED visibility on every animation frame FOR THE DURATION of the
    // gesture. A check made after the pointer stops is worthless here: the moment the moves stop,
    // React Flow's ResizeObserver re-measures and the node comes back — which is exactly what the
    // operator sees, and exactly what makes a naive assertion pass against the bug.
    await page.evaluate(() => {
      const w = window as unknown as { __vis: string[]; __raf: number };
      w.__vis = [];
      const el = document.querySelector('.react-flow__node[data-id="claude:s3"]');
      const tick = () => {
        if (el) w.__vis.push(getComputedStyle(el).visibility);
        w.__raf = requestAnimationFrame(tick);
      };
      tick();
    });

    const b = await box(win(page, 1).locator("[data-window-head]"));
    await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
    await page.mouse.down();
    for (let i = 0; i < 6; i++) {
      await page.mouse.move(b.x + 40 + i * 30, b.y + 200 + i * 40, { steps: 10 });
    }
    await page.mouse.up();

    const samples = await page.evaluate(() => {
      const w = window as unknown as { __vis: string[]; __raf: number };
      cancelAnimationFrame(w.__raf);
      return w.__vis;
    });
    expect(samples.length).toBeGreaterThan(10);
    expect(samples.filter((v) => v !== "visible")).toEqual([]);
  });

  test("leaving the map and coming back finds the same windows, in the same place", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1920, height: 1200 });
    const conns = await mockApp(page);
    await openMap(page);

    await chip(page, 1).click();
    await dragWindow(page, 1, { x: 500, y: 950 });
    await chip(page, 2).click();
    await dragWindow(page, 2, { x: 1450, y: 950 });
    const before = [await box(win(page, 1)), await box(win(page, 2))];
    await expect.poll(() => conns.filter((c) => !c.closed).length).toBe(2);

    // Away — and the sockets MUST close. Records surviving a navigation is the feature; live
    // sockets surviving it behind Settings would be a leak.
    // An in-app navigation, not a reload: this case is about the RECORDS outliving the map's
    // unmount, which is a different mechanism from the reload case below (storage). The topbar
    // link, not a sidebar row — a row press now opens a window instead of navigating.
    await page.locator('header a[href="/templates"]').click();
    await expect(page.locator("[data-session-window]")).toHaveCount(0);
    await expect.poll(() => conns.filter((c) => !c.closed).length).toBe(0);

    // ...and back.
    await openMap(page);
    await expect(page.locator("[data-session-window]")).toHaveCount(2);
    const after = [await box(win(page, 1)), await box(win(page, 2))];
    for (const i of [0, 1]) {
      expect(Math.abs(after[i].x - before[i].x)).toBeLessThanOrEqual(2);
      expect(Math.abs(after[i].y - before[i].y)).toBeLessThanOrEqual(2);
      expect(Math.abs(after[i].width - before[i].width)).toBeLessThanOrEqual(2);
    }
    // Re-opened, never resumed from a stale socket — and never RELAUNCHED.
    expect(conns.filter((c) => c.launched)).toHaveLength(0);
  });

  test("a reload restores the layout, and never re-runs a launch", async ({ page }) => {
    await page.setViewportSize({ width: 1920, height: 1200 });
    const conns = await mockApp(page);
    await openMap(page);
    await chip(page, 1).click();
    await dragWindow(page, 1, { x: 600, y: 950 });
    const before = await box(win(page, 1));
    // The layout write is debounced; give the trailing timer room.
    await page.waitForTimeout(700);

    await page.reload();
    await expect(chip(page, 1)).toBeVisible();
    await expect(win(page, 1)).toBeVisible();
    const after = await box(win(page, 1));
    expect(Math.abs(after.x - before.x)).toBeLessThanOrEqual(2);
    expect(Math.abs(after.y - before.y)).toBeLessThanOrEqual(2);
    // The restored window ATTACHES. `new=1` on a restore would mean a page reload started a
    // session nobody asked for.
    expect(conns.filter((c) => c.launched)).toHaveLength(0);
  });

  test("with the map up, a sidebar row opens a WINDOW instead of navigating", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1920, height: 1200 });
    await mockApp(page);
    await openMap(page);

    await sidebarRow(page, 3).click();
    await expect(win(page, 3)).toBeVisible();
    // The map is still the route: the row press did not take the operator off it.
    await expect(page).toHaveURL(/\/overview$/);

    // The same row again focuses the window it already has rather than opening a second.
    await sidebarRow(page, 3).click();
    await expect(page.locator("[data-session-window]")).toHaveCount(1);

    // ...and off the map the row navigates exactly as it always did.
    //
    // NOT `/mission` — that route's sidebar lists MISSIONS, not sessions (#937), so there is no
    // session row on it to press and this asserted against an element that cannot exist. It was
    // red on `main` before #940 touched anything; fixed here because it gates this PR, and the
    // correction is to pick a route that still has a session list rather than to weaken the
    // assertion, which is the behaviour the test is actually about.
    await page.goto("/");
    await sidebarRow(page, 3).click();
    await expect(page).toHaveURL(/\/s\/claude\/s3$/);
  });

  test("the toolbar stepper moves the cap, and lowering it closes nothing", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1920, height: 1200 });
    await mockApp(page);
    await openMap(page);
    const readout = page.locator("[data-window-readout]");
    await expect(readout).toContainText("0/8");

    await page.locator("[data-window-cap-up]").click();
    await expect(readout).toContainText("0/9");
    // Device-local, so it survives a reload — the cap is a property of this browser, not of the
    // account (#936: no server pref, no `/api/prefs` key).
    await page.reload();
    await expect(page.locator("[data-window-readout]")).toContainText("0/9");

    await chip(page, 1).click();
    await dragWindow(page, 1, { x: 600, y: 950 });
    await chip(page, 2).click();
    await expect(page.locator("[data-session-window]")).toHaveCount(2);

    // Lower it BELOW what is open. Nothing closes: a settings change is not a destructive act.
    for (let i = 0; i < 8; i++) await page.locator("[data-window-cap-down]").click();
    await expect(page.locator("[data-window-readout]")).toContainText("2/1");
    await expect(page.locator("[data-session-window]")).toHaveCount(2);
    // It refuses the NEXT one, and says so.
    await chip(page, 3).click();
    await expect(page.locator("[data-window-notice]")).toBeVisible();
    await expect(page.locator("[data-session-window]")).toHaveCount(2);
    // The floor is a real clamp, not a countdown into negative numbers.
    await expect(page.locator("[data-window-cap-down]")).toBeDisabled();
  });

  test("a full-screen session can be handed back to the map as a window", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1920, height: 1200 });
    const conns = await mockApp(page);
    // The map has to have been up at least once for the workspace to be persisting; open the
    // session the ordinary way from there, then send it full screen and back.
    await openMap(page);
    await chip(page, 2).click();
    await expect(win(page, 2)).toBeVisible();
    await win(page, 2).locator("[data-window-fullscreen]").click();
    await expect(page).toHaveURL(/\/s\/claude\/s2$/);
    await expect(page.locator("[data-session-window]")).toHaveCount(0);
    // ⤢ is lossless now (#936): the record survived, so the round trip has something to return
    // to. The socket did not — the map is unmounted.
    await expect.poll(() => conns.filter((c) => !c.closed).length).toBe(1);

    await page.getByRole("button", { name: "Open this session as a window on the map" }).click();
    await expect(page).toHaveURL(/\/overview$/);
    await expect(win(page, 2)).toBeVisible();
    await expect(page.locator("[data-session-window]")).toHaveCount(1);
  });

  test("+ New session with the map up lands the new session IN a window", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1920, height: 1200 });
    const conns = await mockApp(page, { engines: ["claude"] });
    await openMap(page);

    await page.locator('.sidebar .sidebarBody a[href="/"]').first().click();
    await expect(page).toHaveURL("/");
    await page.getByRole("button", { name: /start session/i }).click();

    // Back on the map, as a window — not full screen.
    await expect(page).toHaveURL(/\/overview$/);
    const fresh = page.locator("[data-session-window]");
    await expect(fresh).toHaveCount(1);
    // ...and it really LAUNCHED: the window's socket carries `new=1`, which is what makes this a
    // new session rather than an attach to nothing.
    await expect.poll(() => conns.filter((c) => c.launched).length).toBe(1);
  });

  test("a launched window survives leaving the map, and never launches twice", async ({
    page,
  }) => {
    // Hermes on #936: freezing the transport identity is right for the LIFE OF A MOUNT and wrong
    // past it. A window opened from the new-session flow transports on `opencode:new-<uuid>` and
    // carries `fresh`; if the record kept both after the map unmounted, coming back would mount
    // the placeholder and re-send `new=1` — a second session started by a navigation.
    await page.setViewportSize({ width: 1920, height: 1200 });
    const conns = await mockApp(page, { engines: ["opencode"] });
    await openMap(page);

    await page.locator('.sidebar .sidebarBody a[href="/"]').first().click();
    await page.getByRole("button", { name: /start session/i }).click();
    await expect(page).toHaveURL(/\/overview$/);
    // It launched under the placeholder, then adopted the id the engine minted.
    await expect.poll(() => conns.filter((c) => c.launched).length).toBe(1);
    await expect(page.locator(`[data-session-window="${RECONCILED}"]`)).toHaveCount(0);
    await expect(page.locator("[data-session-window]")).toHaveCount(1);

    await page.locator('header a[href="/templates"]').click();
    await expect(page.locator("[data-session-window]")).toHaveCount(0);
    await page.goBack();
    await expect(chip(page, 1)).toBeVisible();

    // Back as the REAL session — and attached, not relaunched.
    await expect(page.locator(`[data-session-window="${RECONCILED}"]`)).toHaveCount(1);
    await expect.poll(() => conns.filter((c) => !c.closed).length).toBe(1);
    expect(conns.filter((c) => c.launched)).toHaveLength(1);
    expect(conns.filter((c) => !c.closed)[0].key).toBe(RECONCILED);

    // ...and a reload finds it under the real id too, still without a launch.
    await page.waitForTimeout(700);
    await page.reload();
    await expect(page.locator(`[data-session-window="${RECONCILED}"]`)).toHaveCount(1);
    expect(conns.filter((c) => c.launched)).toHaveLength(1);
  });

  test("a converged window ACTS on the reconciled id, not the placeholder it transports on", async ({
    page,
  }) => {
    // Hermes on #939, finding 1: the record's two identities were right, but the mounted pane
    // never received the action one — so inside a window a converged session's Hand off gate and
    // its Recap "Review now" kept naming a `new-<uuid>` the server rejects (#867's failure, one
    // surface along).
    //
    // Asserted on **Hand off's presence**, which is the cleanest observable of `actionKey`:
    // `canHandoff` is `!actionNative.startsWith("new-")`, so it is absent for exactly as
    // long as the pane is still acting on the placeholder. A request-URL assertion is not usable
    // here — other surfaces legitimately name the reconciled id, so the URL appears either way.
    await page.setViewportSize({ width: 1920, height: 1200 });
    await mockApp(page, { engines: ["opencode"] });
    await openMap(page);

    await page.locator('.sidebar .sidebarBody a[href="/"]').first().click();
    await page.getByRole("button", { name: /start session/i }).click();
    await expect(page).toHaveURL(/\/overview$/);
    const w = page.locator("[data-session-window]");
    await expect(w).toHaveCount(1);
    // Still transporting on the placeholder — that part must NOT change, or the socket dies.
    await expect(w).toHaveAttribute("data-session-window", /:new-/);
    // ...while the pane inside it acts on the id the engine minted. Hand off is menu-first in a
    // window (#1329), so it is reachable through the ⋯ menu rather than as a chip.
    await w.locator("[data-window-menu]").click();
    const menu = page.locator("[role='menu']").last();
    await expect(
      menu.getByRole("menuitem", { name: /hand off session to another engine/i }),
    ).toBeVisible();
  });

  test("at capacity, + New session still LAUNCHES — full screen rather than not at all", async ({
    page,
  }) => {
    // Hermes on #939, finding 2: the request was queued, the map refused it under the cap, and
    // the launch — with the cwd and bypass just chosen on the form — was silently discarded. The
    // operator's intent is to start a session; where it appears is the secondary question.
    await page.setViewportSize({ width: 1920, height: 1200 });
    const conns = await mockApp(page, { engines: ["opencode"] });
    await openMap(page);
    for (let i = 0; i < 7; i++) await page.locator("[data-window-cap-down]").click();
    await expect(page.locator("[data-window-readout]")).toContainText("0/1");
    await chip(page, 1).click();
    await expect(page.locator("[data-session-window]")).toHaveCount(1);

    await page.locator('.sidebar .sidebarBody a[href="/"]').first().click();
    await page.getByRole("button", { name: /start session/i }).click();

    // Full screen, and it really launched.
    await expect(page).toHaveURL(/\/s\/opencode\//);
    await expect.poll(() => conns.filter((c) => c.launched).length).toBe(1);
    // ...and nothing was left queued to fire on an unrelated later visit to the map.
    await page.goto("/overview");
    await expect(chip(page, 1)).toBeVisible();
    await expect(page.locator("[data-session-window]")).toHaveCount(1);
    expect(conns.filter((c) => c.launched)).toHaveLength(1);
  });

  test("a map that cannot host hands the request BACK instead of swallowing it", async ({
    page,
  }) => {
    // Hermes on #939, finding 3: a width-only gate does not establish map capacity, and the
    // drain simply dropped what it could not open. The reachable path is a viewport that changes
    // BETWEEN the request and its arrival — the sidebar link carried `returnTo` while the map was
    // hostable, and by the time the map came back it was 400px tall. Setting the small viewport
    // up front would not exercise this at all: `mapReady` would already be false, the link would
    // never carry `returnTo`, and the request would never be queued (which is why the first cut
    // of this test passed against the bug).
    await page.setViewportSize({ width: 1920, height: 1200 });
    const conns = await mockApp(page, { engines: ["opencode"] });
    await openMap(page);

    await page.locator('.sidebar .sidebarBody a[href="/"]').first().click();
    await expect(page).toHaveURL("/");
    // Too short to host a window at its 560×320 floor, while still far wider than the ≤800px
    // mobile breakpoint — the exact gap a width-only gate leaves open.
    await page.setViewportSize({ width: 1920, height: 400 });
    await page.getByRole("button", { name: /start session/i }).click();

    // Handed back to the full-screen route, carrying its launch params — the session exists.
    await expect(page).toHaveURL(/\/s\/opencode\//);
    await expect.poll(() => conns.filter((c) => c.launched).length).toBe(1);
    await expect(page.locator("[data-session-window]")).toHaveCount(0);

    // ...and having now measured a map that cannot host, the pane stops OFFERING To map — the
    // width alone would still have said yes.
    await expect(
      page.getByRole("button", { name: "Open this session as a window on the map" }),
    ).toHaveCount(0);
  });

  test("reloading the new-session form does not lose the launch to a layout not yet restored", async ({
    page,
  }) => {
    // Hermes on #939, round 2. The precheck counted only OPEN windows, and a reloaded form gives
    // the provider a fresh start: `windows` empty, the whole saved layout still in `restorable`.
    // So "plenty of room" was true, the launch was handed to the map, restore then filled the cap
    // before the queued open arrived, and the request was cleared — zero launch sockets.
    //
    // The reload is load-bearing and must not be optimised away: without it the provider is warm,
    // the precheck is right, and the bug is unreachable.
    await page.setViewportSize({ width: 1920, height: 1200 });
    const conns = await mockApp(page, { engines: ["opencode"] });
    await openMap(page);
    for (let i = 0; i < 7; i++) await page.locator("[data-window-cap-down]").click();
    await expect(page.locator("[data-window-readout]")).toContainText("0/1");
    await chip(page, 1).click();
    await expect(page.locator("[data-session-window]")).toHaveCount(1);
    await page.waitForTimeout(700); // let the debounced layout write settle

    await page.locator('.sidebar .sidebarBody a[href="/"]').first().click();
    await expect(page).toHaveURL("/");
    await page.reload(); // fresh provider, layout unhydrated, history keeps returnTo
    await page.getByRole("button", { name: /start session/i }).click();

    // The session exists — full screen, because the cap has no room for it.
    await expect(page).toHaveURL(/\/s\/opencode\//);
    await expect.poll(() => conns.filter((c) => c.launched).length).toBe(1);
  });

  test("a measured but ZERO-sized map takes the fallback — it is not 'not measured yet'", async ({
    page,
  }) => {
    // Hermes on #939, round 2. The usable map height legitimately clamps to zero when the toolbar
    // chrome consumes the whole area, and `measured = box.h > 0` read that completed measurement
    // as "no measurement yet" — so the fallback never ran and the request sat there until the
    // viewport grew, launching later on an unrelated visit.
    await page.setViewportSize({ width: 1920, height: 1200 });
    const conns = await mockApp(page, { engines: ["opencode"] });
    await openMap(page);

    await page.locator('.sidebar .sidebarBody a[href="/"]').first().click();
    await expect(page).toHaveURL("/");
    await page.setViewportSize({ width: 1920, height: 120 });
    await page.getByRole("button", { name: /start session/i }).click();

    await expect(page).toHaveURL(/\/s\/opencode\//);
    await expect.poll(() => conns.filter((c) => c.launched).length).toBe(1);
    // ...and growing the viewport afterwards must not launch it a SECOND time: the request was
    // handed back, not parked.
    await page.setViewportSize({ width: 1920, height: 1200 });
    await page.waitForTimeout(800);
    expect(conns.filter((c) => c.launched)).toHaveLength(1);
  });

  test("a map-created session gains its title, tether and menu when its launch reconciles (#1037)", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1920, height: 1200 });
    // The reconciled row, served by the map's list only once a map sequence start happens
    // AFTER the {"t":"id"} frame — what the server guarantees (its scan cache is busted
    // immediately before the frame) and what the fix's refetch is for. The entry sequence and
    // the return-from-the-landing sequence both run BEFORE any id frame, so on main nothing
    // ever re-pulls after the reconcile and the row never arrives.
    const reconciled = {
      id: RECONCILED,
      engine: "opencode",
      uuid: "ses_9f2b",
      short_uuid: "ses_9f2b",
      cwd: "/home/u/proj",
      project: { kind: "project", id: "p1", name: "proj", color: "#ffb000" },
      last_mtime: now,
      first_user_message: "",
      title: "Reconciled live session",
      sticky: false,
      archived: false,
      ai_summary: "",
    };
    let idFrameSent = false;
    let postIdSeqs = 0;
    const conns = await mockApp(page, {
      engines: ["opencode"],
      onReconcileFrame: () => {
        idFrameSent = true;
      },
      placeholderFrameToo: true,
    });
    // Override the list route (registered AFTER mockApp's, so it wins) with a counting one.
    await page.route("**/api/sessions?**", (r) => {
      const u = new URL(r.request().url());
      if (u.searchParams.get("snapshot") === "new" && u.searchParams.get("offset") === "0" && idFrameSent) {
        postIdSeqs += 1;
      }
      const rows =
        idFrameSent && postIdSeqs >= 1 ? [...sessions, reconciled] : [...sessions];
      return r.fulfill({
        json: {
          sessions: rows,
          next_offset: null,
          total: rows.length,
          facets: { projects: [], engines: [] },
        },
      });
    });

    await openMap(page);
    // From the map: "+ New session" → the landing → back as a window.
    await page.locator('.sidebar .sidebarBody a[href="/"]').first().click();
    await expect(page).toHaveURL("/");
    await page.getByRole("button", { name: /start session/i }).click();
    await expect(page).toHaveURL(/\/overview/);
    await expect(page.locator("[data-session-window]")).toHaveCount(1);
    await expect(page.locator('[aria-label="Session window: New session"]')).toBeVisible();

    // The launch reconciles; the map learns the row; every window surface follows it.
    // Exactly ONE new map sequence — the reconcile-triggered refetch, nothing else — and the
    // placeholder control frame (still naming a new- id) must not add another.
    await expect.poll(() => postIdSeqs, "one refetch after the id frame").toBe(1);
    await page.waitForTimeout(400); // the negative control's frame lands inside this window
    expect(postIdSeqs, "a placeholder id frame must not refetch").toBe(1);
    await expect(
      page.locator('[aria-label="Session window: Reconciled live session"]'),
    ).toBeVisible(); // the chrome title followed the row
    await expect(page.locator("[data-tether]").first()).toBeVisible(); // the tether link
    await expect(
      page.locator(".tr-ov-chip.opened", { hasText: "Reconciled live session" }),
    ).toBeVisible(); // the chip carries the open marker
    await expect(page.locator("[data-window-menu]")).not.toHaveAttribute(
      "aria-disabled",
      "true",
    ); // the ⋯ menu is enabled
    // ...and the refetch touched the list, never the connection: still the ONE launch socket.
    expect(conns).toHaveLength(1);
    expect(conns[0].closed).toBe(false);
  });
});

test("a phone still navigates: no windows, no overlay", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name === "desktop", "this is the mobile contract");
  await mockApp(page);
  await page.goto("/overview");
  await expect(page.locator(".tr-ov-chip").first()).toBeVisible();
  await page.locator(".tr-ov-chip").first().click();
  await expect(page).toHaveURL(/\/s\/claude\//);
  await expect(page.locator("[data-window-layer]")).toHaveCount(0);
});
