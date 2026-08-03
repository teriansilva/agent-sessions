import { expect, test, type Page } from "@playwright/test";

// #619: the safety net for a swallowed send. A compose submission is recorded BEFORE the composer
// and the server draft are cleared, so even a "successful" send whose bytes the agent discarded
// (#616) stays recoverable from the history chip next to the collapse ✕.
// Real browser: the modal is portalled to <body> over a backdrop-filter pane, and the ring lives in
// localStorage — jsdom models neither the portal/overlay geometry nor a real storage round-trip.

// Records every frame the app sends. `__drop` makes the socket go quietly non-OPEN once it has seen
// the bracketed paste, so the DEFERRED Enter (60ms later, #180) finds a dead socket — the reconnect
// gap of #287, and the only way a send legitimately ends up UNCONFIRMED.
const RECORDING_WS = (dropAfterPaste: boolean) => `
window.__sent = [];
window.__input = [];
window.WebSocket = class {
  constructor(url) {
    this.url = url; this.readyState = 0; this.binaryType = "arraybuffer";
    setTimeout(() => {
      this.readyState = 1; this.onopen && this.onopen();
      const s = "\\x1b[?2004h\\x1b[H\\x1b[2Jready \\u276f ";
      this.onmessage && this.onmessage({ data: new TextEncoder().encode(s).buffer });
      this.onmessage && this.onmessage({ data: JSON.stringify({ t: "seq", n: s.length }) });
    }, 20);
  }
  send(msg) {
    window.__sent.push(String(msg));
    try {
      const m = JSON.parse(msg);
      if (m && m.t === "i") {
        window.__input.push(m.d);
        if (${dropAfterPaste} && m.d.indexOf("\\u001b[200~") !== -1) this.readyState = 3;
      }
    } catch {}
  }
  close() { this.readyState = 3; this.onclose && this.onclose({ code: 1000 }); }
};
`;

declare global {
  interface Window {
    __sent: string[];
    __input: string[];
  }
}

async function boot(page: Page, id: string, dropAfterPaste = false) {
  await page.route(/\/api\/sessions\/[^/]+\/draft$/, (r) =>
    r.fulfill({ json: { id: "", text: "", attachments: [], updated_at: null } }),
  );
  await page.addInitScript(RECORDING_WS(dropAfterPaste));
  await page.goto(`/s/claude/${id}`);
  await expect(page.locator(".xterm")).toBeVisible();
  // Frames sent before readyState 1 are dropped: wait for the connect-time resize frame, exactly as
  // compose-empty-send.spec.ts does. Clicking Send before this races the socket, not the feature.
  await page.waitForFunction(() => (window.__sent?.length ?? 0) > 0);
}

/** The Send button only renders once the compose box is open (collapsed by default on desktop). */
async function openCompose(page: Page) {
  const sendBtn = page.getByRole("button", { name: /^send/i });
  if (!(await sendBtn.isVisible())) {
    await page.getByRole("button", { name: /open compose box/i }).click();
  }
  await expect(sendBtn).toBeVisible();
  return sendBtn;
}

/** The chip may be collapsed into KeyBar's "…" overflow menu on a narrow (mobile) bar, where it
 *  renders as a `role="menuitem"` rather than a button — so match on the (stable) aria-label. */
async function openHistory(page: Page) {
  const chip = page.getByLabel(/sent messages/i);
  if ((await chip.count()) === 0) {
    await page.getByRole("button", { name: /more keys/i }).click();
  }
  await chip.click();
  return page.getByRole("dialog");
}

test("a sent message survives the composer clear and can be restored (#619)", async ({ page }) => {
  await boot(page, "hist-restore");
  const sendBtn = await openCompose(page);
  const ta = page.getByPlaceholder(/type here/i);

  // Nothing to recover yet → no chip. A natural empty state, not a feature flag.
  await expect(page.getByLabel(/sent messages/i)).toHaveCount(0);

  const long = "a looong message that must never be lost, even if the agent swallows it";
  await ta.fill(long);
  await sendBtn.click();

  // Delivered: the composer clears. This is precisely the moment the text used to vanish forever.
  await expect.poll(async () => page.evaluate(() => window.__input.at(-1))).toBe("\r");
  await expect(ta).toHaveValue("");

  // …but it is recoverable.
  const dialog = await openHistory(page);
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText(long)).toBeVisible();
  await expect(dialog.getByText(/unconfirmed/i)).toHaveCount(0); // the Enter reached the socket

  await dialog.getByRole("button", { name: /restore/i }).click();
  await expect(dialog).toHaveCount(0); // restoring closes the modal
  await expect(ta).toHaveValue(long); // …and the exact text is back in the composer
});

test("a send whose deferred Enter never lands is kept and flagged UNCONFIRMED (#619)", async ({
  page,
}) => {
  await boot(page, "hist-unconfirmed", /* dropAfterPaste */ true);
  const sendBtn = await openCompose(page);
  const ta = page.getByPlaceholder(/type here/i);

  await ta.fill("this one never landed");
  await sendBtn.click();

  // The paste went out; the socket died before the deferred Enter (#287) → the turn never submitted.
  await expect
    .poll(async () => page.evaluate(() => window.__input.some((d) => d.includes("\x1b[200~"))))
    .toBe(true);
  await expect(page.getByText(/not sent/i)).toBeVisible();
  await expect(ta).toHaveValue("this one never landed"); // #287: the composer is restored

  const dialog = await openHistory(page);
  await expect(dialog.getByText("this one never landed")).toBeVisible();
  await expect(dialog.getByText(/unconfirmed/i)).toBeVisible();
});
