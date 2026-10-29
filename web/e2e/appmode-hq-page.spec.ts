/** HQ's page through the REAL app with password auth and 2FA on (#1776, Phase 1, direct origin).
 *
 *  A link into one HQ conversation, opened signed out, goes through the normal sign-in and the TOTP
 *  step and lands on THAT conversation — the path survives the SPA's 401 (`loginRedirectUrl`) and
 *  the server's `_safe_next` across both steps. Named appmode-* so the appmode-e2e workflow (which
 *  has the Python half) runs it; web-ci is Node-only and skips it. The relay's version of this
 *  journey is Phase 2, against #1735. */
import { expect, test, type Browser, type Page } from "@playwright/test";

import { hqConversationPath } from "../src/lib/routes";
import { PY_STACK_AVAILABLE } from "./appmode";
import { HOOK_TIMEOUT_MS } from "./harness";
import { PASSWORD, USER, startApp, totp, type App } from "./servedPages";

test.skip(!PY_STACK_AVAILABLE, "needs uv + the Python app");
test.describe.configure({ mode: "serial", timeout: 120_000 });

let app: App;
test.beforeEach(async () => {
  test.info().setTimeout(HOOK_TIMEOUT_MS);
  app = await startApp();
});
test.afterEach(async () => {
  await app?.stop();
});

async function signIn(page: Page): Promise<void> {
  await page.getByLabel("Username").fill(USER);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
}

/** Signed in, 2FA turned on as Settings does, and one HQ conversation created. */
async function prepare(browser: Browser): Promise<{ secret: string; cid: string }> {
  const ctx = await browser.newContext({ baseURL: app.base });
  const op = await ctx.newPage();
  await op.goto("/login");
  await signIn(op);
  await op.waitForURL((u) => !u.pathname.startsWith("/login"));
  const out = await op.evaluate(async () => {
    const csrf = (await (await fetch("/api/config")).json()).csrf as string;
    const headers = { "X-CSRF-Token": csrf, "Content-Type": "application/json" };
    const conv = (await (
      await fetch("/api/ask/conversations", { method: "POST", headers })
    ).json()) as { id: string };
    const enrol = (await (await fetch("/api/2fa/enroll", { method: "POST", headers })).json()) as {
      secret: string;
    };
    return { cid: conv.id, secret: enrol.secret, headers };
  });
  const confirmed = await op.evaluate(
    async ([code, headers]) =>
      (await fetch("/api/2fa/confirm", { method: "POST", headers, body: JSON.stringify({ code }) }))
        .status,
    [totp(out.secret), out.headers] as const,
  );
  expect(confirmed).toBe(204);
  await ctx.close();
  return { secret: out.secret, cid: out.cid };
}

test("signed out, a link to an HQ conversation → sign-in → TOTP → that conversation (#1776)", async ({
  page,
  browser,
}) => {
  const { secret, cid } = await prepare(browser);
  const path = hqConversationPath(cid);
  await page.goto(`${app.base}${path}`);
  await page.waitForURL((u) => u.pathname === "/login");
  expect(new URL(page.url()).searchParams.get("next")).toBe(path);
  await signIn(page);
  await expect(page.getByRole("heading", { name: "Two-factor verification" })).toBeVisible();
  // Confirming the enrolment spent this step's code (anti-replay: a step at or before the last one
  // used is refused), and the server accepts ±1 step: the NEXT step's code is the one to type.
  await page.getByLabel(/code/i).fill(totp(secret, Date.now() + 30_000));
  await page.getByRole("button", { name: "Verify" }).click();
  await page.waitForURL(`${app.base}${path}`);
  // The page, on that conversation — not "isn't here", not the dashboard.
  await expect(page.getByTestId("hq-page")).toBeVisible();
  await expect(page.getByTestId("hq-thread")).toBeVisible();
  await expect(page.getByTestId("hq-missing")).toHaveCount(0);
  await expect(page.getByTestId("ask-sidebar")).toHaveCount(0);
});
