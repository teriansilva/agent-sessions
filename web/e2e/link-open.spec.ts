import {
  type BrowserContext,
  expect,
  type Page,
  test,
} from "@playwright/test";
import { clickHeadAction } from "./headActions";
import { mockRoster } from "./roster";

/** BattleLab links from outside the app (#1232) — in a real browser, because every part of it is
 *  something a DOM emulator cannot model: a real entry navigation (`PerformanceNavigationTiming`),
 *  real `localStorage` across navigations, the map's real measurement deciding whether a window is
 *  possible, two real tabs talking over `BroadcastChannel` + Web Locks, and the real clipboard. */

const now = Math.floor(Date.now() / 1000);
const MID = "msn_" + "b".repeat(32);
const sessions = ["s1", "s2"].map((k, i) => ({
  id: `claude:${k}`,
  engine: "claude",
  uuid: k,
  short_uuid: k,
  cwd: "/home/u/proj",
  project: { kind: "project", id: "p1", name: "proj", color: "#ffb000" },
  last_mtime: now - i * 60,
  first_user_message: "",
  title: `Link session ${i + 1}`,
  sticky: false,
  archived: false,
  ai_summary: "",
}));

async function mockApp(target: Page | BrowserContext) {
  await target.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await target.route("**/api/pulse/notifications**", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0 } }),
  );
  await target.route("**/api/**/draft", (r) =>
    r.fulfill({ json: { id: "d", text: "", attachments: [], updated_at: null } }),
  );
  await target.route("**/api/projects**", (r) => r.fulfill({ json: { projects: [] } }));
  await target.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await target.route(/\/api\/sessions\/[^?]+$/, (r) => {
    const key = decodeURIComponent(new URL(r.request().url()).pathname.split("/").pop()!);
    const row = sessions.find((s) => s.id === key);
    return row
      ? r.fulfill({ json: row })
      : r.fulfill({ status: 404, json: { detail: "not found" } });
  });
  await target.route("**/api/sessions?**", (r) =>
    r.fulfill({
      json: {
        sessions,
        next_offset: null,
        total: sessions.length,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await target.route("**/api/config", (r) =>
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
  if ("goto" in target) await mockRoster(target);
  await target.routeWebSocket(/\/ws\/term\//, (ws) => {
    ws.send(JSON.stringify({ t: "role", role: "owner" }));
    ws.send(JSON.stringify({ t: "seq", n: 0 }));
    ws.send(Buffer.from("\x1b[2J\x1b[Hready\r\n"));
  });
}

const dialog = (page: Page) => page.getByTestId("open-link-dialog");

test.describe("opening BattleLab links", () => {
  test.describe.configure({ timeout: 90_000 });
  // The suite's default storage makes the choice (playwright.config.ts); these tests start unmade.
  test.use({ storageState: { cookies: [], origins: [] } });

  test("desktop: a session link asks once, remembers the map, and Settings resets it", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "the map choice is desktop-only");
    await page.setViewportSize({ width: 1600, height: 1000 });
    await mockApp(page);

    await page.goto("/s/claude/s1");
    await expect(dialog(page)).toBeVisible();
    await expect(dialog(page)).toContainText("Link session 1");
    await expect(page.getByTestId("open-link-remember")).toBeChecked();
    await page.getByTestId("open-link-map").click();

    await expect(page).toHaveURL(/\/overview$/);
    await expect(page.locator('[data-session-window="claude:s1"]')).toBeVisible();

    // Remembered: the next link opens straight into the map, no prompt.
    await page.goto("/s/claude/s2");
    await expect(page).toHaveURL(/\/overview$/);
    await expect(page.locator('[data-session-window="claude:s2"]')).toBeVisible();
    await expect(dialog(page)).toHaveCount(0);

    // Reset in Settings → Appearance → Opening links.
    await page.goto("/settings/appearance");
    const group = page.getByTestId("opening-links");
    await expect(group.getByRole("radio", { name: /In map/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    await group.getByRole("radio", { name: /Ask each time/ }).click();

    await page.goto("/s/claude/s1");
    await expect(dialog(page)).toBeVisible();
    await page.getByTestId("open-link-fullscreen").click();
    await expect(dialog(page)).toHaveCount(0);
    await expect(page).toHaveURL(/\/s\/claude\/s1$/);
  });

  test("desktop: a reload or an in-app navigation never asks", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "the map choice is desktop-only");
    await page.setViewportSize({ width: 1600, height: 1000 });
    await mockApp(page);
    await page.goto("/s/claude/s1");
    await page.getByTestId("open-link-fullscreen").click();
    // "Remember" is checked by default, so that saved full screen — reset it to ask again.
    await page.goto("/settings/appearance");
    await page.getByTestId("opening-links").getByRole("radio", { name: /Ask each time/ }).click();

    // A page WITH the session sidebar: Dashboard has none since #1233, so the landing stands in.
    await page.goto("/");
    await page.locator('.sidebar a[href="/s/claude/s2"]').first().click();
    await expect(page).toHaveURL(/\/s\/claude\/s2$/);
    await page.reload();
    await expect(page.locator('[class*="panelHead"]')).toBeVisible();
    await expect(dialog(page)).toHaveCount(0);
  });

  test("phone: a session link opens full screen and never asks", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "the phone case");
    await mockApp(page);
    await page.goto("/s/claude/s1");
    await expect(page.locator('[class*="panelHead"]')).toBeVisible();
    await expect(dialog(page)).toHaveCount(0);
    await expect(page).toHaveURL(/\/s\/claude\/s1$/);
    await page.goto("/settings/appearance");
    await expect(page.getByTestId("opening-links")).toHaveCount(0);
  });

  test("a link opened in a new tab is handed to the BattleLab tab already open", async ({
    context,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "tabs are a desktop-browser case");
    await mockApp(context);
    const first = await context.newPage();
    await mockRoster(first);
    await first.setViewportSize({ width: 1600, height: 1000 });
    // Any BattleLab page with the sidebar up — the landing, since Dashboard has none (#1233).
    await first.goto("/");
    await expect(first.locator(".sidebar")).toBeVisible();

    const second = await context.newPage();
    await mockRoster(second);
    await second.goto(`/mission?m=${MID}`);
    await expect(second.getByTestId("link-handoff-stub")).toBeVisible();
    await expect(second.getByTestId("link-handoff-stub")).toContainText("The mission");
    // The first tab opened it — a mission link never asks.
    await expect(first).toHaveURL(/\/mission/);

    // A session link: the first tab asks there, and this tab can still take it back.
    const third = await context.newPage();
    await mockRoster(third);
    await third.goto("/s/claude/s2");
    await expect(third.getByTestId("link-handoff-stub")).toBeVisible();
    await expect(dialog(first)).toBeVisible();
    await first.getByTestId("open-link-fullscreen").click();
    await expect(first).toHaveURL(/\/s\/claude\/s2$/);

    await third.getByRole("button", { name: "Open here instead" }).click();
    await expect(third.locator('[class*="panelHead"]')).toBeVisible();
  });

  test("a new tab opened FROM BattleLab stays a new tab — no hand-off, no prompt", async ({
    context,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "tabs are a desktop-browser case");
    // A ctrl/middle-click on a session row asks for a new tab on purpose. Its referrer is this
    // origin, which is what tells it apart from a link clicked in a mail or a chat.
    await mockApp(context);
    const first = await context.newPage();
    await mockRoster(first);
    await first.setViewportSize({ width: 1600, height: 1000 });
    // Any BattleLab page with the sidebar up — the landing, since Dashboard has none (#1233).
    await first.goto("/");
    await expect(first.locator(".sidebar")).toBeVisible();
    const [opened] = await Promise.all([
      context.waitForEvent("page"),
      first.evaluate(() => window.open("/s/claude/s1", "_blank")),
    ]);
    await mockRoster(opened);
    await expect(opened.locator('[class*="panelHead"]')).toBeVisible();
    await expect(opened.getByTestId("link-handoff-stub")).toHaveCount(0);
    await expect(dialog(opened)).toHaveCount(0);
    await expect(dialog(first)).toHaveCount(0);
    await expect(first).toHaveURL(/\/$/);
  });

  test("Share link copies the session's canonical URL when there is no share sheet", async ({
    page,
    context,
  }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.addInitScript(() => {
      // Deterministic: the clipboard path, whatever this platform's Chrome offers.
      Object.defineProperty(Navigator.prototype, "share", { value: undefined, configurable: true });
    });
    await mockApp(page);
    await page.addInitScript(() =>
      localStorage.setItem("battlelab.linkOpenMode", "fullscreen"),
    );
    await page.goto("/s/claude/s1");
    await clickHeadAction(page, "Share a link to this session");
    await expect(page.locator("[data-link-toast]")).toHaveText("Link copied");
    const copied = await page.evaluate(() => navigator.clipboard.readText());
    expect(copied).toBe(new URL("/s/claude/s1", page.url()).toString());
  });
});
