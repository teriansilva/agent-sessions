import { expect, test } from "@playwright/test";

// #477: the compose box draft (text + pasted-image attachment pills) is persisted server-side
// and restored when the session is reopened — survives a full reload, available cross-device.
// Real browser (mobile + desktop projects): the draft endpoints are mocked with an in-memory
// store so we exercise the actual save-debounce / restore / clear-on-send path end to end.
//
// Red before this feature: Compose had no sessionId prop, no getDraft/saveDraft, and never
// restored — so the PUT never fires and the reload comes back empty.

// WS stub so the terminal connects (and a content send actually delivers its frames).
const FAKE_WS = `
window.WebSocket = class {
  constructor(url) { this.url = url; this.readyState = 0; this.binaryType = "arraybuffer";
    setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); }, 20);
  }
  send() {} close() { this.readyState = 3; this.onclose && this.onclose({ code: 1000 }); }
};
`;

type Draft = { text: string; attachments: { name: string; path: string }[] };

/** Install an in-memory mock of the draft + upload endpoints; returns the store so a test can
 *  assert what the client persisted. The store is keyed by the engine-qualified session id. */
async function mockDrafts(page: import("@playwright/test").Page) {
  const store: Record<string, Draft> = {};
  await page.route("**/api/sessions/*/draft", async (route) => {
    const url = route.request().url();
    const sid = decodeURIComponent(url.split("/api/sessions/")[1].split("/draft")[0]);
    if (route.request().method() === "GET") {
      const d = store[sid] ?? { text: "", attachments: [] };
      await route.fulfill({
        json: { id: sid, text: d.text, attachments: d.attachments, updated_at: d.text ? 1 : null },
      });
      return;
    }
    if (route.request().method() === "PUT") {
      const body = route.request().postDataJSON() as Draft;
      if (!body.text && !(body.attachments?.length)) delete store[sid];
      else store[sid] = { text: body.text ?? "", attachments: body.attachments ?? [] };
      await route.fulfill({ json: { id: sid, has_draft: !!store[sid] } });
      return;
    }
    await route.fallback();
  });
  await page.route("**/api/upload", (r) =>
    r.fulfill({ json: { name: "shot.png", path: "/home/u/.agent-sessions/uploads/shot.png" } }),
  );
  return store;
}

/** The compose textarea is only mounted when the box is open (collapsed by default on desktop). */
async function openCompose(page: import("@playwright/test").Page) {
  const ta = page.getByPlaceholder(/type here/i);
  if (!(await ta.isVisible())) {
    await page.getByRole("button", { name: /open compose box/i }).click();
  }
  await expect(ta).toBeVisible();
  return ta;
}

test("a typed draft is saved and restored verbatim after a full reload (#477)", async ({ page }) => {
  const store = await mockDrafts(page);
  await page.addInitScript(FAKE_WS);
  await page.goto("/s/claude/draft-text");
  await expect(page.locator(".xterm")).toBeVisible();

  const ta = await openCompose(page);
  await ta.fill("remember the refresh-token rotation");

  // The debounced PUT lands on the server (the heart of cross-device persistence).
  await expect.poll(() => store["claude:draft-text"]?.text).toBe("remember the refresh-token rotation");

  // Reload from scratch: the draft is fetched and restored into the box (which auto-opens).
  await page.reload();
  await expect(page.locator(".xterm")).toBeVisible();
  await expect(page.getByPlaceholder(/type here/i)).toHaveValue("remember the refresh-token rotation");
});

test("a pasted image is restored as an attachment pill after reload (#477)", async ({ page }) => {
  const store = await mockDrafts(page);
  await page.addInitScript(FAKE_WS);
  await page.goto("/s/claude/draft-img");
  const xterm = page.locator(".xterm");
  await expect(xterm).toBeVisible();

  // Paste an image over the terminal → Compose attaches it as a pill (#157) and saves the draft.
  await xterm.evaluate((host) => {
    const file = new File([new Uint8Array([1, 2, 3])], "shot.png", { type: "image/png" });
    const dt = new DataTransfer();
    dt.items.add(file);
    host.dispatchEvent(
      new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }),
    );
  });
  await expect(page.getByText("shot.png")).toBeVisible();
  await expect.poll(() => store["claude:draft-img"]?.attachments?.length ?? 0).toBe(1);

  await page.reload();
  await expect(page.locator(".xterm")).toBeVisible();
  // The pill comes back from the persisted draft on the fresh load.
  await expect(page.getByText("shot.png")).toBeVisible();
});

test("sending the message clears the saved draft (#477)", async ({ page }) => {
  const store = await mockDrafts(page);
  await page.addInitScript(FAKE_WS);
  await page.goto("/s/claude/draft-send");
  await expect(page.locator(".xterm")).toBeVisible();

  const ta = await openCompose(page);
  await ta.fill("about to send this");
  await expect.poll(() => store["claude:draft-send"]?.text).toBe("about to send this");

  // A content send takes the bracketed-paste path and then clears the draft server-side.
  await page.getByRole("button", { name: /^send/i }).click();
  await expect.poll(() => store["claude:draft-send"]).toBeUndefined();

  // Reload: nothing to restore — the box comes back empty.
  await page.reload();
  await expect(page.locator(".xterm")).toBeVisible();
  const ta2 = await openCompose(page);
  await expect(ta2).toHaveValue("");
});
