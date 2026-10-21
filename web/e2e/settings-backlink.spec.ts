import { expect, test } from "@playwright/test";
import { promptPath, settingsPath } from "../src/routes/settingsTabs";
import { openSettingsFromMenu } from "./settingsNav";

// #155: leaving Settings should return you to the session you came from — not drop you on the
// new-session landing and deselect it. Real browser, no backend needed (the shell mounts the
// terminal in a connecting state without a ws). Settings is opened from the operator menu (#1085),
// which renders once the config names an auth mode — so that one route is mocked.

test.beforeEach(async ({ page }) => {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: { csrf: "x", auth_mode: "none", terminal_backend: "ws", new_session_engines: [] },
    }),
  );
});

test("Settings back returns to the originating session (#155)", async ({
  page,
}, testInfo) => {
  await page.goto("/s/claude/back-test");
  await expect(page.locator(".xterm")).toBeVisible();

  await openSettingsFromMenu(page);
  // Desktop: bare /settings replace-redirects to the first section (#357). A phone keeps
  // /settings — it IS the section index there (#956). Either way the back link is on screen.
  await expect(page).toHaveURL(
    testInfo.project.name === "mobile"
      ? /\/settings$/
      : /\/settings\/appearance$/,
  );

  await page.getByRole("link", { name: "Back to sessions" }).click();
  await expect(page).toHaveURL(/\/s\/claude\/back-test$/);
});

test("Settings back falls back to the landing when opened directly (#155)", async ({
  page,
}, testInfo) => {
  await page.goto(settingsPath());
  // Desktop: canonical redirect to the first section (#357); phone: the index (#956).
  await expect(page).toHaveURL(
    testInfo.project.name === "mobile"
      ? /\/settings$/
      : /\/settings\/appearance$/,
  );
  await page.getByRole("link", { name: "Back to sessions" }).click();
  // No return state → land on the new-session page (the safe default).
  await expect(
    page.getByRole("heading", { name: /start a new session/i }),
  ).toBeVisible();
});

test("Back to sessions still returns to the session after an in-app prompt link (#957)", async ({
  page,
}, testInfo) => {
  const phone = testInfo.project.name === "mobile";
  await page.goto("/s/claude/back-test");
  await expect(page.locator(".xterm")).toBeVisible();
  await openSettingsFromMenu(page);

  // Settings → AI → Session review → "Prompts → Tail review", all in-app.
  const nav = page.getByRole("navigation", { name: "Settings", exact: true });
  await nav.getByRole("link", { name: "Session review", exact: true }).click();
  await page.getByRole("link", { name: /Prompts → Tail review/ }).click();
  await expect(page).toHaveURL(new RegExp(`${promptPath("tail_review")}$`));

  if (phone) {
    await page.getByRole("link", { name: "Back to settings" }).click();
    await expect(page).toHaveURL(new RegExp(`${settingsPath()}$`));
  }
  await page.getByRole("link", { name: "Back to sessions" }).click();
  await expect(page).toHaveURL(/\/s\/claude\/back-test$/);
});
