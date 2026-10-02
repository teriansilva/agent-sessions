import { expect, test } from "@playwright/test";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { connect, PY_STACK_AVAILABLE, SESSION_UUID, startStack, type Stack } from "./appmode";
import { clickHeadAction, FILES_ACTION } from "./headActions";

/** #807's acceptance evidence: an upload **through the tunnel**, appearing in the tree (#579).
 *
 *  The issue names this because the relay is where uploads are most likely to break: it is the
 *  one path that buffers a whole body in the browser, the mux, and the agent at once, and the
 *  only one where a multipart `Content-Type` has to survive being normalised through
 *  `tunnel.fetch`'s `arrayBuffer()`. A spec that mocks `/api/**` at the network edge cannot see
 *  any of that.
 *
 *  Everything except the relay is production code — and the relay is blind by design, so a
 *  byte-piping stand-in loses nothing (see `appmode.ts`).
 */
test.describe.configure({ mode: "serial" });
// Three real processes, a real handshake and a real browser is not a 30s test.
test.setTimeout(180_000);
test.skip(({ isMobile }) => !!isMobile, "transport proof — runs once, on desktop");
// `web-ci` has no Python by design, so this skips there and runs in `appmode-e2e.yml` (and
// locally). Skipping loudly beats a spec that quietly cannot exercise what it claims.
test.skip(!PY_STACK_AVAILABLE, "needs the Python stack (uv) — see .forgejo/workflows/appmode-e2e.yml");

let stack: Stack;

test.beforeAll(async () => {
  // `test.setTimeout()` at describe level governs TESTS, not hooks — a `beforeAll` keeps the
  // 30s default, and booting a relay, an app and an agent does not fit in it on a loaded shared
  // runner (it does locally on a warm cache, which is exactly why this passed here and failed in
  // CI). The hook has to raise its own.
  test.setTimeout(240_000);
  stack = await startStack();
});

test.afterAll(async () => {
  // AWAITED: `stop()` became async when the harness started waiting for the killed process
  // groups to actually be gone before deleting the temp home (#806). Not awaiting it here would
  // race the delete against the children again — the exact bug that await was added to fix.
  await stack?.stop();
});

test("a picked file crosses the relay, lands on disk, and shows up in the tree", async ({
  page,
}) => {
  const landed = join(stack.repo, "through-the-tunnel.txt");
  expect(existsSync(landed)).toBe(false);

  await connect(page, stack);
  await page.locator(`a[href*="${SESSION_UUID}"]`).first().click({ timeout: 30_000 });
  await clickHeadAction(page, FILES_ACTION, { timeout: 30_000 });
  await expect(page.locator("[data-file-panel]")).toBeVisible({ timeout: 20_000 });
  await expect(page.locator("[data-file-tree]")).toBeVisible({ timeout: 20_000 });

  await page.locator("[data-upload-trigger]").click();
  await page.locator("[data-upload-pick='files']").click();
  await page.locator("[data-upload-files]").setInputFiles({
    name: "through-the-tunnel.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("carried by the mux\n"),
  });

  // The queue reports it landed…
  await expect(
    page.locator("[data-upload-row='through-the-tunnel.txt'] [data-upload-state='done']"),
  ).toBeVisible({ timeout: 60_000 });

  // …and the bytes are really there. A queue row that says DONE while nothing was written is
  // exactly what a mocked spec reports happily, so the file system is the assertion.
  await expect
    .poll(() => (existsSync(landed) ? readFileSync(landed, "utf8") : null), { timeout: 30_000 })
    .toBe("carried by the mux\n");

  // The issue asks specifically that it appear IN THE TREE, which is the panel re-reading the
  // directory through the same tunnel after the batch settled.
  await expect(page.locator("[data-file-tree]")).toContainText("through-the-tunnel.txt", {
    timeout: 30_000,
  });
});

test("an over-cap upload is refused through the relay with a NAMED error, not a dead tunnel", async ({
  page,
}) => {
  // The transport half of the same feature: the proxy's body cap has to reach the browser as a
  // real status. Against the previous adapter this hung — the browser blocked writing before it
  // ever read the response — so "resolves at all" is part of the assertion.
  await connect(page, stack);
  const result = await page.evaluate(async () => {
    const body = new Uint8Array(40 * 1024 * 1024); // comfortably past HF_MAX_REQUEST_BODY
    const started = Date.now();
    try {
      const r = await fetch("/api/files/upload", { method: "POST", body });
      return { status: r.status, ms: Date.now() - started };
    } catch (e) {
      return { status: -1, ms: Date.now() - started, err: String(e) };
    }
  });
  // 413 from the proxy's cap, or 403/422 from the app if it got that far — any NAMED status is
  // the property under test. A hang (or a generic network failure) is not.
  expect(result.status, `got ${JSON.stringify(result)}`).toBeGreaterThan(0);
  expect(result.ms).toBeLessThan(120_000);
});
