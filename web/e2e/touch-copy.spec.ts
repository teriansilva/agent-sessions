import { expect, test } from "@playwright/test";

// #415: on touch the .touchLayer overlay + xterm's `user-select: none` mean text can't be
// selected (so it can't be copied). A press-and-hold should enter selection mode: disable the
// overlay, allow native selection of the DOM-rendered rows, and seed a selection at the finger
// (so the OS selection handles + Copy bubble appear). We assert a real text selection exists
// after a long-press — impossible on current code.

const FAKE_WS_TEXT = `
window.WebSocket = class {
  constructor(url) {
    this.url = url; this.readyState = 0; this.binaryType = "blob";
    setTimeout(() => {
      this.readyState = 1;
      if (this.onopen) this.onopen();
      const s = "\\x1b[H\\x1b[2Jhello selectable world here";
      const buf = new TextEncoder().encode(s).buffer;
      if (this.onmessage) this.onmessage({ data: buf });
      if (this.onmessage) this.onmessage({ data: JSON.stringify({ t: "seq", n: s.length }) });
    }, 30);
  }
  send() {}
  close() { this.readyState = 3; if (this.onclose) this.onclose({ code: 1000 }); }
};
`;

test("press-and-hold selects text (enables copy)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "touch-only behavior");

  await page.addInitScript(FAKE_WS_TEXT);
  await page.goto("/s/claude/copytest");

  const surface = page.locator("[data-touch-surface]");
  await expect(surface).toBeVisible();
  // Wait until the text has actually rendered into the top row.
  await expect
    .poll(async () => page.locator(".xterm-screen").innerText(), {
      timeout: 5000,
    })
    .toContain("selectable");

  // Press-and-hold (touchstart, hold past the long-press threshold, lift — no move) over a word
  // in the top row.
  await surface.evaluate((el) => {
    const r = el.getBoundingClientRect();
    const cellH = r.height / 24;
    const x = Math.round(r.x + r.width * 0.2); // within "hello selectable world here"
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
    // Hold past the 450ms threshold, then lift without moving.
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

  // A non-empty selection must now exist (red on current code: overlay + user-select:none).
  await expect
    .poll(
      async () =>
        page.evaluate(
          () => (window.getSelection()?.toString() ?? "").trim().length,
        ),
      {
        timeout: 3000,
      },
    )
    .toBeGreaterThan(0);
});
