import { expect, test } from "@playwright/test";

// #209: Ctrl+V over the terminal must NOT forward the raw ^V (U+0016) keystroke to the PTY
// — the agent (Claude Code) reads ^V as "paste image from clipboard" and prints "no image
// found in clipboard" on a text paste. xterm's custom key handler suppresses the keystroke
// without preventDefault, so the browser's native paste event still drives the real paste.
// A text paste event must still reach the PTY (the #181 capture-phase path).

// A WebSocket stub that records every frame the app sends (JSON `{t:"i",d}` for input).
const RECORDING_WS = `
window.__sent = [];
window.WebSocket = class {
  constructor(url) { this.url = url; this.readyState = 0; this.binaryType = "arraybuffer";
    setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); }, 20);
  }
  send(d) { window.__sent.push(String(d)); }
  close() { this.readyState = 3; this.onclose && this.onclose({ code: 1000 }); }
};
`;

// The ^V control char (U+0016) appears JSON-escaped as  on the wire.
const CTRL_V_ESCAPED = "\\u00" + "16";

test("Ctrl+V over the terminal sends no raw ^V keystroke to the PTY (#209)", async ({
  page,
}, testInfo) => {
  // Ctrl+V is a physical-keyboard (desktop) concern; on the touch/mobile project there is
  // no hardware Ctrl+V and the touch overlay owns focus, so the assertion isn't meaningful.
  test.skip(
    testInfo.project.name === "mobile",
    "Ctrl+V is a desktop-only concern",
  );
  await page.addInitScript(RECORDING_WS);
  await page.goto("/s/claude/paste-keys");
  await expect(page.locator(".xterm")).toBeVisible();
  // Wait for the socket to actually OPEN — sends are dropped before readyState 1. The terminal now
  // attaches only once the grid goes quiet (#304), which can land past a fixed delay, so gate on the
  // connect-time resize frame appearing in __sent rather than a timeout.
  await page.waitForFunction(
    () =>
      ((window as unknown as { __sent?: unknown[] }).__sent?.length ?? 0) > 0,
  );

  // A REAL (trusted) Ctrl+V so xterm runs its actual key path — without the custom handler
  // this is exactly what would emit ^V to the PTY.
  await page.locator(".xterm").click();
  await page.keyboard.press("Control+v");
  await page.waitForTimeout(50);

  const sent = await page.evaluate(
    () => (window as unknown as { __sent: string[] }).__sent,
  );
  expect(sent.some((f) => f.includes(CTRL_V_ESCAPED))).toBe(false);
});

test("a text paste over the terminal still reaches the PTY (#181 path intact)", async ({
  page,
}) => {
  await page.addInitScript(RECORDING_WS);
  await page.goto("/s/claude/paste-text");
  await expect(page.locator(".xterm")).toBeVisible();
  // Wait for the socket to OPEN (see the #304 note above) before pasting, so the frame isn't dropped.
  await page.waitForFunction(
    () =>
      ((window as unknown as { __sent?: unknown[] }).__sent?.length ?? 0) > 0,
  );

  // Synthesize a text paste on a node inside the terminal host; the capture-phase listener
  // forwards it via term.paste(text) (#181).
  await page.locator(".xterm").evaluate((host) => {
    const dt = new DataTransfer();
    dt.setData("text/plain", "hello-from-paste");
    host.dispatchEvent(
      new ClipboardEvent("paste", {
        clipboardData: dt,
        bubbles: true,
        cancelable: true,
      }),
    );
  });

  const sent = await page.evaluate(
    () => (window as unknown as { __sent: string[] }).__sent,
  );
  expect(sent.some((f) => f.includes("hello-from-paste"))).toBe(true);
});
