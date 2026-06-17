import { expect, test } from "@playwright/test";

// Reconcile engines (#163/#315/#449): creating a session must navigate to a `new-<uuid>`
// placeholder id (which the ws new=1 launch accepts + reconciles), NOT a bare UUID (which 4404s
// "session not found"). #454: antigravity was minting a bare UUID — covered here too.
// Real browser, network mocked so the landing renders without a backend.

for (const engine of ["opencode", "antigravity"] as const) {
  test(`${engine} Start navigates to a new-<uuid> placeholder, not a bare uuid (#163/#454)`, async ({
    page,
  }) => {
    await page.route("**/api/config", (r) =>
      r.fulfill({
        json: {
          csrf: "x",
          new_session_engines: [engine],
          terminal_backend: "ws",
          auth_mode: "none",
          default_project: "/home/u/proj", // folder default when no project is selected (#448)
        },
      }),
    );
    await page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: { projects: [] } }));
    await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
    await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));

    await page.goto("/");
    await expect(page.getByLabel("Launch folder")).toHaveValue("/home/u/proj");
    await page.getByRole("button", { name: /start session/i }).click();
    await expect(page).toHaveURL(
      new RegExp(
        `/s/${engine}/new-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`,
      ),
    );
  });
}
