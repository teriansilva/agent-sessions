import { expect, test } from "@playwright/test";

// #163: creating an opencode session must navigate to a `new-<uuid>` placeholder id (which the
// ws new=1 launch accepts + reconciles), not a bare UUID (which 4404s "session not found").
// Real browser, network mocked so the landing renders without a backend.

test.beforeEach(async ({ page }) => {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["opencode"],
        terminal_backend: "ws",
        auth_mode: "none",
        // The folder defaults from here when no project is selected (#448).
        default_project: "/home/u/proj",
      },
    }),
  );
  // No project entities → the folder falls back to the config default (#448).
  await page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
});

test("opencode Start navigates to a new-<uuid> placeholder (#163)", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByLabel("Launch folder")).toHaveValue("/home/u/proj");
  await page.getByRole("button", { name: /start session/i }).click();
  await expect(page).toHaveURL(
    /\/s\/opencode\/new-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
  );
});
