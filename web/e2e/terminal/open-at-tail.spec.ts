// #584: opening a claude session lands mid-history ("starts in the middle") with NO scroll-to-
// bottom button. Claude is a mouse-tracking TUI (arms ?1000/?1002/?1003/?1006) that owns its own
// scroll, so xterm's buffer never leaves the tail → `atBottom` stays true, and the #559 FAB only
// appeared once the user had scrolled the agent UP (`appScrolledUp`). On a FRESH attach the user
// hasn't scrolled, so the FAB was hidden and there was no way back to claude's live prompt.
//
// Fix (Approach A): for an app-consuming session, once mouse tracking is known post-attach, reveal
// the existing ↓ FAB so the user has a one-tap jump to the tail before any wheel-up gesture; the
// tap forwards a bounded downward wheel burst (SGR mouse report) to walk the agent to its tail.
// jsdom can't model xterm's mouse-mode wheel forwarding — this needs a real browser.
import { expect, test } from "@playwright/test";

// claude's real shape: NORMAL buffer + mouse tracking (1000/1002/1003) + SGR encoding (1006).
const MOUSE_TRACKING = "\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h";
const WHEEL_DOWN = /\x1b\[<65[;0-9]*[Mm]/; // eslint-disable-line no-control-regex -- literal ESC

// A fake server that attaches a mouse-tracking session and paints a frame (like claude on
// re-attach) — WITHOUT the user ever scrolling. `have=0` fresh attach.
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

async function waitForPaint(page: import("@playwright/test").Page) {
  await expect
    .poll(async () => page.locator(".xterm-screen").innerText(), { timeout: 5000 })
    .toContain("row 0");
}

test("mouse-tracking claude: the ↓ FAB is available on a FRESH attach, before any user scroll", async ({
  page,
}) => {
  await page.addInitScript(fakeWs(MOUSE_TRACKING));
  await page.goto("/s/claude/open-tail-fresh");
  await waitForPaint(page);

  // The reported bug: the user never scrolled, yet must reach claude's live tail. The FAB must be
  // present so there is a way back — this was hidden before the fix (RED).
  const fab = page.getByRole("button", { name: /scroll to bottom/i });
  await expect(fab).toBeVisible();

  // Tapping it forwards a DOWNWARD wheel burst (button 65) to walk the mouse-tracking agent to its
  // tail — not a no-op xterm scrollToBottom.
  await page.evaluate(() => ((window as unknown as { __sentInput: string[] }).__sentInput = []));
  await fab.click();
  await expect
    .poll(() => page.evaluate(() => (window as unknown as { __sentInput: string[] }).__sentInput.join("")))
    .toMatch(WHEEL_DOWN);
  // …and once jumped, the FAB clears (the app-tail-unknown state is resolved).
  await expect(fab).toHaveCount(0);
});

test("a non-mouse-tracking session does NOT get the fresh-attach FAB (only real scrollback drives it)", async ({
  page,
}) => {
  // codex/antigravity: normal buffer, no mouse tracking. The fresh-attach FAB must NOT show — those
  // sessions use xterm's own scrollback + `computeAtBottom`, unchanged by this fix.
  await page.addInitScript(fakeWs("")); // no mouse-tracking modes
  await page.goto("/s/codex/open-tail-nomouse");
  await waitForPaint(page);
  await page.waitForTimeout(1000); // past the attach-settle window
  await expect(page.getByRole("button", { name: /scroll to bottom/i })).toHaveCount(0);
});
