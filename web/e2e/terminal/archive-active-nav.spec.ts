// Archiving the ACTIVE session leaves its terminal route (#631). Runs against the isolated bench
// (mocked /api + in-page fake terminal server) — no backend. Red-before/green-after: without the
// SessionList navigate("/") on archive-success, archiving the session you're viewing removed its
// sidebar row but left the /s/:engine/:id route mounted, so its terminal socket kept
// reconnecting to a now-archived session (a background agent would relaunch-loop). The fix leaves
// the route → SessionView/Terminal unmount → the socket is disposed.
//
// NOTE: needs CI or a local browser to run (`npx playwright test e2e/terminal/archive-active-nav`);
// the dev host is too loaded to run Playwright here, so this spec ships unverified-in-env.
import { expect, test } from "@playwright/test";
import { expectTerminalShows, setupBench } from "./harness";

const SESSIONS = [{ engine: "claude", uuid: "aaa", title: "Archive Me" }];

// readyState of the live fake WS the terminal opened. The harness exposes the latest socket as
// `window.__BENCH_LAST_WS__`; this MUST be read inside `page.evaluate` (browser scope) — a
// module-scope helper isn't defined in the page (that was the `lastWs is not defined` failure).
const wsReadyState = (page: import("@playwright/test").Page) =>
  page.evaluate(
    () =>
      (window as unknown as { __BENCH_LAST_WS__?: { readyState?: number } }).__BENCH_LAST_WS__
        ?.readyState ?? null,
  );

test.beforeEach(async ({ page }) => {
  await setupBench(page, { sessions: SESSIONS });
  // The sidebar POSTs here on archive (#631); the bench's generic /api mocks don't cover it.
  // Registered AFTER setupBench so it wins over the broad `**/api/sessions**` route.
  await page.route(/\/api\/sessions\/[^/]+\/archive$/, (r) =>
    r.fulfill({ json: { id: "claude:aaa", archived: true } }),
  );
});

test("archiving the active session leaves the terminal route and disposes its socket (#631)", async ({
  page,
}, testInfo) => {
  await page.goto("/s/claude/aaa");
  await expectTerminalShows(page, "HIST claudeaaa END");

  // The live terminal socket is OPEN before we archive.
  expect(await wsReadyState(page)).toBe(1); // WebSocket.OPEN

  // Open the row's action menu (on mobile it lives behind the nav drawer) and archive.
  if (testInfo.project.name === "mobile") {
    await page.locator("header .navToggle").click();
    await expect(page.locator("aside.sidebar")).toBeVisible();
  }
  const row = page.locator("ul[aria-label] li").first();
  await row.hover();
  await row.getByRole("button", { name: "Session actions" }).click();
  await page.getByRole("menuitem", { name: /archive session/i }).click();

  // Left the session route for the new-session landing; the terminal is gone.
  await expect(page).toHaveURL(/\/$/);
  await expect(page.locator(".xterm-rows")).toHaveCount(0);

  // The socket was disposed — NOT left reconnecting to the now-archived session.
  await page.waitForTimeout(300);
  expect(await wsReadyState(page)).toBe(3); // WebSocket.CLOSED
});
