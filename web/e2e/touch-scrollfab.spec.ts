import { expect, test } from "@playwright/test";

// Real-browser repro for the mobile "jump to bottom" FAB (↓): on a phone, after flinging UP into
// history, *tapping* the FAB must return to — and STAY at — the live tail.
//
// The real bug (the earlier #519 fix missed it): the FAB paints ABOVE the coarse-pointer touch
// overlay and, per real hit-testing, RECEIVES the tap itself (elementFromPoint at its centre
// returns the FAB, not the overlay). Its onClick already scrolls to the tail — but a scroll-up
// gesture leaves a momentum "fling" running inside the touch overlay, and that fling is only
// cancelled when the OVERLAY next receives a touch. A tap on the FAB never reaches the overlay,
// so the leftover velocity keeps scrolling and drags the view straight back off the tail one
// frame later — "tapping jump-to-bottom does nothing on phones." The fix cancels that momentum
// when the FAB jumps to the tail.
//
// This test drives a REAL tap through Playwright's input pipeline (not synthetic events dispatched
// on the overlay, which bypass hit-testing and falsely passed before), while a fling is still in
// flight — the exact device condition. jsdom can't model layout / hit-test / momentum, so this is
// a real-touch (Pixel 7) e2e test.

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

test("tapping the scroll-to-bottom FAB returns to the tail even with fling momentum", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "touch-only behavior");

  await page.addInitScript(FAKE_WS);
  await page.goto("/s/claude/fabtest");

  const viewport = page.locator(".xterm-viewport");
  await expect(viewport).toBeVisible();
  await expect
    .poll(
      async () => viewport.evaluate((el) => el.scrollHeight - el.clientHeight),
      { timeout: 5000 },
    )
    .toBeGreaterThan(100);

  // One finger-drag DOWN over the capture overlay = scroll UP into history, lifting WHILE moving so
  // a momentum fling is launched (the condition that undoes the naive jump-to-tail).
  const flingUpOnce = () =>
    page.locator("[data-touch-surface]").evaluate((el) => {
      const r = el.getBoundingClientRect();
      const cx = Math.round(r.x + r.width / 2);
      const touch = (y: number) =>
        new Touch({
          identifier: 1,
          target: el,
          clientX: cx,
          clientY: Math.round(y),
        });
      const fire = (type: string, y: number) =>
        el.dispatchEvent(
          new TouchEvent(type, {
            cancelable: true,
            bubbles: true,
            touches: type === "touchend" ? [] : [touch(y)],
          }),
        );
      let y = r.y + r.height * 0.2;
      fire("touchstart", y);
      for (let i = 0; i < 12; i++) {
        y += r.height * 0.06; // fast, steady move → real velocity at lift
        fire("touchmove", y);
      }
      fire("touchend", y); // lift mid-move → momentum fling starts
    });

  const fab = page.locator('[aria-label="Scroll to bottom"]');
  // Scroll up off the tail until the FAB shows.
  for (let i = 0; i < 25 && !(await fab.isVisible()); i++) {
    await flingUpOnce();
    await page.waitForTimeout(80);
  }
  await expect(fab).toBeVisible();

  // One last vigorous fling so momentum is DEFINITELY in flight, then immediately real-tap the FAB
  // — no settle. This is the exact "fling up, then tap jump-to-bottom" gesture users perform.
  await flingUpOnce();
  const box = await fab.boundingBox();
  if (!box) throw new Error("FAB has no box");
  await page.touchscreen.tap(
    Math.round(box.x + box.width / 2),
    Math.round(box.y + box.height / 2),
  );

  // Let any (cancelled, if fixed) momentum decay, then assert the STEADY state is the tail. Unfixed,
  // the leftover fling drags the view back to the top within a few frames and the FAB reappears; the
  // fix cancels the fling so we stay pinned at the bottom (atBottom ⇒ FAB hidden).
  await page.waitForTimeout(1200);
  await expect(fab).toBeHidden();
  const distanceFromBottom = await viewport.evaluate(
    (el) => el.scrollHeight - el.clientHeight - el.scrollTop,
  );
  expect(distanceFromBottom).toBeLessThan(40); // genuinely at the live tail, not dragged back up
});
