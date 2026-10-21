import { expect, test } from "@playwright/test";

// #554: auto copy-on-select. On top of #536 (plain-drag selection under app mouse-tracking +
// Ctrl/Cmd+C copy), finishing a MOUSE selection copies it to the clipboard automatically — no
// keypress — and flashes a "Copied" toast. A plain click (no selection) must never clobber the
// clipboard. Real browser required: jsdom models neither xterm's mouse routing/selection nor the
// async clipboard. Desktop + chromium only (clipboard-read permission is chromium-only).

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
const CLAUDE =
  "\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h\x1b[?2004h\x1b[H\x1b[2J";

async function dragSelect(page: import("@playwright/test").Page) {
  const box = (await page.locator(".xterm-screen").boundingBox())!;
  const y = box.y + 8;
  await page.mouse.move(box.x + 10, y);
  await page.mouse.down();
  await page.mouse.move(box.x + 180, y, { steps: 8 });
  await page.mouse.up();
}

async function openTerm(page: import("@playwright/test").Page, id: string) {
  await page.addInitScript(stub(CLAUDE));
  await page.goto(`/s/claude/${id}`);
  await expect
    .poll(async () => page.locator(".xterm-screen").innerText(), {
      timeout: 5000,
    })
    .toContain("selectable");
}

test("auto copy-on-select copies the selection to the clipboard + flashes the toast (#554)", async ({
  page,
  browserName,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "mouse behavior");
  test.skip(
    browserName !== "chromium",
    "clipboard-read permission is chromium-only",
  );
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await openTerm(page, "copysel-auto");
  await page.evaluate(() => navigator.clipboard.writeText("SENTINEL"));

  await dragSelect(page); // NO Ctrl+C — the selection alone must land on the clipboard
  await expect
    .poll(async () => page.evaluate(() => navigator.clipboard.readText()))
    .toContain("selectable");
  // The confirmation toast appears (it auto-hides after COPIED_TOAST_MS, so assert promptly).
  await expect(page.locator("[data-copied-toast]")).toBeVisible();
});

test("a plain click (no selection) never clobbers the clipboard (#554 guard)", async ({
  page,
  browserName,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "mouse behavior");
  test.skip(
    browserName !== "chromium",
    "clipboard-read permission is chromium-only",
  );
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await openTerm(page, "copysel-guard");
  await page.evaluate(() => navigator.clipboard.writeText("SENTINEL"));

  await page.locator(".xterm-screen").click(); // focus, no drag → no selection
  // Give any (wrong) auto-copy a chance to fire, then assert the sentinel is untouched.
  await page.waitForTimeout(150);
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
    "SENTINEL",
  );
  await expect(page.locator("[data-copied-toast]")).toHaveCount(0);
});
