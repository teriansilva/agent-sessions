import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

import { devices, expect, test, type Page } from "@playwright/test";

import { connect, PY_STACK_AVAILABLE, SESSION_UUID, startStack, type Stack } from "./appmode";
import { TOUCH_FLOOR } from "./touchTarget";

/** The mission shelf against the REAL server (#1740 P1a): every route is the production one —
 *  the manifest, the verdict, and the mission-bound read path with its sniffing, sandbox and
 *  download headers — over both transports.
 *
 *  **Direct** (desktop, then a phone-sized context): each kind opens in its viewer from real bytes
 *  on disk, a PDF is download-only with attachment headers, a deleted file is missing, a file under
 *  a folder excluded after delivery is refused (403) and shown as not accessible, and a verdict set
 *  in the lightbox is what the next manifest read returns.
 *
 *  **App mode** goes through the real relay, mux and agent: the bytes come through the tunnel seam
 *  (object URLs), and no native shelf raw URL is ever handed to the browser.
 */
test.describe.configure({ mode: "serial" });
test.setTimeout(240_000);
test.skip(({ isMobile }) => !!isMobile, "runs once, on desktop; the phone pass is a context below");
test.skip(!PY_STACK_AVAILABLE, "needs the Python stack (uv) — see .forgejo/workflows/appmode-e2e.yml");

const MEDIA = join(process.cwd(), "e2e", "fixtures", "media");
const SESSION = `claude:${SESSION_UUID}`;
const SANDBOX = "sandbox; default-src 'none'; img-src 'self' data:; media-src 'self'";

let stack: Stack;
let mid = "";
let ids: Record<string, string> = {};

test.beforeAll(async () => {
  test.setTimeout(240_000);
  stack = await startStack();
  const r = stack.repo;
  mkdirSync(join(r, "out"), { recursive: true });
  mkdirSync(join(r, "private"), { recursive: true });
  writeFileSync(join(r, "out", "poster.png"), readFileSync(join(MEDIA, "dot.png")));
  writeFileSync(join(r, "out", "hero.webm"), readFileSync(join(MEDIA, "clip.webm")));
  writeFileSync(join(r, "out", "vo.ogg"), readFileSync(join(MEDIA, "tone.ogg")));
  writeFileSync(join(r, "out", "gone.png"), readFileSync(join(MEDIA, "dot.png")));
  writeFileSync(join(r, "private", "hidden.png"), readFileSync(join(MEDIA, "dot.png")));
  writeFileSync(join(r, "CHANGELOG.md"), "# v0.21\n\n<script>alert('nope')</script>\n- the hero video\n");
  writeFileSync(join(r, "out", "release notes.pdf"), Buffer.from("%PDF-1.4\n%\xe2\xe3\n%%EOF\n", "latin1"));
  const out = execFileSync("uv", ["run", "python", "tests/appmode/seed_shelf.py", r, SESSION], {
    cwd: resolve(process.cwd(), ".."),
    env: stack.env,
    encoding: "utf8",
  });
  ({ mission_id: mid, ids } = JSON.parse(out.trim().split("\n").pop()!));
  // After delivery: one file is deleted, and one folder is excluded by the operator.
  unlinkSync(join(r, "out", "gone.png"));
  execFileSync(
    "uv",
    [
      "run",
      "python",
      "-c",
      "import sys; from agent_sessions import prefs; prefs.set_folder_exclusions([sys.argv[1]])",
      join(r, "private"),
    ],
    { cwd: resolve(process.cwd(), ".."), env: stack.env },
  );
});

test.afterAll(async () => {
  await stack?.stop();
});

const base = () => `http://127.0.0.1:${stack.appPort}`;
const tile = (page: Page, key: string) => page.locator(`[data-shelf-tile="${ids[key]}"]`);

async function openShelf(page: Page) {
  await page.goto(`${base()}/mission?m=${mid}`);
  await page.getByRole("tab", { name: /Shelf/ }).click({ timeout: 30_000 });
  // Every seeded DELIVERED item (the found-in-git test adds found tiles of its own).
  await expect(page.locator("[data-shelf-tile]:has([data-source='delivered'])")).toHaveCount(9, {
    timeout: 30_000,
  });
}

test("direct: every kind opens from real bytes through the mission-bound route", async ({ page }) => {
  await openShelf(page);
  await tile(page, "image").click();
  const img = page.getByRole("dialog").locator("[data-media-el='image']");
  await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.naturalWidth), { timeout: 30_000 }).toBe(64);
  expect(await img.getAttribute("src")).toBe(`/api/missions/${mid}/shelf/${ids.image}/raw`);

  await page.keyboard.press("ArrowRight");
  const video = page.getByRole("dialog").locator("[data-media-el='video']");
  await video.evaluate((el: HTMLVideoElement) => {
    el.muted = true;
    return el.play();
  });
  await expect.poll(() => video.evaluate((el: HTMLVideoElement) => el.currentTime), { timeout: 30_000 }).toBeGreaterThan(0);

  await page.keyboard.press("ArrowRight");
  const audio = page.getByRole("dialog").locator("[data-media-el='audio']");
  await audio.evaluate((el: HTMLAudioElement) => {
    el.muted = true;
    return el.play();
  });
  await expect.poll(() => audio.evaluate((el: HTMLAudioElement) => el.currentTime), { timeout: 30_000 }).toBeGreaterThan(0);

  await page.keyboard.press("ArrowRight");
  const doc = page.getByRole("dialog").locator("[data-shelf-text-doc]");
  await expect(doc).toContainText("<script>alert('nope')</script>");
  await expect(doc.locator("script")).toHaveCount(0);

  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("dialog").locator("[data-shelf-download-only]")).toBeVisible();

  await page.keyboard.press("ArrowRight");
  const link = page.getByRole("dialog").locator("[data-shelf-link]");
  await expect(link).toHaveAttribute("target", "_blank");
  await expect(link).toHaveAttribute("rel", "noopener noreferrer");

  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("dialog").locator("[data-shelf-text-item]")).toHaveText(
    "The badge is the old style.\n<b>Re-shoot</b> from staging.",
  );
});

test("direct: the PDF is download-only, with attachment headers and the sandbox", async ({ page }) => {
  const raw = `${base()}/api/missions/${mid}/shelf/${ids.pdf}/raw`;
  const inline = await page.request.get(raw);
  expect(inline.status()).toBe(415);
  expect(inline.headers()["content-security-policy"]).toBe(SANDBOX);
  const dl = await page.request.get(`${raw}?download=1`);
  expect(dl.status()).toBe(200);
  const h = dl.headers();
  expect(h["content-type"]).toBe("application/octet-stream");
  expect(h["content-disposition"]).toContain("attachment;");
  expect(h["content-disposition"]).toContain("filename*=UTF-8''release%20notes.pdf");
  expect(h["x-content-type-options"]).toBe("nosniff");
  expect(h["cache-control"]).toBe("no-store");
  expect(h["content-security-policy"]).toBe(SANDBOX);
  expect((await dl.body()).subarray(0, 5).toString()).toBe("%PDF-");
  // The UI's Download is that same URL, as an attachment.
  await openShelf(page);
  await tile(page, "pdf").click();
  const [file] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("dialog").locator("[data-shelf-download-only] [data-shelf-download]").click(),
  ]);
  expect(file.suggestedFilename()).toBe("release notes.pdf");
});

test("direct: a deleted file is missing and an excluded one is refused, both kept on the shelf", async ({ page }) => {
  const gone = await page.request.get(`${base()}/api/missions/${mid}/shelf/${ids.gone}/raw`);
  expect(gone.status()).toBe(404);
  const hidden = await page.request.get(`${base()}/api/missions/${mid}/shelf/${ids.private}/raw`);
  expect(hidden.status()).toBe(403);
  expect(hidden.headers()["content-security-policy"]).toBe(SANDBOX);
  await openShelf(page);
  await expect(tile(page, "gone")).toHaveAttribute("data-file-state", "missing");
  await expect(tile(page, "gone")).toContainText("Missing");
  await expect(tile(page, "private")).toHaveAttribute("data-file-state", "inaccessible");
  await expect(tile(page, "private")).toContainText("Not accessible");
});

test("direct, on a phone: the lightbox sheet sets a verdict the server keeps", async ({ browser }) => {
  const ctx = await browser.newContext({ ...devices["Pixel 7"] });
  const page = await ctx.newPage();
  try {
    await openShelf(page);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    await tile(page, "image").click();
    const dock = page.getByRole("dialog").getByRole("radio", { name: "Needs work" });
    await expect(dock).toBeVisible();
    const box = await dock.boundingBox();
    expect(box!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    await page.getByRole("dialog").getByLabel("Note · optional").fill("Re-export it");
    await dock.click();
    await expect(dock).toHaveAttribute("aria-checked", "true");
    const m = await (await page.request.get(`${base()}/api/missions/${mid}/shelf`)).json();
    const item = m.items.find((i: { id: string }) => i.id === ids.image);
    expect(item.verdict).toBe("needs_work");
    expect(item.note).toBe("Re-export it");
    await page.keyboard.press("Escape");
    await expect(tile(page, "image")).toContainText("Needs work");
  } finally {
    await ctx.close();
  }
});

test("app mode: the shelf's bytes come through the tunnel, never as a relay request", async ({ page }) => {
  const native: string[] = [];
  page.on("request", (r) => {
    if (/\/shelf\/shf_[0-9a-f]+\/raw/.test(r.url())) native.push(r.url());
  });
  await connect(page, stack);
  await page.evaluate((m) => {
    history.pushState({}, "", `/mission?m=${m}`);
    dispatchEvent(new PopStateEvent("popstate"));
  }, mid);
  await page.getByRole("tab", { name: /Shelf/ }).click({ timeout: 30_000 });
  await expect(tile(page, "image")).toBeVisible({ timeout: 30_000 });
  await tile(page, "image").click();
  const img = page.getByRole("dialog").locator("[data-media-el='image']");
  await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.naturalWidth), { timeout: 30_000 }).toBe(64);
  expect(await img.getAttribute("src")).toMatch(/^blob:/);
  await page.keyboard.press("ArrowRight");
  const video = page.getByRole("dialog").locator("[data-media-el='video']");
  await expect.poll(async () => (await video.getAttribute("src")) ?? "", { timeout: 30_000 }).toMatch(/^blob:/);
  expect(native).toEqual([]);
});

test("found in git: a commit after the session joined is a code change with its diff", async ({ page }) => {
  // The seeder took the session's baseline as it joined. Now the session commits, then leaves.
  const env = { ...process.env, GIT_AUTHOR_NAME: "t", GIT_AUTHOR_EMAIL: "t@t", GIT_COMMITTER_NAME: "t", GIT_COMMITTER_EMAIL: "t@t" };
  writeFileSync(join(stack.repo, "timeline.ts"), "export const FPS = 24;\n");
  execFileSync("git", ["-C", stack.repo, "add", "timeline.ts"], { env });
  execFileSync("git", ["-C", stack.repo, "commit", "-qm", "render: add the hero timeline"], { env });
  const cfg = await (await page.request.get(`${base()}/api/config`)).json();
  const detach = await page.request.post(`${base()}/api/missions/${mid}/detach`, {
    data: { session_key: SESSION },
    headers: { "X-CSRF-Token": cfg.csrf, Origin: base() },
  });
  expect(detach.status()).toBe(200);
  await expect
    .poll(async () => {
      const m = await (await page.request.get(`${base()}/api/missions/${mid}/shelf`)).json();
      return m.items.filter((i: { kind: string }) => i.kind === "code").length;
    }, { timeout: 30_000 })
    .toBe(1);
  await openShelf(page);
  const code = page.locator("[data-shelf-tile][data-kind='code']");
  await expect(code).toContainText("render: add the hero timeline");
  await code.click();
  await page.getByRole("dialog").locator("[data-shelf-code-file='timeline.ts']").click();
  const diff = page.getByRole("dialog").locator("[data-shelf-diff='timeline.ts']");
  await expect(diff).toContainText("+export const FPS = 24;");
  // The deleted file is listed with no viewer; the excluded folder is never reported.
  const listed = await page.getByRole("dialog").locator("[data-shelf-code-file]").evaluateAll((els) =>
    els.map((e) => e.getAttribute("data-shelf-code-file")),
  );
  expect(listed).toContain("out/gone.png");
  expect(listed.some((p) => (p ?? "").startsWith("private/"))).toBe(false);
});
