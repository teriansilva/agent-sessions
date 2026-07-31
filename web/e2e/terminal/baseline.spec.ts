// Baseline console behavior, locked (#301 slice 1). These run entirely against the isolated bench
// (mocked /api + in-page fake terminal server) — no backend, no prod/staging reach. They pin the
// behavior the #300 fix established: switching and reloading keep the right session's scroll-up
// intact, with no fragments and no spurious resize wipe. A regression here is a real regression.
import { test, expect } from "@playwright/test";
import { setupBench, expectTerminalShows, expectTerminalHidden } from "./harness";

const SESSIONS = [
  { engine: "claude", uuid: "aaa", title: "Session Alpha" },
  { engine: "claude", uuid: "bbb", title: "Session Beta" },
];

// defaultHistory keys strip non-alphanumerics: "claude:aaa" → marker id "claudeaaa".
const A_END = "HIST claudeaaa END";
const B_END = "HIST claudebbb END";

/** Content-sized history (fixes #387): same markers as the harness default, but each
 *  line padded so the per-session replay exceeds the #374 blank-attach backstop's 512-byte
 *  threshold — the #300 contract is about CONTENT sessions, and a sub-512B bench history
 *  made the deliberate blank-attach jiggle race this spec's "no repaint" assertion. */
const contentHistory = (key: string) => {
  const id = key.replace(/[^a-z0-9]/gi, "");
  const pad = "·".repeat(70);
  const lines = [`HIST ${id} BEGIN`];
  for (let i = 1; i <= 6; i++) lines.push(`HIST ${id} line ${i} ${pad}`);
  lines.push(`HIST ${id} END`, `LIVE ${id} $ `);
  return lines;
};

test.beforeEach(async ({ page }) => {
  await setupBench(page, {
    sessions: SESSIONS,
    history: Object.fromEntries(
      SESSIONS.map((s) => {
        const k = `${s.engine}:${s.uuid}`;
        return [k, contentHistory(k)];
      }),
    ),
  });
});

test("live render: the session's scroll-up is shown on open", async ({ page }) => {
  await page.goto("/s/claude/aaa");
  await expectTerminalShows(page, A_END);
  // The fake server served it — if a real WS had escaped, the tripwire would have thrown.
});

test("in-app switch keeps the right history, no fragments (#300)", async ({ page }, testInfo) => {
  // The switch path here clicks a sidebar row; on mobile that lives behind a nav drawer (a separate
  // UI path covered elsewhere). The #300 connect-stability fix it exercises is viewport-agnostic.
  test.skip(testInfo.project.name === "mobile", "sidebar switch is drawer-gated on mobile");
  await page.goto("/s/claude/aaa");
  await expectTerminalShows(page, A_END);

  // Switch through the router (the bug surface) — NOT a reload.
  await page.locator('a[href$="/s/claude/bbb"]').click();
  await expectTerminalShows(page, B_END);
  await expectTerminalHidden(page, A_END);

  // ...and back. Alpha's full history returns intact (no wiped/fragmented render).
  await page.locator('a[href$="/s/claude/aaa"]').click();
  await expectTerminalShows(page, A_END);
  await expectTerminalHidden(page, B_END);
});

test("reload keeps the session's history", async ({ page }) => {
  await page.goto("/s/claude/aaa");
  await expectTerminalShows(page, A_END);
  await page.reload();
  await expectTerminalShows(page, A_END);
});

test("connect happens once the grid is stable — no resize wipe on attach (#300)", async ({ page }) => {
  // wipeOnResizeChange is on (models the real agent repaint). If the client connected before layout
  // settled and then sent a correcting resize, the server would wipe and A_END would vanish. It must not.
  await page.goto("/s/claude/aaa");
  await expectTerminalShows(page, A_END);
  // Give any stray post-connect resize a chance to fire, then re-assert it's still there.
  await page.waitForTimeout(400);
  await expectTerminalShows(page, A_END);
  await expect(page.locator(".xterm-rows")).not.toContainText("repainted");
});

test("TEETH + current limitation: a post-connect grid change repaints the live view (#301)", async ({ page }) => {
  // Two jobs at once:
  //  1. Proves the bench has teeth — it can actually detect a wipe end-to-end through the real app
  //     (if this passed vacuously, the green specs above would be meaningless).
  //  2. Locks the real CURRENT behavior: the live xterm has no scroll-up backing store, so a genuine
  //     resize makes the agent repaint and the prior screen is gone. This is exactly the gap #301's
  //     lazy history pane (slice 3) closes — when that lands, this test gets revisited.
  await page.goto("/s/claude/aaa");
  await expectTerminalShows(page, A_END);
  // A real grid change (halve the width) → fit → the client sends a size-changing resize → wipe.
  await page.setViewportSize({ width: 640, height: 800 });
  await expectTerminalShows(page, "repainted");
  await expectTerminalHidden(page, A_END);
});

test("isolation tripwire: a real WebSocket is a hard failure", async ({ page }) => {
  // Self-test of the gate: without the fake WS the app would open a real WS to /ws/term/... and the
  // page.on('websocket') tripwire would throw. Here the fake WS is installed, so the page is clean —
  // we assert the terminal came up purely from the mock.
  await page.goto("/s/claude/aaa");
  await expect(page.locator(".xterm-rows")).toBeVisible();
  await expectTerminalShows(page, A_END);
});
