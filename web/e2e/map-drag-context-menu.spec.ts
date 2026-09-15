import { expect, type Locator, type Page, test } from "@playwright/test";

/** #968 — the map's clusters move where they are put, and every session on the map opens the
 *  sidebar's menu. In a real browser, because every claim here lives where jsdom cannot see:
 *  React Flow's d3-drag (and its click suppression after a drag), the node transform the pins
 *  are read back from, pointer-anchored popover geometry, coarse-pointer media, and — for
 *  archive — the WebSocket lifecycle, asserted on the sockets rather than on the DOM. */

const now = Math.floor(Date.now() / 1000);
const mk = (n: number, engine: string, project: "p1" | "p2", age: number) => ({
  id: `${engine}:m${n}`,
  engine,
  uuid: `m${n}`,
  short_uuid: `m${n}`,
  cwd: project === "p1" ? "/home/u/alpha" : "/home/u/beta",
  project: {
    kind: "project",
    id: project,
    name: project === "p1" ? "Alpha" : "Beta",
    color: project === "p1" ? "#ffb000" : "#3b9eff",
  },
  last_mtime: now - age,
  first_user_message: "",
  title: `Menu session ${n}`,
  sticky: false,
  archived: false,
  ai_summary: "",
});
const SESSIONS = [
  mk(1, "claude", "p1", 10),
  mk(2, "claude", "p1", 20),
  mk(3, "opencode", "p2", 30),
];

interface Conn {
  key: string;
  closed: boolean;
}

async function mockApp(
  page: Page,
  opts: {
    visible?: () => typeof SESSIONS;
    /** AI review configured → the menu offers Review now. */
    aiConfigured?: boolean;
    /** Hidden cwds (`projects_hidden`) — filtering the map applies locally, per layout. */
    hidden?: string[];
  } = {},
): Promise<{ conns: Conn[] }> {
  const log = { conns: [] as Conn[] };
  const visible = opts.visible ?? (() => SESSIONS);
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
      json: {
        projects: [
          { id: "p1", name: "Alpha", color: "#ffb000", archived: false },
          { id: "p2", name: "Beta", color: "#3b9eff", archived: false },
        ],
      },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/sessions?**", (r) =>
    r.fulfill({
      json: {
        sessions: visible(),
        next_offset: null,
        total: visible().length,
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
        // Every cluster open in every layout, so chips are on screen whichever grouping a test picks.
        overview_expanded: [
          "project:p1",
          "project:p2",
          "/home/u/alpha",
          "/home/u/beta",
          "agent:claude",
          "agent:opencode",
        ],
        projects_hidden: opts.hidden ?? [],
        project_names: {},
        compose_default: "collapsed",
        ...(opts.aiConfigured ? { ai_review: { configured: true } } : {}),
      },
    }),
  );
  await page.routeWebSocket(/\/ws\/term\//, (ws) => {
    const key = decodeURIComponent(new URL(ws.url()).pathname.replace("/ws/term/", ""));
    const conn: Conn = { key, closed: false };
    log.conns.push(conn);
    ws.onClose(() => {
      conn.closed = true;
    });
    ws.send(JSON.stringify({ t: "role", role: "owner" }));
    ws.send(JSON.stringify({ t: "seq", n: 0 }));
    ws.send(Buffer.from(`\x1b[2J\x1b[Hhello from ${key}\r\n`));
  });
  return log;
}

const chip = (page: Page, n: number): Locator =>
  page.locator(".tr-overview .tr-ov-chip", { hasText: `Menu session ${n}` });
const node = (page: Page, id: string): Locator =>
  page.locator(`.react-flow__node[data-id="${id}"]`);
const win = (page: Page, key: string): Locator =>
  page.locator(`[data-session-window="${key}"]`);
const menu = (page: Page): Locator => page.getByRole("menu", { name: "Session actions" });

/** A node's FLOW position, read off the transform React Flow writes — screen coordinates move
 *  with every fitView, so they cannot prove a position survived a reload. */
async function flowPos(loc: Locator): Promise<{ x: number; y: number }> {
  const t = await loc.evaluate((el) => (el as HTMLElement).style.transform);
  const m = /translate\((-?[\d.]+)px,\s*(-?[\d.]+)px\)/.exec(t);
  if (!m) throw new Error(`no translate in "${t}"`);
  return { x: Number(m[1]), y: Number(m[2]) };
}

async function box(loc: Locator) {
  const b = await loc.boundingBox();
  if (!b) throw new Error("no box");
  return b;
}

/** A `contextmenu` with real pointer coordinates — Playwright's `dispatchEvent("contextmenu")`
 *  builds an event without them, which is not what a browser delivers for a right-click. */
async function contextMenuAt(loc: Locator, clientX: number, clientY: number) {
  await loc.evaluate(
    (el, p) =>
      el.dispatchEvent(
        new MouseEvent("contextmenu", {
          bubbles: true,
          cancelable: true,
          button: 2,
          clientX: p.x,
          clientY: p.y,
        }),
      ),
    { x: clientX, y: clientY },
  );
}

/** Press on a cluster's header, cross the drag threshold, travel, release. */
async function dragHeader(page: Page, groupId: string, dx: number, dy: number) {
  const head = await box(node(page, groupId).locator(".tr-ov-group-head .tr-ov-path"));
  const x = head.x + head.width / 2;
  const y = head.y + head.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + 12, y + 12, { steps: 4 });
  await page.mouse.move(x + dx, y + dy, { steps: 14 });
  await page.mouse.up();
}

async function selectLayout(page: Page, label: "folders" | "projects" | "agents") {
  await page
    .locator(".tr-overview")
    .getByRole("radio", { name: new RegExp(`group by ${label}`, "i") })
    .click();
}

test.describe("desktop", () => {
  // eslint-disable-next-line no-empty-pattern -- Playwright requires the destructuring form
  test.beforeEach(async ({}, testInfo) => {
    test.skip(testInfo.project.name === "mobile", "pointer paths; the phone has its own cases below");
  });

  test("a cluster drags by its header, keeps its place across a reload, still toggles on click, and Reset returns it", async ({
    page,
  }) => {
    await mockApp(page);
    await page.goto("/overview");
    const beta = node(page, "group:project:p2");
    await expect(chip(page, 3)).toBeVisible();
    const head = beta.locator(".tr-ov-group-head");
    const computed = await flowPos(beta);
    await expect(head).toHaveAttribute("aria-expanded", "true");
    await expect(page.locator("[data-reset-layout]")).toHaveCount(0);

    await dragHeader(page, "group:project:p2", 240, 180);

    const moved = await flowPos(beta);
    expect(Math.abs(moved.x - computed.x) + Math.abs(moved.y - computed.y)).toBeGreaterThan(100);
    // A drag is not a click: the cluster did not collapse on release.
    await expect(head).toHaveAttribute("aria-expanded", "true");
    // No rebuild on drop (#936): its chips stay painted, and so does its neighbour's.
    await expect(chip(page, 3)).toBeVisible();
    await expect(chip(page, 1)).toBeVisible();
    await expect(page.locator("[data-reset-layout]")).toBeVisible();

    await page.reload();
    await expect(chip(page, 3)).toBeVisible();
    // The pin is stored in whole flow pixels, so the restored transform may round the drop by <1px.
    const restored = await flowPos(node(page, "group:project:p2"));
    const drift = { dx: restored.x - moved.x, dy: restored.y - moved.y, moved, restored };
    expect(Math.abs(drift.dx), JSON.stringify(drift)).toBeLessThanOrEqual(1);
    expect(Math.abs(drift.dy), JSON.stringify(drift)).toBeLessThanOrEqual(1);

    // A plain click on the header still toggles.
    await node(page, "group:project:p2").locator(".tr-ov-group-head .tr-ov-path").click();
    await expect(node(page, "group:project:p2").locator(".tr-ov-group-head")).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    await node(page, "group:project:p2").locator(".tr-ov-group-head .tr-ov-path").click();

    await page.locator("[data-reset-layout]").click();
    await expect(page.locator("[data-reset-layout]")).toHaveCount(0);
    await expect.poll(() => flowPos(node(page, "group:project:p2"))).toEqual(computed);
  });

  test("folder and agent clusters drag too, and pins are kept per layout", async ({ page }) => {
    await mockApp(page);
    await page.goto("/overview");
    await expect(chip(page, 1)).toBeVisible();

    for (const [label, id] of [
      ["folders", "group:/home/u/beta"],
      ["agents", "group:agent:opencode"],
    ] as const) {
      await selectLayout(page, label);
      const cluster = node(page, id);
      await expect(cluster).toBeVisible();
      await expect(page.locator("[data-reset-layout]")).toHaveCount(0);
      const before = await flowPos(cluster);
      await dragHeader(page, id, 200, 160);
      const after = await flowPos(cluster);
      expect(Math.abs(after.x - before.x) + Math.abs(after.y - before.y)).toBeGreaterThan(100);
      await expect(page.locator("[data-reset-layout]")).toBeVisible();
    }
    // The Projects layout was never touched, so it has nothing to reset.
    await selectLayout(page, "projects");
    await expect(page.locator("[data-reset-layout]")).toHaveCount(0);
  });

  test("right-click on a chip opens the session menu at the pointer, stays on screen at the edge, and opens no window; the chip ⋯ opens it too", async ({
    page,
  }) => {
    const log = await mockApp(page);
    await page.goto("/overview");
    const c1 = chip(page, 1);
    await expect(c1).toBeVisible();

    // Through the locator, not raw coordinates: the map's initial fit can still be settling when the
    // chip first reports visible, and a coordinate read before it settles lands on empty canvas.
    // `click` waits for the chip to be stable; the pointer is then measured off where it settled.
    await c1.click({ button: "right", position: { x: 80, y: 40 } });
    await expect(menu(page)).toBeVisible();
    const cb = await box(c1);
    const px = Math.round(cb.x + 80);
    const py = Math.round(cb.y + 40);
    const mb = await box(menu(page));
    expect(Math.abs(mb.x - px)).toBeLessThanOrEqual(2);
    expect(Math.abs(mb.y - py)).toBeLessThanOrEqual(2);
    await expect(page.getByRole("menuitem", { name: "Rename session" })).toBeVisible();
    await expect(page.getByRole("menuitem", { name: "Archive session" })).toBeVisible();
    // Focus is in the menu (menu-button pattern), and Esc hands it back to the chip's ⋯.
    await expect(page.getByRole("menuitem").first()).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(menu(page)).toHaveCount(0);
    await expect(c1.locator("[data-chip-menu]")).toBeFocused();

    // At the viewport's bottom-right corner the menu flips left and up to stay fully on screen.
    const vp = page.viewportSize()!;
    await contextMenuAt(c1, vp.width - 4, vp.height - 4);
    await expect(menu(page)).toBeVisible();
    const eb = await box(menu(page));
    expect(eb.x).toBeGreaterThanOrEqual(0);
    expect(eb.y).toBeGreaterThanOrEqual(0);
    expect(eb.x + eb.width).toBeLessThanOrEqual(vp.width);
    const placed = await menu(page).evaluate((el) => ({
      top: (el as HTMLElement).style.getPropertyValue("--rm-top"),
      right: (el as HTMLElement).style.getPropertyValue("--rm-right"),
      offsetHeight: (el as HTMLElement).offsetHeight,
      innerHeight: window.innerHeight,
      scrollY: window.scrollY,
    }));
    expect(eb.y + eb.height, JSON.stringify({ eb, vp, placed })).toBeLessThanOrEqual(vp.height);
    await page.keyboard.press("Escape");

    // The chip's ⋯ opens the same menu, under the button.
    const kebab = chip(page, 2).locator("[data-chip-menu]");
    await kebab.click();
    await expect(menu(page)).toBeVisible();
    const kb = await box(kebab);
    expect((await box(menu(page))).y).toBeGreaterThanOrEqual(kb.y + kb.height - 1);
    await page.keyboard.press("Escape");

    // None of that opened a window or a socket.
    await page.waitForTimeout(300);
    await expect(page.locator("[data-session-window]")).toHaveCount(0);
    expect(log.conns).toHaveLength(0);
  });

  test("Rename from the map opens a dialog and POSTs the new title", async ({ page }) => {
    await mockApp(page);
    let renamed: { url: string; body: unknown } | null = null;
    await page.route("**/api/sessions/*/rename", async (r) => {
      renamed = { url: decodeURIComponent(r.request().url()), body: r.request().postDataJSON() };
      await r.fulfill({ json: { id: "claude:m1", title: "Renamed from the map" } });
    });
    await page.goto("/overview");
    await chip(page, 1).locator("[data-chip-menu]").click();
    await page.getByRole("menuitem", { name: "Rename session" }).click();
    const dialog = page.locator("[data-session-text-dialog='title']");
    await expect(dialog).toBeVisible();
    await dialog.getByLabel("Session title").fill("Renamed from the map");
    await dialog.getByRole("button", { name: "Save" }).click();
    await expect.poll(() => renamed?.body).toEqual({ title: "Renamed from the map" });
    expect(renamed!.url).toContain("/api/sessions/claude:m1/rename");
    await expect(dialog).toHaveCount(0);
  });

  test("Archive from a window: its socket is closed when the request arrives, nothing reconnects, and focus lands on the map", async ({
    page,
  }) => {
    let visible = SESSIONS;
    const log = await mockApp(page, { visible: () => visible });
    let socketClosedAtRequest: boolean | null = null;
    await page.route("**/api/sessions/*/archive", async (r) => {
      socketClosedAtRequest = log.conns
        .filter((c) => c.key === "claude:m1")
        .every((c) => c.closed);
      visible = SESSIONS.filter((s) => s.id !== "claude:m1");
      await r.fulfill({ json: { id: "claude:m1", archived: true } });
    });
    await page.goto("/overview");

    await chip(page, 1).click();
    await expect(win(page, "claude:m1").locator(".xterm-screen")).toBeVisible();
    await expect.poll(() => log.conns.filter((c) => !c.closed).length).toBe(1);

    await win(page, "claude:m1").locator("[data-window-menu]").click();
    await page.getByRole("menuitem", { name: "Archive session" }).click();

    await expect.poll(() => socketClosedAtRequest).toBe(true);
    await expect(page.locator("[data-session-window]")).toHaveCount(0);
    await expect(chip(page, 1)).toHaveCount(0); // the refetch dropped it
    // Nothing reconnects afterwards: still the one connection, and it is closed.
    await page.waitForTimeout(1500);
    expect(log.conns.map((c) => c.key)).toEqual(["claude:m1"]);
    expect(log.conns.every((c) => c.closed)).toBe(true);
    // The ⋯ that opened the menu is gone with its window; focus is on the map, not <body>.
    expect(
      await page.evaluate(() => document.activeElement?.hasAttribute("data-overview-map") ?? false),
    ).toBe(true);
  });

  test("a refused archive shows the server's detail, keeps the chip, and reopens no window or socket", async ({
    page,
  }) => {
    const log = await mockApp(page);
    await page.route("**/api/sessions/*/archive", (r) =>
      r.fulfill({ status: 409, json: { detail: "session is busy — try again" } }),
    );
    await page.goto("/overview");
    await chip(page, 1).click();
    await expect(win(page, "claude:m1").locator(".xterm-screen")).toBeVisible();
    // The window's socket is live before the archive, so "no socket reopens" means something.
    await expect.poll(() => log.conns.filter((c) => !c.closed).length).toBe(1);

    await win(page, "claude:m1").locator("[data-window-menu]").click();
    await page.getByRole("menuitem", { name: "Archive session" }).click();

    await expect(page.locator("[data-map-error]")).toContainText("session is busy — try again");
    await expect(chip(page, 1)).toBeVisible();
    await page.waitForTimeout(1500);
    await expect(page.locator("[data-session-window]")).toHaveCount(0);
    expect(log.conns).toHaveLength(1);
    expect(log.conns[0].closed).toBe(true);
  });

  test("a window whose session leaves the map keeps a visible, disabled ⋯ that says why", async ({
    page,
  }) => {
    let visible = SESSIONS;
    await mockApp(page, { visible: () => visible });
    await page.route("**/api/sessions/*/favorite", (r) =>
      r.fulfill({ json: { id: "claude:m1", sticky: true } }),
    );
    await page.setViewportSize({ width: 1920, height: 1200 });
    await page.goto("/overview");

    await chip(page, 2).click();
    const w2 = win(page, "claude:m2");
    await expect(w2).toBeVisible();
    // Park it low so chip 1 stays reachable.
    const head = await box(w2.locator("[data-window-head]"));
    await page.mouse.move(head.x + head.width / 2, head.y + head.height / 2);
    await page.mouse.down();
    await page.mouse.move(900, 1150, { steps: 12 });
    await page.mouse.up();

    // Session 2 drops out of the list; any map mutation's refetch will now omit it.
    visible = SESSIONS.filter((s) => s.id !== "claude:m2");
    await chip(page, 1).locator("[data-chip-menu]").click();
    await page.getByRole("menuitem", { name: "Favorite session" }).click();
    await expect(chip(page, 2)).toHaveCount(0);

    const btn = w2.locator("[data-window-menu]");
    await expect(btn).toBeVisible();
    await expect(btn).toHaveAttribute("aria-disabled", "true");
    await expect(btn).toHaveAttribute("title", /isn't on the map/);
    // Forced: Playwright refuses an aria-disabled target, which is the state being asserted.
    await btn.click({ force: true });
    await page.waitForTimeout(200);
    await expect(menu(page)).toHaveCount(0);
  });
  test("a Review now in flight is never sent twice — not after the menu closes and reopens, nor after another session's menu replaces it", async ({
    page,
  }) => {
    await mockApp(page, { aiConfigured: true });
    const reviews: string[] = [];
    let release: () => void = () => {};
    const parked = new Promise<void>((r) => (release = r));
    await page.route("**/api/sessions/*/review", async (r) => {
      reviews.push(decodeURIComponent(r.request().url()));
      await parked;
      await r.fulfill({
        json: {
          id: "claude:m1",
          title: "Menu session 1",
          ai_summary: "reviewed",
          ai_title: "",
          intervention_required: false,
          intervention_reason: "",
          reviewed_at: now,
          review_excluded: false,
          ai_recap: "",
          recap_fingerprint: "",
        },
      });
    });
    await page.goto("/overview");
    const k1 = chip(page, 1).locator("[data-chip-menu]");
    const reviewItem = () => page.getByRole("menuitem", { name: "Review session now" });

    await k1.click();
    await reviewItem().click();
    await expect.poll(() => reviews.length).toBe(1);

    // The host that sent it has closed. Reopening the same session's menu finds Review now disabled,
    // and pressing it anyway sends nothing.
    await k1.click();
    await expect(reviewItem()).toHaveAttribute("aria-disabled", "true");
    await reviewItem().click({ force: true });
    await page.keyboard.press("Escape");

    // Another session's menu replaces the host entirely; its own Review now is untouched…
    await chip(page, 2).locator("[data-chip-menu]").click();
    await expect(reviewItem()).not.toHaveAttribute("aria-disabled", "true");
    await page.keyboard.press("Escape");
    // …and coming back to the first session still finds its review in flight.
    await k1.click();
    await expect(reviewItem()).toHaveAttribute("aria-disabled", "true");
    await page.waitForTimeout(300);
    expect(reviews).toHaveLength(1);

    // Settled: offered again, in the menu that is already open.
    release();
    await expect(reviewItem()).not.toHaveAttribute("aria-disabled", "true");
    expect(reviews).toHaveLength(1);
  });

  for (const [item, mode, label] of [
    ["Rename session", "title", "Session title"],
    ["Set session tag", "tag", "Session tag"],
  ] as const) {
    test(`the ${mode} dialog keeps Tab and Shift+Tab inside itself, and Escape returns focus to the chip's ⋯`, async ({
      page,
    }) => {
      await mockApp(page);
      await page.goto("/overview");
      const kebab = chip(page, 1).locator("[data-chip-menu]");
      await kebab.click();
      await page.getByRole("menuitem", { name: item }).click();
      const dialog = page.locator(`[data-session-text-dialog='${mode}']`);
      await expect(dialog.getByLabel(label)).toBeFocused();
      const focusInDialog = () =>
        page.evaluate(() => !!document.activeElement?.closest("[data-session-text-dialog]"));
      // More presses than the dialog has stops, both ways: the cycle wraps and never leaves.
      for (let i = 0; i < 6; i++) {
        await page.keyboard.press("Shift+Tab");
        expect(await focusInDialog(), `Shift+Tab #${i + 1}`).toBe(true);
      }
      for (let i = 0; i < 6; i++) {
        await page.keyboard.press("Tab");
        expect(await focusInDialog(), `Tab #${i + 1}`).toBe(true);
      }
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0);
      await expect(kebab).toBeFocused();
    });
  }

  test("a chip ⋯ panned next to the viewport's left edge opens its menu fully on screen", async ({
    page,
  }) => {
    // The sidebar collapsed, so the map reaches the viewport's left edge — the condition the
    // right-aligned placement never met in the sidebar.
    await page.addInitScript(() => localStorage.setItem("tr-sidebar-collapsed", "1"));
    await mockApp(page);
    await page.goto("/overview");
    const kebab = chip(page, 1).locator("[data-chip-menu]");
    await expect(kebab).toBeVisible();

    // Pan the empty canvas left until the ⋯ sits about 100px from the viewport's left edge.
    const pane = await box(page.locator(".react-flow__pane"));
    const kb0 = await box(kebab);
    const sx = pane.x + pane.width - 60;
    const sy = pane.y + 120;
    await page.mouse.move(sx, sy);
    await page.mouse.down();
    await page.mouse.move(sx - (kb0.x - 100), sy, { steps: 16 });
    await page.mouse.up();
    const kb = await box(kebab);
    expect(kb.x).toBeGreaterThanOrEqual(0);
    expect(kb.x).toBeLessThan(160);

    await kebab.click();
    await expect(menu(page)).toBeVisible();
    const mb = await box(menu(page));
    const vp = page.viewportSize()!;
    expect(mb.x, JSON.stringify({ kb, mb })).toBeGreaterThanOrEqual(0);
    expect(mb.x + mb.width).toBeLessThanOrEqual(vp.width);
  });

  test("a window's ⋯ disables when the LAYOUT takes its session off the map — and a collapsed cluster does not count", async ({
    page,
  }) => {
    // /home/u/alpha is hidden. Session 1 belongs to the Alpha entity, so it stays on the map in
    // Projects and leaves it in Folders — filtering done locally, with the API row unchanged.
    await mockApp(page, { hidden: ["/home/u/alpha"] });
    await page.setViewportSize({ width: 1920, height: 1200 });
    await page.goto("/overview");
    await expect(chip(page, 1)).toBeVisible();
    await chip(page, 1).click();
    const w1 = win(page, "claude:m1");
    await expect(w1).toBeVisible();
    // Park the window low, clear of the clusters.
    const head = await box(w1.locator("[data-window-head]"));
    await page.mouse.move(head.x + head.width / 2, head.y + head.height / 2);
    await page.mouse.down();
    await page.mouse.move(1300, 1150, { steps: 12 });
    await page.mouse.up();
    const btn = w1.locator("[data-window-menu]");
    await expect(btn).not.toHaveAttribute("aria-disabled", "true");

    await selectLayout(page, "folders");
    await expect(chip(page, 1)).toHaveCount(0);
    await expect(btn).toHaveAttribute("aria-disabled", "true");
    await btn.click({ force: true });
    await page.waitForTimeout(200);
    await expect(menu(page)).toHaveCount(0);

    // Back in Projects, collapsing Alpha hides the chip but not the session: the ⋯ stays live.
    await selectLayout(page, "projects");
    await expect(btn).not.toHaveAttribute("aria-disabled", "true");
    await node(page, "group:project:p1").locator(".tr-ov-group-head .tr-ov-path").click();
    await expect(chip(page, 1)).toHaveCount(0);
    await expect(btn).not.toHaveAttribute("aria-disabled", "true");
    await btn.click();
    await expect(menu(page)).toBeVisible();
  });
});

test.describe("phone", () => {
  // eslint-disable-next-line no-empty-pattern -- Playwright requires the destructuring form
  test.beforeEach(async ({}, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "coarse-pointer geometry");
  });

  const zoomOf = (page: Page) =>
    page
      .locator("[data-overview-map]")
      .evaluate((el) => Number((el as HTMLElement).style.getPropertyValue("--ov-zoom")));

  test("at zoom ≥0.55 the chip ⋯ is ≥44 screen px and apart from the body tap: ⋯ opens the sheet (which survives a resize), the body navigates", async ({
    page,
  }) => {
    await mockApp(page);
    await page.goto("/overview");
    const c1 = chip(page, 1);
    await expect(c1).toBeVisible();
    for (let i = 0; i < 10 && (await zoomOf(page)) < 0.55; i++) {
      await page.locator(".react-flow__controls-zoomin").tap();
    }
    expect(await zoomOf(page)).toBeGreaterThanOrEqual(0.55);

    const kebab = c1.locator("[data-chip-menu]");
    const kb = await box(kebab);
    expect(kb.width).toBeGreaterThanOrEqual(43.5);
    expect(kb.height).toBeGreaterThanOrEqual(43.5);
    // A body tap point just LEFT of the ⋯ square, at the chip's foot: inside the chip and on
    // screen (a zoomed chip can hang off the viewport edge), but outside the ⋯ target.
    const cb = await box(c1);
    const vp = page.viewportSize()!;
    const body = { x: kb.x - 12, y: cb.y + cb.height - 8 };
    const insideKebab =
      body.x >= kb.x && body.x <= kb.x + kb.width && body.y >= kb.y && body.y <= kb.y + kb.height;
    expect(insideKebab).toBe(false);
    expect(body.x).toBeGreaterThan(Math.max(cb.x, 0));
    expect(body.y).toBeLessThan(Math.min(cb.y + cb.height, vp.height));

    await kebab.tap();
    await expect(menu(page)).toBeVisible();
    await expect(page).toHaveURL(/\/overview$/);
    // The phone's browser chrome fires resize on the very tap that opens the sheet (#384).
    await page.evaluate(() => window.dispatchEvent(new Event("resize")));
    await page.waitForTimeout(200);
    await expect(menu(page)).toBeVisible();
    await page.getByRole("button", { name: "Cancel" }).tap();
    await expect(menu(page)).toHaveCount(0);

    await page.touchscreen.tap(body.x, body.y);
    await expect(page).toHaveURL(/\/s\/claude\/m1$/);
  });

  test("a tap on a cluster header still toggles it — the header being a drag handle does not swallow the tap", async ({
    page,
  }) => {
    await mockApp(page);
    await page.goto("/overview");
    await expect(chip(page, 1)).toBeVisible();
    for (const [label, id] of [
      ["projects", "group:project:p1"],
      ["folders", "group:/home/u/alpha"],
    ] as const) {
      await page
        .locator(".tr-overview")
        .getByRole("radio", { name: new RegExp(`group by ${label}`, "i") })
        .tap();
      const head = node(page, id).locator(".tr-ov-group-head");
      await expect(head).toHaveAttribute("aria-expanded", "true");
      const pb = await box(head.locator(".tr-ov-path"));
      await page.touchscreen.tap(pb.x + pb.width / 2, pb.y + pb.height / 2);
      await expect(head).toHaveAttribute("aria-expanded", "false");
      await page.touchscreen.tap(pb.x + pb.width / 2, pb.y + pb.height / 2);
      await expect(head).toHaveAttribute("aria-expanded", "true");
    }
  });

  test("at the 0.2 minimum zoom the ⋯ hides, and a contextmenu on the chip still opens the sheet", async ({
    page,
  }) => {
    await mockApp(page);
    await page.goto("/overview");
    await expect(chip(page, 1)).toBeVisible();
    for (let i = 0; i < 20 && (await zoomOf(page)) > 0.2001; i++) {
      await page.locator(".react-flow__controls-zoomout").tap();
    }
    expect(await zoomOf(page)).toBeLessThanOrEqual(0.2001);
    await expect(page.locator("[data-overview-map]")).toHaveAttribute("data-zoom-far", "");
    await expect(chip(page, 1).locator("[data-chip-menu]")).toBeHidden();

    const cb = await box(chip(page, 1));
    await contextMenuAt(chip(page, 1), cb.x + cb.width / 2, cb.y + cb.height / 2);
    await expect(menu(page)).toBeVisible();
    await expect(page).toHaveURL(/\/overview$/);
  });
});
