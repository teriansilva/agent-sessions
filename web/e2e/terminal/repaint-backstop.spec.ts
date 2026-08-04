// Blank-attach repaint backstop (#349 follow-up). Selecting an idle session sometimes
// painted fragments or nothing until the viewport was resized — only a REAL geometry
// change reliably makes winch-repaint agents redraw. When an attach delivers (almost)
// no bytes, the client jiggles rows−1 → rows; the bench models the agent's
// repaint-on-grid-change via wipeOnResizeChange, so the repaint marker appearing
// WITHOUT any user input is exactly the user-visible contract.
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

  // Owner taps REPAINT → the agent redraws its current frame. No RESTART, no reconnect.
  await page.getByRole("button", { name: /repaint/i }).click();
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
  await expect(page.getByRole("button", { name: /repaint/i })).toHaveCount(0); // owner-only → absent
  await expect(page.getByRole("button", { name: /restart/i })).toHaveCount(0); // RESTART removed (#503)
});
