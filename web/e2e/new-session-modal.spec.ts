import { expect, test, type Page } from "@playwright/test";
import { mockRoster } from "./roster";

// #1648, real browser: New session opens OVER the session map, so the map's windows stay
// connected. Before this, the sidebar's New session navigated to /new, which unmounted the map:
// every terminal socket closed, every API pane stopped polling, and every composer's text went.
//
// Desktop: one terminal window and one API window are open on the map. Open New session, start a
// session, and the terminal's socket never closes or reconnects, the API pane's reads never stop,
// no uplink veil appears, and the API composer still holds its text. A full map says so and
// creates nothing until Start full screen.
// Mobile: the real sidebar link (in the drawer) opens a full-height sheet without leaving the map,
// at the Pixel 7 project's size and at 320 px; Start opens the session full screen.

const now = Math.floor(Date.now() / 1000);
const row = (over: Record<string, unknown>) => ({
  cwd: "/home/u/proj",
  project: { kind: "project", id: "p1", name: "proj", color: "#ffb000" },
  last_mtime: now,
  first_user_message: "",
  sticky: false,
  archived: false,
  ai_summary: "",
  ...over,
});
const TERM = row({ id: "claude:s1", engine: "claude", uuid: "s1", short_uuid: "s1", title: "Map terminal" });
const API = row({
  id: "claude-api:a6",
  engine: "claude-api",
  uuid: "a6",
  short_uuid: "a6",
  title: "Map API session",
  last_mtime: now - 60,
});

interface Conn {
  key: string;
  closed: boolean;
}
interface Log {
  conns: Conn[];
  apiReads: number[];
}

async function mockApp(page: Page, theme = "dark", cap?: number): Promise<Log> {
  const log: Log = { conns: [], apiReads: [] };
  await page.addInitScript(
    ({ t, c }) => {
      localStorage.setItem("tr-theme", t);
      if (c) localStorage.setItem("tr-overview-window-cap", String(c));
      if (sessionStorage.getItem("seeded")) return;
      sessionStorage.setItem("seeded", "1");
      localStorage.setItem(
        "tr-overview-workspace",
        JSON.stringify([
          { key: "claude:s1", engine: "claude", id: "s1", title: "Map terminal", x: 40, y: 380, w: 560, h: 340, z: 1 },
          { key: "claude-api:a6", engine: "claude-api", id: "a6", title: "Map API session", x: 640, y: 380, w: 560, h: 340, z: 2 },
        ]),
      );
    },
    { t: theme, c: cap },
  );
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/pulse/notifications**", (r) => r.fulfill({ json: { notifications: [], unread: 0 } }));
  await page.route("**/api/**/draft", (r) => r.fulfill({ json: { id: "d", text: "", attachments: [], updated_at: null } }));
  await page.route("**/api/projects**", (r) =>
    r.fulfill({
      json: {
        projects: [
          { id: "p1", name: "proj", color: "#ffb000", archived: false, folders: ["/home/u/proj"], default_folder: "/home/u/proj" },
        ],
      },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/sessions?**", (r) =>
    r.fulfill({ json: { sessions: [TERM, API], next_offset: null, total: 2, facets: { projects: [], engines: [] } } }),
  );
  await page.route(/\/api\/sessions\/claude-api(?::|%3A)a6$/, (r) => r.fulfill({ json: API }));
  await page.route(/\/api\/structured\/sessions\/claude-api(?::|%3A)a6(\/[a-z]+)?(\?.*)?$/, (r) => {
    log.apiReads.push(Date.now());
    const snap = {
      session_key: "claude-api:a6",
      revision: 1,
      event_cursor: 1,
      cwd: "/home/u/proj",
      state: "idle",
      active_turn: null,
      model_requested: null,
      model_effective: null,
      turns: [],
      omitted_turns: 0,
      pending_requests: [],
      native: { native_id: "n6", worker: "w1", background_active: false },
      read_only: null,
    };
    if (r.request().url().includes("/events"))
      return r.fulfill({ json: { session_key: snap.session_key, revision: 1, next_cursor: 1, events: [] } });
    return r.fulfill({ json: snap });
  });
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["claude"],
        terminal_backend: "ws",
        auth_mode: "none",
        onboarded: true,
        theme,
        default_project: "/home/u/proj",
        overview_expanded: ["project:p1"],
        projects_hidden: [],
        project_names: {},
        compose_default: "collapsed",
      },
    }),
  );
  await page.routeWebSocket(/\/ws\/term\//, (ws) => {
    const conn: Conn = { key: decodeURIComponent(new URL(ws.url()).pathname.replace("/ws/term/", "")), closed: false };
    log.conns.push(conn);
    ws.onClose(() => {
      conn.closed = true;
    });
    ws.send(JSON.stringify({ t: "role", role: "owner" }));
    ws.send(JSON.stringify({ t: "seq", n: 0 }));
    ws.send(Buffer.from(`\x1b[2J\x1b[Hhello from ${conn.key}\r\n`));
  });
  await mockRoster(page);
  return log;
}

const modal = (page: Page) => page.getByTestId("new-session-modal");
/** Set CAPTURE_DIR to write the PR's screenshots from these real renders. */
async function shot(page: Page, name: string) {
  const dir = process.env.CAPTURE_DIR;
  if (!dir) return;
  await page.waitForTimeout(800); // past the dialog's deploy-in (fade + rise, --t-slow)
  await page.screenshot({ path: `${dir}/${name}.png` });
}
const newSessionLink = (page: Page) => page.locator("aside.sidebar").getByRole("link", { name: /new session/i });

test.describe("desktop: the map's windows stay connected through New session", () => {
  test.beforeEach(async ({ page }, info) => {
    test.skip(info.project.name !== "desktop", "map windows are desktop-only (map-window-layer.md)");
    await page.setViewportSize({ width: 1440, height: 900 });
  });

  for (const theme of ["dark", "light"]) {
    test(`open, start, close: no socket closes, the API pane keeps polling, its text stays (${theme})`, async ({ page }) => {
      const log = await mockApp(page, theme);
      await page.goto("/overview");
      const term = page.locator('[data-session-window="claude:s1"]');
      const apiWin = page.locator('[data-session-window="claude-api:a6"]');
      await expect(term.locator(".xterm-screen")).toBeVisible();
      await expect(apiWin.getByTestId("structured-pane")).toBeVisible();
      const box = apiWin.getByRole("textbox", { name: /^Message / });
      await box.fill("kept while I start another");
      await expect.poll(() => log.conns.filter((c) => c.key === "claude:s1").length).toBe(1);

      await newSessionLink(page).click();
      await expect(modal(page)).toBeVisible();
      await expect(page).toHaveURL(/\/overview$/);
      // The map is still mounted under it, and the API pane keeps reading the whole time.
      await shot(page, `n1648-open-d-${theme}`);
      const before = log.apiReads.length;
      // An idle API pane polls every 10 s: a read lands while the modal is open.
      await expect.poll(() => log.apiReads.length, { timeout: 15_000 }).toBeGreaterThan(before);
      await expect(page.locator(".tr-overview")).toBeVisible();

      await modal(page).getByRole("button", { name: "Start session" }).click();
      await expect(modal(page)).toHaveCount(0);
      await expect(page).toHaveURL(/\/overview$/);
      // The new session opened as a window in place.
      await expect(page.locator('[data-session-window^="claude:"]')).toHaveCount(2);
      await expect.poll(() => log.conns.length).toBe(2);

      const s1 = log.conns.filter((c) => c.key === "claude:s1");
      expect(s1).toHaveLength(1); // never reconnected
      expect(s1[0].closed).toBe(false); // never closed
      expect(await page.getByTestId("uplink-veil").count()).toBe(0);
      await expect(box).toHaveValue("kept while I start another");
    });
  }

  test("Esc closes it and focus returns to the New session link", async ({ page }) => {
    await mockApp(page);
    await page.goto("/overview");
    await expect(page.locator('[data-session-window="claude:s1"] .xterm-screen')).toBeVisible();
    await newSessionLink(page).click();
    await expect(modal(page)).toBeVisible();
    await expect(modal(page).getByRole("button", { name: "Close new session" })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(modal(page)).toHaveCount(0);
    await expect(newSessionLink(page)).toBeFocused();
  });

  test("a full map says so, and creates nothing until Start full screen", async ({ page }) => {
    const log = await mockApp(page, "dark", 2); // the cap is the two windows already open
    await page.goto("/overview");
    await expect(page.locator('[data-session-window="claude-api:a6"]')).toBeVisible();
    await newSessionLink(page).click();
    await expect(modal(page).getByTestId("new-session-map-full")).toContainText("The map is full.");
    const start = modal(page).getByRole("button", { name: "Start full screen" });
    await expect(start).toBeVisible();
    await shot(page, "n1648-full-d-dark");
    await page.waitForTimeout(1_000);
    expect(log.conns.map((c) => c.key)).toEqual(["claude:s1"]); // nothing new so far
    await start.click();
    await expect(page).toHaveURL(/\/s\/claude\/[0-9a-f-]{36}$/);
  });
});

test.describe("mobile: the real sidebar link opens a full-height sheet over the map", () => {
  test.beforeEach(() => {
    test.skip(test.info().project.name !== "mobile", "the phone's sheet");
  });

  for (const [label, size] of [
    ["Pixel 7", null],
    ["320 px", { width: 320, height: 640 }],
  ] as const) {
    for (const theme of ["dark", "light"]) {
      test(`${label}, ${theme}: sheet over the map, full height, 44 px controls, Start opens full screen`, async ({ page }) => {
        if (size) await page.setViewportSize(size);
        await mockApp(page, theme);
        await page.goto("/overview");
        await expect(page.locator(".tr-overview")).toBeVisible();
        await page.getByRole("button", { name: /open session list/i }).click();
        await newSessionLink(page).click();
        await expect(modal(page)).toBeVisible();
        await expect(page).toHaveURL(/\/overview$/);
        await expect(page.locator(".tr-overview")).toHaveCount(1); // the map is still mounted under it
        const vp = page.viewportSize()!;
        // The sheet slides up (the shared sheet's arrival): measure it once it has settled.
        await expect.poll(async () => Math.round((await modal(page).boundingBox())!.y)).toBe(0);
        const sheet = (await modal(page).boundingBox())!;
        expect(Math.round(sheet.height)).toBe(vp.height);
        expect(Math.round(sheet.width)).toBe(vp.width);
        await expect(modal(page).getByTestId("new-session-modal-note")).toContainText("full screen");
        await shot(page, `n1648-sheet-${size ? "320" : "p7"}-${theme}`);
        const close = (await modal(page).getByRole("button", { name: "Close new session" }).boundingBox())!;
        expect(close.height).toBeGreaterThanOrEqual(43.5);
        const start = modal(page).getByRole("button", { name: "Start session" });
        await start.scrollIntoViewIfNeeded();
        expect((await start.boundingBox())!.height).toBeGreaterThanOrEqual(43.5);
        await start.click();
        await expect(page).toHaveURL(/\/s\/claude\/[0-9a-f-]{36}$/);
      });
    }
  }
});
