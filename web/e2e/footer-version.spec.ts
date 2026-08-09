import { expect, test } from "@playwright/test";

// #661: the status footer shows the running version. The preview build is unstamped ("dev"),
// so the tag mirrors the server's /api/version — route it deterministically (the static
// preview has no backend). Runs under both the desktop and mobile projects.

// No-op WebSocket so the shell mounts without a backend (E2E serves the static SPA only).
const NOOP_WS = `
window.WebSocket = class {
  constructor() { this.readyState = 0; this.binaryType = "arraybuffer";
    setTimeout(() => { this.readyState = 1; if (this.onopen) this.onopen(); }, 20); }
  send() {} close() { this.readyState = 3; if (this.onclose) this.onclose({ code: 1000 }); }
};
`;

test("the status footer renders the running version as a hud tag (#661)", async ({
  page,
}) => {
  await page.addInitScript(NOOP_WS);
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "9.9.9" } }),
  );
  await page.goto("/");
  const footer = page.locator("footer.hud-classbar");
  await expect(footer).toBeVisible();
  await expect(footer.locator(".hud-version")).toHaveText("V9.9.9");
  // Unstamped build + fresh context (no SW swap) ⇒ the update chip must NOT show: an
  // unstamped bundle can't claim to be stale, and there's no fresh shell to offer.
  await expect(footer.locator(".hud-update-chip")).toHaveCount(0);
});
