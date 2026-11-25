/** The boot sequence (#1531, docs/design.md v2 §8 rule 5) in a real browser: once per visit,
 *  Full motion only, never blocking, dismissed by any input, real reads only. The app stands it
 *  down under automation (navigator.webdriver), so these tests clear that flag first. */
import { expect, test, type Page } from "@playwright/test";

import { DASHBOARD_PATH, NEW_SESSION_PATH } from "../src/lib/routes";
import { mockNightjarShell, storeMotionChoice } from "./nightjarShell";

async function asAPerson(page: Page) {
  await page.addInitScript(() => {
    Object.defineProperty(Navigator.prototype, "webdriver", { configurable: true, get: () => false });
  });
}

test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await mockNightjarShell(page);
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        auth_mode: "none",
        username: "commander",
        hostname: "forward-base",
        terminal_backend: "ws",
        pulse: { configured: true },
        new_session_engines: ["claude"],
        onboarded: true,
        hq_contact: "romania-woman",
        hq_contact_rev: 1,
      },
    }),
  );
});

test("boots once per visit in Full, over a live app, and shows real reads (#1531)", async ({ page }) => {
  await asAPerson(page);
  await storeMotionChoice(page, "full");
  await page.goto(DASHBOARD_PATH);
  const boot = page.getByTestId("boot-sequence");
  await expect(boot).toBeVisible();
  await expect(boot).toContainText("forward-base");
  await expect(boot).toContainText("commander");
  await expect(page.locator(".app")).toHaveClass(/\bbooting\b/);
  // Never blocking: the layer does not take presses.
  await expect(boot).toHaveCSS("pointer-events", "none");
  await expect(boot).toBeHidden({ timeout: 3000 });
  await expect(page.locator(".app")).not.toHaveClass(/\bbooting\b/);

  // The same visit (this tab) does not boot again.
  await page.reload();
  await expect(page.locator(".app")).toBeVisible();
  await page.waitForTimeout(300);
  await expect(page.getByTestId("boot-sequence")).toHaveCount(0);
});

test("on a phone the closed session drawer stays closed through the boot (#1531)", async ({
  page,
}) => {
  // Hermes on #1560: the panels' power-on rise ends at `transform: none`, and with `both` fill it
  // overrode the off-canvas drawer's translateX(-100%) — the CLOSED drawer showed at x=0 for the
  // whole boot, then jumped away.
  await page.setViewportSize({ width: 390, height: 844 });
  await asAPerson(page);
  await storeMotionChoice(page, "full");
  // A route WITH the session drawer (the Dashboard has none).
  await page.goto(NEW_SESSION_PATH);
  await expect(page.getByTestId("boot-sequence")).toBeVisible();
  await expect(page.locator(".sidebar")).toHaveCount(1);
  await expect(page.locator(".app")).toHaveClass(/\bbooting\b/);
  // Sampled across the boot: the drawer never enters the screen.
  for (let i = 0; i < 6; i++) {
    const box = await page.locator(".sidebar").evaluate((el) => el.getBoundingClientRect().toJSON());
    expect(box.width, "the drawer is laid out").toBeGreaterThan(0);
    expect(box.right, "and off the screen").toBeLessThanOrEqual(0);
    await page.waitForTimeout(150);
  }
});

test("any input skips the boot at once (#1531)", async ({ page }) => {
  await asAPerson(page);
  await storeMotionChoice(page, "full");
  await page.goto(DASHBOARD_PATH);
  await expect(page.getByTestId("boot-sequence")).toBeVisible();
  await page.keyboard.press("Shift");
  await expect(page.getByTestId("boot-sequence")).toHaveCount(0);
});

for (const mode of ["reduced", "off"] as const) {
  test(`no boot in the ${mode} motion mode (#1531)`, async ({ page }) => {
    await asAPerson(page);
    await storeMotionChoice(page, mode);
    await page.goto(DASHBOARD_PATH);
    await expect(page.locator(".app")).toBeVisible();
    await page.waitForTimeout(300);
    await expect(page.getByTestId("boot-sequence")).toHaveCount(0);
    await expect(page.locator(".app")).not.toHaveClass(/\bbooting\b/);
  });
}
