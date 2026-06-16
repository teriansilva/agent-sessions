import { expect, test } from "@playwright/test";

// #414: opencode is a full-screen TUI that manages its own history and enables mouse tracking,
// so xterm keeps no scrollback for it and term.scrollLines() is a no-op. A desktop wheel still
// scrolls it because xterm routes the wheel to the app (mouse-wheel reports) whenever mouse
// tracking is on — REGARDLESS of buffer. opencode runs in the NORMAL buffer with mouse tracking
// (not the alternate buffer), so touch scroll must forward the gesture whenever the app consumes
// the wheel, not only in the alt buffer. These harnesses stub a websocket that enables mouse
// tracking (and, in one case, the alt buffer) and record every outbound input frame ({t:"i"}).

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

async function dragAndAssertForwarded(page: import("@playwright/test").Page) {
  const surface = page.locator("[data-touch-surface]");
  await expect(surface).toBeVisible();
  await expect
    .poll(async () => page.locator(".xterm-screen").innerText(), { timeout: 5000 })
    .toContain("row 0");
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
  // Wheel/scroll input (SGR mouse-wheel \x1b[<64..|<65.. or cursor keys \x1b[A/\x1b[B), not text.
  // eslint-disable-next-line no-control-regex -- matching the literal ESC in mouse/cursor sequences
  expect(sent).toMatch(/\x1b\[(<6[45][;0-9]*[Mm]|A|B)/);
}

// The real opencode shape: NORMAL buffer + mouse tracking (1000) + SGR encoding (1006). This is
// the case an alt-buffer-only check missed — it must forward the wheel here too.
test("touch drag with mouse tracking (normal buffer, opencode) forwards scroll to the app", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "touch-only behavior");
  await page.addInitScript(fakeWs("\x1b[?1000h\x1b[?1006h"));
  await page.goto("/s/opencode/mousetracktest");
  await dragAndAssertForwarded(page);
});

// Alt-screen app (alternate buffer + mouse tracking): the secondary forward path.
test("touch drag in the alt screen forwards scroll to the app", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "touch-only behavior");
  await page.addInitScript(fakeWs("\x1b[?1049h\x1b[?1000h\x1b[?1006h"));
  await page.goto("/s/opencode/altscreentest");
  await dragAndAssertForwarded(page);
});
