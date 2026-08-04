// #559: selecting text must not scroll the terminal viewport around. Two facets, both against the
// isolated bench (mocked /api + in-page fake WS — no backend):
//
//  A/B. Live output arriving WHILE a selection is in progress must not follow the tail and yank the
//       view (and the highlighted text) away — desktop (mouse drag) + mobile (long-press select).
//  C.   Dragging a selection PAST the top edge must not auto-scroll the viewport out from under the
//       selection (xterm's drag-select edge auto-scroll) — desktop.
//
// Real browser required: jsdom models neither xterm's selection/drag-scroll nor the viewport's
// scrollTop, which is exactly where this bug lives. Red before the fix (the view drifts), green
// after (the viewport is pinned while a selection is active).
import { test, expect, type Page } from "@playwright/test";
import { setupBench, pushOutput } from "./harness";

const SESSIONS = [{ engine: "claude", uuid: "aaa", title: "Sel Alpha" }];

// Deep enough that real scrollback exists on every viewport, so the viewport's scrollTop is a
// meaningful signal. Padded so the replay is comfortably non-blank/non-sparse (avoids the bench's
// deliberate blank-attach jiggle race — same reason follow-tail.spec pads its history).
const pad = "·".repeat(70);
const selHistory = [
  `SEL-TOP-MARKER ${pad}`,
  ...Array.from({ length: 120 }, (_, i) => `select line ${i + 1} ${pad}`),
  "LIVE tail $ ",
];

const STREAMED = "STREAMED-WHILE-SELECTING-XYZ";
const streamChunk =
  "\r\n" + Array.from({ length: 6 }, () => STREAMED).join("\r\n") + "\r\n";

const vpScrollTop = (page: Page) =>
  page
    .locator(".xterm-viewport")
    .evaluate((el) => (el as HTMLElement).scrollTop);

test.beforeEach(async ({ page }) => {
  await setupBench(page, {
    sessions: SESSIONS,
    history: { "claude:aaa": selHistory },
    // This spec is about selection vs scroll, not resize-wipe; pin the bench so a stray repaint
    // nudge can never wipe the injected history out from under an assertion.
    wipeOnResizeChange: false,
  });
  await page.goto("/s/claude/aaa");
  await expect(page.locator(".xterm-rows")).toContainText("LIVE tail", {
    timeout: 5000,
  });
  // Let the initial attach replay settle so the #407 tail-lock has released into steady state
  // (otherwise output follows regardless of selection).
  await page.waitForTimeout(1000);
});

test("streaming output does not drift the viewport out from under a mouse selection", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "mouse-drag selection");

  // Start a real drag-selection gesture over the visible rows and keep the mouse DOWN — a selection
  // is in progress for its duration (the pin is armed by the trusted left-press, exactly the point
  // at which a drag-select may begin; whether the headless canvas paints selection cells varies).
  const box = (await page.locator(".xterm-screen").boundingBox())!;
  const y = box.y + box.height * 0.5;
  await page.mouse.move(box.x + 20, y);
  await page.mouse.down();
  await page.mouse.move(box.x + 220, y, { steps: 10 });

  // With the mouse still held, the agent streams output. The viewport must STAY put (the streamed
  // line lands off-screen at the bottom); on the old code, follow-the-tail scrolled it away.
  const before = await vpScrollTop(page);
  await pushOutput(page, streamChunk);
  await page.waitForTimeout(300);
  const after = await vpScrollTop(page);
  await page.mouse.up();

  expect(after).toBe(before); // pinned — did not follow the tail
  expect(await page.locator(".xterm-rows").textContent()).not.toContain(
    STREAMED,
  ); // stayed off-screen
});

test("streaming output does not drift the viewport out from under a touch selection", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "touch long-press selection");

  const surface = page.locator("[data-touch-surface]");
  await expect(surface).toBeVisible();
  // Press-and-hold over a visible row → enter select-mode + seed a word selection (the #415 path).
  await surface.evaluate((el) => {
    const r = el.getBoundingClientRect();
    const cellH = r.height / 24;
    const x = Math.round(r.x + r.width * 0.3);
    const y = Math.round(r.y + cellH * 0.5);
    const touch = new Touch({
      identifier: 1,
      target: el,
      clientX: x,
      clientY: y,
    });
    el.dispatchEvent(
      new TouchEvent("touchstart", {
        cancelable: true,
        bubbles: true,
        touches: [touch],
      }),
    );
    return new Promise<void>((resolve) =>
      setTimeout(() => {
        el.dispatchEvent(
          new TouchEvent("touchend", {
            cancelable: true,
            bubbles: true,
            touches: [],
          }),
        );
        resolve();
      }, 600),
    );
  });
  await expect
    .poll(
      () =>
        page.evaluate(
          () => (window.getSelection()?.toString() ?? "").trim().length,
        ),
      {
        timeout: 3000,
      },
    )
    .toBeGreaterThan(0);

  // Selection is active (select-mode stays on because the lift left a non-empty selection). Streamed
  // output must not follow the tail and scroll the selected text away.
  const before = await vpScrollTop(page);
  await pushOutput(page, streamChunk);
  await page.waitForTimeout(300);
  const after = await vpScrollTop(page);

  expect(after).toBe(before); // pinned — did not follow the tail
});

test("drag-selecting past the top edge does not auto-scroll the viewport", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "mouse-drag edge auto-scroll");

  const box = (await page.locator(".xterm-screen").boundingBox())!;
  // Begin a selection mid-screen, then drag ABOVE the terminal and hold — xterm's SelectionService
  // would auto-scroll the viewport upward while the button is held outside the rows.
  await page.mouse.move(box.x + 20, box.y + box.height * 0.6);
  await page.mouse.down();
  const before = await vpScrollTop(page);
  await page.mouse.move(box.x + 140, box.y - 40, { steps: 6 });
  await page.waitForTimeout(500); // let several auto-scroll ticks fire
  const after = await vpScrollTop(page);
  await page.mouse.up();

  expect(after).toBe(before); // pinned — the drag did not auto-scroll the viewport
});
