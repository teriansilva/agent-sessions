import { expect, test } from "@playwright/test";

// Real-browser repro for the mobile "jump to bottom" FAB (↓): on a phone, *tapping* the FAB
// must scroll the terminal back to the live tail. The bug: the coarse-pointer touch-capture
// overlay wins the hit-test over the FAB, and its capture-phase `touchstart`+preventDefault
// suppresses the FAB's synthesized click — so a tap just focuses the keyboard and the view
// never moves. jsdom can't model this (no real layout / hit-test / click synthesis), so this
// is a real-touch (Pixel 7) test driven through Playwright's actual input pipeline.

const FAKE_WS = `
window.WebSocket = class {
  constructor(url) {
    this.url = url; this.readyState = 0; this.binaryType = "blob";
    setTimeout(() => {
      this.readyState = 1;
      if (this.onopen) this.onopen();
      let s = "";
      for (let i = 0; i < 400; i++) s += "line " + i + " ------------------------------\\r\\n";
      const buf = new TextEncoder().encode(s).buffer;
      if (this.onmessage) this.onmessage({ data: buf });
      if (this.onmessage) this.onmessage({ data: JSON.stringify({ t: "seq", n: s.length }) });
    }, 30);
  }
  send() {}
  close() { this.readyState = 3; if (this.onclose) this.onclose({ code: 1000 }); }
};
`;

test("tapping the scroll-to-bottom FAB jumps to the live tail", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "touch-only behavior");

  await page.addInitScript(FAKE_WS);
  await page.goto("/s/claude/fabtest");

  const viewport = page.locator(".xterm-viewport");
  await expect(viewport).toBeVisible();
  await expect
    .poll(async () => viewport.evaluate((el) => el.scrollHeight - el.clientHeight), { timeout: 5000 })
    .toBeGreaterThan(100);

  // One finger-drag DOWN over the capture overlay = scroll UP into history. Each call is a
  // self-contained touchstart→moves→touchend gesture on [data-touch-surface].
  const dragUpOnce = () =>
    page.locator("[data-touch-surface]").evaluate((el) => {
      const r = el.getBoundingClientRect();
      const cx = Math.round(r.x + r.width / 2);
      const touch = (y: number) =>
        new Touch({ identifier: 1, target: el, clientX: cx, clientY: Math.round(y) });
      const fire = (type: string, y: number) =>
        el.dispatchEvent(
          new TouchEvent(type, {
            cancelable: true,
            bubbles: true,
            touches: type === "touchend" ? [] : [touch(y)],
          }),
        );
      let y = r.y + r.height * 0.25;
      fire("touchstart", y);
      for (let i = 0; i < 12; i++) {
        y += r.height * 0.05;
        fire("touchmove", y);
      }
      fire("touchend", y);
    });

  const fab = page.locator('[aria-label="Scroll to bottom"]');
  // Keep scrolling up until the FAB shows (off the live tail). Looping instead of a single drag +
  // immediate assertion makes this robust to per-gesture timing on a loaded CI host — one drag
  // occasionally doesn't clear the FAB's 8-line dead zone before the assertion samples.
  for (let i = 0; i < 25 && !(await fab.isVisible()); i++) {
    await dragUpOnce();
    await page.waitForTimeout(80);
  }
  await expect(fab).toBeVisible(); // we scrolled up off the tail
  const scrolledUp = await viewport.evaluate((el) => el.scrollHeight - el.clientHeight - el.scrollTop);
  expect(scrolledUp).toBeGreaterThan(20); // genuinely off the bottom

  // Reproduce the device condition: on real mobile the coarse-pointer capture overlay wins the
  // hit-test over the FAB and *receives the tap* (the user-confirmed symptom is "keyboard pops up,
  // no scroll"). Chromium's Pixel-7 emulation does NOT occlude the FAB (elementFromPoint at its
  // centre returns the FAB's own <svg>), so a synthetic "real tap" would hit the FAB instead — not
  // the bug. So drive the overlay directly: a no-move touch (tap) on [data-touch-surface] at the
  // FAB's centre, exactly what the overlay sees on a phone, and assert the user's symptom flips.
  const fabBox = await fab.boundingBox();
  if (!fabBox) throw new Error("FAB has no box");
  const fx = Math.round(fabBox.x + fabBox.width / 2);
  const fy = Math.round(fabBox.y + fabBox.height / 2);
  await page.locator("[data-touch-surface]").evaluate(
    (el, [x, y]) => {
      const touch = new Touch({ identifier: 9, target: el, clientX: x, clientY: y });
      const fire = (type: string, withTouch: boolean) =>
        el.dispatchEvent(
          new TouchEvent(type, {
            cancelable: true,
            bubbles: true,
            touches: withTouch ? [touch] : [],
            changedTouches: [touch],
          }),
        );
      fire("touchstart", true); // no touchmove → touchScroll treats it as a tap → onTap(x, y)
      fire("touchend", false);
    },
    [fx, fy],
  );

  // Fixed: the overlay's onTap recognises the FAB rect → jumps to the live tail (FAB hides) and
  // does NOT open the keyboard. Unfixed: onTap falls through to focus the xterm textarea (the
  // "keyboard pops up") and the FAB stays. Assert on that exact distinction — robust to xterm's
  // viewport/refit quirks under emulation, which make a raw scrollTop assertion flaky here.
  const keyboardOpened = await page.evaluate(() => {
    const ae = document.activeElement;
    return !!ae && ae.classList.contains("xterm-helper-textarea");
  });
  expect(keyboardOpened).toBe(false); // the bug was: tapping the FAB just opened the keyboard
  await expect(fab).toBeHidden(); // the tap registered as "jump to bottom" → atBottom → FAB hides
});
