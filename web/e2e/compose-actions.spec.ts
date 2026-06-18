import { expect, test } from "@playwright/test";

// #487: the compose action bar is consolidated. Only the mic + Send stay inline; every other
// action (nav keys, copy, attach, interrupt, and collapse) lives in a single "…" menu that opens
// UPWARD from the bottom-anchored bar. Real-browser test: inline set, a portalled upward menu, and
// the interrupt PTY frame.

// WebSocket stub recording every frame the app sends (mirrors compose-empty-send.spec.ts).
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

const CTRLC_FRAME = '"d":"\\u0003"'; // {"t":"i","d":"\x03"} — Ctrl-C, JSON-escaped

test("only mic + Send stay inline; attach/interrupt/collapse live in the … menu, opening upward (#487)", async ({
  page,
}) => {
  await page.addInitScript(RECORDING_WS);
  await page.goto("/s/claude/compose-actions-487");
  await expect(page.locator(".xterm")).toBeVisible();
  await page.waitForFunction(
    () => ((window as unknown as { __sent?: unknown[] }).__sent?.length ?? 0) > 0,
  );

  // Open the box if collapsed (desktop default) so Send renders.
  const send = page.getByRole("button", { name: /^send/i });
  if (!(await send.isVisible())) {
    await page.getByRole("button", { name: /open compose box/i }).click();
  }
  await expect(send).toBeVisible();

  // Attach + interrupt are NOT inline buttons anymore — only reachable from the menu.
  await expect(page.getByRole("button", { name: /attach file/i })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /interrupt/i })).toHaveCount(0);

  // Open the "…" menu → it opens ABOVE the trigger and holds the consolidated actions.
  const more = page.getByRole("button", { name: /more actions/i });
  const moreBox = (await more.boundingBox())!;
  await more.click();
  const attachItem = page.getByRole("menuitem", { name: /attach file/i });
  await expect(attachItem).toBeVisible();
  await expect(page.getByRole("menuitem", { name: /interrupt/i })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: /collapse compose/i })).toBeVisible();
  // Upward: every menu item sits above the trigger's top edge.
  expect((await attachItem.boundingBox())!.y).toBeLessThan(moreBox.y);

  // Interrupt still sends a Ctrl-C frame to the PTY, then the menu closes.
  await page.getByRole("menuitem", { name: /interrupt/i }).click();
  await page.waitForFunction(
    (m) => ((window as unknown as { __sent?: string[] }).__sent ?? []).some((f) => f.includes(m)),
    CTRLC_FRAME,
  );
  await expect(page.getByRole("menuitem", { name: /interrupt/i })).toBeHidden();
});
