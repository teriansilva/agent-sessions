import { expect, test } from "@playwright/test";

// #157: pasting an image anywhere over the terminal opens Compose and adds the upload as an
// attachment pill — it must NOT go to the PTY. Real browser: synthesize the paste event with
// a File via the Clipboard API and mock the upload endpoint.

const FAKE_WS = `
window.WebSocket = class {
  constructor(url) { this.url = url; this.readyState = 0; this.binaryType = "blob";
    setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); }, 20);
  }
  send() {} close() { this.readyState = 3; this.onclose && this.onclose({ code: 1000 }); }
};
`;

test("paste image over terminal → Compose attachment pill (#157)", async ({ page }) => {
  await page.route("**/api/upload", (r) =>
    r.fulfill({ json: { name: "shot.png", path: "/uploads/shot.png" } }),
  );
  await page.addInitScript(FAKE_WS);
  await page.goto("/s/claude/img-paste");

  const xterm = page.locator(".xterm");
  await expect(xterm).toBeVisible();

  // Dispatch a paste with an image File on a node inside the terminal host. The capture-phase
  // listener on the host catches it (`#157`) and forwards to Compose.
  await xterm.evaluate((host) => {
    const file = new File([new Uint8Array([1, 2, 3])], "shot.png", { type: "image/png" });
    const dt = new DataTransfer();
    dt.items.add(file);
    host.dispatchEvent(
      new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }),
    );
  });

  await expect(page.getByText("shot.png")).toBeVisible();
});
