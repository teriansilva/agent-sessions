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
      },
    }),
  );
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [{ cwd: "/home/u/proj", label: "/home/u/proj" }] } }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
});

test("opencode Start navigates to a new-<uuid> placeholder (#163)", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("option", { name: "/home/u/proj" })).toBeAttached();
  await page.getByRole("button", { name: /start session/i }).click();
  await expect(page).toHaveURL(
    /\/s\/opencode\/new-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
  );
});
