import { expect, test } from "@playwright/test";

// #1038 — Kimi's own repaint cycle wipes the scrollback and pins the view off the live tail.
//
// Kimi's TUI repaints its whole frame with `ESC[2J ESC[H ESC[3J` (verified in a live session's
// ring: 95 erase-scrollback sequences). In xterm.js CSI 3J deletes the ENTIRE scrollback
// mid-stream; the browser's scrollTop clamp during the collapse plus the client's sticky
// reader-anchor then pin the viewport far off the tail — the ↓ FAB appears with no user input,
// the view climbs to the top, and "loading older history…" fires. Codex's repaint had the same
// shape and #600 strips its CSI 3J on both layers (server: scrollback.sanitize_live_output;
// client: lib/scrollErase.ts); Kimi joins that engine set.
//
// This drives the REAL Terminal component + real xterm; only the websocket is stubbed (the
// touch.spec.ts pattern). The stubbed stream reproduces Kimi's cycle structure — a full-frame
// repaint (~380 lines × ~236 cols ≈ 90 KB) delivered in 64 KiB chunks exactly the way the
// server reads the pty (`os.read(fd, 65536)`), so a chunk boundary lands mid-cycle, which is
// the condition the drift engages under. Validated in a ring-replay harness (real xterm 5.5 +
// the Terminal.tsx follow/anchor logic, real 16.2 MB ring slices): red 3/3 without the strip,
// 0 drift with it — see issue #1038's evidence pack.

const CYCLES = 12;

/** A smaller stream for the reader-mode case: small kimi-style repaint frames (the post-strip
 *  live shape — full-screen clear WITHOUT the scrollback wipe) so the buffer stays in the
 *  regime where xterm's DOM scroll-area sync is reliable. The huge-frame regime hits an xterm
 *  scroll-area quirk that is orthogonal to this fix (the freeze reproduces with the wipes
 *  stripped AND with them absent); the drift case above engages long before any freeze. */
const KIMI_READ_WS = `
window.__kimiFramesSent = 0;
window.__kimiDone = false;
window.__startKimiCycles = () => {};
window.WebSocket = class {
  constructor(url) {
    this.url = url; this.readyState = 0; this.binaryType = "blob";
    this._timer = 0;
    setTimeout(() => {
      this.readyState = 1;
      if (this.onopen) this.onopen();
      const enc = new TextEncoder();
      let s = "\\x1b[H\\x1b[2J\\x1b[3J";
      for (let i = 0; i < 200; i++) s += "kimi transcript " + i + " " + "-".repeat(120) + "\\r\\n";
      if (this.onmessage) this.onmessage({ data: enc.encode(s).buffer });
      if (this.onmessage) this.onmessage({ data: JSON.stringify({ t: "seq", n: s.length }) });
      window.__startKimiCycles = () => {
        if (this._timer) return;
        let frame = 0;
        this._timer = setInterval(() => {
          frame++;
          window.__kimiFramesSent = frame;
          let c = "\\x1b[?2026h\\x1b[2J\\x1b[H";
          for (let i = 0; i < 40; i++)
            c += " kimi live " + frame + " row " + i + " " + "-".repeat(120) + "\\r\\n";
          c += " " + " ".repeat(120) + "context: 3%";
          c += "\\x1b[?2026l\\x1b[3A\\x1b[6G\\x1b[?25l";
          const buf = enc.encode(c);
          for (let off = 0; off < buf.length; off += 65536)
            if (this.onmessage)
              this.onmessage({
                data: buf.slice(off, Math.min(buf.length, off + 65536)).buffer,
              });
          if (frame >= 4) {
            clearInterval(this._timer);
            window.__kimiDone = true;
          }
        }, 120);
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

/** The stubbed ws: an attach replay (kept intact — server-authored), a seq boundary, then
 *  Kimi-style full-frame repaint cycles on demand, each split into 64 KiB ws messages. */
const KIMI_CYCLE_WS = `
window.__kimiFramesSent = 0;
window.__kimiDone = false;
window.__startKimiCycles = () => {};
window.WebSocket = class {
  constructor(url) {
    this.url = url; this.readyState = 0; this.binaryType = "blob";
    this._timer = 0;
    setTimeout(() => {
      this.readyState = 1;
      if (this.onopen) this.onopen();
      const enc = new TextEncoder();
      // Attach replay: the server-authored clear + a full screen of transcript. Its ESC[3J is
      // intentionally NOT stripped (pre-attach junk) — the client filter opens only post-seq.
      let s = "\\x1b[H\\x1b[2J\\x1b[3J";
      for (let i = 0; i < 300; i++) s += "kimi transcript " + i + " " + "-".repeat(180) + "\\r\\n";
      if (this.onmessage) this.onmessage({ data: enc.encode(s).buffer });
      if (this.onmessage) this.onmessage({ data: JSON.stringify({ t: "seq", n: s.length }) });
      window.__startKimiCycles = () => {
        if (this._timer) return;
        let frame = 0;
        this._timer = setInterval(() => {
          frame++;
          window.__kimiFramesSent = frame;
          // One full repaint cycle: sync-wrapped, ESC[2J ESC[H ESC[3J, ~380 wide lines, the
          // composer redraw and the cursor-park tail — the byte shape of the live ring.
          let c = "\\x1b[?2026h\\x1b[2J\\x1b[H\\x1b[3J";
          for (let i = 0; i < 380; i++)
            c += " kimi cycle " + frame + " line " + i + " " + "-".repeat(190) + "\\r\\n";
          c += " \\x1b[38;2;90;90;90m│\\x1b[39m > prompt".padEnd(236) + "\\r\\n";
          c += " " + " ".repeat(220) + "context: 3%";
          c += "\\x1b[?2026l\\x1b[3A\\x1b[6G\\x1b[?25l";
          const buf = enc.encode(c);
          // Pace the chunks the way the real server does: each read is await-sent before the
          // next, so ws messages arrive with small gaps rather than back-to-back.
          for (let off = 0; off < buf.length; off += 65536) {
            const slice = buf.slice(off, Math.min(buf.length, off + 65536));
            setTimeout(() => {
              if (this.onmessage) this.onmessage({ data: slice.buffer });
            }, (off / 65536) * 24);
          }
          if (frame >= ${CYCLES}) {
            setTimeout(() => {
              clearInterval(this._timer);
              window.__kimiDone = true;
            }, Math.ceil(buf.length / 65536) * 24);
          }
        }, 90);
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

test.describe("kimi live-stream scrollback (#1038)", () => {
  test("streaming kimi repaints never move the viewport off the tail (no user input)", async ({
    page,
  }) => {
    const historyCalls: string[] = [];
    await page.route(/\/api\/history/, (r) => {
      historyCalls.push(r.request().url());
      return r.fulfill({ status: 404, json: { detail: "none" } });
    });
    await page.addInitScript(KIMI_CYCLE_WS);
    await page.goto("/s/kimi/live-drift");

    const viewport = page.locator(".xterm-viewport");
    await expect(viewport).toBeVisible();
    await expect(page.locator(".xterm-screen")).toContainText("kimi transcript");
    await expect
      .poll(
        async () => viewport.evaluate((el) => el.scrollHeight - el.clientHeight),
        { timeout: 5000 },
      )
      .toBeGreaterThan(100); // the attach replay produced scrollback

    // Sample THROUGHOUT the stream: viewport gap off the tail + FAB visibility, every 50 ms,
    // until all cycles are sent and the parse queue has drained. The failure this pins is a
    // TRANSIENT — a check made only at the end can miss the drift engaging mid-stream.
    await page.evaluate(async () => {
      const w = window as unknown as {
        __samples: { gap: number; fab: boolean }[];
        __startKimiCycles: () => void;
        __kimiDone: boolean;
      };
      w.__samples = [];
      const vp = document.querySelector(".xterm-viewport");
      const iv = setInterval(() => {
        const fab = document.querySelector<HTMLButtonElement>(
          'button[aria-label="Scroll to bottom"]',
        );
        w.__samples.push({
          gap: vp ? vp.scrollHeight - vp.clientHeight - vp.scrollTop : -1,
          fab: !!fab,
        });
      }, 50);
      w.__startKimiCycles();
      await new Promise<void>((resolve) => {
        const done = setInterval(() => {
          if (w.__kimiDone) {
            clearInterval(iv);
            clearInterval(done);
            resolve();
          }
        }, 100);
      });
      // Let the last chunk's write callback + render settle before stopping the sampler.
      await new Promise((r) => setTimeout(r, 400));
    });

    const samples = await page.evaluate(
      () => (window as unknown as { __samples: { gap: number; fab: boolean }[] }).__samples,
    );
    // The sampler must have covered the stream, not just the end state.
    expect(samples.length).toBeGreaterThan(CYCLES);
    // The ↓ FAB never appears — the viewport never left the live tail without user input.
    expect(
      samples.filter((s) => s.fab),
      "FAB visible while streaming with no user scroll",
    ).toEqual([]);
    // …and the viewport gap stays within ONE screen of the tail the whole time. The bug's
    // drift pins the viewport at the TOP (gap ≈ the whole buffer, thousands of px).
    const maxGap = Math.max(...samples.map((s) => s.gap));
    const clientHeight = await viewport.evaluate((el) => el.clientHeight);
    expect(maxGap, "viewport left the tail mid-stream").toBeLessThanOrEqual(clientHeight);
    // Settled state: at the tail, and the wipe-and-regrow never armed the history loader.
    await expect
      .poll(async () => viewport.evaluate((el) => el.scrollHeight - el.clientHeight - el.scrollTop))
      .toBeLessThanOrEqual(160);
    // THE DETERMINISTIC WITNESS: the scrollback the cycles produced is still there. Pre-fix
    // every repaint cycle's ESC[3J deletes everything above the screen, so the scroll area
    // caps at roughly the attach + ONE frame (~29k px here) no matter how many cycles stream;
    // with the strip the buffer accumulates every cycle (~28k px each). This is the
    // timing-independent form of the reported bug — the history the operator scrolls back
    // through simply stops existing — and it does not depend on catching the mid-collapse
    // transient the drift (and the FAB) engage through.
    const scrollbackPx = await viewport.evaluate((el) => el.scrollHeight);
    expect(
      scrollbackPx,
      "kimi's repaint cycles deleted the scrollback instead of accumulating it",
    ).toBeGreaterThan(120_000);
    expect(historyCalls, "no history fetch without a user scroll to the top").toEqual([]);
  });

  test("a reading position survives kimi repaints, and the tail jump resumes following", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "touch-only behavior");
    await page.addInitScript(KIMI_READ_WS);
    await page.goto("/s/kimi/live-read");

    const viewport = page.locator(".xterm-viewport");
    await expect(viewport).toBeVisible();
    await expect(page.locator(".xterm-screen")).toContainText("kimi transcript");
    await expect
      .poll(
        async () => viewport.evaluate((el) => el.scrollHeight - el.clientHeight),
        { timeout: 5000 },
      )
      .toBeGreaterThan(100);

    // The operator scrolls up into the transcript (real touch drags over the capture surface —
    // the momentum fling walks the reader toward the top; bounded buffer, so it gets there).
    const dragDownOverTouchSurface = () =>
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
    let atTop = false;
    for (let attempt = 0; attempt < 6 && !atTop; attempt++) {
      await dragDownOverTouchSurface();
      await page.waitForTimeout(250);
      atTop = await viewport.evaluate((el) => el.scrollTop === 0);
    }
    expect(atTop, "the reader reached the top of the transcript").toBe(true);

    // While they read, kimi keeps repainting. The frames must not push the reader off the top,
    // and the content they scrolled up to see must still exist — pre-fix the repaint's ESC[3J
    // deleted exactly these lines mid-read; the strip keeps them.
    await page.evaluate(() => {
      (window as unknown as { __startKimiCycles: () => void }).__startKimiCycles();
    });
    await expect
      .poll(
        () =>
          page.evaluate(
            () => (window as unknown as { __kimiDone: boolean }).__kimiDone,
          ),
        { timeout: 15000 },
      )
      .toBe(true);
    await page.waitForTimeout(400);
    expect(
      await viewport.evaluate((el) => el.scrollTop),
      "frames pushed the top-anchored reader down",
    ).toBe(0);
    await expect(page.locator(".xterm-screen")).toContainText("kimi transcript 0 ");

    // One tap on the ↓ FAB jumps back to the tail; following resumes.
    await page.locator('button[aria-label="Scroll to bottom"]').tap();
    await expect
      .poll(
        async () =>
          viewport.evaluate((el) => el.scrollHeight - el.clientHeight - el.scrollTop),
      )
      .toBeLessThanOrEqual(160);
  });
});
