import { expect, test, type Page } from "@playwright/test";

// Opening an alt-screen session (claude, opencode), and every reconnect to one, repaints the
// agent's frame without a REPAINT tap. Normal-buffer sessions keep the heuristic backstop
// (terminal/repaint-backstop.spec.ts), because there a repaint's clear wipes the scroll-up.
//
// The attach backstop used to repaint only when its heuristic judged the screen blank or a sparse
// fragment. An attach that painted a full but STALE frame (the replay drawn at the old geometry)
// passed the heuristic, and a reconnect never armed it at all — so the operator pressed REPAINT
// on almost every visit. Real browser: stub the socket, deliver a fully painted attach, and assert
// the rows−1 → rows nudge the REPAINT button sends.

const PAINTED_WS = `
window.__sent = [];
window.__socks = [];
(() => {
  const line = (i) => "line " + String(i).padStart(2, "0") + " " + "x".repeat(60) + "\\r\\n";
  // claude (≥ 2.1.178) draws in the alternate screen.
  let body = "\x1b[?1049h";
  for (let i = 0; i < 80; i++) body += line(i);
  const painted = new TextEncoder().encode(body).buffer;
  window.WebSocket = class {
    constructor(url) {
      this.url = url; this.readyState = 0; this.binaryType = "arraybuffer";
      const first = window.__socks.length === 0;
      window.__socks.push(this);
      setTimeout(() => {
        this.readyState = 1;
        this.onopen && this.onopen();
        if (first) {
          this.onmessage && this.onmessage({ data: painted });
          this.onmessage && this.onmessage({ data: JSON.stringify({ t: "seq", n: painted.byteLength }) });
        } else {
          // A caught-up reconnect: nothing new to send.
          this.onmessage && this.onmessage({ data: JSON.stringify({ t: "seq", n: painted.byteLength }) });
        }
      }, 20);
    }
    send(d) { window.__sent.push({ at: window.__socks.length, d: String(d) }); }
    close() { this.readyState = 3; }
  };
})();
`;

type Sent = { at: number; d: string };

/** Did socket `n` (1-based) send the repaint nudge — a resize one row short of a later one? */
async function nudged(page: Page, n: number): Promise<boolean> {
  return page.evaluate((sock) => {
    const rs = ((window as unknown as { __sent: Sent[] }).__sent ?? [])
      .filter((s) => s.at === sock && s.d.includes('"t":"r"'))
      .map((s) => JSON.parse(s.d) as { cols: number; rows: number });
    return rs.some((a, i) =>
      rs.slice(i + 1).some((b) => b.cols === a.cols && b.rows === a.rows + 1),
    );
  }, n);
}

test("opening a session repaints even when the attach painted a full frame", async ({ page }) => {
  await page.addInitScript(PAINTED_WS);
  await page.goto("/s/claude/repaint-on-attach");
  await expect(page.locator(".xterm-rows")).toContainText("line 79");
  await expect.poll(() => nudged(page, 1), { timeout: 5_000 }).toBe(true);
});

test("a reconnect repaints too", async ({ page }) => {
  await page.addInitScript(PAINTED_WS);
  await page.goto("/s/claude/repaint-on-reconnect");
  await expect(page.locator(".xterm-rows")).toContainText("line 79");
  // A transient drop: the socket reconnects and resumes from its offset.
  await page.evaluate(() => {
    const s = (window as unknown as { __socks: { onclose?: (e: { code: number }) => void; readyState: number }[] }).__socks[0];
    s.readyState = 3;
    s.onclose?.({ code: 1006 });
  });
  await expect
    .poll(() => page.evaluate(() => (window as unknown as { __socks: unknown[] }).__socks.length), {
      timeout: 10_000,
    })
    .toBe(2);
  await expect.poll(() => nudged(page, 2), { timeout: 5_000 }).toBe(true);
});
