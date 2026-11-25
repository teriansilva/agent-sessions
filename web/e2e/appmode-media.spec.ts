import { expect, test, type Page } from "@playwright/test";
import { randomBytes } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { connect as tcpConnect, type Socket } from "node:net";
import { join } from "node:path";
import { connect, openSessions, PY_STACK_AVAILABLE, SESSION_UUID, startStack, type Stack } from "./appmode";
import { clickHeadAction, FILES_ACTION } from "./headActions";
import { openLinksFullScreen } from "./linkMode";

/** Media preview against the REAL server (#1530): both transports, end to end.
 *
 *  **App mode** goes through the real relay, mux and agent: the bytes must come through the
 *  tunnel seam (never a native `/api/files/raw` request, which would ask the relay and leak the
 *  path into its log), under the 25 MB cap, and a near-cap file must end in a real state — never a
 *  spinner that does not end.
 *
 *  **Direct** talks to the same real app on its own port: a scripted SVG opened straight from its
 *  raw URL runs nothing (the route's sandbox CSP), and with every stream slot held by stalled
 *  readers a fifth video says Busy, then plays once a slot frees. None of this is reachable from a
 *  spec that mocks `/api/**`.
 */
test.describe.configure({ mode: "serial" });
test.setTimeout(240_000);
test.skip(({ isMobile }) => !!isMobile, "transport proof — runs once, on desktop");
test.skip(!PY_STACK_AVAILABLE, "needs the Python stack (uv) — see .forgejo/workflows/appmode-e2e.yml");

const MEDIA = join(process.cwd(), "e2e", "fixtures", "media");
const MB = 1024 * 1024;

let stack: Stack;

test.beforeAll(async () => {
  test.setTimeout(240_000);
  stack = await startStack();
  const clip = readFileSync(join(MEDIA, "clip.webm"));
  writeFileSync(join(stack.repo, "dot.png"), readFileSync(join(MEDIA, "dot.png")));
  writeFileSync(join(stack.repo, "clip.webm"), clip);
  writeFileSync(join(stack.repo, "fifth.webm"), clip);
  // Real WebM bytes padded out: the size is what matters for the cap.
  writeFileSync(join(stack.repo, "big.webm"), Buffer.concat([clip, Buffer.alloc(26 * MB - clip.length)]));
  writeFileSync(join(stack.repo, "near.webm"), Buffer.concat([clip, Buffer.alloc(24 * MB - clip.length)]));
  writeFileSync(
    join(stack.repo, "badge.svg"),
    '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><rect width="40" height="40"/>' +
      '<script>alert("svg ran a script")</script></svg>\n',
  );
  // Bigger than any socket buffer, so a reader that stops reading really holds its stream.
  writeFileSync(join(stack.repo, "held.bin"), randomBytes(16 * MB));
});

test.afterAll(async () => {
  await stack?.stop();
});

async function openInViewer(page: Page, name: string) {
  const viewer = page.locator("[data-file-viewer]");
  if (await viewer.isVisible()) {
    await page.keyboard.press("Escape");
    await expect(viewer).toBeHidden();
  }
  await page.locator("[data-file-row]", { hasText: name }).first().click({ timeout: 30_000 });
  await expect(viewer).toBeVisible({ timeout: 20_000 });
  return viewer;
}

test("app mode: previews and Download come through the tunnel, never as a relay request", async ({ page }) => {
  const nativeRaw: string[] = [];
  page.on("request", (r) => {
    if (r.url().includes("/api/files/raw")) nativeRaw.push(r.url());
  });
  await connect(page, stack);
  await openSessions(page);
  await page.locator(`a[href*="${SESSION_UUID}"]`).first().click({ timeout: 30_000 });
  await clickHeadAction(page, FILES_ACTION, { timeout: 30_000 });
  await expect(page.locator("[data-file-tree]")).toBeVisible({ timeout: 20_000 });

  let viewer = await openInViewer(page, "dot.png");
  const img = viewer.locator("[data-media-el='image']");
  await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.naturalWidth), { timeout: 30_000 }).toBe(64);
  expect(await img.getAttribute("src")).toMatch(/^blob:/);

  const [download] = await Promise.all([
    page.waitForEvent("download"),
    viewer.locator("[data-media-download]").click(),
  ]);
  expect(download.suggestedFilename()).toBe("dot.png");
  expect(readFileSync((await download.path())!)).toEqual(readFileSync(join(MEDIA, "dot.png")));

  viewer = await openInViewer(page, "clip.webm");
  const video = viewer.locator("[data-media-el='video']");
  await expect
    .poll(() => video.evaluate((el: HTMLVideoElement) => el.readyState).catch(() => 0), { timeout: 30_000 })
    .toBeGreaterThanOrEqual(1);
  expect(await video.getAttribute("src")).toMatch(/^blob:/);

  // Over the cap: refused up front, with the reason, and Download disabled.
  viewer = await openInViewer(page, "big.webm");
  await expect(viewer).toContainText("Viewer // Home Free // Too large to preview");
  await expect(viewer.locator("[data-media-download]").first()).toBeDisabled();

  // Near the cap: it plays, or it says it timed out (with Retry). Never a spinner that does not end.
  viewer = await openInViewer(page, "near.webm");
  await expect
    .poll(() => viewer.locator("[data-media-preview]").getAttribute("data-media-phase"), { timeout: 120_000 })
    .not.toBe("loading");
  const phase = await viewer.locator("[data-media-preview]").getAttribute("data-media-phase");
  expect(["ready", "timedout", "cantplay"]).toContain(phase);
  if (phase === "timedout") await expect(viewer.locator("[data-media-retry]")).toBeVisible();

  expect(nativeRaw, "a native /api/files/raw request would have asked the relay").toEqual([]);
});

test("direct: a scripted SVG opened straight from its raw URL runs nothing", async ({ page }) => {
  const dialogs: string[] = [];
  page.on("dialog", (d) => {
    dialogs.push(d.message());
    void d.dismiss();
  });
  const url = `http://127.0.0.1:${stack.appPort}/api/files/raw?path=${encodeURIComponent(join(stack.repo, "badge.svg"))}`;
  const resp = await page.goto(url);
  expect(resp?.status()).toBe(200);
  expect(resp?.headers()["content-security-policy"]).toMatch(/^sandbox;/);
  expect(resp?.headers()["x-content-type-options"]).toBe("nosniff");
  await page.waitForTimeout(1_000);
  expect(dialogs).toEqual([]);
});

/** A GET that reads the headers and then stops reading: it holds its stream slot. */
function stalledReader(port: number, path: string): Promise<Socket> {
  return new Promise((resolve, reject) => {
    const s = tcpConnect(port, "127.0.0.1", () => {
      s.write(
        `GET /api/files/raw?path=${encodeURIComponent(path)}&download=1 HTTP/1.1\r\nHost: 127.0.0.1:${port}\r\n\r\n`,
      );
    });
    let head = "";
    s.on("data", function onData(chunk: Buffer) {
      head += chunk.toString("latin1");
      if (head.includes("\r\n\r\n")) {
        s.off("data", onData);
        s.pause();
        if (!head.startsWith("HTTP/1.1 200")) reject(new Error(head.slice(0, 80)));
        else resolve(s);
      }
    });
    s.on("error", reject);
  });
}

test("direct: a fifth stream on a saturated server says Busy, then plays once a slot frees", async ({ page }) => {
  const held: Socket[] = [];
  for (let i = 0; i < 4; i++) held.push(await stalledReader(stack.appPort, join(stack.repo, "held.bin")));
  try {
    // The config's stored link mode belongs to the preview origin; this is the app's own port.
    await openLinksFullScreen(page.context());
    await page.goto(`http://127.0.0.1:${stack.appPort}/s/claude/${SESSION_UUID}`);
    await clickHeadAction(page, FILES_ACTION, { timeout: 30_000 });
    await expect(page.locator("[data-file-tree]")).toBeVisible({ timeout: 20_000 });
    const viewer = await openInViewer(page, "fifth.webm");
    await expect(viewer).toContainText("Viewer // Busy", { timeout: 20_000 });
    for (const s of held) s.destroy();
    held.length = 0;
    const video = viewer.locator("[data-media-el='video']");
    await expect
      .poll(() => video.evaluate((el: HTMLVideoElement) => el.readyState).catch(() => 0), { timeout: 30_000 })
      .toBeGreaterThanOrEqual(1);
  } finally {
    for (const s of held) s.destroy();
  }
});

test("direct: the server refuses a file over the caller's max before sending a byte", async ({ request }) => {
  // What app mode's tunnel relies on (Hermes 6644): `max` is judged on the size at open, which is
  // also the body's fixed length, so neither the agent's proxy nor the tunnel ever receives more.
  const url = (max: number) =>
    `http://127.0.0.1:${stack.appPort}/api/files/raw?path=${encodeURIComponent(join(stack.repo, "big.webm"))}&max=${max}`;
  const over = await request.get(url(25 * MB));
  expect(over.status()).toBe(413);
  expect((await over.body()).length).toBeLessThan(1024);
  const fits = await request.get(url(30 * MB));
  expect(fits.status()).toBe(200);
  expect(Number(fits.headers()["content-length"])).toBe(26 * MB);
});
