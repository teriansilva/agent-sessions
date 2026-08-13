import { expect, test } from "@playwright/test";

// #559: the scroll-to-bottom (↓) FAB reached parity with codex only for sessions where xterm keeps
// its own scrollback. claude (and any mouse-tracking TUI) owns its OWN scroll — xterm's buffer never
// leaves the tail, so `atBottom` stays true, the FAB never showed, and `scrollToBottom()` was a
// no-op. This drives a REAL browser (desktop wheel + Pixel 7 touch) against a stubbed mouse-tracking
// session and asserts: scrolling the agent up keeps the FAB, and tapping it forwards a downward
// wheel burst (SGR mouse report `ESC [ < 65 … M/m`) that returns the agent to its live tail. jsdom
// can't model xterm's mouse-mode wheel forwarding, so this is a real-browser test.
//
// #584: the FAB now also shows on a FRESH attach of a mouse-tracking session (it opens off its live
// tail with nothing scrolled yet, and the app can't measure the agent's scroll) — so these tests
// assert the FAB is present right after attach, and that a tap forwards the jump-to-tail + clears it.

function fakeWs(enter: string) {
  return `
window.__sentInput = [];
window.WebSocket = class {
  constructor(url) {
    this.url = url; this.readyState = 0; this.binaryType = "blob";
    setTimeout(() => {
      this.readyState = 1;
      if (this.onopen) this.onopen();
      let s = ${JSON.stringify(enter)};
      for (let i = 0; i < 40; i++) s += "\\x1b[" + (i + 1) + ";1Hrow " + i + " ----------------";
      const buf = new TextEncoder().encode(s).buffer;
      if (this.onmessage) this.onmessage({ data: buf });
      if (this.onmessage) this.onmessage({ data: JSON.stringify({ t: "seq", n: s.length }) });
    }, 30);
  }
  send(msg) {
    try { const m = JSON.parse(msg); if (m && m.t === "i") window.__sentInput.push(m.d); } catch {}
  }
  close() { this.readyState = 3; if (this.onclose) this.onclose({ code: 1000 }); }
};
`;
}

// claude's real shape: NORMAL buffer + mouse tracking (1000/1002/1003) + SGR encoding (1006). The
// wheel is consumed by the app, so xterm keeps no usable scrollback.
const MOUSE_TRACKING = "\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h";
const WHEEL_DOWN = /\x1b\[<65[;0-9]*[Mm]/; // eslint-disable-line no-control-regex -- literal ESC

async function waitForPaint(page: import("@playwright/test").Page) {
  await expect
    .poll(async () => page.locator(".xterm-screen").innerText(), {
      timeout: 5000,
    })
    .toContain("row 0");
}

test("mouse-tracking session (desktop): wheel-up reveals the ↓ FAB; clicking it forwards a jump-to-tail", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name === "mobile", "desktop wheel path");
  await page.addInitScript(fakeWs(MOUSE_TRACKING));
  await page.goto("/s/claude/mtrack-fab-desktop");
  await waitForPaint(page);

  const fab = page.getByRole("button", { name: /scroll to bottom/i });
  await expect(fab).toBeVisible(); // #584: a fresh mouse-tracking attach opens off-tail → FAB shown

  // Scroll the agent up: a real wheel over the terminal. xterm forwards it to the app AND our
  // tracker notes the agent is now off the tail → the FAB stays visible.
  await page.locator(".xterm-screen").hover();
  await page.mouse.wheel(0, -300);
  await expect(fab).toBeVisible();

  // Clicking the FAB must forward DOWNWARD wheel reports (button 65) to walk the agent back to its
  // tail — not just call the (no-op here) xterm scrollToBottom().
  await page.evaluate(
    () => ((window as unknown as { __sentInput: string[] }).__sentInput = []),
  );
  await fab.click();
  await expect
    .poll(() =>
      page.evaluate(() =>
        (window as unknown as { __sentInput: string[] }).__sentInput.join(""),
      ),
    )
    .toMatch(WHEEL_DOWN);
  await expect(fab).toHaveCount(0); // returned to the tail → hidden again
});

test("mouse-tracking session (mobile): a scroll-up drag reveals the ↓ FAB; tapping it forwards a jump-to-tail", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "touch path");
  await page.addInitScript(fakeWs(MOUSE_TRACKING));
  await page.goto("/s/claude/mtrack-fab-mobile");
  await waitForPaint(page);

  const fab = page.getByRole("button", { name: /scroll to bottom/i });
  await expect(fab).toBeVisible(); // #584: a fresh mouse-tracking attach opens off-tail → FAB shown

  // Drag the finger DOWN on the touch overlay (finger down = scroll UP into history) → the agent
  // scrolls up and the FAB stays visible.
  const surface = page.locator("[data-touch-surface]");
  await expect(surface).toBeVisible();
  await surface.evaluate((el) => {
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
    let y = r.y + r.height * 0.25;
    fire("touchstart", y);
    for (let i = 0; i < 12; i++) {
      y += r.height * 0.05; // finger down → scroll the agent up into history
      fire("touchmove", y);
    }
    fire("touchend", y);
  });
  await expect(fab).toBeVisible();

  // Tap the FAB → forwards downward wheel reports to return the agent to its tail.
  await page.evaluate(
    () => ((window as unknown as { __sentInput: string[] }).__sentInput = []),
  );
  await fab.click();
  await expect
    .poll(() =>
      page.evaluate(() =>
        (window as unknown as { __sentInput: string[] }).__sentInput.join(""),
      ),
    )
    .toMatch(WHEEL_DOWN);
  await expect(fab).toHaveCount(0);
});
