import { expect, test } from "@playwright/test";

// #155: leaving Settings should return you to the session you came from — not drop you on the
// new-session landing and deselect it. Real browser, no backend needed (the shell mounts the
// terminal in a connecting state without a ws).

test("Settings back returns to the originating session (#155)", async ({ page }, testInfo) => {
  await page.goto("/s/claude/back-test");
  await expect(page.locator(".xterm")).toBeVisible();

  // The gear lives in the desktop sidebar topbar and the mobile top bar; one is visible per
  // viewport. Tap the one for this project's viewport.
  const surface = testInfo.project.name === "mobile" ? ".mobilebar" : ".topbar";
  await page.locator(surface).getByRole("link", { name: "Settings" }).click();
  await expect(page).toHaveURL(/\/settings$/);

  await page.getByRole("link", { name: "Back to sessions" }).click();
  await expect(page).toHaveURL(/\/s\/claude\/back-test$/);
});

test("Settings back falls back to the landing when opened directly (#155)", async ({ page }) => {
  await page.goto("/settings");
  await page.getByRole("link", { name: "Back to sessions" }).click();
  // No return state → land on the new-session page (the safe default).
  await expect(page.getByRole("heading", { name: /start a new session/i })).toBeVisible();
});
