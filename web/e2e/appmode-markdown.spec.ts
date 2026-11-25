import { expect, test, type Page } from "@playwright/test";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { connect, openSessions, PY_STACK_AVAILABLE, SESSION_UUID, startStack, type Stack } from "./appmode";
import { clickHeadAction, FILES_ACTION } from "./headActions";

/** The Markdown preview against the REAL server over Home Free (#2114).
 *
 *  A repo image in a previewed document must come through the tunnel seam (`api.fileBlob`) as a
 *  `blob:` URL — never a native `/api/files/raw` request, which would ask the relay and put the
 *  path in its log. A missing image and one over the 25 MB tunnel cap each end in their fallback
 *  text, and closing the viewer revokes the object URLs it made. */
test.describe.configure({ mode: "serial" });
test.setTimeout(240_000);
test.skip(({ isMobile }) => !!isMobile, "transport proof — runs once, on desktop");
test.skip(!PY_STACK_AVAILABLE, "needs the Python stack (uv) — see .forgejo/workflows/appmode-e2e.yml");

const PNG = readFileSync(join(process.cwd(), "e2e", "fixtures", "media", "dot.png"));
const MB = 1024 * 1024;

let stack: Stack;

test.beforeAll(async () => {
  test.setTimeout(240_000);
  stack = await startStack();
  mkdirSync(join(stack.repo, "shots"), { recursive: true });
  writeFileSync(join(stack.repo, "shots", "dot.png"), PNG);
  writeFileSync(join(stack.repo, "shots", "huge.png"), Buffer.concat([PNG, Buffer.alloc(26 * MB)]));
  writeFileSync(
    join(stack.repo, "GUIDE.md"),
    [
      "# Guide",
      "",
      "![dot](shots/dot.png)",
      "",
      "![gone](shots/missing.png)",
      "",
      "![huge](shots/huge.png)",
      "",
    ].join("\n"),
  );
});

test.afterAll(async () => {
  await stack?.stop();
});

async function openGuide(page: Page) {
  await page.locator("[data-file-row]", { hasText: "GUIDE.md" }).first().click({ timeout: 30_000 });
  const viewer = page.locator("[data-file-viewer]");
  await expect(viewer).toBeVisible({ timeout: 20_000 });
  return viewer;
}

test("app mode: a previewed document's repo images come through the tunnel, never as a relay request", async ({
  page,
}) => {
  const nativeRaw: string[] = [];
  page.on("request", (r) => {
    if (r.url().includes("/api/files/raw")) nativeRaw.push(r.url());
  });
  await connect(page, stack);
  await openSessions(page);
  await page.locator(`a[href*="${SESSION_UUID}"]`).first().click({ timeout: 30_000 });
  await clickHeadAction(page, FILES_ACTION, { timeout: 30_000 });
  await expect(page.locator("[data-file-tree]")).toBeVisible({ timeout: 20_000 });

  const viewer = await openGuide(page);
  const doc = viewer.locator("[data-md-preview]");
  await expect(doc.getByRole("heading", { level: 1, name: "Guide" })).toBeVisible({ timeout: 30_000 });

  const img = doc.locator("img[alt='dot']");
  await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.naturalWidth), { timeout: 30_000 }).toBe(64);
  const src = (await img.getAttribute("src"))!;
  expect(src).toMatch(/^blob:/);

  await expect(doc.locator("[data-md-image-missing$='/shots/missing.png']")).toContainText("could not be loaded", {
    timeout: 30_000,
  });
  await expect(doc.locator("[data-md-image-missing$='/shots/huge.png']")).toContainText("through Home Free", {
    timeout: 60_000,
  });

  // Closing the viewer revokes the object URL it made: the blob is no longer readable.
  await page.keyboard.press("Escape");
  await expect(viewer).toBeHidden();
  const readable = await page.evaluate(async (u) => {
    try {
      await fetch(u);
      return true;
    } catch {
      return false;
    }
  }, src);
  expect(readable, "the image's object URL outlived the viewer").toBe(false);

  expect(nativeRaw, "a native /api/files/raw request would have asked the relay").toEqual([]);
});
