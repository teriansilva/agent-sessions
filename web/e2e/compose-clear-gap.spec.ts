import { expect, test, type Page } from "@playwright/test";

// #1062: a compose Send is three frames — the line-clear (Ctrl-A Ctrl-K), the bracketed paste, and
// the Enter. The clear and the paste used to go out in the SAME task, so they reached the agent in
// one pty read; claude 2.1.278 then holds a paste that carries an image path PENDING, and the first
// Enter finalises that paste instead of submitting the turn. That is the "I had to press Enter
// twice" bug, and it is the same boundary defect #180 fixed for the trailing \r, at the boundary
// that was never split.
//
// Measured against a live claude PTY, submission confirmed from claude's OWN transcript store
// (a submitted turn writes a `user` record) rather than from the screen:
//
//   gap 0ms,  Enter delay 120ms  → second Enter needed, 3/3
//   gap 0ms,  Enter delay 1500ms → second Enter needed, 1/1   ← not a race a longer wait fixes
//   gap 16 / 40 / 80ms           → submitted on the first press
//
// Real browser rather than jsdom: what has to hold is a property of the frames the client actually
// emits *over time*, and jsdom does not run the app's timers. The assertion is a LOWER bound only —
// setTimeout may fire late on a loaded runner but never early — so it cannot flake upward. Against
// the unfixed client the two frames are ~0 ms apart, which is the red.

// A WebSocket stub recording every frame the app sends, with the time it was sent.
const RECORDING_WS = `
window.__sent = [];
window.WebSocket = class {
  constructor(url) { this.url = url; this.readyState = 0; this.binaryType = "arraybuffer";
    setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); }, 20);
  }
  send(d) { window.__sent.push({ at: performance.now(), d: String(d) }); }
  close() { this.readyState = 3; this.onclose && this.onclose({ code: 1000 }); }
};
`;

type Frame = { at: number; d: string };
declare global {
  interface Window {
    __sent: Frame[];
  }
}

const CLEAR_SEQ = "\\u0001\\u000b"; // Ctrl-A Ctrl-K, as it appears JSON-encoded on the wire
const PASTE_START = "\\u001b[200~"; // bracketed-paste opener
const ENTER_FRAME = '"d":"\\r"'; // {"t":"i","d":"\r"} — the bare carriage return

// Comfortably under CLEAR_DELAY_MS / ENTER_DELAY_MS (80 / 60), so a late timer can never fail this,
// and far above the ~0 ms the unfixed client produced.
const MIN_GAP_MS = 40;

/** Open the session pane, wait for the socket, and reveal the composer's Send button. */
async function openComposer(page: Page, sid: string) {
  await page.addInitScript(RECORDING_WS);
  await page.goto(`/s/claude/${sid}`);
  await expect(page.locator(".xterm")).toBeVisible();
  // Frames sent before readyState 1 are dropped; the connect-time resize frame is the signal.
  await page.waitForFunction(() => (window.__sent?.length ?? 0) > 0);
  const sendBtn = page.getByRole("button", { name: /^send/i });
  if (!(await sendBtn.isVisible())) {
    await page.getByRole("button", { name: /open compose box/i }).click();
  }
  await expect(sendBtn).toBeVisible();
  return sendBtn;
}

/** The delivery's three frames, in the order the client emitted them. */
async function deliveryFrames(page: Page) {
  await page.waitForFunction(
    (f) => (window.__sent ?? []).some((x) => x.d.includes(f)),
    ENTER_FRAME,
  );
  const sent = await page.evaluate(() => window.__sent);
  const clear = sent.find((f) => f.d.includes(CLEAR_SEQ));
  const paste = sent.find((f) => f.d.includes(PASTE_START));
  const enter = sent.find((f) => f.d.includes(ENTER_FRAME));
  expect(clear, "a line-clear frame").toBeTruthy();
  expect(paste, "a bracketed-paste frame").toBeTruthy();
  expect(enter, "an Enter frame").toBeTruthy();
  return { clear: clear!, paste: paste!, enter: enter! };
}

test("the line-clear and the bracketed paste leave in separate frames, separated in time (#1062)", async ({
  page,
}) => {
  const sendBtn = await openComposer(page, "clear-gap-1062");
  await page.getByPlaceholder(/type here/i).fill("hello world");
  await sendBtn.click();

  const { clear, paste, enter } = await deliveryFrames(page);
  // Never bundled into one frame — one frame is one pty write is one read.
  expect(paste.d.includes(CLEAR_SEQ)).toBe(false);
  expect(paste.d.includes(ENTER_FRAME)).toBe(false);
  // …and never in the same TASK either, which is the part that was broken: two frames written
  // back-to-back still arrive in a single read.
  expect(paste.at - clear.at).toBeGreaterThanOrEqual(MIN_GAP_MS);
  // #180's boundary still holds.
  expect(enter.at - paste.at).toBeGreaterThanOrEqual(MIN_GAP_MS);
});

test("a send carrying an ATTACHMENT — the payload that actually broke — is spaced the same way (#1062)", async ({
  page,
}) => {
  // The production trigger: the pasted message carries an image path, which is what claude holds
  // pending when the clear arrives in the same read. The client-side guarantee is identical for
  // every payload; this pins it on the one the bug was reported against.
  await page.route("**/api/upload", (r) =>
    r.fulfill({ json: { name: "shot.png", path: "/uploads/shot.png" } }),
  );
  const sendBtn = await openComposer(page, "clear-gap-1062-attach");
  await page.getByPlaceholder(/type here/i).fill("look at this");
  // Drive the hidden file input directly rather than the Attach chip: that chip is inline on a wide
  // key bar and inside KeyBar's "…" overflow on a narrow one (#500), and which affordance opens the
  // picker is not what this spec pins — the delivery's frame spacing is.
  await page.locator('input[type="file"]').first().setInputFiles({
    name: "shot.png",
    mimeType: "image/png",
    buffer: Buffer.from([0x89, 0x50, 0x4e, 0x47]),
  });
  await expect(page.getByText("shot.png")).toBeVisible();
  await sendBtn.click();

  const { clear, paste, enter } = await deliveryFrames(page);
  expect(paste.d).toContain("/uploads/shot.png"); // the attachment really is in this paste
  expect(paste.at - clear.at).toBeGreaterThanOrEqual(MIN_GAP_MS);
  expect(enter.at - paste.at).toBeGreaterThanOrEqual(MIN_GAP_MS);
});
