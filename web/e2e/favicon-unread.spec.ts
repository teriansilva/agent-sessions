/** The tab's favicon (#1736): an unread dot while the bell has unread notifications or HQ has an
 *  unseen reply, gone once both are read; and the relay shell's own relay icon.
 *
 *  A real browser, because the claim is about the document the tab paints from: the one
 *  `<link rel="icon">` in the served page, its href swapped in place, and the file that href names
 *  actually being served by the build. jsdom can check an attribute, not that the URL resolves. */
import { expect, test, type Page } from "@playwright/test";

import { mockMissions } from "./mission-console";

const NOW = Math.floor(Date.now() / 1000);

const CONFIG = {
  csrf: "x",
  new_session_engines: ["claude"],
  terminal_backend: "ws",
  auth_mode: "none",
  overview_expanded: [],
  projects_hidden: [],
  pulse: { configured: true },
};

const NOTE = {
  id: "n1",
  title: "claude needs a decision",
  reason: "waiting on a menu choice",
  project: "agent-sessions",
  engine: "claude",
  session_id: "claude:abc",
  action_id: "a1",
  ts: NOW - 60,
  read: false,
};

interface Unread {
  bell: number;
  ask: boolean;
}

async function mockApp(page: Page, state: Unread) {
  await page.route("**/api/config", (r) => r.fulfill({ json: CONFIG }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "t" } }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route("**/api/pulse/orchestrator", (r) =>
    r.fulfill({ status: 500, json: { detail: "off" } }),
  );
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: {
        cache_version: 2,
        generated_at: NOW,
        window_days: 3,
        scan_depth: "fast",
        input_fingerprint: null,
        synthesis_skipped: false,
        banner: null,
        cards: [],
      },
    }),
  );
  const list = () => ({
    notifications: [{ ...NOTE, read: state.bell === 0 }],
    unread: state.bell,
    uncertain: 0,
    settled: [],
  });
  await page.route("**/api/pulse/notifications/read", (r) => {
    state.bell = 0;
    return r.fulfill({ json: { ...list(), marked: 1 } });
  });
  await page.route(/\/api\/pulse\/notifications$/, (r) => r.fulfill({ json: list() }));
  await page.route("**/api/ask/unread", (r) =>
    r.fulfill({ json: { unread: state.ask, conversations: state.ask ? 1 : 0 } }),
  );
}

const iconHref = (page: Page) =>
  page.locator('link[rel~="icon"]').evaluateAll((links) => links.map((l) => l.getAttribute("href")));

/** The href the page shows is the ONE icon link, and the build serves it as an SVG. */
async function expectIcon(page: Page, href: string) {
  await expect.poll(() => iconHref(page)).toEqual([href]);
  const res = await page.request.get(href);
  expect(res.status(), href).toBe(200);
  expect(res.headers()["content-type"], href).toContain("image/svg+xml");
}

test("an unread bell notification puts the dot on the favicon; Mark all read takes it off", async ({
  page,
}) => {
  const state: Unread = { bell: 1, ask: false };
  await mockApp(page, state);
  await mockMissions(page);
  await page.goto("/mission");

  await expectIcon(page, "/favicon-unread.svg");

  await page.getByRole("button", { name: /^Notifications/ }).click();
  await page.getByRole("button", { name: "Mark all read" }).click();

  await expectIcon(page, "/favicon.svg");
});

test("an unseen HQ reply puts the dot on the favicon, and it goes once nothing is unseen", async ({
  page,
}) => {
  const state: Unread = { bell: 0, ask: true };
  await mockApp(page, state);
  await mockMissions(page);
  await page.goto("/mission");

  await expectIcon(page, "/favicon-unread.svg");
  // The HQ icon says the same thing at the same time: one read, two readers.
  await expect(page.locator('.hud-topbar [data-testid="ask-unread-dot"]')).toBeVisible();

  // Seen elsewhere; this tab re-reads when it comes back to the foreground.
  state.ask = false;
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));

  await expectIcon(page, "/favicon.svg");
  await expect(page.locator('.hud-topbar [data-testid="ask-unread-dot"]')).toHaveCount(0);
});

test("the relay connect page carries the relay favicon, and the build serves it", async ({ page }) => {
  await page.goto("/connect.html");
  await expectIcon(page, "/favicon-relay.svg");
});
