// Blank-attach repaint backstop (#349 follow-up). Selecting an idle session sometimes
// painted fragments or nothing until the viewport was resized — only a REAL geometry
// change reliably makes winch-repaint agents redraw. When an attach delivers (almost)
// no bytes, the client jiggles rows−1 → rows; the bench models the agent's
// repaint-on-grid-change via wipeOnResizeChange, so the repaint marker appearing
// WITHOUT any user input is exactly the user-visible contract.
import { expect, test } from "@playwright/test";
import { setupBench } from "./harness";

const SESSIONS = [{ engine: "claude", uuid: "aaaaaaaa-0000-4000-8000-00000000000a", title: "t" }];
const KEY = "claude:aaaaaaaa-0000-4000-8000-00000000000a";

test("a (nearly) blank attach repaints by itself — no input, no manual resize", async ({
  page,
}) => {
  await setupBench(page, {
    sessions: SESSIONS,
    history: { [KEY]: [] }, // idle session, empty replay → the broken case
    wipeOnResizeChange: true, // grid change → agent repaints (bench marker)
  });
  await page.goto("/s/claude/aaaaaaaa-0000-4000-8000-00000000000a");
  // Within ~1.5s the rows jiggle must have fired and the agent's repaint landed.
  // Generous budget: under runner load the SPA boot + xterm mount alone can eat
  // several seconds before the 800ms backstop window even starts.
  await expect(page.locator(".xterm-rows")).toContainText("LIVE (repainted)", {
    timeout: 15000,
  });
});

test("a large replay that leaves visible rows blank still repaints (#407)", async ({ page }) => {
  await setupBench(page, {
    sessions: SESSIONS,
    // Models the production report: content can briefly paint during replay, then a
    // later clear leaves the final visible rows blank. Byte-count-only logic would
    // consider this a rich attach and never recover.
    history: { [KEY]: ["brief content\r\n\x1b[2J\x1b[H" + " ".repeat(900)] },
    wipeOnResizeChange: true,
  });
  await page.goto("/s/claude/aaaaaaaa-0000-4000-8000-00000000000a");
  await expect(page.locator(".xterm-rows")).toContainText("LIVE (repainted)", {
    timeout: 15000,
  });
});

test("an attach that painted real content is NOT flicker-jiggled", async ({ page }) => {
  await setupBench(page, {
    sessions: SESSIONS,
    history: { [KEY]: Array.from({ length: 80 }, (_, i) => `content line ${i}`) },
    wipeOnResizeChange: true, // a jiggle would wipe this content with the marker
  });
  await page.goto("/s/claude/aaaaaaaa-0000-4000-8000-00000000000a");
  await expect(page.locator(".xterm-rows")).toContainText("content line", { timeout: 15000 });
  await page.waitForTimeout(2500); // comfortably past the backstop window (timers are wall-clock)
  await expect(page.locator(".xterm-rows")).not.toContainText("LIVE (repainted)");
  await expect(page.locator(".xterm-rows")).toContainText("content line");
});

test("a caught-up reconnect (no delta) is never jiggled (Hermes #374)", async ({ page }) => {
  // After a transient drop the client reconnects with have == total; the server
  // correctly sends nothing and the screen is already painted — the backstop must
  // not mistake the empty delta for a blank attach and wipe a good frame.
  await setupBench(page, {
    sessions: SESSIONS,
    history: { [KEY]: Array.from({ length: 80 }, (_, i) => `content line ${i}`) },
    wipeOnResizeChange: true,
  });
  await page.goto("/s/claude/aaaaaaaa-0000-4000-8000-00000000000a");
  await expect(page.locator(".xterm-rows")).toContainText("content line", { timeout: 15000 });
  await page.waitForTimeout(2000); // past the first-attach backstop window (rich → no jiggle)
  await page.evaluate(() => {
    const ws = (window as unknown as { __BENCH_LAST_WS__: { readyState: number; onclose: ((e: { code: number }) => void) | null } }).__BENCH_LAST_WS__;
    ws.readyState = 3;
    ws.onclose?.({ code: 1006 }); // transient drop → client auto-reconnects with have>0
  });
  await page.waitForTimeout(4000); // reconnect (0.6s backoff) + would-be backstop window
  await expect(page.locator(".xterm-rows")).not.toContainText("LIVE (repainted)");
  await expect(page.locator(".xterm-rows")).toContainText("content line");
});
