import { expect, test, type Page, type Route } from "@playwright/test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { headAction, FILES_ACTION } from "./headActions";

/** Media preview in the Files viewer (#1530) — real-browser proof, desktop AND mobile.
 *
 *  The browser's own <img>/<video>/<audio> decode real bytes here (small fixtures under
 *  `e2e/fixtures/media`), so "it loaded" is the element's own state (`naturalWidth`,
 *  `readyState`), not markup. The API is mocked at the network edge; the server's half — the
 *  containment, the headers, Range and the stream cap — is pinned by `tests/test_files_raw.py`
 *  and, through the real Home Free stack, by `appmode-media.spec.ts`.
 */

const NOW = Math.floor(Date.now() / 1000);
const CWD = "/home/u/proj";
const MEDIA = join(process.cwd(), "e2e", "fixtures", "media");
const PNG = readFileSync(join(MEDIA, "dot.png"));
const WEBM = readFileSync(join(MEDIA, "clip.webm"));
const OGG = readFileSync(join(MEDIA, "tone.ogg"));
const SCRIPTED_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40">' +
  '<rect width="40" height="40" fill="#ffb000"/><script>alert("svg ran a script")</script></svg>\n';
const NUL_FREE_WAV = "RIFF-a-wave-header-made-entirely-of-printable-bytes";

const SESSION = {
  id: "claude:aaaaaaaa-0000-4000-8000-000000000001",
  engine: "claude",
  title: "media preview session",
  cwd: CWD,
  project: { kind: "folder", id: CWD, name: "proj" },
  last_mtime: NOW - 120,
  archived: false,
  favorite: false,
};

/** name → what /read reports and what /raw serves. `raw` null = no inline bytes (415). */
const FILES: Record<
  string,
  { read: Record<string, unknown>; raw: Buffer | string | null; mime: string }
> = {
  "dot.png": { read: { binary: true, mime: "image/png", preview: "image" }, raw: PNG, mime: "image/png" },
  "clip.webm": { read: { binary: true, mime: "video/webm", preview: "video" }, raw: WEBM, mime: "video/webm" },
  "tone.ogg": { read: { binary: true, mime: "audio/ogg", preview: "audio" }, raw: OGG, mime: "audio/ogg" },
  "capture.mkv": { read: { binary: true, mime: "video/x-matroska", preview: null }, raw: null, mime: "video/x-matroska" },
  "broken.mp4": {
    read: { binary: true, mime: "video/mp4", preview: "video" },
    raw: Buffer.from("this is not an mp4 at all, just text bytes"),
    mime: "video/mp4",
  },
  "busy.webm": { read: { binary: true, mime: "video/webm", preview: "video" }, raw: WEBM, mime: "video/webm" },
  "flicker.webm": { read: { binary: true, mime: "video/webm", preview: "video" }, raw: WEBM, mime: "video/webm" },
  "gone.png": { read: { binary: true, mime: "image/png", preview: "image" }, raw: PNG, mime: "image/png" },
  "badge.svg": {
    read: {
      binary: false,
      mime: "image/svg+xml",
      preview: "image",
      content: SCRIPTED_SVG,
      truncated: false,
      version: "v1",
      editable: true,
      readonly_reason: null,
      eol: "\n",
      bom: false,
    },
    raw: SCRIPTED_SVG,
    mime: "image/svg+xml",
  },
  // A NUL-free PNG is `binary: false` too; only an SVG may offer Source (Hermes, PR #1533).
  "nulfree.png": {
    read: {
      binary: false,
      mime: "image/png",
      preview: "image",
      content: "PNG-shaped-but-entirely-printable-bytes",
      truncated: false,
      version: "v1",
      editable: true,
      readonly_reason: null,
      eol: "\n",
      bom: false,
    },
    raw: PNG,
    mime: "image/png",
  },
  "nulfree.wav": {
    read: {
      binary: false,
      mime: "audio/wav",
      preview: "audio",
      content: NUL_FREE_WAV,
      truncated: false,
      version: "v1",
      editable: true,
      readonly_reason: null,
      eol: "\n",
      bom: false,
    },
    raw: NUL_FREE_WAV,
    mime: "audio/wav",
  },
};

interface Mock {
  rawRequests: string[];
  writes: unknown[];
  /** How many more raw requests for busy.webm answer 503 before it serves. */
  busyLeft: number;
  /** How many more ELEMENT requests (not the one-byte probe) for flicker.webm answer 503. */
  flickerLeft: number;
}

async function mockApp(page: Page): Promise<Mock> {
  const m: Mock = { rawRequests: [], writes: [], busyLeft: 0, flickerLeft: 0 };
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: { csrf: "x", new_session_engines: [], terminal_backend: "ws", auth_mode: "none", hostname: "test" },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [SESSION], next_offset: null, total: 1, facets: { projects: [], engines: ["claude"] } },
    }),
  );
  await page.route("**/api/files/capabilities", (r) => r.fulfill({ json: { ok: true, reason: "" } }));
  await page.route("**/api/files/list**", (r) =>
    r.fulfill({
      json: {
        path: CWD,
        parent: "/home/u",
        root: "/home/u",
        entries: Object.keys(FILES).map((name) => ({
          name,
          path: `${CWD}/${name}`,
          kind: "file",
          size: 2048,
          mtime: NOW,
        })),
        total: Object.keys(FILES).length,
        complete: true,
        truncated: false,
      },
    }),
  );
  await page.route("**/api/files/read**", (r) => {
    const p = new URL(r.request().url()).searchParams.get("path") ?? "";
    const f = FILES[p.split("/").pop() ?? ""];
    const raw = f?.raw;
    const size = raw === null || raw === undefined ? 1288490188 : Buffer.byteLength(raw);
    return r.fulfill({ json: { path: p, size, editable: false, readonly_reason: "is binary", ...f?.read } });
  });
  await page.route("**/api/files/write", async (r) => {
    m.writes.push(r.request().postDataJSON());
    await r.fulfill({ json: { path: `${CWD}/badge.svg`, version: "v2", size: 10, retained: null } });
  });
  await page.route("**/api/files/raw**", (r: Route) => {
    const url = new URL(r.request().url());
    m.rawRequests.push(url.search);
    const name = (url.searchParams.get("path") ?? "").split("/").pop() ?? "";
    const f = FILES[name];
    const download = url.searchParams.get("download") === "1";
    const headers = {
      "x-content-type-options": "nosniff",
      "content-security-policy": "sandbox; default-src 'none'; img-src 'self' data:; media-src 'self'",
      "cache-control": "no-store",
      "accept-ranges": "bytes",
      "content-disposition": `${download ? "attachment" : "inline"}; filename="${name}"`,
    };
    if (name === "gone.png") return r.fulfill({ status: 404, json: { detail: "no such file or directory" } });
    if (name === "busy.webm" && m.busyLeft > 0) {
      m.busyLeft -= 1;
      return r.fulfill({ status: 503, headers: { "retry-after": "1" }, json: { detail: "busy" } });
    }
    if (name === "flicker.webm" && m.flickerLeft > 0 && r.request().headers()["range"] !== "bytes=0-0") {
      m.flickerLeft -= 1;
      return r.fulfill({ status: 503, headers: { "retry-after": "1" }, json: { detail: "busy" } });
    }
    if (!f || (f.raw === null && !download))
      return r.fulfill({ status: 415, json: { detail: "not a type the viewer can show" } });
    const body = f.raw === null ? Buffer.from("matroska-bytes") : f.raw;
    const range = r.request().headers()["range"];
    if (range === "bytes=0-0") {
      const b = Buffer.from(body).subarray(0, 1);
      return r.fulfill({
        status: 206,
        headers: { ...headers, "content-range": `bytes 0-0/${Buffer.byteLength(body)}` },
        contentType: f.mime,
        body: b,
      });
    }
    return r.fulfill({
      status: 200,
      headers,
      contentType: download ? "application/octet-stream" : f.mime,
      body: typeof body === "string" ? Buffer.from(body) : body,
    });
  });
  return m;
}

async function openFile(page: Page, name: string) {
  const viewer = page.locator("[data-file-viewer]");
  if (await viewer.isVisible()) {
    await page.keyboard.press("Escape");
    await expect(viewer).toBeHidden();
  }
  const panel = page.locator("[data-file-panel]");
  if (!(await panel.isVisible())) {
    const trigger = await headAction(page, FILES_ACTION);
    await trigger.click();
    await expect(panel).toBeVisible();
  }
  await page.locator("[data-file-row]", { hasText: name }).first().click();
  await expect(viewer).toBeVisible();
  return viewer;
}

async function start(page: Page): Promise<Mock> {
  const m = await mockApp(page);
  await page.goto(`/s/claude/${SESSION.id.split(":")[1]}`);
  await expect(page.locator("#root")).toBeVisible();
  return m;
}

test.describe("media preview", () => {
  test("a PNG, a WebM and an Ogg open in the viewer and the browser decodes them", async ({ page }) => {
    await start(page);

    let viewer = await openFile(page, "dot.png");
    const img = viewer.locator("[data-media-el='image']");
    await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBe(64);
    await expect(viewer).toContainText("Viewer // IMAGE // PNG · 64 × 48");
    await expect(viewer).toContainText("IMAGE //");

    viewer = await openFile(page, "clip.webm");
    const video = viewer.locator("[data-media-el='video']");
    await expect.poll(() => video.evaluate((el: HTMLVideoElement) => el.readyState)).toBeGreaterThanOrEqual(1);
    await expect(viewer).toContainText("160 × 120");

    viewer = await openFile(page, "tone.ogg");
    const audio = viewer.locator("[data-media-el='audio']");
    await expect.poll(() => audio.evaluate((el: HTMLAudioElement) => el.readyState)).toBeGreaterThanOrEqual(1);
  });

  test("Fit and 1:1 toggle, and every media control holds the 44px floor", async ({ page }) => {
    await start(page);
    const viewer = await openFile(page, "dot.png");
    const fit = viewer.locator("[data-media-fit]");
    const actual = viewer.locator("[data-media-actual]");
    await expect(fit).toHaveAttribute("aria-pressed", "true");
    await actual.click();
    await expect(actual).toHaveAttribute("aria-pressed", "true");
    await expect(fit).toHaveAttribute("aria-pressed", "false");
    const img = viewer.locator("[data-media-el='image']");
    expect(await img.evaluate((el) => getComputedStyle(el).maxWidth)).toBe("none");
    await fit.click();
    expect(await img.evaluate((el) => getComputedStyle(el).maxWidth)).toBe("100%");
    for (const sel of ["[data-media-fit]", "[data-media-actual]", "[data-media-download]"]) {
      const h = await viewer.locator(sel).evaluate((el) => el.getBoundingClientRect().height);
      expect(h, sel).toBeGreaterThanOrEqual(44);
    }
  });

  test("Download asks for the attachment and the browser saves it under the file's name", async ({ page }) => {
    await start(page);
    const viewer = await openFile(page, "dot.png");
    const [download] = await Promise.all([
      page.waitForEvent("download"),
      viewer.locator("[data-media-download]").click(),
    ]);
    expect(download.suggestedFilename()).toBe("dot.png");
    // An <a download> navigation bypasses page.route, so the URL it asked for is the assertion.
    const asked = new URL(download.url());
    expect(asked.pathname).toBe("/api/files/raw");
    expect(asked.searchParams.get("download")).toBe("1");
    expect(asked.searchParams.get("path")).toBe(`${CWD}/dot.png`);
  });

  test("a type the browser cannot show says so, with Download", async ({ page }) => {
    await start(page);
    const viewer = await openFile(page, "capture.mkv");
    await expect(viewer.locator("[data-media-preview='none']")).toContainText("Viewer // Not previewable");
    await expect(viewer.locator("[data-media-download]")).toHaveAttribute("href", /download=1/);
    await expect(viewer).not.toContainText("Not rendered");
  });

  test("undecodable bytes probe once and say Can't play here", async ({ page }) => {
    const m = await start(page);
    const viewer = await openFile(page, "broken.mp4");
    await expect(viewer).toContainText("Viewer // Can't play here");
    await expect(viewer.locator("[data-media-download]")).toBeVisible();
    expect(m.rawRequests.length).toBeGreaterThanOrEqual(2); // the element, then the probe
  });

  test("a file that vanished says the read's message, not Can't play here", async ({ page }) => {
    await start(page);
    const viewer = await openFile(page, "gone.png");
    await expect(viewer).toContainText("no such file or directory");
    await expect(viewer).not.toContainText("Can't play here");
  });

  test("a busy server: Busy, an automatic retry after Retry-After, then it plays", async ({ page }) => {
    const m = await start(page);
    m.busyLeft = 2; // the element's request and the status probe both meet a full house
    const viewer = await openFile(page, "busy.webm");
    await expect(viewer).toContainText("Viewer // Busy");
    const video = viewer.locator("[data-media-el='video']");
    await expect
      .poll(() => video.evaluate((el: HTMLVideoElement) => el.readyState).catch(() => 0), { timeout: 15_000 })
      .toBeGreaterThanOrEqual(1);
    expect(m.rawRequests.some((q) => q.includes("attempt=1"))).toBe(true);
  });

  test("a slot that frees before the probe: re-requested and played, never Can't play here", async ({ page }) => {
    // Hermes 6644: the element meets a full house (503), the slot frees, the probe sees 206.
    const m = await start(page);
    m.flickerLeft = 1;
    const viewer = await openFile(page, "flicker.webm");
    const video = viewer.locator("[data-media-el='video']");
    await expect
      .poll(() => video.evaluate((el: HTMLVideoElement) => el.readyState).catch(() => 0), { timeout: 15_000 })
      .toBeGreaterThanOrEqual(1);
    await expect(viewer).not.toContainText("Can't play here");
    expect(m.rawRequests.some((q) => q.includes("attempt=1"))).toBe(true);
  });

  test("an SVG with a <script> shows as an image, runs nothing, and keeps its editor", async ({ page }) => {
    const m = await start(page);
    const dialogs: string[] = [];
    page.on("dialog", (d) => {
      dialogs.push(d.message());
      void d.dismiss();
    });
    const viewer = await openFile(page, "badge.svg");
    const img = viewer.locator("[data-media-el='image']");
    await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBe(40);
    await expect(viewer.locator("[data-svg-preview]")).toHaveAttribute("aria-pressed", "true");
    // Source is today's editor, unchanged: edit and save as any text file.
    await viewer.locator("[data-svg-source]").click();
    await expect(img).toBeHidden();
    await viewer.locator("[data-edit-toggle]").click();
    const editor = viewer.locator(".cm-content");
    await editor.click();
    await page.keyboard.press("End");
    await page.keyboard.type("<!-- edited -->");
    await viewer.locator("[data-save]").click();
    await expect.poll(() => m.writes.length).toBe(1);
    expect(JSON.stringify(m.writes[0])).toContain("edited");
    expect(dialogs).toEqual([]);
  });

  test("a NUL-free WAV is media, not text: it never opens the editor", async ({ page }) => {
    await start(page);
    const viewer = await openFile(page, "nulfree.wav");
    await expect(viewer.locator("[data-media-preview='audio']")).toBeVisible();
    await expect(viewer.locator("[data-edit-toggle]")).toHaveCount(0);
    await expect(viewer.locator(".cm-content")).toHaveCount(0);
  });

  test("a NUL-free non-SVG image is a picture only: no Source, no editor", async ({ page }) => {
    await start(page);
    const viewer = await openFile(page, "nulfree.png");
    const img = viewer.locator("[data-media-el='image']");
    await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBe(64);
    await expect(viewer.locator("[data-svg-source]")).toHaveCount(0);
    await expect(viewer.locator("[data-edit-toggle]")).toHaveCount(0);
    await expect(viewer.locator(".cm-content")).toHaveCount(0);
  });

  test("nothing scrolls sideways with a preview open", async ({ page }) => {
    await start(page);
    await openFile(page, "dot.png");
    const [scroll, client] = await page.evaluate(() => [
      document.scrollingElement!.scrollWidth,
      document.scrollingElement!.clientWidth,
    ]);
    expect(scroll).toBeLessThanOrEqual(client);
  });
});
