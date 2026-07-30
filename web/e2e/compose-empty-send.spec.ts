import { expect, test } from "@playwright/test";

// #474: with the compose box EMPTY, the Send button must act as a bare Return — a single
// `{t:"i",d:"\r"}` frame — so it submits whatever the user typed DIRECTLY into the console.
// Previously empty Send was a no-op (no frame at all). The bare \r must NOT be preceded by the
// content path's line-clear (Ctrl-A Ctrl-K) or bracketed paste, which would erase the
// console-typed prompt line instead of submitting it.

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

const ENTER_FRAME = '"d":"\\r"'; // {"t":"i","d":"\r"} — a bare carriage return
const CLEAR_SEQ = "\\u0001\\u000b"; // Ctrl-A Ctrl-K, the content path's line-clear
const PASTE_START = "\\u001b[200~"; // bracketed-paste opener, the content path

test("empty compose Send sends a bare Return to submit console-typed input (#474)", async ({
  page,
}) => {
  await page.addInitScript(RECORDING_WS);
  await page.goto("/s/claude/empty-send-474");
  await expect(page.locator(".xterm")).toBeVisible();
  // Wait for the socket to OPEN — frames sent before readyState 1 are dropped. The connect-time
  // resize frame appearing in __sent is the signal (see terminal-paste.spec.ts / #304).
  await page.waitForFunction(
    () => ((window as unknown as { __sent?: unknown[] }).__sent?.length ?? 0) > 0,
  );

  // The Send button only renders when the compose box is open (collapsed by default on desktop,
  // open on mobile). Open it if needed, then leave the textarea empty.
  const sendBtn = page.getByRole("button", { name: /^send/i });
  if (!(await sendBtn.isVisible())) {
    await page.getByRole("button", { name: /open compose box/i }).click();
  }
  await expect(sendBtn).toBeVisible();

  await sendBtn.click();

  // A single bare Return must reach the PTY...
  await page.waitForFunction(
    (frame) =>
      ((window as unknown as { __sent?: string[] }).__sent ?? []).some((f) => f.includes(frame)),
    ENTER_FRAME,
  );
  const sent = await page.evaluate(() => (window as unknown as { __sent: string[] }).__sent);
  // ...and the destructive content path must NOT have run (no clear, no bracketed paste).
  expect(sent.some((f) => f.includes(CLEAR_SEQ))).toBe(false);
  expect(sent.some((f) => f.includes(PASTE_START))).toBe(false);
});
