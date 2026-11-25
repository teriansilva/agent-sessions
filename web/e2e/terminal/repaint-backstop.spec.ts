// Blank-attach repaint backstop (#349 follow-up). Selecting an idle session sometimes
// painted fragments or nothing until the viewport was resized — only a REAL geometry
// change reliably makes winch-repaint agents redraw. When an attach delivers (almost)
// no bytes, the client jiggles rows−1 → rows; the bench models the agent's
// repaint-on-grid-change via wipeOnResizeChange, so the repaint marker appearing
// WITHOUT any user input is exactly the user-visible contract.
import { expect, test } from "@playwright/test";
import { setupBench } from "./harness";
import { ROSTER } from "../roster";
import { ACTIONS_MENU, clickHeadAction, closeHeadActionsMenu, headActionsReady, paneHead } from "../headActions";

const SESSIONS = [
  {
    engine: "claude",
    uuid: "aaaaaaaa-0000-4000-8000-00000000000a",
    title: "t",
  },
];
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

test("a large replay that leaves visible rows blank still repaints (#407)", async ({
  page,
}) => {
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

test("a large replay that painted only a sparse fragment repaints (#416)", async ({
  page,
}) => {
  // Operator screenshot: a substantial Claude frame was delivered but only the top few rows
  // rendered, the rest of the (tall) grid left blank — self-healing on the agent's next repaint.
  // visibleRowsBlank is FALSE (there IS text), so the #407 guard alone never recovers it. Model
  // it: 3 visible lines + ~6KB of no-op SGR bytes — a big replay that painted only a sparse grid.
  await setupBench(page, {
    sessions: SESSIONS,
    history: {
      [KEY]: ["line A\r\nline B\r\nline C\r\n" + "\x1b[m".repeat(2000)],
    },
    wipeOnResizeChange: true,
  });
  await page.goto("/s/claude/aaaaaaaa-0000-4000-8000-00000000000a");
  await expect(page.locator(".xterm-rows")).toContainText("LIVE (repainted)", {
    timeout: 15000,
  });
});

test("an attach that painted real content is NOT flicker-jiggled", async ({
  page,
}) => {
  await setupBench(page, {
    sessions: SESSIONS,
    history: {
      [KEY]: Array.from({ length: 80 }, (_, i) => `content line ${i}`),
    },
    wipeOnResizeChange: true, // a jiggle would wipe this content with the marker
  });
  await page.goto("/s/claude/aaaaaaaa-0000-4000-8000-00000000000a");
  await expect(page.locator(".xterm-rows")).toContainText("content line", {
    timeout: 15000,
  });
  await page.waitForTimeout(2500); // comfortably past the backstop window (timers are wall-clock)
  await expect(page.locator(".xterm-rows")).not.toContainText(
    "LIVE (repainted)",
  );
  await expect(page.locator(".xterm-rows")).toContainText("content line");
});

test("a caught-up reconnect (no delta) is never jiggled (Hermes #374)", async ({
  page,
}) => {
  // After a transient drop the client reconnects with have == total; the server
  // correctly sends nothing and the screen is already painted — the backstop must
  // not mistake the empty delta for a blank attach and wipe a good frame.
  await setupBench(page, {
    sessions: SESSIONS,
    history: {
      [KEY]: Array.from({ length: 80 }, (_, i) => `content line ${i}`),
    },
    wipeOnResizeChange: true,
  });
  await page.goto("/s/claude/aaaaaaaa-0000-4000-8000-00000000000a");
  await expect(page.locator(".xterm-rows")).toContainText("content line", {
    timeout: 15000,
  });
  await page.waitForTimeout(2000); // past the first-attach backstop window (rich → no jiggle)
  await page.evaluate(() => {
    const ws = (
      window as unknown as {
        __BENCH_LAST_WS__: {
          readyState: number;
          onclose: ((e: { code: number }) => void) | null;
        };
      }
    ).__BENCH_LAST_WS__;
    ws.readyState = 3;
    ws.onclose?.({ code: 1006 }); // transient drop → client auto-reconnects with have>0
  });
  await page.waitForTimeout(4000); // reconnect (0.6s backoff) + would-be backstop window
  await expect(page.locator(".xterm-rows")).not.toContainText(
    "LIVE (repainted)",
  );
  await expect(page.locator(".xterm-rows")).toContainText("content line");
});

test("the REPAINT button recovers a mid-session blank — no reconnect, no restart (#485)", async ({
  page,
}) => {
  // The first-attach backstop (#349/#407/#416) only arms once, on attach. When a winch-repaint
  // agent (Claude/Ink) clears its viewport mid-think and goes quiet, nothing auto-heals it — the
  // operator was left staring at a fragment. REPAINT is the manual, non-destructive recovery: it
  // fires the same rows−1→rows nudge to force the agent to redraw, WITHOUT killing the process.
  await setupBench(page, {
    sessions: SESSIONS,
    // Rich attach → the first-attach backstop stays silent (the "NOT flicker-jiggled" contract),
    // so the only "LIVE (repainted)" marker can come from the REPAINT click below.
    history: {
      [KEY]: Array.from({ length: 80 }, (_, i) => `content line ${i}`),
    },
    wipeOnResizeChange: true, // grid change → the bench agent repaints (marker)
  });
  await page.goto("/s/claude/aaaaaaaa-0000-4000-8000-00000000000a");
  await expect(page.locator(".xterm-rows")).toContainText("content line", {
    timeout: 15000,
  });
  await page.waitForTimeout(2000); // past the first-attach backstop window
  await expect(page.locator(".xterm-rows")).not.toContainText(
    "LIVE (repainted)",
  ); // no auto-jiggle

  // The agent clears its own viewport mid-think and stops emitting — a blank/fragment, NO reconnect.
  await page.evaluate(() => {
    const ws = (
      window as unknown as {
        __BENCH_LAST_WS__: {
          onmessage: ((e: { data: ArrayBuffer }) => void) | null;
        };
      }
    ).__BENCH_LAST_WS__;
    ws.onmessage?.({ data: new TextEncoder().encode("\x1b[2J\x1b[H").buffer });
  });
  await expect(page.locator(".xterm-rows")).not.toContainText("content line"); // screen went blank

  // Owner taps REPAINT → the agent redraws its current frame. No RESTART, no reconnect. On a phone
  // REPAINT lives in the header's Actions menu (#948 P6), so it is reached wherever it is.
  await clickHeadAction(page, /repaint/i);
  await expect(page.locator(".xterm-rows")).toContainText("LIVE (repainted)", {
    timeout: 5000,
  });
});

test("REPAINT is hidden for a read-only secondary viewer (#485)", async ({
  page,
}) => {
  // A geometry nudge would reach the shared PTY, so REPAINT is owner-only — the server already
  // drops a secondary's resize frames, and a dead button is worse than no button. The stream still
  // flows (a secondary is read-only, never blank) behind the take-over banner.
  await setupBench(page, {
    sessions: SESSIONS,
    history: {
      [KEY]: Array.from({ length: 12 }, (_, i) => `content line ${i}`),
    },
    role: "secondary",
  });
  await page.goto("/s/claude/aaaaaaaa-0000-4000-8000-00000000000a");
  await expect(page.locator(".xterm-rows")).toContainText("content line", {
    timeout: 15000,
  });
  await expect(page.getByText(/read-only|another tab/i)).toBeVisible(); // take-over banner
  // Owner-only → absent. On a phone every header action is inside the Actions menu (#948 P6), so the
  // bar alone would pass whether or not REPAINT is offered; look inside the menu too.
  if ((await headActionsReady(page)) === "collapsed") {
    await paneHead(page).getByRole("button", { name: ACTIONS_MENU }).click();
    await expect(page.getByRole("menu", { name: ACTIONS_MENU })).toBeVisible();
    await expect(page.getByRole("menuitem", { name: /repaint/i })).toHaveCount(0);
    await closeHeadActionsMenu(page);
  }
  await expect(page.getByRole("button", { name: /repaint/i })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /restart/i })).toHaveCount(0); // RESTART removed (#503)
});

test("an engine with terminal.repaint = winch is not resize-jiggled on open or window focus", async ({
  page,
}) => {
  // Antigravity (`agy`) already repaints its active viewport on `dtach -a -r winch`'s same-size
  // SIGWINCH, whereas any geometry change (`rows - 1 -> rows`) makes it emit `ESC[2J ESC[3J ESC[H`
  // and re-stream the entire conversation from turn 1 to the bottom. Neither the 800ms attach
  // backstop (on a sparse bottom prompt) nor the window-focus auto-repaint (#503) may jiggle its
  // rows.
  const agyId = "aaaaaaaa-0000-4000-8000-00000000000b";
  const agyKey = `antigravity:${agyId}`;
  await setupBench(page, {
    sessions: [{ engine: "antigravity", uuid: agyId, title: "agy" }],
    history: {
      // Sparse bottom viewport (> 4 KiB replay, only 3 visible lines) that triggers the #416
      // backstop on `repaint = "none"` engines.
      [agyKey]: ["agy prompt 1\r\nagy prompt 2\r\nagy prompt 3\r\n" + "\x1b[m".repeat(2000)],
    },
    wipeOnResizeChange: true,
  });
  await page.goto(`/s/antigravity/${agyId}`);
  await expect(page.locator(".xterm-rows")).toContainText("agy prompt 3", {
    timeout: 15000,
  });
  // Wait past the 800ms attach backstop window + trigger a window focus event (#503).
  await page.waitForTimeout(1200);
  await page.evaluate(() => {
    window.dispatchEvent(new Event("focus"));
  });
  await page.waitForTimeout(600);
  await expect(page.locator(".xterm-rows")).not.toContainText("LIVE (repainted)");
  await expect(page.locator(".xterm-rows")).toContainText("agy prompt 3");
});

test("a delayed /api/engines response still suppresses auto-jiggle on a winch engine while keeping manual REPAINT", async ({
  page,
}) => {
  // Returning browsers carry `battlelab.engineRuntimes.v1` in localStorage, so RuntimeGate mounts
  // `<Terminal>` before `/api/engines` finishes. If `/api/engines` lands after the 800ms attach
  // backstop (or after a window focus event), automatic repaint must fail closed until the roster
  // arrives rather than guessing `false` and resize-jiggling Antigravity. Manual REPAINT remains
  // available throughout.
  const agyId = "aaaaaaaa-0000-4000-8000-00000000000c";
  const agyKey = `antigravity:${agyId}`;
  await setupBench(page, {
    sessions: [{ engine: "antigravity", uuid: agyId, title: "agy" }],
    history: {
      [agyKey]: ["agy prompt 1\r\nagy prompt 2\r\nagy prompt 3\r\n" + "\x1b[m".repeat(2000)],
    },
    wipeOnResizeChange: true,
  });
  await page.route("**/api/engines", async (route) => {
    await new Promise((r) => setTimeout(r, 2500));
    await route.fulfill({ json: { engines: ROSTER.engines, problems: [] } });
  });
  await page.goto(`/s/antigravity/${agyId}`);
  await expect(page.locator(".xterm-rows")).toContainText("agy prompt 3", {
    timeout: 15000,
  });
  // Past the 800ms attach backstop while `/api/engines` is still in flight, plus a focus event:
  await page.waitForTimeout(1200);
  await page.evaluate(() => {
    window.dispatchEvent(new Event("focus"));
  });
  // Wait for `/api/engines` (2.5s) to resolve + any deferred callbacks or focus events to settle:
  await page.waitForTimeout(2000);
  await page.evaluate(() => {
    window.dispatchEvent(new Event("focus"));
  });
  await page.waitForTimeout(600);
  await expect(page.locator(".xterm-rows")).not.toContainText("LIVE (repainted)");
  await expect(page.locator(".xterm-rows")).toContainText("agy prompt 3");

  // Manual REPAINT remains available and functional.
  await clickHeadAction(page, /repaint/i);
  await expect(page.locator(".xterm-rows")).toContainText("LIVE (repainted)", {
    timeout: 5000,
  });
});

test("a failed initial /api/engines load keeps both automatic repaint paths suppressed", async ({
  page,
}) => {
  // If the initial `/api/engines` request fails (status: "failed", loaded: false), `repaintsOnWinch`
  // remains `undefined` so neither the 800ms attach backstop nor window focus / visibilitychange
  // (#503) resize-jiggles the session while the roster is unknown.
  const agyId = "aaaaaaaa-0000-4000-8000-00000000000d";
  const agyKey = `antigravity:${agyId}`;
  await setupBench(page, {
    sessions: [{ engine: "antigravity", uuid: agyId, title: "agy" }],
    history: {
      [agyKey]: ["agy prompt 1\r\nagy prompt 2\r\nagy prompt 3\r\n" + "\x1b[m".repeat(2000)],
    },
    wipeOnResizeChange: true,
  });
  await page.route("**/api/engines", async (route) => {
    await route.fulfill({ status: 500, body: "roster unavailable" });
  });
  await page.goto(`/s/antigravity/${agyId}`);
  await expect(page.locator(".xterm-rows")).toContainText("agy prompt 3", {
    timeout: 15000,
  });
  // Wait past the 800ms attach backstop after `/api/engines` has failed, then fire both focus and
  // visibilitychange (#503):
  await page.waitForTimeout(1200);
  await page.evaluate(() => {
    window.dispatchEvent(new Event("focus"));
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await page.waitForTimeout(600);
  await expect(page.locator(".xterm-rows")).not.toContainText("LIVE (repainted)");
  await expect(page.locator(".xterm-rows")).toContainText("agy prompt 3");
});
