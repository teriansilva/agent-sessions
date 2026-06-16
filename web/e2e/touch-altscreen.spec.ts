import { expect, test } from "@playwright/test";

// #414: opencode runs in the ALTERNATE screen (a full-screen TUI that manages its own
// history). In the alt buffer xterm has no scrollback, so term.scrollLines() is a no-op —
// touch scroll must instead forward the gesture to the app the same way a desktop wheel does
// (xterm translates a wheel into mouse-wheel reports while mouse tracking is on). This harness
// stubs a websocket that enters the alt screen + enables SGR mouse tracking, and records every
// outbound input frame ({t:"i"}) so we can assert the drag reached the app.

const FAKE_WS_ALT = `
window.__sentInput = [];
window.WebSocket = class {
  constructor(url) {
    this.url = url; this.readyState = 0; this.binaryType = "blob";
    setTimeout(() => {
      this.readyState = 1;
      if (this.onopen) this.onopen();
      // Enter alt screen, enable mouse tracking (1000) + SGR encoding (1006), draw a frame.
      let s = "\\x1b[?1049h\\x1b[?1000h\\x1b[?1006h";
      for (let i = 0; i < 40; i++) s += "\\x1b[" + (i + 1) + ";1Hrow " + i + " ----------------";
      const buf = new TextEncoder().encode(s).buffer;
      if (this.onmessage) this.onmessage({ data: buf });
      if (this.onmessage) this.onmessage({ data: JSON.stringify({ t: "seq", n: s.length }) });
    }, 30);
  }
  send(msg) {
    try {
      const m = JSON.parse(msg);
      if (m && m.t === "i") window.__sentInput.push(m.d);
    } catch {}
  }
  close() { this.readyState = 3; if (this.onclose) this.onclose({ code: 1000 }); }
};
`;

test("touch drag in the alt screen forwards scroll to the app (mouse-wheel reports)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "touch-only behavior");

  await page.addInitScript(FAKE_WS_ALT);
  await page.goto("/s/opencode/altscreentest");

  const surface = page.locator("[data-touch-surface]");
  await expect(surface).toBeVisible();
  // Wait until the alt-screen frame has rendered (so the buffer is in the alternate type).
  await expect
    .poll(async () => page.locator(".xterm-screen").innerText(), { timeout: 5000 })
    .toContain("row 0");
  // Clear any input sent during connect/resize so we only measure the drag.
  await page.evaluate(() => {
    (window as unknown as { __sentInput: string[] }).__sentInput = [];
  });

  await surface.evaluate((el) => {
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
      y += r.height * 0.05; // finger down → scroll the app's history
      fire("touchmove", y);
    }
    fire("touchend", y);
  });

  // The app must have received scroll input (SGR mouse-wheel reports look like \x1b[<64.. or <65..,
  // or cursor keys \x1b[A/\x1b[B as a fallback). On current code nothing is forwarded → red.
  await expect
    .poll(
      async () =>
        page.evaluate(() => (window as unknown as { __sentInput: string[] }).__sentInput.length),
      { timeout: 3000 },
    )
    .toBeGreaterThan(0);

  const sent = await page.evaluate(
    () => (window as unknown as { __sentInput: string[] }).__sentInput.join(""),
  );
  // It should be wheel/scroll input, not arbitrary text.
  // eslint-disable-next-line no-control-regex -- matching the literal ESC in mouse/cursor sequences
  expect(sent).toMatch(/\x1b\[(<6[45][;0-9]*[Mm]|A|B)/);
});
