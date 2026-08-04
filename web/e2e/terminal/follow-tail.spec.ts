// Follow-the-tail behaviour, locked. Entirely against the isolated bench (mocked /api +
// in-page fake WS) — no backend.
//
// The bug this guards: the console followed the live tail too aggressively. While the
// initial attach replay's tail-lock was held, EVERY output chunk force-scrolled to the
// bottom, and the lock only ever released on a wheel/touch/key gesture. So a desktop user
// who scrolled up by dragging the scrollbar (no such gesture) was yanked back to the bottom
// by the next line of output, and there was no scroll-to-bottom button on desktop to get
// back deliberately. Now: follow only when the viewport is already at the tail, and show the
// button on every pointer type when it isn't.
import { test, expect, type Page } from "@playwright/test";
import { setupBench, pushOutput } from "./harness";

const SESSIONS = [{ engine: "claude", uuid: "aaa", title: "Tail Alpha" }];

// Deep enough that real scrollback exists on every viewport; first line is the marker we
// expect to STAY in view after streaming while scrolled up. Lines are padded so the replay
// is comfortably non-blank/non-sparse — avoiding the bench's deliberate blank-attach jiggle
// race (same reason baseline.spec pads its history).
const TOP_MARKER = "FOLLOW-TOP-MARKER";
const pad = "·".repeat(70);
const tailHistory = [
  `${TOP_MARKER} ${pad}`,
  ...Array.from({ length: 120 }, (_, i) => `attach line ${i + 1} ${pad}`),
  "LIVE tail $ ",
];

// A streamed line that lands at the very bottom — off-screen while scrolled to the top.
const STREAMED = "STREAMED-WHILE-READING-XYZ";
const streamChunk =
  "\r\n" + Array.from({ length: 6 }, () => STREAMED).join("\r\n") + "\r\n";

const rowsText = async (page: Page) =>
  (await page.locator(".xterm-rows").textContent()) ?? "";

/** Scroll up via the native xterm viewport scrollbar — NOT a wheel/touch/key gesture. This is the
 *  exact path that the old tail-lock never noticed (it only released on a real gesture). */
async function scrollUpViaScrollbar(page: Page) {
  await page.locator(".xterm-viewport").evaluate((el) => {
    el.scrollTop = 0;
    el.dispatchEvent(new Event("scroll", { bubbles: true }));
  });
}

test.beforeEach(async ({ page }) => {
  await setupBench(page, {
    sessions: SESSIONS,
    history: { "claude:aaa": tailHistory },
    // This spec exercises follow-the-tail, not resize-wipe; pin the bench so a stray repaint
    // nudge can never wipe the injected history out from under an assertion.
    wipeOnResizeChange: false,
  });
});

test("streaming output does not yank a scrolled-up viewport back to the tail", async ({
  page,
}) => {
  await page.goto("/s/claude/aaa");
  await expect(page.locator(".xterm-rows")).toContainText("LIVE tail", {
    timeout: 5000,
  });
  // Let the initial attach replay settle so the #407 tail-lock has released into steady state.
  await page.waitForTimeout(1000);

  // Scroll up with the scrollbar (no gesture). The top marker comes into view and the
  // scroll-to-bottom button appears.
  await scrollUpViaScrollbar(page);
  await expect(page.locator(".xterm-rows")).toContainText(TOP_MARKER);
  await expect(
    page.getByRole("button", { name: /scroll to bottom/i }),
  ).toBeVisible();

  // The agent streams more output. The viewport must STAY where the reader put it — the new
  // line lands off-screen at the bottom, the top marker is still visible, and the button stays.
  await pushOutput(page, streamChunk);
  await page.waitForTimeout(300);
  await expect(page.locator(".xterm-rows")).toContainText(TOP_MARKER);
  expect(await rowsText(page)).not.toContain(STREAMED);
  await expect(
    page.getByRole("button", { name: /scroll to bottom/i }),
  ).toBeVisible();

  // Clicking the button jumps to the live tail (the streamed line) and dismisses the button.
  await page.getByRole("button", { name: /scroll to bottom/i }).click();
  await expect(page.locator(".xterm-rows")).toContainText(STREAMED);
  await expect(
    page.getByRole("button", { name: /scroll to bottom/i }),
  ).toBeHidden();
});

test("the scroll-to-bottom button is available on desktop, hidden while on the tail", async ({
  page,
}, testInfo) => {
  // The button used to be gated behind coarse pointers (mobile only). It now shows on desktop too.
  test.skip(
    testInfo.project.name !== "desktop",
    "asserts the desktop (fine-pointer) gate",
  );
  await page.goto("/s/claude/aaa");
  await expect(page.locator(".xterm-rows")).toContainText("LIVE tail", {
    timeout: 5000,
  });
  await page.waitForTimeout(1000);

  // On the tail: no button.
  await expect(
    page.getByRole("button", { name: /scroll to bottom/i }),
  ).toBeHidden();

  // Off the tail: the button appears.
  await scrollUpViaScrollbar(page);
  await expect(
    page.getByRole("button", { name: /scroll to bottom/i }),
  ).toBeVisible();
});

test("output keeps following the tail when the viewport is already at the bottom", async ({
  page,
}) => {
  // The flip side: a reader sitting on the live tail still follows new output automatically.
  await page.goto("/s/claude/aaa");
  await expect(page.locator(".xterm-rows")).toContainText("LIVE tail", {
    timeout: 5000,
  });
  await page.waitForTimeout(1000);

  await pushOutput(page, streamChunk);
  await expect(page.locator(".xterm-rows")).toContainText(STREAMED);
  await expect(
    page.getByRole("button", { name: /scroll to bottom/i }),
  ).toBeHidden();
});
