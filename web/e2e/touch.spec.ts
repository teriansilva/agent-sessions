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

const CODEX_CLEAR_WS = `
window.WebSocket = class {
  constructor(url) {
    this.url = url; this.readyState = 0; this.binaryType = "blob";
    setTimeout(() => {
      this.readyState = 1;
      if (this.onopen) this.onopen();
      let s = "\\x1b[H\\x1b[2J\\x1b[3J";
      for (let i = 0; i < 400; i++) s += "codex transcript " + i + " ------------------------------\\r\\n";
      const enc = new TextEncoder();
      if (this.onmessage) this.onmessage({ data: enc.encode(s).buffer });
      if (this.onmessage) this.onmessage({ data: JSON.stringify({ t: "seq", n: s.length }) });
      setTimeout(() => {
        const live = "\\x1b[?2026h\\x1b[H\\x1b[2J\\x1b[3Jcodex live frame\\r\\n\\x1b[?2026l";
        if (this.onmessage) this.onmessage({ data: enc.encode(live).buffer });
      }, 50);
    }, 30);
  }
  send() {}
  close() { this.readyState = 3; if (this.onclose) this.onclose({ code: 1000 }); }
};
`;

const CODEX_LIVE_UPDATES_WS = `
window.__codexFramesSent = 0;
window.__startCodexFrames = () => {};
window.WebSocket = class {
  constructor(url) {
    this.url = url; this.readyState = 0; this.binaryType = "blob";
    this._timer = 0;
    setTimeout(() => {
      this.readyState = 1;
      if (this.onopen) this.onopen();
      let s = "\\x1b[H\\x1b[2J\\x1b[3J";
      for (let i = 0; i < 500; i++) s += "codex transcript " + i + " ------------------------------\\r\\n";
      const enc = new TextEncoder();
      if (this.onmessage) this.onmessage({ data: enc.encode(s).buffer });
      if (this.onmessage) this.onmessage({ data: JSON.stringify({ t: "seq", n: s.length }) });
      window.__startCodexFrames = () => {
        if (this._timer) return;
        let frame = 0;
        this._timer = setInterval(() => {
          frame++;
          window.__codexFramesSent = frame;
          let live = "\\x1b[?2026h\\x1b[H\\x1b[2Jcodex live frame " + frame + "\\r\\n";
          for (let row = 0; row < 40; row++) live += "live " + frame + " row " + row + "\\r\\n";
          live += "\\x1b[?2026l";
          if (this.onmessage) this.onmessage({ data: enc.encode(live).buffer });
          if (frame >= 16) clearInterval(this._timer);
        }, 60);
      };
    }, 30);
  }
  send() {}
  close() {
    if (this._timer) clearInterval(this._timer);
    this.readyState = 3;
    if (this.onclose) this.onclose({ code: 1000 });
  }
};
`;

async function dragDownOverTouchSurface(page: import("@playwright/test").Page) {
  await page.locator("[data-touch-surface]").evaluate((el) => {
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
      y += r.height * 0.05; // move finger down → scroll up into older output
      fire("touchmove", y);
    }
    fire("touchend", y);
  });
}

function scrollMetrics(viewport: import("@playwright/test").Locator) {
  return viewport.evaluate((el) => ({
    scrollTop: el.scrollTop,
    bottomGap: el.scrollHeight - el.clientHeight - el.scrollTop,
  }));
}

async function dragIntoScrollback(
  page: import("@playwright/test").Page,
  viewport: import("@playwright/test").Locator,
) {
  for (let attempt = 0; attempt < 5; attempt++) {
    await dragDownOverTouchSurface(page);
    for (let tick = 0; tick < 10; tick++) {
      const metrics = await scrollMetrics(viewport);
      if (metrics.bottomGap > 100) return metrics;
      await page.waitForTimeout(50);
    }
  }
  return scrollMetrics(viewport);
}

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
  await dragDownOverTouchSurface(page);

  await expect
    .poll(async () => viewport.evaluate((el) => el.scrollTop), { timeout: 3000 })
    .toBeLessThan(before); // scrolled up into the scrollback
});

test("Codex live clear-scrollback repaint does not break mobile touch scroll", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "touch-only behavior");

  await page.addInitScript(CODEX_CLEAR_WS);
  await page.goto("/s/codex/touchtest");

  const viewport = page.locator(".xterm-viewport");
  await expect(viewport).toBeVisible();
  await expect(page.locator(".xterm-screen")).toContainText("codex live frame");
  await expect
    .poll(async () => viewport.evaluate((el) => el.scrollHeight - el.clientHeight), {
      timeout: 5000,
    })
    .toBeGreaterThan(100);
  const before = await viewport.evaluate((el) => el.scrollTop);
  expect(before).toBeGreaterThan(0);

  await dragDownOverTouchSurface(page);

  await expect
    .poll(async () => viewport.evaluate((el) => el.scrollTop), { timeout: 3000 })
    .toBeLessThan(before);
});

test("Codex live updates preserve mobile scrollback position while the user is reading", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "touch-only behavior");

  await page.addInitScript(CODEX_LIVE_UPDATES_WS);
  await page.goto("/s/codex/live-scroll");

  const viewport = page.locator(".xterm-viewport");
  await expect(viewport).toBeVisible();
  await expect
    .poll(async () => viewport.evaluate((el) => el.scrollHeight - el.clientHeight), {
      timeout: 5000,
    })
    .toBeGreaterThan(100);
  const before = await scrollMetrics(viewport);
  expect(before.scrollTop).toBeGreaterThan(0);

  const reading = await dragIntoScrollback(page, viewport);
  expect(reading.scrollTop).toBeLessThan(before.scrollTop);
  expect(reading.bottomGap).toBeGreaterThan(100);

  await page.evaluate(() =>
    (window as unknown as { __startCodexFrames: () => void }).__startCodexFrames(),
  );
  await expect
    .poll(() => page.evaluate(() => (window as unknown as { __codexFramesSent: number }).__codexFramesSent), {
      timeout: 5000,
    })
    .toBeGreaterThanOrEqual(8);
  const afterLive = await scrollMetrics(viewport);
  expect(afterLive.bottomGap).toBeGreaterThan(100);
  expect(afterLive.scrollTop).toBeLessThanOrEqual(reading.scrollTop + 60);
});

test("Codex live updates preserve unarmed viewport scrollback position", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "touch-only behavior");

  await page.addInitScript(CODEX_LIVE_UPDATES_WS);
  await page.goto("/s/codex/viewport-scroll");

  const viewport = page.locator(".xterm-viewport");
  await expect(viewport).toBeVisible();
  await expect
    .poll(async () => viewport.evaluate((el) => el.scrollHeight - el.clientHeight), {
      timeout: 5000,
    })
    .toBeGreaterThan(100);
  const before = await scrollMetrics(viewport);
  expect(before.scrollTop).toBeGreaterThan(0);

  const reading = await viewport.evaluate((el) => {
    el.scrollTop = Math.max(0, el.scrollTop - 700);
    el.dispatchEvent(new Event("scroll", { bubbles: true }));
    return {
      scrollTop: el.scrollTop,
      bottomGap: el.scrollHeight - el.clientHeight - el.scrollTop,
    };
  });
  expect(reading.bottomGap).toBeGreaterThan(100);
  await expect.poll(async () => (await scrollMetrics(viewport)).bottomGap).toBeGreaterThan(100);

  await page.evaluate(() =>
    (window as unknown as { __startCodexFrames: () => void }).__startCodexFrames(),
  );
  await expect
    .poll(() => page.evaluate(() => (window as unknown as { __codexFramesSent: number }).__codexFramesSent), {
      timeout: 5000,
    })
    .toBeGreaterThanOrEqual(8);
  const afterLive = await scrollMetrics(viewport);
  expect(afterLive.bottomGap).toBeGreaterThan(100);
  expect(afterLive.scrollTop).toBeLessThanOrEqual(reading.scrollTop + 60);
});

test("Codex viewport resize preserves mobile scrollback position while the user is reading", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "touch-only behavior");

  await page.addInitScript(CODEX_LIVE_UPDATES_WS);
  await page.goto("/s/codex/resize-scroll");

  const viewport = page.locator(".xterm-viewport");
  await expect(viewport).toBeVisible();
  await expect
    .poll(async () => viewport.evaluate((el) => el.scrollHeight - el.clientHeight), {
      timeout: 5000,
    })
    .toBeGreaterThan(100);

  const reading = await dragIntoScrollback(page, viewport);
  expect(reading.bottomGap).toBeGreaterThan(100);

  const size = page.viewportSize() ?? { width: 393, height: 851 };
  await page.setViewportSize({ width: size.width, height: Math.max(520, size.height - 180) });
  await page.waitForTimeout(400);

  const afterResize = await scrollMetrics(viewport);
  expect(afterResize.bottomGap).toBeGreaterThan(100);
  expect(afterResize.scrollTop).toBeLessThanOrEqual(reading.scrollTop + 80);
});
