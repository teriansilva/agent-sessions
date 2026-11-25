import { execFileSync } from "node:child_process";
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";

import { expect, test, type Browser, type Page } from "@playwright/test";

import { PY_STACK_AVAILABLE } from "./appmode";
import { HOOK_TIMEOUT_MS } from "./harness";
import { PASSWORD, USER, startApp, totp, type App } from "./servedPages";
import { TOUCH_FLOOR } from "./touchTarget";

// #1596 (epic #1531 Phase 12): the pages outside the SPA, served by the REAL app with password auth
// on — sign-in, QR sign-in, the TOTP step, a recovery code, the forced password change, and a device
// link approved and denied. The restyle may change only how they look, so each flow is driven end to
// end through the rendered templates (field names, actions, `next`, link.js's hooks) and every page
// is checked for the shared NIGHTJAR shell: the app's sheets, the atmosphere, 44 px controls, no
// sideways scroll. Named appmode-* so the appmode-e2e workflow (which has the Python half) runs it;
// web-ci is Node-only and skips it.

test.skip(!PY_STACK_AVAILABLE, "needs uv + the Python app");
test.describe.configure({ mode: "serial", timeout: 120_000 });

let app: App;
test.beforeEach(async () => {
  const info = test.info();
  info.setTimeout(HOOK_TIMEOUT_MS);
  app = await startApp({ forcePasswordChange: info.title.includes("password change") });
});
test.afterEach(async () => {
  await app?.stop();
});

/** The served-page capture mode (#1596): with NIGHTJAR_CAPTURE_DIR set, every checked state is also
 *  photographed at 1440 and 390, dark and light (the device's colour scheme, which these pages
 *  follow), named `<capture id>-<width>-<scheme>-1x.png` like the sweep (visual/nightjar). The ids
 *  are the ledger's `page:*` captures. Desktop project only: one set is enough. */
const CAPTURE_DIR = process.env.NIGHTJAR_CAPTURE_DIR;
async function capture(page: Page, id: string | undefined): Promise<void> {
  if (!CAPTURE_DIR || !id || test.info().project.name !== "desktop") return;
  mkdirSync(CAPTURE_DIR, { recursive: true });
  const size = page.viewportSize();
  for (const [w, h] of [
    [1440, 960],
    [390, 844],
  ])
    for (const scheme of ["dark", "light"] as const) {
      await page.setViewportSize({ width: w, height: h });
      await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
      await page.waitForTimeout(150);
      await page.screenshot({ path: resolve(CAPTURE_DIR, `${id.replace(/[:/]/g, "_")}-${w}-${scheme}-1x.png`) });
    }
  if (size) await page.setViewportSize(size);
  await page.emulateMedia({ colorScheme: null, reducedMotion: null });
}

/** The shared shell, measured on the page as served (and captured, in the capture mode). */
async function expectNightjarPage(page: Page, captureId?: string): Promise<void> {
  const sheets = await page.locator('link[rel="stylesheet"]').evaluateAll((ls) =>
    ls.map((l) => new URL((l as HTMLLinkElement).href).pathname),
  );
  expect(sheets).toEqual(
    ["fonts", "tokens", "motion", "nightjar", "pages"].map((n) => `/static/nightjar/${n}.css`),
  );
  await expect(page.locator(".nj-atmo")).toBeAttached();
  // The palette is in force (served, not restated): the card wears the rail colour.
  expect(await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--hud").trim())).not.toBe("");
  await expect(page.locator(".nj-frame").first()).not.toHaveCSS("clip-path", "none");
  for (const b of await page.locator("button:visible, input:visible:not([type=hidden])").all()) {
    expect((await b.boundingBox())!.height, await b.evaluate((e) => e.outerHTML.slice(0, 80))).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await capture(page, captureId);
}

/** The session cookie is `Secure`: Chromium keeps and sends it to http://127.0.0.1 from the page,
 *  but Playwright's request API does not, so the checks run inside the page. */
const authCheck = (page: Page) => page.evaluate(async () => (await fetch("/api/auth-check")).status);

async function signIn(page: Page, password = PASSWORD): Promise<void> {
  await page.getByLabel("Username").fill(USER);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
}

async function signedInContext(browser: Browser): Promise<Page> {
  const ctx = await browser.newContext({ baseURL: app.base });
  const page = await ctx.newPage();
  await page.goto("/login");
  await signIn(page);
  await page.waitForURL((u) => !u.pathname.startsWith("/login"));
  return page;
}

test("sign-in: a wrong password's error line, then the right one gets in", async ({ page }) => {
  await page.goto(`${app.base}/login?next=/missions`);
  await expectNightjarPage(page, "page:login");
  await expect(page.getByRole("heading", { name: /Authenticate, Commander/ })).toBeVisible();
  await expect(page.locator('input[name="next"]')).toHaveValue("/missions");

  await signIn(page, "not the password");
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCSS("color", await page.evaluate(() => {
    const p = document.createElement("p");
    p.style.color = "var(--danger-text)";
    document.body.append(p);
    return getComputedStyle(p).color;
  }));
  await expectNightjarPage(page, "page:login-error");

  await signIn(page);
  await page.waitForURL(`${app.base}/missions`);
  expect(await authCheck(page)).toBe(204);
});

test("QR sign-in: link.js shows the code, a signed-in phone approves, and the page signs in", async ({ page, browser }) => {
  await page.goto(`${app.base}/login`);
  const panel = page.locator("#qr-extra");
  await expect(panel).toBeVisible();
  await expect(page.locator("#qr-status")).toHaveClass(/\blive\b/);
  await expect(page.locator("#qr-status")).toContainText("Waiting for approval");
  await expectNightjarPage(page, "page:login-qr");
  const src = (await page.locator("#qr-img").getAttribute("src"))!;
  const cid = /[?&]c=([^&]+)/.exec(decodeURIComponent(src))?.[1] ?? /c=([A-Za-z0-9_-]+)/.exec(src)![1];

  const phone = await signedInContext(browser);
  await phone.goto(`/link/approve?c=${cid}`);
  await expectNightjarPage(phone, "page:link-confirm");
  await expect(phone.locator('[data-mode="confirm"]')).toBeVisible();
  await phone.getByRole("button", { name: "Approve" }).click();
  await expect(phone.locator('[data-mode="approved"]')).toBeVisible();
  await expectNightjarPage(phone, "page:link-approved");

  await page.waitForURL((u) => !u.pathname.startsWith("/login"), { timeout: 20_000 });
  expect(await authCheck(page)).toBe(204);
  await phone.context().close();
});

test("QR sign-in denied: the phone says denied and the page stays signed out", async ({ page, browser }) => {
  await page.goto(`${app.base}/login`);
  await expect(page.locator("#qr-extra")).toBeVisible();
  const src = (await page.locator("#qr-img").getAttribute("src"))!;
  const cid = /c=([A-Za-z0-9_-]+)/.exec(decodeURIComponent(src))![1];

  const phone = await signedInContext(browser);
  await phone.goto(`/link/approve?c=${cid}`);
  await phone.getByRole("button", { name: "Deny" }).click();
  await expect(phone.locator('[data-mode="denied"]')).toBeVisible();
  await expectNightjarPage(phone, "page:link-denied");

  await expect(page.locator("#qr-status")).toContainText("denied", { timeout: 20_000 });
  expect(await authCheck(page)).toBe(401);
  await phone.context().close();
});

test("TOTP: the second step after the password, a wrong code's error, then a recovery code", async ({ page, browser }) => {
  // Turn 2FA on as the operator would (Settings calls these), with a session and its CSRF token.
  const op = await signedInContext(browser);
  const info = await op.evaluate(async () => {
    const csrf = (await (await fetch("/api/config")).json()).csrf as string;
    const headers = { "X-CSRF-Token": csrf, "Content-Type": "application/json" };
    return (await (await fetch("/api/2fa/enroll", { method: "POST", headers })).json()) as {
      secret: string;
      recovery_codes: string[];
    };
  });
  const confirmed = await op.evaluate(
    async (code) => {
      const csrf = (await (await fetch("/api/config")).json()).csrf as string;
      const headers = { "X-CSRF-Token": csrf, "Content-Type": "application/json" };
      return (await fetch("/api/2fa/confirm", { method: "POST", headers, body: JSON.stringify({ code }) })).status;
    },
    totp(info.secret),
  );
  expect(confirmed).toBe(204);
  await op.context().close();

  await page.goto(`${app.base}/login`);
  await signIn(page);
  await expect(page.getByRole("heading", { name: "Two-factor verification" })).toBeVisible();
  await expectNightjarPage(page, "page:totp");
  await page.getByLabel(/code/i).fill("000000");
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expectNightjarPage(page, "page:totp-error");

  await page.getByLabel(/code/i).fill(info.recovery_codes[0]);
  await page.getByRole("button", { name: "Verify" }).click();
  await page.waitForURL((u) => !u.pathname.startsWith("/login"));
  expect(await authCheck(page)).toBe(204);
});

test("the forced first-login password change: a mismatch's error, then the new password works", async ({ page, browser }) => {
  await page.goto(`${app.base}/login`);
  await signIn(page);
  await page.goto(`${app.base}/change-password`);
  await expect(page.getByRole("heading", { name: "Set your password" })).toBeVisible();
  await expectNightjarPage(page, "page:password");

  const NEW = "a much longer new passphrase";
  await page.locator('input[name="current"]').fill(PASSWORD);
  await page.locator('input[name="new"]').fill(NEW);
  await page.locator('input[name="confirm"]').fill(NEW + "!");
  await page.getByRole("button", { name: "Set password" }).click();
  await expect(page.getByRole("alert")).toContainText("passwords do not match");
  await expectNightjarPage(page, "page:password-error");

  await page.locator('input[name="current"]').fill(PASSWORD);
  await page.locator('input[name="new"]').fill(NEW);
  await page.locator('input[name="confirm"]').fill(NEW);
  await page.getByRole("button", { name: "Set password" }).click();
  await page.waitForURL((u) => !u.pathname.startsWith("/change-password"));

  // The new password signs in from a fresh browser.
  const ctx = await browser.newContext({ baseURL: app.base });
  const fresh = await ctx.newPage();
  await fresh.goto("/login");
  await signIn(fresh, NEW);
  await fresh.waitForURL((u) => !u.pathname.startsWith("/login"));
  await ctx.close();
});

test("a link nobody can approve: the invalid outcome", async ({ browser }) => {
  const phone = await signedInContext(browser);
  await phone.goto("/link/approve?c=not-a-challenge");
  await expect(phone.locator('[data-mode="invalid"]')).toBeVisible();
  await expectNightjarPage(phone, "page:link-invalid");
  await phone.context().close();
});

test("the share placeholder an older box answers at /p/: the served sheets, script-free", async ({ page }) => {
  // The page `routes/shares.py` answers when the box has no share.html build — its exact HTML, read
  // from the app, served on the app's origin so its /static/nightjar sheets are the real ones.
  const html = execFileSync(
    "uv",
    ["run", "python", "-c", "from agent_sessions.routes.shares import PLACEHOLDER_PAGE; print(PLACEHOLDER_PAGE)"],
    { cwd: resolve(process.cwd(), ".."), encoding: "utf8" },
  );
  await page.route(`${app.base}/p/placeholder`, (r) => r.fulfill({ contentType: "text/html", body: html }));
  await page.goto(`${app.base}/p/placeholder`);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(page.locator("script")).toHaveCount(0);
  await expectNightjarPage(page, "page:share-placeholder");
});

// #1601 review: these templates never set `data-motion`, so they run Full (pages.css) — the
// atmosphere's sweep band included — unless the device asks to reduce motion, which stills the
// atmosphere and drops the sweep. The nightjar.css sweep rule used to treat "absent" as reduced.
test("the served atmosphere sweeps in Full and drops the sweep under reduced motion", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "no-preference" });
  await page.goto(`${app.base}/login`);
  expect(await page.locator("html").getAttribute("data-motion")).toBeNull();
  const sweep = page.locator(".nj-atmo .nj-sweep");
  const running = () =>
    page.locator(".nj-atmo").evaluate((el) =>
      el
        .getAnimations({ subtree: true })
        .filter((a) => a.effect?.getComputedTiming().iterations === Infinity && a.playState === "running")
        .map((a) => (a.effect as KeyframeEffect).target?.className ?? ""),
    );
  await expect(sweep).toBeVisible();
  await expect.poll(running).toContain("nj-sweep");

  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect(sweep).toBeHidden();
  await expect.poll(running).toEqual([]);
  await expect(page.locator(".nj-atmo .nj-hex")).toBeVisible();
});
