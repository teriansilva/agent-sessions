// Auto-repaint on resurface (#503 follow-up). visibilitychange only fires on a tab
// show/hide — it never fires when you alt-tab BACK to another app/window while the tab stayed
// visibilityState "visible", nor on a bfcache restore. Those left a stale/frozen frame that only
// the manual REPAINT button recovered. The owner terminal now also nudges (rows−1 → rows) on
// window 'focus' and 'pageshow'; the bench models the agent's repaint-on-grid-change via
// wipeOnResizeChange, so the "LIVE (repainted)" marker appearing WITHOUT user input after a
// resurface is exactly the user-visible contract. Rich history keeps the blank-attach backstop
// silent so these tests isolate the resurface trigger.
import { expect, test } from "@playwright/test";
import { setupBench } from "./harness";

const SESSIONS = [
  {
    engine: "claude",
    uuid: "aaaaaaaa-0000-4000-8000-00000000000a",
    title: "t",
  },
];
const KEY = "claude:aaaaaaaa-0000-4000-8000-00000000000a";
const RICH = Array.from({ length: 80 }, (_, i) => `content line ${i}`);

// We dispatch the window 'focus' / 'pageshow' events directly — they ARE the listener's trigger,
// and orchestrating a true OS window blur/refocus is unreliable headless. (Contrast the FAB test,
// where synthetic events would have bypassed hit-testing; here there is no hit-test to bypass.)

test("window refocus repaints a stale owner frame — no manual REPAINT", async ({
  page,
}) => {
  await setupBench(page, {
    sessions: SESSIONS,
    history: { [KEY]: RICH },
    wipeOnResizeChange: true,
  });
  await page.goto("/s/claude/aaaaaaaa-0000-4000-8000-00000000000a");
  await expect(page.locator(".xterm-rows")).toContainText("content line", {
    timeout: 15000,
  });

  // Baseline: past the blank-attach backstop window, a healthy rich frame is NOT jiggled.
  await page.waitForTimeout(2500);
  await expect(page.locator(".xterm-rows")).not.toContainText(
    "LIVE (repainted)",
  );

  // Alt-tab back to the window (tab stayed "visible", so visibilitychange never fires) → the new
  // 'focus' trigger nudges the agent, which repaints.
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(page.locator(".xterm-rows")).toContainText("LIVE (repainted)", {
    timeout: 8000,
  });
});

test("pageshow (bfcache restore) repaints a stale owner frame", async ({
  page,
}) => {
  await setupBench(page, {
    sessions: SESSIONS,
    history: { [KEY]: RICH },
    wipeOnResizeChange: true,
  });
  await page.goto("/s/claude/aaaaaaaa-0000-4000-8000-00000000000a");
  await expect(page.locator(".xterm-rows")).toContainText("content line", {
    timeout: 15000,
  });
  await page.waitForTimeout(2500);
  await expect(page.locator(".xterm-rows")).not.toContainText(
    "LIVE (repainted)",
  );

  await page.evaluate(() => window.dispatchEvent(new Event("pageshow")));
  await expect(page.locator(".xterm-rows")).toContainText("LIVE (repainted)", {
    timeout: 8000,
  });
});

test("a read-only secondary viewer does NOT repaint on refocus", async ({
  page,
}) => {
  // The resurface repaint is owner-only (a secondary is read-only and never drives the pty).
  await setupBench(page, {
    sessions: SESSIONS,
    history: { [KEY]: RICH },
    wipeOnResizeChange: true,
    role: "secondary",
  });
  await page.goto("/s/claude/aaaaaaaa-0000-4000-8000-00000000000a");
  await expect(page.locator(".xterm-rows")).toContainText("content line", {
    timeout: 15000,
  });
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await page.waitForTimeout(1500);
  await expect(page.locator(".xterm-rows")).not.toContainText(
    "LIVE (repainted)",
  );
});
