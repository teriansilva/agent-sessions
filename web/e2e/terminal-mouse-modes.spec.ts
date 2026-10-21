import { expect, test } from "@playwright/test";

// #397: a fresh client attaching to an alt-screen TUI (opencode) gets the active xterm
// private modes (mouse reporting + SGR + bracketed paste) RE-EMITTED by the server on
// attach — the agent set them once at startup and those bytes are long gone from the
// stream. Without that replay the xterm never learns the app wants mouse events, so a wheel
// gesture over the (scrollback-less) alt buffer does nothing — the reported bug.
//
// This is a REAL-browser test of the mechanism the server fix relies on: a real wheel event
// over a real xterm.js can only be encoded as an SGR mouse report once mode 1000+1006 are
// active. jsdom can't model wheel→mouse encoding, so this is the right surface to pin it.
//
// A WebSocket stub that (a) records every frame the app sends and (b) drives the attach: it
// enters the alt screen, then — modelling the #397 server replay — optionally re-emits the
// mouse/SGR/bracketed-paste DECSET sequences before the live frame.
const STUB = (withModes: boolean) => `
window.__sent = [];
window.WebSocket = class {
  constructor(url) { this.url = url; this.readyState = 0; this.binaryType = "arraybuffer";
    const enc = new TextEncoder();
    setTimeout(() => {
      this.readyState = 1;
      this.onopen && this.onopen();
      this.onmessage && this.onmessage({ data: JSON.stringify({ t: "role", role: "owner" }) });
      const enter = "\\x1b[?1049h";                                  // opencode enters alt-screen
      const modes = ${withModes ? '"\\x1b[?1000h\\x1b[?1006h\\x1b[?2004h"' : '""'}; // #397 replay
      const bytes = enc.encode(enter + modes + "opencode TUI live frame");
      this.onmessage && this.onmessage({ data: bytes.buffer });
      this.onmessage && this.onmessage({ data: JSON.stringify({ t: "seq", n: bytes.length }) });
    }, 20);
  }
  send(d) { window.__sent.push(String(d)); }
  close() { this.readyState = 3; this.onclose && this.onclose({ code: 1000 }); }
};
`;

// JSON.stringify escapes the ESC (U+001B) in a mouse report as the literal text   ,
// so an SGR mouse report on the wire reads as  [<…M . `[<` is unique to SGR mouse.
const SGR_MOUSE = "\\u00" + "1b[<";

async function wheelOverTerminal(page: import("@playwright/test").Page) {
  await expect(page.locator(".xterm")).toBeVisible();
  // The socket must be OPEN (sends before readyState 1 are dropped) — gate on the
  // connect-time resize frame landing in __sent rather than a fixed delay.
  await page.waitForFunction(
    () =>
      ((window as unknown as { __sent?: unknown[] }).__sent?.length ?? 0) > 0,
  );
  await page.locator(".xterm").hover();
  await page.mouse.wheel(0, 120);
  await page.waitForTimeout(80);
  return page.evaluate(
    () => (window as unknown as { __sent: string[] }).__sent,
  );
}

test("alt-screen modes replayed on attach → a wheel scroll reports SGR mouse to the PTY (#397)", async ({
  page,
}, testInfo) => {
  // Wheel mouse-reporting is a desktop pointer concern; the mobile project has no wheel.
  test.skip(
    testInfo.project.name === "mobile",
    "wheel is a desktop-only concern",
  );
  await page.addInitScript(STUB(true));
  await page.goto("/s/claude/mode-replay-on");
  const sent = await wheelOverTerminal(page);
  // GREEN with the fix: xterm encoded the wheel as an SGR mouse report because the replayed
  // modes told it the app wants mouse events.
  expect(sent.some((f) => f.includes(SGR_MOUSE))).toBe(true);
});

test("without the mode replay, the same wheel scroll reports nothing — the #397 bug", async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.name === "mobile",
    "wheel is a desktop-only concern",
  );
  await page.addInitScript(STUB(false));
  await page.goto("/s/claude/mode-replay-off");
  const sent = await wheelOverTerminal(page);
  // RED repro: with no modes set, the alt buffer has no scrollback and the wheel produces no
  // mouse report — exactly the "scroll does nothing" the operator reported.
  expect(sent.some((f) => f.includes(SGR_MOUSE))).toBe(false);
});
