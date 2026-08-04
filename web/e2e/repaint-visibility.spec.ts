import { expect, test } from "@playwright/test";

// #503: returning to a backgrounded/minimized tab should auto-repaint (the same non-destructive
// rows−1→rows nudge as the REPAINT button), because a frozen/blanked tab often comes back stale.
// Real-browser test: stub the WebSocket to record frames, fire `visibilitychange`, and assert a
// fresh resize ({"t":"r",…}) frame is emitted. role defaults to "owner", so the effect is active.

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

const resizeCount = () =>
  ((window as unknown as { __sent?: string[] }).__sent ?? []).filter((f) =>
    f.includes('"t":"r"'),
  ).length;

test("returning to the foreground auto-repaints (a fresh resize nudge) for the owner (#503)", async ({
  page,
}) => {
  await page.addInitScript(RECORDING_WS);
  await page.goto("/s/claude/repaint-503");
  await expect(page.locator(".xterm")).toBeVisible();
  // role defaults to owner → the REPAINT control is present and the auto-repaint effect is wired.
  await expect(
    page.getByRole("button", { name: /repaint screen/i }),
  ).toBeVisible();
  // Let the initial connect + fit settle (its own resize frames land first).
  await page.waitForFunction(
    () =>
      ((window as unknown as { __sent?: unknown[] }).__sent?.length ?? 0) > 0,
  );
  const before = await page.evaluate(resizeCount);

  // Simulate returning to the tab.
  await page.evaluate(() =>
    document.dispatchEvent(new Event("visibilitychange")),
  );

  // The auto-repaint jiggle emits new resize frames.
  await page.waitForFunction((b) => {
    const n = (
      (window as unknown as { __sent?: string[] }).__sent ?? []
    ).filter((f) => f.includes('"t":"r"')).length;
    return n > b;
  }, before);
});
