import { expect, test } from "@playwright/test";
import { existsSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { connect, PY_STACK_AVAILABLE, startStack, type Stack } from "./appmode";

/** #1931 follow-up: the operator's avatar **through the Home Free relay**.
 *
 *  Two things broke or could break on this path, and a spec that mocks `/api/**` sees neither:
 *  - the upload is a real multipart POST that has to survive `tunnel.fetch`, the mux and the agent's
 *    proxy into the real app, and land on disk;
 *  - the image is shown from bytes fetched THROUGH the tunnel (an object URL). A plain
 *    `<img src="/api/profile/avatar">` would ask the relay's origin, which has no such route.
 *
 *  Everything except the relay is production code — and the relay is blind by design, so a
 *  byte-piping stand-in loses nothing (see `appmode.ts`).
 */
test.describe.configure({ mode: "serial" });
test.setTimeout(180_000);
test.skip(({ isMobile }) => !!isMobile, "transport proof — runs once, on desktop");
test.skip(!PY_STACK_AVAILABLE, "needs the Python stack (uv) — see .forgejo/workflows/appmode-e2e.yml");

const WIDE = resolve(process.cwd(), "src/assets/hero.png"); // 343×361: not square

let stack: Stack;

test.beforeAll(async () => {
  test.setTimeout(240_000);
  stack = await startStack();
});

test.afterAll(async () => {
  await stack?.stop();
});

test("an avatar uploads through the relay, lands on disk, and shows from tunnelled bytes", async ({
  page,
}) => {
  const home = stack.env.HOME;
  const stored = join(stack.env.AGENT_SESSIONS_PROFILE_DIR ?? join(home, ".config", "agent-sessions", "profile"), "avatar");
  expect(existsSync(stored)).toBe(false);

  // Any request for the avatar that reaches the relay's own origin is the bug this guards.
  const leaked: string[] = [];
  page.on("request", (req) => {
    if (req.url().includes("/api/profile/avatar")) leaked.push(`${req.method()} ${req.url()}`);
  });

  await connect(page, stack);
  await page.evaluate(() => {
    history.pushState({}, "", "/settings/profile");
    dispatchEvent(new PopStateEvent("popstate"));
  });
  const panel = page.getByTestId("profile-settings");
  await expect(panel).toBeVisible({ timeout: 30_000 });
  await expect(panel.locator('[data-avatar][data-kind="accent"]')).toBeVisible();

  await panel.getByTestId("profile-avatar-input").setInputFiles(WIDE);
  await expect(panel.getByRole("alert")).toHaveCount(0);

  // The bytes are really there, as an image the server sniffed (WebP, or PNG where the browser
  // can't encode WebP). A preview that renders while nothing was written is what a mock reports.
  await expect.poll(() => existsSync(stored), { timeout: 30_000 }).toBe(true);
  const head = readFileSync(stored).subarray(0, 12);
  expect(
    head.subarray(0, 4).toString("latin1") === "RIFF" || head[0] === 0x89,
    `stored bytes start ${head.toString("hex")}`,
  ).toBe(true);

  // Shown from bytes fetched through the tunnel: an object URL that decoded to the 256 px square.
  const img = panel.locator("[data-avatar] img");
  await expect(img).toHaveAttribute("src", /^blob:/, { timeout: 30_000 });
  await expect
    .poll(() => img.evaluate((i: HTMLImageElement) => i.complete && i.naturalWidth), { timeout: 30_000 })
    .toBe(256);

  // Removing it works through the tunnel too.
  await panel.getByRole("button", { name: "Remove" }).click();
  await expect(panel.locator('[data-avatar][data-kind="accent"]')).toBeVisible({ timeout: 30_000 });
  await expect.poll(() => existsSync(stored), { timeout: 30_000 }).toBe(false);

  expect(leaked, "avatar requests must go through the tunnel, never the relay's origin").toEqual([]);
});
