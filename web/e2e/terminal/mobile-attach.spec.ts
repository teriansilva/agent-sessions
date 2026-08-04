// #349: attach during a mobile viewport animation must not blank the terminal.
//
// Mechanism under test (client side): connectWhenStable's hard cap. Mobile address-bar /
// keyboard animations outlast the old ~1.5s budget, so the client connected MID-animation
// at an intermediate grid; the post-connect correcting resize then triggered the agent's
// repaint-on-grid-change, wiping the just-delivered scroll-up (the bench models that wipe
// via wipeOnResizeChange, the #299/#300 mechanism). Coarse-pointer devices now get a ~3s
// budget, so the connect happens at the settled grid and the history survives with no
// input ever sent — the user-visible symptom was "blank until I type in the compose box".
//
// On the pre-#349 client (MAX_FRAMES=90 for all pointers) this spec FAILS: the ~2.2s
// width jitter outlives the 1.5s cap, the wipe fires, and HIST...BEGIN is gone.
import { expect, test } from "@playwright/test";
import { expectTerminalShows, setupBench } from "./harness";

test("mobile: attach during viewport animation keeps history without any input (#349)", async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== "mobile",
    "coarse-pointer budget is mobile-only",
  );

  await setupBench(page, {
    sessions: [
      {
        engine: "claude",
        uuid: "m0b11e00-0000-4000-8000-000000000001",
        title: "m",
      },
    ],
    wipeOnResizeChange: true, // the agent-repaint wipe the old timing tripped
  });
  // Start at a non-final width, then animate width steps past the OLD 1.5s budget while
  // staying inside the new coarse-pointer one (~3s). Width changes move xterm's cols, so
  // a mid-animation connect is guaranteed to need a correcting (wiping) resize later.
  await page.setViewportSize({ width: 340, height: 700 });
  await page.goto("/s/claude/m0b11e00-0000-4000-8000-000000000001");
  // Steps every ~80ms — FASTER than the 8-quiet-frame (~130ms) stability window, like a
  // real animation that moves every frame — so the grid is never "quiet" until the end.
  // Total ~2.2s: past the old 1.5s cap (old client connects mid-animation → correcting
  // resize → wipe), inside the new ~3s coarse-pointer cap (new client connects settled).
  for (let i = 0; i < 27; i++) {
    const w = 340 + ((i * 7) % 48); // jitter 340..387, never repeating consecutively
    await page.setViewportSize({ width: w, height: 700 });
    await page.waitForTimeout(80);
  }
  await page.setViewportSize({ width: 390, height: 700 }); // settle at the final width
  // Settled. The terminal must show the session history — delivered on the single
  // stable-size connect — without the test ever typing a byte.
  await expectTerminalShows(page, "BEGIN");
  await expectTerminalShows(page, "END");
  // And the wipe marker must NOT have replaced it.
  await expect(page.locator(".xterm-rows")).not.toContainText(
    "LIVE (repainted)",
  );
});
