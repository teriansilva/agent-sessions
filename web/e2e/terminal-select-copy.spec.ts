import { expect, test } from "@playwright/test";

// #536: select + copy in the terminal. Mouse-tracking agents (?1000h ?1002h ?1003h, re-emitted on
// every attach by #397) make xterm suppress unmodified drag-selection — so a plain drag selected
// nothing, and Ctrl+C sent ^C = SIGINT even when the user had a selection to copy.
// #617: the #536 twin was gated on the NORMAL buffer to protect the clickable UI of alt-screen
// TUIs. Wrong axis — claude (>=2.1.178) and opencode BOTH render mouse-tracking on the ALTERNATE
// screen, so selection was dead in exactly the agents that needed it. The gate is now the GESTURE:
// a press that moves is a drag (select), a press that lifts in place is a click (forward to the
// TUI). Buffer type no longer participates.
// Real browser required: jsdom models neither xterm's mouse routing nor selection rendering.
//
// Pinned here, per session shape:
//  - ALT screen + mouse (claude >=2.1.178, opencode): plain drag SELECTS; a plain CLICK still
//    reaches the TUI as a mouse report; Ctrl+C with a selection copies and sends NO ^C.
//  - NORMAL buffer + mouse (claude <=2.1.177): unchanged — plain drag selects.
//  - NORMAL buffer, NO mouse (antigravity, codex): xterm's native selection, no twin (#582).
//  - Ctrl+C without a selection still interrupts.

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

// claude's REAL attach prefix since 2.1.178: alternate screen + mouse tracking + SGR + bracketed
// paste. Verified against 39 recorded boot rings — it enters ?1049h at startup and never leaves.
const CLAUDE =
  "\x1b[?1049h\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h\x1b[?2004h\x1b[H\x1b[2J";
// opencode: alt screen + mouse tracking (the #397/#414 contract this fix must not break).
const OPENCODE = "\x1b[?1049h\x1b[?1000h\x1b[?1006h\x1b[H\x1b[2J";
// claude <= 2.1.177 (and any inline mouse-tracking agent): mouse tracking on the NORMAL buffer.
const CLAUDE_LEGACY =
  "\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h\x1b[?2004h\x1b[H\x1b[2J";
// antigravity (`agy`): INLINE on the normal buffer with NO mouse tracking (verified against agy
// 1.0.16 — it flips ?1049h only for the ~0.6s splash, then leaves it and renders the conversation
// inline, arming only ?2004h and never ?1000/?1002/?1003/?1006). xterm selects natively here, so
// the force-select twin must stay OUT of the way; firing it turned the drag into an empty
// Shift-incremental extend and selected nothing — the reported "selection doesn't work" in agy.
const AGY = "\x1b[?2004h\x1b[H\x1b[2J";

const SIGINT_FRAME = '"d":"\\u0003"'; // {"t":"i","d":"\x03"} — ^C reaching the PTY

async function dragSelect(
  page: import("@playwright/test").Page,
  shift = false,
) {
  const box = (await page.locator(".xterm-screen").boundingBox())!;
  const y = box.y + 8;
  if (shift) await page.keyboard.down("Shift");
  await page.mouse.move(box.x + 10, y);
  await page.mouse.down();
  await page.mouse.move(box.x + 180, y, { steps: 8 });
  await page.mouse.up();
  if (shift) await page.keyboard.up("Shift");
}

/** A press that lifts where it landed: the click a TUI's own clickable UI depends on. */
async function clickInPlace(page: import("@playwright/test").Page) {
  const box = (await page.locator(".xterm-screen").boundingBox())!;
  await page.mouse.move(box.x + 40, box.y + 8);
  await page.mouse.down();
  await page.mouse.up();
}

const selectionCells = (page: import("@playwright/test").Page) =>
  page.evaluate(
    () => document.querySelector(".xterm-selection")?.childElementCount ?? 0,
  );

/** xterm paints `.xterm-selection` on its next frame, so an instantaneous gesture (click,
 *  double-click) has no cells the moment the mouse event returns. Poll, never read once. */
const expectSelection = async (page: import("@playwright/test").Page) =>
  expect.poll(async () => selectionCells(page)).toBeGreaterThan(0);

const sentJoined = (page: import("@playwright/test").Page) =>
  page.evaluate(() =>
    (window as never as { __sent: string[] }).__sent.join(""),
  );

/** The SGR mouse reports the app actually received (`ESC [ < b ; x ; y M|m`). ESC is JSON-encoded
 *  on the wire, so the literal "[<" marker is what survives. */
const mouseReports = (page: import("@playwright/test").Page) =>
  page.evaluate(() =>
    (window as never as { __sent: string[] }).__sent.filter((f) =>
      f.includes("[<"),
    ),
  );

async function openTerm(
  page: import("@playwright/test").Page,
  prefix: string,
  id: string,
) {
  await page.addInitScript(stub(prefix));
  await page.goto(`/s/claude/${id}`);
  await expect
    .poll(async () => page.locator(".xterm-screen").innerText(), {
      timeout: 5000,
    })
    .toContain("selectable");
}

test("alt-screen mouse-tracking agent (claude >=2.1.178): plain drag SELECTS (#617)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "mouse behavior");
  // RED before #617: the buffer guard skipped the twin on the alt screen, so the drag was forwarded
  // to the agent and nothing was selected — copy-on-select never fired and Ctrl+C SIGINTed instead.
  await openTerm(page, CLAUDE, "selcopy-alt-drag");
  await dragSelect(page);
  await expectSelection(page);
  // The override must not break typing: a keystroke after the drag still reaches the PTY.
  await page.keyboard.type("x");
  await expect
    .poll(async () => sentJoined(page))
    .toContain('{"t":"i","d":"x"}');
});

test("alt-screen TUI still gets a plain CLICK as a mouse report (#617 — the click, not the buffer)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "mouse behavior");
  // This is what the old buffer guard was really protecting: opencode's clickable UI. A press that
  // lifts in place is replayed to xterm unmodified, so the SGR report still reaches the app.
  await openTerm(page, OPENCODE, "selcopy-alt-click");
  await clickInPlace(page);
  // A COMPLETE press+release pair at the same cell — a TUI hit-tests on the pair, and a lone
  // release (or a lone press) would leave its click handling half-finished.
  await expect.poll(async () => (await mouseReports(page)).length).toBe(2);
  const [press, release] = await mouseReports(page);
  expect(press).toMatch(/\[<0;\d+;\d+M/); // 'M' = button press
  expect(release).toMatch(/\[<0;\d+;\d+m/); // 'm' = release
  expect(press.replace(/M"?}?$/, "")).toBe(release.replace(/m"?}?$/, "")); // same button + cell
  await page.waitForTimeout(150); // let a selection paint if one (wrongly) started
  expect(await selectionCells(page)).toBe(0); // a click selects nothing
});

test("alt-screen TUI: a DRAG selects and leaks NO mouse report to the app (#617)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "mouse behavior");
  await openTerm(page, OPENCODE, "selcopy-alt-tui-drag");
  await dragSelect(page);
  await expectSelection(page);
  // The press was swallowed, so the app must not see a stray half-gesture (a release with no
  // press) that it could mistake for a click.
  expect(await mouseReports(page)).toEqual([]);
});

test("normal-buffer mouse-tracking agent (claude <=2.1.177): plain drag still selects (#536)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "mouse behavior");
  await openTerm(page, CLAUDE_LEGACY, "selcopy-normal-drag");
  await dragSelect(page);
  await expectSelection(page);
});

test("double-click selects a word even though no drag follows (#617)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "mouse behavior");
  // detail > 1 short-circuits the drag-vs-click deferral, or word/line select would never fire.
  await openTerm(page, CLAUDE, "selcopy-dblclick");
  const box = (await page.locator(".xterm-screen").boundingBox())!;
  await page.mouse.dblclick(box.x + 20, box.y + 8); // inside "hello", not the space after it
  await expectSelection(page);
});

test("inline agent with NO mouse tracking (antigravity) still selects on a plain drag (#582)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "mouse behavior");
  await openTerm(page, AGY, "selcopy-nomouse");
  await dragSelect(page);
  await expectSelection(page);
});

test("Ctrl+C with a selection copies it and never reaches the PTY (#536)", async ({
  page,
  browserName,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "mouse behavior");
  test.skip(
    browserName !== "chromium",
    "clipboard-read permission is chromium-only",
  );
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await openTerm(page, CLAUDE, "selcopy-copy");
  await page.evaluate(() => navigator.clipboard.writeText("SENTINEL"));
  await dragSelect(page);
  await page.keyboard.press("Control+c");
  await expect
    .poll(async () => page.evaluate(() => navigator.clipboard.readText()))
    .toContain("selectable");
  const sent = await page.evaluate(
    () => (window as never as { __sent: string[] }).__sent,
  );
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
        (window as never as { __sent: string[] }).__sent.some((f) =>
          f.includes('"d":"\\u0003"'),
        ),
      ),
    )
    .toBe(true);
});

test("Shift+drag keeps selecting (an explicitly modified press is the user's intent)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "mouse behavior");
  await openTerm(page, OPENCODE, "selcopy-shift");
  await dragSelect(page, true);
  await expectSelection(page);
});
