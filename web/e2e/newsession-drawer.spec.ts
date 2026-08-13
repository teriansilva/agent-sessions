import { expect, test } from "@playwright/test";

// #283: same-route nav taps must close the mobile off-canvas drawer in ONE tap. The bug:
// New session is a `Link to="/"`, so when you're already on "/" the click doesn't change
// `location.pathname` — and the drawer's only close trigger was a pathname-change effect.
// The drawer sat open over the landing, so the tap looked dead ("tap several times"). The
// fix threads a shared close handler onto the same-route-capable links.
//
// Runs on the `desktop` AND `mobile` Playwright projects (playwright.config). The drawer
// assertions only apply where the off-canvas drawer exists (mobile / hasTouch). This must
// live in the real-browser harness: jsdom can't model the off-canvas overlay or a real tap,
// and a passing jsdom unit test is exactly what hid this regression the first time.
//
// No backend needed: under `vite preview`, /api/* 404s, but the shell + routing + landing
// all mount — which is all this interaction exercises.

const landing = (page: import("@playwright/test").Page) =>
  page.getByRole("heading", { name: /start a new session/i });

/** Open the off-canvas drawer if we're on a mobile viewport; returns whether it's mobile. */
async function openDrawerIfMobile(
  page: import("@playwright/test").Page,
): Promise<boolean> {
  const hamburger = page.getByRole("button", { name: /open session list/i });
  const isMobile = await hamburger.isVisible().catch(() => false);
  if (isMobile) {
    await hamburger.click();
    await expect(page.locator(".app")).toHaveClass(/navOpen/);
  }
  return isMobile;
}

test("New session closes the drawer in one tap when already at / (#283)", async ({
  page,
}) => {
  await page.goto("/");
  await expect(landing(page)).toBeVisible();

  const isMobile = await openDrawerIfMobile(page);
  await page.getByRole("link", { name: /new session/i }).click();

  // The regression: on mobile the drawer must be gone after a single tap.
  if (isMobile) await expect(page.locator(".app")).not.toHaveClass(/navOpen/);
  await expect(landing(page)).toBeVisible();
});

test("from a session view, one New session click shows the landing (#283)", async ({
  page,
}) => {
  await page.goto("/s/claude/fake-session-id");
  await expect(landing(page)).toHaveCount(0);

  const isMobile = await openDrawerIfMobile(page);
  await page.getByRole("link", { name: /new session/i }).click();

  await expect(landing(page)).toBeVisible();
  await expect(page).toHaveURL(/\/$/);
  if (isMobile) await expect(page.locator(".app")).not.toHaveClass(/navOpen/);
});
