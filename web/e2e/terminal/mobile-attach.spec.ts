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
// On the pre-#349 client (MAX_FRAMES=90 for all pointers) this spec FAILS: the animation
// outlives the 90-frame cap, the wipe fires, and HIST...BEGIN is gone.
import { expect, test } from "@playwright/test";
import { expectTerminalShows, setupBench } from "./harness";

const KEY = "claude:m0b11e00-0000-4000-8000-000000000001";

/** Content-sized history (#918), exactly as `baseline.spec.ts` already does for #387.
 *
 *  The harness default serialises to **458 bytes** for this session key, and `Terminal.tsx`
 *  only skips the 800ms blank-attach backstop when the attach delivered **≥ 512**. So this
 *  fixture never qualified for the skip: the deliberate repaint jiggle fired on every run,
 *  independently of everything below. Padding to 1304 bytes puts it on the CONTENT side of
 *  that gate, which is the side #349 is about — the bug was a mobile terminal that went blank
 *  with real scrollback in it.
 *
 *  The harness default is deliberately left alone: other specs exercise the sub-512
 *  blank-attach path on purpose. */
const contentHistory = (key: string) => {
  const id = key.replace(/[^a-z0-9]/gi, "");
  const pad = "·".repeat(70);
  const lines = [`HIST ${id} BEGIN`];
  for (let i = 1; i <= 6; i++) lines.push(`HIST ${id} line ${i} ${pad}`);
  lines.push(`HIST ${id} END`, `LIVE ${id} $ `);
  return lines;
};

/** How many FRAMES the modelled animation runs for.
 *
 *  Both client budgets it has to sit between are frame-counted, so this is too:
 *  above the pre-#349 cap of 90 (old client connects mid-animation → wipe → this spec is a
 *  real regression test) and below the coarse-pointer cap of 180 (new client waits it out and
 *  attaches settled). 50 frames of margin on each side, and — unlike a wall-clock duration —
 *  the margin cannot be eaten by a slow host, because the units match. */
const ANIM_FRAMES = 130;

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
    history: { [KEY]: contentHistory(KEY) },
    wipeOnResizeChange: true, // the agent-repaint wipe the old timing tripped
  });

  // THE ANIMATION IS DRIVEN BY FRAMES, NOT BY WALL CLOCK (#918) — and that is the whole fix.
  //
  // The original drove it from the test side: 27 × `setViewportSize` + `waitForTimeout(80)`.
  // Every property it depends on is counted in FRAMES by the client — the 8-frame quiet window
  // and the 90/180-frame caps — so a wall-clock instrument was asserting a frame-domain
  // property, and the two clocks are related by a ratio the test does not control. Measured on
  // this host: 80ms spans 4–5 frames against a quiet window of 8. A 2× margin, and on CI it is
  // not enough — the grid looks QUIET between two steps, the client attaches mid-animation, and
  // the correcting resize wipes the history. Reproduced deterministically (2/2) by holding the
  // total duration inside the cap and widening the gap to 300ms: the failure is the quiet window
  // being satisfied between steps, NOT the frame cap being reached.
  //
  // So the jitter runs IN THE PAGE, one step per `requestAnimationFrame`. The gap is then
  // exactly one frame — never the eight that would end the settle early — and the total is
  // exactly `ANIM_FRAMES`, on any host at any frame rate. It moves the terminal's own box rather
  // than the viewport, which is the same thing as far as the code under test can tell: the
  // client re-`fit()`s each frame and compares the resulting grid. It never learns what moved.
  //
  // Installed as an INIT SCRIPT so it is already in place when the terminal mounts. Started
  // from the test side it would race the settle it exists to outlast — the client attaches
  // ~8 frames after mount, and an animation that begins later tests nothing.
  await page.addInitScript((frames) => {
    const w = window as unknown as { __animDone?: boolean };
    w.__animDone = false;
    let i = 0;
    const tick = () => {
      const host = document.querySelector(".xterm")?.parentElement as
        | HTMLElement
        | null;
      if (!host) {
        requestAnimationFrame(tick); // terminal not mounted yet
        return;
      }
      if (i >= frames) {
        host.style.maxWidth = ""; // settled: the grid stops moving, the quiet window can run
        w.__animDone = true;
        return;
      }
      // Alternating by a wide margin so EVERY frame changes the column count — a jitter too
      // small to move `cols` would let the quiet window run while the box is still moving.
      host.style.maxWidth = `${i % 2 === 0 ? 260 : 340}px`;
      i++;
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }, ANIM_FRAMES);

  await page.setViewportSize({ width: 390, height: 700 });
  await page.goto("/s/claude/m0b11e00-0000-4000-8000-000000000001");

  // Wait out the modelled animation in its own units.
  await page.waitForFunction(
    () => (window as unknown as { __animDone?: boolean }).__animDone === true,
    undefined,
    { timeout: 30_000 },
  );

  // Settled. The terminal must show the session history — delivered on the single
  // stable-size connect — without the test ever typing a byte.
  await expectTerminalShows(page, "BEGIN");
  await expectTerminalShows(page, "END");

  // AND IT MUST STILL BE TRUE AFTER THE BACKSTOP WINDOW HAS CLOSED (#918).
  //
  // `not.toContainText` samples a moment. The repaint backstop's window is 800ms, so an
  // instantaneous negative can pass and then be falsified a few hundred milliseconds later —
  // a green that means "we looked too early", not "history survived".
  await page.waitForTimeout(1200);
  await expect(page.locator(".xterm-rows")).not.toContainText(
    "LIVE (repainted)",
  );
  // …and the history is still the thing on screen, not merely "the wipe marker is absent".
  // A cleared terminal satisfies the negative above all by itself.
  await expectTerminalShows(page, "BEGIN");
  await expectTerminalShows(page, "END");
});
