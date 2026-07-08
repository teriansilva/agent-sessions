import { expect, test } from "@playwright/test";

// #536: select + copy in the terminal. claude arms mouse tracking (?1000h ?1002h ?1003h,
// re-emitted on every attach by #397), and xterm.js suppresses unmodified drag-selection
// whenever the app owns the mouse — so plain drag selected nothing, and Ctrl+C (no copy path
// existed) sent ^C = SIGINT to the agent even when the user had a selection to copy.
// Real browser required: jsdom models neither xterm's mouse routing nor selection rendering.
//
// Pinned here:
//  - NORMAL buffer (inline agents): plain drag selects; Ctrl+C with a selection copies and
//    sends NO ^C; Ctrl+C without a selection still interrupts (sends ^C).
//  - ALT screen (opencode): plain drag still forwards mouse to the TUI (no selection —
//    clicks there are real UI); Shift+drag remains the selection path.

const stub = (prefix: string) => `
window.__sent = [];
window.WebSocket = class {
  constructor(url) { this.url = url; this.readyState = 0; this.binaryType = "arraybuffer";
    setTimeout(() => {
      this.readyState = 1; this.onopen && this.onopen();
      const s = ${JSON.stringify(prefix)} + "hello selectable world here";
      const buf = new TextEncoder().encode(s).buffer;
      this.onmessage && this.onmessage({ data: buf });
      this.onmessage && this.onmessage({ data: JSON.stringify({ t: "seq", n: s.length }) });
    }, 30);
  }
  send(d) { window.__sent.push(String(d)); }
  close() { this.readyState = 3; this.onclose && this.onclose({ code: 1000 }); }
};
`;

// claude's real attach prefix: mouse tracking + SGR + bracketed paste, then a normal-buffer frame.
const CLAUDE = "\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h\x1b[?2004h\x1b[H\x1b[2J";
// opencode's: alt screen + mouse tracking (the #397/#414 contract this fix must not break).
const OPENCODE = "\x1b[?1049h\x1b[?1000h\x1b[?1006h\x1b[H\x1b[2J";
// antigravity (`agy`): INLINE on the normal buffer with NO mouse tracking (verified against agy
// 1.0.16 — it flips ?1049h only for the ~0.6s splash, then leaves it and renders the conversation
// inline, arming only ?2004h and never ?1000/?1002/?1003/?1006). xterm selects natively here, so
// the #536 force-select twin must stay OUT of the way; firing it turned the drag into an empty
// Shift-incremental extend and selected nothing — the reported "selection doesn't work" in agy.
const AGY = "\x1b[?2004h\x1b[H\x1b[2J";

const SIGINT_FRAME = '"d":"\\u0003"'; // {"t":"i","d":"\x03"} — ^C reaching the PTY

async function dragSelect(page: import("@playwright/test").Page, shift = false) {
  const box = (await page.locator(".xterm-screen").boundingBox())!;
  const y = box.y + 8;
  if (shift) await page.keyboard.down("Shift");
  await page.mouse.move(box.x + 10, y);
  await page.mouse.down();
  await page.mouse.move(box.x + 180, y, { steps: 8 });
  await page.mouse.up();
  if (shift) await page.keyboard.up("Shift");
}

const selectionCells = (page: import("@playwright/test").Page) =>
  page.evaluate(() => document.querySelector(".xterm-selection")?.childElementCount ?? 0);

async function openTerm(page: import("@playwright/test").Page, prefix: string, id: string) {
  await page.addInitScript(stub(prefix));
  await page.goto(`/s/claude/${id}`);
  await expect
    .poll(async () => page.locator(".xterm-screen").innerText(), { timeout: 5000 })
    .toContain("selectable");
}

test("plain drag selects text while the agent owns the mouse (#536)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "mouse behavior");
  await openTerm(page, CLAUDE, "selcopy-drag");
  await dragSelect(page);
  expect(await selectionCells(page)).toBeGreaterThan(0);
  // The override must not break typing: a keystroke after the drag still reaches the PTY.
  await page.keyboard.type("x");
  await expect
    .poll(async () => page.evaluate(() => (window as never as { __sent: string[] }).__sent))
    .toContain('{"t":"i","d":"x"}');
});

test("inline agent with NO mouse tracking (antigravity) still selects on a plain drag", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "mouse behavior");
  // RED before the fix: the #536 force-select twin fired for agy too (normal buffer), replacing
  // the plain press with a Shift press → xterm did an empty incremental extend → nothing selected.
  // GREEN after: with no mouse tracking the twin is skipped, so xterm's native drag-selection runs.
  await openTerm(page, AGY, "selcopy-nomouse");
  await dragSelect(page);
  expect(await selectionCells(page)).toBeGreaterThan(0);
});

test("Ctrl+C with a selection copies it and never reaches the PTY (#536)", async ({
  page,
  browserName,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "mouse behavior");
  test.skip(browserName !== "chromium", "clipboard-read permission is chromium-only");
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await openTerm(page, CLAUDE, "selcopy-copy");
  await page.evaluate(() => navigator.clipboard.writeText("SENTINEL"));
  await dragSelect(page);
  await page.keyboard.press("Control+c");
  await expect
    .poll(async () => page.evaluate(() => navigator.clipboard.readText()))
    .toContain("selectable");
  const sent = await page.evaluate(() => (window as never as { __sent: string[] }).__sent);
  expect(sent.some((f) => f.includes(SIGINT_FRAME))).toBe(false); // the copy never interrupts
  // The selection survives the copy (browser copy semantics) — no clearing side effects.
  expect(await selectionCells(page)).toBeGreaterThan(0);
});

test("Ctrl+C without a selection still interrupts the agent (#536 guard)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "mouse behavior");
  await openTerm(page, CLAUDE, "selcopy-sigint");
  await page.locator(".xterm-screen").click(); // focus, no drag → no selection
  await page.keyboard.press("Control+c");
  await expect
    .poll(async () =>
      page.evaluate(() =>
        (window as never as { __sent: string[] }).__sent.some((f) => f.includes('"d":"\\u0003"')),
      ),
    )
    .toBe(true);
});

test("alt-screen TUI keeps its mouse: plain drag forwards, Shift+drag selects (#536 pin)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "mouse behavior");
  await openTerm(page, OPENCODE, "selcopy-alt");
  await dragSelect(page);
  expect(await selectionCells(page)).toBe(0); // the TUI got the drag, not the selection layer
  // SGR mouse reports reached the app (ESC is JSON-encoded in the frame text, so the
  // literal "[<" marker is what survives on the wire).
  await expect
    .poll(async () =>
      page.evaluate(() => (window as never as { __sent: string[] }).__sent.join("")),
    )
    .toContain("[<");
  await dragSelect(page, true);
  expect(await selectionCells(page)).toBeGreaterThan(0); // Shift override intact
});
