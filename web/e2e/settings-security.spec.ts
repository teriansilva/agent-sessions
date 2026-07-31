import { expect, test } from "@playwright/test";

// #682: the Security tab is config-driven. In Home Free (auth_mode=none) it must show the
// login-off explainer + the enable-login recipe instead of rendering blank (both the 2FA and
// Account cards hide in none-mode). In single-user it must still show the 2FA + Sign out cards.
// Network is mocked so Settings renders without a backend.

function baseRoutes(page, authMode: "none" | "single-user") {
  page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: authMode,
        two_factor_enabled: false,
        overview_expanded: [],
        projects_hidden: [],
        onboarded: true,
      },
    }),
  );
  page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } },
    }),
  );
  page.route("**/api/projects**", (r) => r.fulfill({ json: { projects: [] } }));
}

test("Home Free (none): Security tab shows the login-off explainer + enable-login recipe, not blank", async ({
  page,
}) => {
  baseRoutes(page, "none");
  await page.goto("/settings/security");
  await expect(page.getByRole("heading", { name: /^login$/i })).toBeVisible();
  await expect(page.getByText(/login is off/i)).toBeVisible();
  // The 2FA / Sign-out cards must NOT be here in none-mode.
  await expect(page.getByRole("button", { name: /sign out/i })).toHaveCount(0);
  // The recipe is behind a disclosure — open it and confirm the verified command shows.
  await page.getByText(/prefer a password login/i).click();
  await expect(page.getByText(/reset-password --prompt/)).toBeVisible();
  await expect(page.getByText(/AGENT_SESSIONS_AUTH_MODE=single-user/)).toBeVisible();
});

test("single-user: Security tab shows the normal cards, not the login-off card", async ({
  page,
}) => {
  baseRoutes(page, "single-user");
  await page.goto("/settings/security");
  // The 2FA card's heading is always present in single-user mode…
  await expect(page.getByRole("heading", { name: /two-factor authentication/i })).toBeVisible();
  await expect(page.getByRole("heading", { name: /^account$/i })).toBeVisible();
  // …and the login-off card is not shown.
  await expect(page.getByText(/login is off/i)).toHaveCount(0);
});
