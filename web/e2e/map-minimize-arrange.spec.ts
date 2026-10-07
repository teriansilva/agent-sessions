import { expect, type Locator, type Page, test } from "@playwright/test";
import { ASK_STREAM, fulfillAsk } from "./askStream";
import { openAsk } from "./askSidebar";
import { mockRoster } from "./roster";

/** The map's window workspace, operator request 2026-10: minimize a window to a tray, Arrange the
 *  open windows (tiled, sorted by name), and Ask's "Open in map" / "Open all in map".
 *
 *  Minimize is asserted on the SOCKET, not the DOM: a parked window must stay live (no close, no
 *  reconnect) and must not resize the agent (no `{t:"r"}` frame while parked). Unmounting the
 *  window — the obvious wrong implementation — closes the socket and fails here. Desktop only: window mode does not exist
 *  on a phone (the existing overview-windows spec pins that). */

const now = Math.floor(Date.now() / 1000);
const sessions = [1, 2, 3].map((n) => ({
  id: `claude:s${n}`,
  engine: "claude",
  uuid: `s${n}`,
  short_uuid: `s${n}`,
  cwd: "/home/u/proj",
  project: { kind: "project", id: "p1", name: "proj", color: "#ffb000" },
  last_mtime: now - n * 60,
  first_user_message: "",
  title: `Window session ${n}`,
  sticky: false,
  archived: false,
  ai_summary: "",
}));

interface Log {
  conns: { key: string; closed: boolean }[];
  resize: Record<string, number>;
}

async function mockApp(page: Page): Promise<Log> {
  const log: Log = { conns: [], resize: {} };
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
        pulse: { configured: true },
      },
    }),
  );
  await page.routeWebSocket(/\/ws\/term\//, (ws) => {
    const key = decodeURIComponent(new URL(ws.url()).pathname.replace("/ws/term/", ""));
    const conn = { key, closed: false };
    log.conns.push(conn);
    log.resize[key] ??= 0;
    ws.onMessage((raw) => {
      if (typeof raw === "string" && raw.includes('"t":"r"')) log.resize[key] += 1;
    });
    ws.onClose(() => {
      conn.closed = true;
    });
    ws.send(JSON.stringify({ t: "role", role: "owner" }));
    ws.send(JSON.stringify({ t: "seq", n: 0 }));
    ws.send(Buffer.from(`\x1b[2J\x1b[Hhello from ${key}\r\n`));
  });
  await mockRoster(page);
  return log;
}

/** Two windows restored from storage at overlapping rects, s2 first in the list but named
 *  later — so Arrange has both an overlap to undo and an order to fix. */
async function seedWindows(page: Page) {
  await page.addInitScript(() => {
    if (sessionStorage.getItem("seeded")) return;
    sessionStorage.setItem("seeded", "1");
    localStorage.setItem(
      "tr-overview-workspace",
      JSON.stringify([
        { key: "claude:s2", engine: "claude", id: "s2", title: "Window session 2", x: 60, y: 60, w: 720, h: 480, z: 1 },
        { key: "claude:s1", engine: "claude", id: "s1", title: "Window session 1", x: 120, y: 120, w: 720, h: 480, z: 2 },
      ]),
    );
  });
}

const win = (page: Page, n: number): Locator =>
  page.locator(`[data-session-window="claude:s${n}"]`);

test.beforeEach(async ({ page }, info) => {
  test.skip(info.project.name !== "desktop", "window mode is desktop-only");
  await page.setViewportSize({ width: 1920, height: 1200 });
});

test("minimize parks a window in the tray — socket stays live, no resize — and the chip restores it", async ({
  page,
}) => {
  const log = await mockApp(page);
  await seedWindows(page);
  await page.goto("/overview");
  await expect(win(page, 1).locator(".xterm-screen")).toBeVisible();
  await expect(win(page, 2).locator(".xterm-screen")).toBeVisible();
  await expect.poll(() => log.conns.length).toBe(2);
  await page.waitForTimeout(800); // let the opening fit settle
  const connsBefore = log.conns.length;
  const resizeBefore = log.resize["claude:s1"] ?? 0;

  await win(page, 1).locator("[data-window-minimize]").click();
  await expect(win(page, 1)).toBeHidden();
  const chip = page.locator('[data-window-tray-chip="claude:s1"]');
  await expect(chip).toBeVisible();
  await expect(chip).toContainText("Window session 1");
  // Not covered by the map's hint line (a higher layer, pointer-events: none — so a click passes
  // through it and only geometry shows the chip is hidden under it).
  const [cb, hb] = [await chip.boundingBox(), await page.locator(".tr-ov-hint").boundingBox()];
  expect(cb!.y + cb!.height).toBeLessThanOrEqual(hb!.y);
  await page.waitForTimeout(600); // a resize would be debounced into this window
  expect(log.conns.length).toBe(connsBefore);
  expect(log.conns.filter((c) => c.key === "claude:s1" && !c.closed)).toHaveLength(1);
  expect(log.resize["claude:s1"] ?? 0).toBe(resizeBefore);
  // Still counted under the cap — it is still a live terminal.
  await expect(page.locator("[data-window-readout]")).toContainText("2/");

  // The parked window persists parked across a reload.
  await page.waitForTimeout(500); // the debounced layout write
  await page.reload();
  await expect(win(page, 2).locator(".xterm-screen")).toBeVisible();
  await expect(page.locator('[data-window-tray-chip="claude:s1"]')).toBeVisible();
  await expect(win(page, 1)).toBeHidden();

  await page.locator('[data-window-tray-chip="claude:s1"]').click();
  await expect(win(page, 1)).toBeVisible();
  await expect(win(page, 1)).toHaveAttribute("data-focused", "true");
  await expect(page.locator("[data-window-tray]")).toHaveCount(0);
});

test("Arrange tiles the open windows side by side, sorted by name", async ({ page }) => {
  await mockApp(page);
  await seedWindows(page);
  await page.goto("/overview");
  await expect(win(page, 1)).toBeVisible();
  await expect(win(page, 2)).toBeVisible();

  await page.locator("[data-window-arrange]").click();
  await expect
    .poll(async () => {
      const [a, b] = [await win(page, 1).boundingBox(), await win(page, 2).boundingBox()];
      return a && b ? a.x + a.width <= b.x + 1 && Math.abs(a.y - b.y) < 1 : false;
    })
    .toBe(true);
  // Tiles fill the overlay: two windows → two columns, each at least the floor.
  const a = (await win(page, 1).boundingBox())!;
  expect(a.width).toBeGreaterThanOrEqual(560);
  expect(a.height).toBeGreaterThanOrEqual(320);
});

test("Ask: Open in map opens one window; Open all in map opens every match", async ({ page }) => {
  await mockApp(page);
  await page.route(ASK_STREAM, (r) =>
    fulfillAsk(r, {
      answer: "Three sessions touched it.",
      matches: sessions.map((s) => ({ id: s.id, title: s.title, why: "" })),
      stage: "catalog",
      configured: true,
    }),
  );
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  await panel.getByTestId("composer-input").fill("which sessions?");
  await panel.getByTestId("composer-send").click();
  await expect(panel.getByTestId("ask-match")).toHaveCount(3);
  // Each row: Open (the full-screen route) and Open in map.
  const row = panel.getByTestId("ask-match").nth(1);
  await expect(row.getByRole("link", { name: "Open Window session 2" })).toHaveAttribute(
    "href",
    "/s/claude/s2",
  );
  await row.getByRole("button", { name: "Open Window session 2 in map" }).click();
  await expect(page).toHaveURL(/\/overview$/);
  await expect(win(page, 2).locator(".xterm-screen")).toBeVisible();
  await expect(page.locator("[data-session-window]")).toHaveCount(1);

  // Open all: the one already open is focused, the other two open beside it.
  await panel.getByTestId("ask-match-map-all").click();
  await expect(page.locator("[data-session-window]")).toHaveCount(3);
  for (const n of [1, 2, 3]) await expect(win(page, n)).toBeVisible();
});

const askThree = async (page: Page) => {
  await page.route(ASK_STREAM, (r) =>
    fulfillAsk(r, {
      answer: "Three sessions.",
      matches: sessions.map((s) => ({ id: s.id, title: s.title, why: "" })),
      stage: "catalog",
      configured: true,
    }),
  );
  const panel = await openAsk(page);
  await panel.getByTestId("composer-input").fill("which sessions?");
  await panel.getByTestId("composer-send").click();
  await expect(panel.getByTestId("ask-match")).toHaveCount(3);
  return panel;
};

test("after a reload, a stored window that fills the cap can still be opened in the map from Ask (Hermes on #1320)", async ({
  page,
}) => {
  await mockApp(page);
  await page.addInitScript(() => localStorage.setItem("tr-overview-window-cap", "2"));
  await seedWindows(page); // s1 + s2 stored: the cap is full, nothing restored yet
  await page.goto("/dashboard");
  const panel = await askThree(page);
  const stored = panel.getByRole("button", { name: "Open Window session 1 in map" });
  await expect(stored).toBeEnabled();
  // The session with no stored window genuinely has no room.
  await expect(panel.getByRole("button", { name: "Open Window session 3 in map" })).toBeDisabled();
  await stored.click();
  await expect(page).toHaveURL(/\/overview$/);
  await expect(win(page, 1)).toHaveAttribute("data-focused", "true");
  await expect(page.locator("[data-session-window]")).toHaveCount(2);
});

test("first visit on a map too short to host: no Open all; one Open in map still lands full screen (Hermes on #1320)", async ({
  page,
}) => {
  await mockApp(page);
  await page.setViewportSize({ width: 1920, height: 400 });
  await page.goto("/dashboard");
  const panel = await askThree(page);
  // Never measured → one window is offered (its refusal has a home), a batch is not.
  await expect(panel.getByTestId("ask-match-map-all")).toHaveCount(0);
  await panel.getByRole("button", { name: "Open Window session 2 in map" }).click();
  await expect(page).toHaveURL(/\/s\/claude\/s2$/);
});

test("a stale 'can host' does not lose a batch: map → Dashboard → shrink → Open all lists every refused session (Hermes on #1320)", async ({
  page,
}) => {
  await mockApp(page);
  await page.goto("/overview"); // measured: this map can host windows
  await expect(page.locator(".tr-overview .tr-ov-chip").first()).toBeVisible();
  // Leave WITHIN the SPA, so the last measurement (`hostable: true`) is kept.
  await page.locator('[data-testid="section-nav"] >> text=Dashboard').first().click();
  await expect(page).toHaveURL(/\/dashboard/);
  await page.setViewportSize({ width: 1920, height: 400 });
  const panel = await askThree(page);
  await panel.getByTestId("ask-match-map-all").click();
  // The map now measures too small and refuses the whole batch. It must not trade three
  // sessions for one full-screen redirect: the operator stays and sees all three.
  await expect(page).toHaveURL(/\/overview$/);
  const alert = page.locator("[data-map-refused]");
  await expect(alert).toBeVisible();
  for (const n of [1, 2, 3])
    await expect(alert.locator(`[data-map-refused-link="claude:s${n}"]`)).toHaveAttribute(
      "href",
      `/s/claude/s${n}`,
    );
  await page.waitForTimeout(500);
  await expect(page).toHaveURL(/\/overview$/);
  await alert.locator('[data-map-refused-link="claude:s2"]').click();
  await expect(page).toHaveURL(/\/s\/claude\/s2$/);
});
