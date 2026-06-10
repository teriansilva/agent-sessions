// Scroll-up lazy-load bench spec (#348 Phase 3). Entirely against the isolated bench
// (mocked /api incl. the new /history route + in-page fake WS) — no backend.
//
// Proves the issue's acceptance flow end-to-end through the real app:
//   scroll to top → loading pill (overlay, not a buffer row) → older lines prepended
//   ABOVE the existing content with the anchor held (the previously-top visible marker
//   line is still in the viewport) → at the oldest page, the start-of-history pill.
// Runs on BOTH projects: desktop scrolls with the mouse wheel; mobile drags the
// touch-capture surface (the same path a real phone uses).
import { test, expect, type Page } from "@playwright/test";
import { setupBench } from "./harness";

const SESSIONS = [{ engine: "claude", uuid: "aaa", title: "Lazy Alpha" }];

// Initial WS-delivered scroll-up: deep enough that real scrollback exists on every
// viewport. First line is the anchor marker the prepend must keep in view.
const ATTACH_TOP = "ATTACH-TOP-MARKER";
const attachHistory = [
  ATTACH_TOP,
  ...Array.from({ length: 110 }, (_, i) => `attach line ${i + 1}`),
  "LIVE tail $ ",
];

const olderPage = [
  "OLDER-PAGE-BEGIN",
  ...Array.from({ length: 30 }, (_, i) => `older line ${i + 1}`),
  "OLDER-PAGE-END",
].join("\r\n");

const oldestPage = ["OLDEST-PAGE-BEGIN", "the very first turn", "OLDEST-PAGE-END"].join("\r\n");

/** One scroll-up step: wheel on desktop; a touch drag on the capture surface on mobile
 *  (wheel events never reach xterm there — the touch layer overlays it). */
async function scrollUpOnce(page: Page, mobile: boolean) {
  if (!mobile) {
    await page.locator(".xterm").hover();
    await page.mouse.wheel(0, -800);
    return;
  }
  await page.evaluate(() => {
    const surface = document.querySelector('[data-touch-surface]') as HTMLElement;
    const rect = surface.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    let y = rect.top + 40;
    const ev = (type: string, py: number) => {
      const touch = new Touch({ identifier: 1, target: surface, clientX: x, clientY: py });
      return new TouchEvent(type, {
        touches: type === "touchend" ? [] : [touch],
        changedTouches: [touch],
        bubbles: true,
        cancelable: true,
      });
    };
    surface.dispatchEvent(ev("touchstart", y));
    for (let i = 0; i < 10; i++) {
      y += 45; // finger moves DOWN → scroll toward older output
      surface.dispatchEvent(ev("touchmove", y));
    }
    surface.dispatchEvent(ev("touchend", y));
  });
}

/** Keep scrolling up until `predicate` holds (bounded). */
async function scrollUpUntil(page: Page, mobile: boolean, predicate: () => Promise<boolean>) {
  for (let i = 0; i < 60; i++) {
    if (await predicate()) return;
    await scrollUpOnce(page, mobile);
    await page.waitForTimeout(60);
  }
  expect(await predicate(), "scroll-up never reached the expected state").toBe(true);
}

const rowsText = async (page: Page) => (await page.locator(".xterm-rows").textContent()) ?? "";

test.beforeEach(async ({ page }) => {
  await setupBench(page, {
    sessions: SESSIONS,
    history: { "claude:aaa": attachHistory },
    lazyPages: [
      // Page 1 held long enough to observe the loading pill.
      { ansi: olderPage, cursor: 7, has_more: true, delayMs: 700 },
      { ansi: oldestPage, cursor: null, has_more: false },
    ],
  });
});

test("scroll to top → loading pill → older page above with anchor held → start-of-history", async ({
  page,
}, testInfo) => {
  const mobile = testInfo.project.name === "mobile";
  await page.goto("/s/claude/aaa");
  await expect(page.locator(".xterm-rows")).toContainText("LIVE tail", { timeout: 5000 });

  // 1. Scroll to the very top of the delivered scrollback → the fetch fires and the
  //    loading pill (an overlay, not a buffer row) appears while the response is held.
  await scrollUpUntil(page, mobile, () =>
    page.locator('[data-hist-pill="loading"]').isVisible(),
  );
  await expect(page.locator('[data-hist-pill="loading"]')).toContainText(
    "loading older history",
  );

  // 2. Page lands: the pill clears and the ANCHOR HELD — the previously-top visible
  //    line is still in the viewport (the rewrite re-scrolled to it).
  await expect(page.locator('[data-hist-pill="loading"]')).toBeHidden({ timeout: 5000 });
  await expect(page.locator(".xterm-rows")).toContainText(ATTACH_TOP);
  // The older page is NOT in the viewport yet — it sits ABOVE the anchor.
  expect(await rowsText(page)).not.toContain("OLDER-PAGE-END");

  // 3. Scrolling further up reveals the prepended older page above the anchor.
  await scrollUpUntil(page, mobile, async () => (await rowsText(page)).includes("OLDER-PAGE"));

  // 4. Keep going: the next (oldest) page arrives and at its top the quiet
  //    start-of-history pill shows; the oldest content is reachable.
  await scrollUpUntil(page, mobile, () => page.locator('[data-hist-pill="end"]').isVisible());
  await expect(page.locator('[data-hist-pill="end"]')).toContainText("start of history");
  await scrollUpUntil(page, mobile, async () =>
    (await rowsText(page)).includes("OLDEST-PAGE-BEGIN"),
  );

  // 5. Scroll-to-bottom behaviour unchanged: the live tail is still reachable below.
  if (!mobile) {
    await page.locator(".xterm").hover();
    for (let i = 0; i < 60 && !(await rowsText(page)).includes("LIVE tail"); i++) {
      await page.mouse.wheel(0, 800);
    }
    await expect(page.locator(".xterm-rows")).toContainText("LIVE tail");
  }
});

test("history fetch error shows the retry pill; tap retries the same cursor", async ({
  page,
}, testInfo) => {
  const mobile = testInfo.project.name === "mobile";
  // Re-mock with a failing first call, then a good final page.
  await setupBench(page, {
    sessions: SESSIONS,
    history: { "claude:aaa": attachHistory },
    lazyPages: [
      { ansi: "", cursor: null, has_more: false, status: 500 },
      { ansi: oldestPage, cursor: null, has_more: false },
    ],
  });
  await page.goto("/s/claude/aaa");
  await expect(page.locator(".xterm-rows")).toContainText("LIVE tail", { timeout: 5000 });

  await scrollUpUntil(page, mobile, () => page.locator('[data-hist-pill="error"]').isVisible());
  await expect(page.locator('[data-hist-pill="error"]')).toContainText("tap to retry");

  // Tap-to-retry re-issues the request (same cursor — first call, so no `before`).
  await page.locator('[data-hist-pill="error"]').click();
  await expect(page.locator('[data-hist-pill="error"]')).toBeHidden({ timeout: 5000 });
  await scrollUpUntil(page, mobile, async () =>
    (await rowsText(page)).includes("OLDEST-PAGE-BEGIN"),
  );
  await scrollUpUntil(page, mobile, () => page.locator('[data-hist-pill="end"]').isVisible());
});
