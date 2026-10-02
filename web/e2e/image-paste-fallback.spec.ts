import { expect, test } from "@playwright/test";

// #530: some clipboard backends (observed: Windows Chrome 149, after its lazy clipboard-format
// rework rolled out) fire a paste event whose DataTransfer yields no materializable file even
// though the OS clipboard holds an image. Image paste must fall back to
// navigator.clipboard.read() instead of silently doing nothing. Real browser: a real PNG on the
// (permission-granted) async clipboard + a paste event with an EMPTY DataTransfer models the
// degraded sync path — red on the pre-#530 code, green with the fallback.

const FAKE_WS = `
window.WebSocket = class {
  constructor(url) { this.url = url; this.readyState = 0; this.binaryType = "blob";
    setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); }, 20);
  }
  send() {} close() { this.readyState = 3; this.onclose && this.onclose({ code: 1000 }); }
};
`;

// 1x1 transparent PNG
const PNG_B64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";

test.use({ permissions: ["clipboard-read", "clipboard-write"] });

async function putPngOnClipboard(page: import("@playwright/test").Page) {
  await page.evaluate(async (b64) => {
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const blob = new Blob([bytes], { type: "image/png" });
    await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
  }, PNG_B64);
}

test("degraded compose paste (empty DataTransfer) falls back to the async clipboard (#530)", async ({
  page,
}) => {
  await page.route("**/api/upload", (r) =>
    r.fulfill({
      json: { name: "clipboard.png", path: "/uploads/clipboard.png" },
    }),
  );
  await page.addInitScript(FAKE_WS);
  await page.goto("/s/claude/img-paste-fallback");
  await expect(page.locator(".xterm")).toBeVisible();

  const ta = page.getByPlaceholder(/Type here/);
  const toggle = page.getByRole("button", { name: "Open compose box" });
  await expect(ta.or(toggle)).toBeVisible();
  if (await toggle.isVisible().catch(() => false)) await toggle.click();
  await expect(ta).toBeVisible();

  await putPngOnClipboard(page);
  await ta.evaluate((el) => {
    el.dispatchEvent(
      new ClipboardEvent("paste", {
        clipboardData: new DataTransfer(), // the degraded sync path: no items, no text
        bubbles: true,
        cancelable: true,
      }),
    );
  });

  await expect(page.getByText("clipboard.png")).toBeVisible();
});

test("degraded paste over the terminal falls back to the async clipboard (#530)", async ({
  page,
}) => {
  await page.route("**/api/upload", (r) =>
    r.fulfill({
      json: { name: "clipboard.png", path: "/uploads/clipboard.png" },
    }),
  );
  await page.addInitScript(FAKE_WS);
  await page.goto("/s/claude/img-paste-fallback-term");

  const xterm = page.locator(".xterm");
  await expect(xterm).toBeVisible();

  await putPngOnClipboard(page);
  await xterm.evaluate((host) => {
    host.dispatchEvent(
      new ClipboardEvent("paste", {
        clipboardData: new DataTransfer(),
        bubbles: true,
        cancelable: true,
      }),
    );
  });

  await expect(page.getByText("clipboard.png")).toBeVisible();
});

test("plain-text paste is untouched by the fallback — no prompt, no upload (#530)", async ({
  page,
}) => {
  let uploads = 0;
  await page.route("**/api/upload", (r) => {
    uploads += 1;
    return r.fulfill({
      json: { name: "clipboard.png", path: "/uploads/clipboard.png" },
    });
  });
  await page.addInitScript(FAKE_WS);
  await page.goto("/s/claude/img-paste-fallback-text");

  const ta = page.getByPlaceholder(/Type here/);
  const toggle = page.getByRole("button", { name: "Open compose box" });
  await expect(ta.or(toggle)).toBeVisible();
  if (await toggle.isVisible().catch(() => false)) await toggle.click();
  await expect(ta).toBeVisible();

  // Image on the async clipboard AND text in the DataTransfer: the text must win — the
  // fallback only exists for pastes that would otherwise be no-ops.
  await putPngOnClipboard(page);
  await ta.click();
  await ta.evaluate((el) => {
    const dt = new DataTransfer();
    dt.setData("text/plain", "hello from the clipboard");
    el.dispatchEvent(
      new ClipboardEvent("paste", {
        clipboardData: dt,
        bubbles: true,
        cancelable: true,
      }),
    );
  });

  // jsdom-free real browser: an un-prevented paste event dispatched synthetically does not
  // mutate the textarea, so assert the negative space instead — no upload fired and no
  // attachment pill appeared.
  await page.waitForTimeout(500);
  expect(uploads).toBe(0);
  await expect(page.getByText("clipboard.png")).not.toBeVisible();
});
