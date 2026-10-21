import { expect, test } from "@playwright/test";

// The compose key bar must carry LEFT and RIGHT arrow chips next to up/down — TUIs like
// opencode's permission dialog ("Allow once / Allow always / Reject") are navigated with
// ←/→, and a phone has no other way to send them. Asserts the actual {"t":"i","d":…} frames
// the client sends, on desktop + mobile.

// WebSocket stub recording every frame the app sends (mirrors compose-actions.spec.ts).
const RECORDING_WS = `
window.__sent = [];
window.WebSocket = class {
  constructor(url) { this.url = url; this.readyState = 0; this.binaryType = "arraybuffer";
    setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); }, 20); }
  send(d) { window.__sent.push(String(d)); }
  close() { this.readyState = 3; this.onclose && this.onclose({ code: 1000 }); }
};
`;

const sent = (page: import("@playwright/test").Page) =>
  page.evaluate(
    () => (window as unknown as { __sent?: string[] }).__sent ?? [],
  );

/** Click a key-bar chip wherever it currently lives — inline, or inside the "…" overflow. */
async function tapKey(page: import("@playwright/test").Page, name: string) {
  const chip = page.getByRole("button", { name, exact: true });
  if (await chip.isVisible()) {
    await chip.click();
    return;
  }
  await page.getByRole("button", { name: /more keys/i }).click();
  await page.getByRole("menuitem", { name, exact: true }).click();
}

test("key bar sends Left and Right arrow sequences to the PTY", async ({ page }) => {
  await page.addInitScript(RECORDING_WS);
  await page.goto("/s/claude/left-right-keys");
  await expect(page.locator(".xterm")).toBeVisible();
  await page.waitForFunction(
    () =>
      ((window as unknown as { __sent?: unknown[] }).__sent?.length ?? 0) > 0,
  );

  // Open the box if collapsed (desktop default) so the key bar renders.
  const send = page.getByRole("button", { name: /^send/i });
  if (!(await send.isVisible())) {
    await page.getByRole("button", { name: /open compose box/i }).click();
  }
  await expect(send).toBeVisible();

  await tapKey(page, "Left");
  await tapKey(page, "Right");
  await expect
    .poll(() => sent(page))
    .toContain('{"t":"i","d":"\\u001b[D"}');
  await expect
    .poll(() => sent(page))
    .toContain('{"t":"i","d":"\\u001b[C"}');
});
