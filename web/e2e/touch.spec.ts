import { expect, test } from "@playwright/test";

// Verifies the headline fix in a REAL touch browser: a one-finger drag over the
// terminal scrolls its scrollback. Drives the real Terminal component, but stubs the
// websocket so we get scrollback without a backend, then dispatches real touch events.

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

test("one-finger drag scrolls the terminal scrollback", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "touch-only behavior");

  await page.addInitScript(FAKE_WS);
  await page.goto("/s/claude/touchtest");

  const viewport = page.locator(".xterm-viewport");
  await expect(viewport).toBeVisible();
  // Wait until 400 lines are in and the view auto-scrolled to the bottom (scrollable).
  await expect
    .poll(async () => viewport.evaluate((el) => el.scrollHeight - el.clientHeight), {
      timeout: 5000,
    })
    .toBeGreaterThan(100);
  const before = await viewport.evaluate((el) => el.scrollTop);
  expect(before).toBeGreaterThan(0); // sitting at the bottom of the scrollback

  // A quick downward drag over the touch-capture surface scrolls up into history,
  // driven via real touch events.
  await page.locator("[data-touch-surface]").evaluate((el) => {
    const r = el.getBoundingClientRect();
    const cx = Math.round(r.x + r.width / 2);
    const touch = (y: number) =>
      new Touch({ identifier: 1, target: el, clientX: cx, clientY: Math.round(y) });
    const fire = (type: string, y: number) =>
      el.dispatchEvent(
        new TouchEvent(type, { cancelable: true, bubbles: true, touches: type === "touchend" ? [] : [touch(y)] }),
      );
    let y = r.y + r.height * 0.25;
    fire("touchstart", y);
    for (let i = 0; i < 12; i++) {
      y += r.height * 0.05; // move finger down → scroll up into older output
      fire("touchmove", y);
    }
    fire("touchend", y);
  });

  await expect
    .poll(async () => viewport.evaluate((el) => el.scrollTop), { timeout: 3000 })
    .toBeLessThan(before); // scrolled up into the scrollback
});
