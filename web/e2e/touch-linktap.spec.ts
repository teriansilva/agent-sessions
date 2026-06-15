import { expect, test } from "@playwright/test";

// #415: on touch, the .touchLayer overlay swallows every tap (it only (re)opens the keyboard),
// so tapping a URL in the output never reaches xterm's WebLinksAddon. A tap on a link cell must
// open the URL. We stub the websocket to print a single line that is just a URL at the home
// position, and record window.open calls.

const FAKE_WS_LINK = `
window.__opened = [];
const _open = window.open;
window.open = (url) => { window.__opened.push(url); return null; };
window.WebSocket = class {
  constructor(url) {
    this.url = url; this.readyState = 0; this.binaryType = "blob";
    setTimeout(() => {
      this.readyState = 1;
      if (this.onopen) this.onopen();
      const s = "\\x1b[H\\x1b[2Jhttps://example.com/foo";
      const buf = new TextEncoder().encode(s).buffer;
      if (this.onmessage) this.onmessage({ data: buf });
      if (this.onmessage) this.onmessage({ data: JSON.stringify({ t: "seq", n: s.length }) });
    }, 30);
  }
  send() {}
  close() { this.readyState = 3; if (this.onclose) this.onclose({ code: 1000 }); }
};
`;

test("tap on a link opens the URL", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "touch-only behavior");

  await page.addInitScript(FAKE_WS_LINK);
  await page.goto("/s/claude/linktaptest");

  const surface = page.locator("[data-touch-surface]");
  await expect(surface).toBeVisible();
  // Wait until the URL has actually rendered into the top row (avoids racing grid-stable connect).
  await expect
    .poll(async () => page.locator(".xterm-screen").innerText(), { timeout: 5000 })
    .toContain("example.com");

  // Tap (touchstart+touchend, no move) near the start of the top row, inside the URL text.
  await surface.evaluate((el) => {
    const r = el.getBoundingClientRect();
    const rows = 24;
    const cellH = r.height / rows;
    const x = Math.round(r.x + r.width * 0.15); // within "https://example.com/foo"
    const y = Math.round(r.y + cellH * 0.5); // top row
    const touch = new Touch({ identifier: 1, target: el, clientX: x, clientY: y });
    el.dispatchEvent(
      new TouchEvent("touchstart", { cancelable: true, bubbles: true, touches: [touch] }),
    );
    el.dispatchEvent(new TouchEvent("touchend", { cancelable: true, bubbles: true, touches: [] }));
  });

  await expect
    .poll(async () => page.evaluate(() => (window as unknown as { __opened: string[] }).__opened), {
      timeout: 3000,
    })
    .toContain("https://example.com/foo");
});
