import { expect, test } from "@playwright/test";
import { settingsPath } from "../src/routes/settingsTabs";

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
  page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [] } }),
  );
  page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions: [],
        next_offset: null,
        total: 0,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  page.route("**/api/projects**", (r) => r.fulfill({ json: { projects: [] } }));
}

test("Home Free (none): Security tab shows the login-off explainer + enable-login recipe, not blank", async ({
  page,
}) => {
  baseRoutes(page, "none");
  await page.goto(settingsPath("security"));
  await expect(page.getByRole("heading", { name: /^login$/i })).toBeVisible();
  await expect(page.getByText(/login is off/i)).toBeVisible();
  // The 2FA / Sign-out cards must NOT be here in none-mode.
  await expect(page.getByRole("button", { name: /sign out/i })).toHaveCount(0);
  // The recipe is behind a disclosure — open it and confirm the verified command shows.
  await page.getByText(/prefer a password login/i).click();
  await expect(page.getByText(/reset-password --prompt/)).toBeVisible();
  await expect(
    page.getByText(/AGENT_SESSIONS_AUTH_MODE=single-user/),
  ).toBeVisible();
});

test("single-user: Security tab shows the normal cards, not the login-off card", async ({
  page,
}) => {
  baseRoutes(page, "single-user");
  await page.goto(settingsPath("security"));
  // The 2FA card's heading is always present in single-user mode…
  await expect(
    page.getByRole("heading", { name: /two-factor authentication/i }),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: /^account$/i })).toBeVisible();
  // …and the login-off card is not shown.
  await expect(page.getByText(/login is off/i)).toHaveCount(0);
});

// The operator can change their own password from Settings → Security: the card posts the
// current + new password to /api/password, refuses a short or mistyped new one before sending,
// and says plainly when the current password was wrong.
test.describe("single-user: Password card", () => {
  test("changes the password with the current one and a confirmed new one", async ({
    page,
  }) => {
    baseRoutes(page, "single-user");
    const posted: unknown[] = [];
    await page.route("**/api/password", (r) => {
      posted.push(r.request().postDataJSON());
      return r.fulfill({ status: 204 });
    });
    await page.goto(settingsPath("security"));

    await expect(
      page.getByRole("heading", { name: /^password$/i }),
    ).toBeVisible();
    const current = page.getByLabel("Current password");
    const next = page.getByLabel("New password", { exact: true });
    const confirm = page.getByLabel("Confirm new password");
    const submit = page.getByRole("button", { name: /change password/i });
    await expect(submit).toBeDisabled();

    await current.fill("old-password-1");
    // Too short: the button stays off and the rule is shown as the reason.
    await next.fill("short");
    await confirm.fill("short");
    await expect(submit).toBeDisabled();
    // Long enough but mistyped: still off, and the mismatch is named.
    await next.fill("a-brand-new-password");
    await confirm.fill("a-brand-new-passwore");
    await expect(page.getByText(/passwords don’t match/i)).toBeVisible();
    await expect(submit).toBeDisabled();

    await confirm.fill("a-brand-new-password");
    await expect(submit).toBeEnabled();
    await submit.click();

    await expect(page.getByRole("status")).toHaveText(/password changed/i);
    expect(posted).toEqual([
      {
        current_password: "old-password-1",
        new_password: "a-brand-new-password",
      },
    ]);
    // The fields clear so the password is not left on screen.
    await expect(current).toHaveValue("");
    await expect(next).toHaveValue("");
    await expect(confirm).toHaveValue("");
  });

  test("says the current password is wrong when the server refuses it", async ({
    page,
  }) => {
    baseRoutes(page, "single-user");
    await page.route("**/api/password", (r) =>
      r.fulfill({
        status: 403,
        json: { detail: "current password is incorrect" },
      }),
    );
    await page.goto(settingsPath("security"));

    await page.getByLabel("Current password").fill("not-my-password");
    await page
      .getByLabel("New password", { exact: true })
      .fill("a-brand-new-password");
    await page.getByLabel("Confirm new password").fill("a-brand-new-password");
    await page.getByRole("button", { name: /change password/i }).click();

    await expect(page.getByRole("alert")).toHaveText(
      /current password is incorrect/i,
    );
    // A refused change is not a sign-out: the page stays on Settings → Security.
    await expect(page).toHaveURL(/security/);
  });
});

test("Home Free (none): no Password card", async ({ page }) => {
  baseRoutes(page, "none");
  await page.goto(settingsPath("security"));
  await expect(page.getByText(/login is off/i)).toBeVisible();
  await expect(page.getByRole("heading", { name: /^password$/i })).toHaveCount(
    0,
  );
});
