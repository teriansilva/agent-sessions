/** Settings B in NIGHTJAR (#1574, Phase 11: agents, system, about), in a real browser.
 *
 *  - SECRETS: the 2FA set-up shows its (fixture) key and recovery codes ONCE — they are gone the
 *    moment the code is verified — and the Sharing list never renders a link's token or URL, even
 *    if a payload carried one (the server keeps only its digest).
 *  - The update progress panel scans the shared Acquiring strip while the installer runs, and not
 *    once it has finished.
 *  - A roster row's plan usage is the shared Gauge: a meter with its value.
 *  - Defaults: the chosen default-agent card wears the accent edge (a choice, not a state). */
import { expect, test, type Page } from "@playwright/test";
import { settingsPath } from "../src/routes/settingsTabs";
import { mockRoster } from "./roster";

// FAKE fixture values: they open nothing.
const KEY = "JBSWY3DPEHPK3PXP";
const CODES = ["7F3K-92QD", "LM4X-0PZ8", "QW7C-R2NB", "H8TY-55JK"];
const TOKEN = "shrtok_9f2c7e4b1a8d3f6e";

async function base(page: Page, config: Record<string, unknown> = {}) {
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "single-user",
        two_factor_enabled: false,
        overview_expanded: [],
        projects_hidden: [],
        onboarded: true,
        agent_defaults: { default_engine: "claude", bypass: false },
        ...config,
      },
    }),
  );
  await mockRoster(page);
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } } }),
  );
  await page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route("**/api/pulse/notifications**", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
}

test("2FA set-up shows its key and recovery codes once, and drops them when the code is verified", async ({
  page,
}) => {
  await base(page);
  await page.route("**/api/2fa/enroll", (r) =>
    r.fulfill({
      json: { secret: KEY, otpauth_uri: `otpauth://totp/BattleLab:test?secret=${KEY}&issuer=BattleLab`, recovery_codes: CODES },
    }),
  );
  await page.route("**/api/2fa/confirm", (r) => r.fulfill({ status: 204, body: "" }));
  await page.goto(settingsPath("security"));
  await page.getByRole("button", { name: "Enable two-factor auth" }).click();
  await expect(page.getByText(KEY)).toBeVisible();
  for (const c of CODES) await expect(page.getByText(c)).toBeVisible();
  // The code field is a field, square, with the inset focus ring (nj-field look).
  const code = page.getByPlaceholder("6-digit code");
  await code.fill("123456");
  expect(await code.evaluate((el) => getComputedStyle(el).borderRadius)).toBe("0px");
  await page.getByRole("button", { name: "Confirm & enable" }).click();
  // Verified: the key is never shown again on this page.
  await expect(page.getByText(KEY)).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText(KEY);
});

test("the Sharing list never renders a link's token or URL, even if a payload carried one", async ({
  page,
}) => {
  await base(page);
  const now = Math.floor(Date.now() / 1000);
  await page.route(/\/api\/shares(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        available: true,
        reason: null,
        shares: [
          {
            share_id: "shr_fixture_0001",
            session_key: "claude:7c1e1a00-5b2d-4c8e-9f31-0a6d2e8b4001",
            kind: "terminal",
            mode: "view",
            title: "Share-mask timing bound",
            has_password: true,
            expires_at: now + 3600,
            created_at: now - 600,
            revoked_at: null,
            revoked_reason: null,
            open_count: 2,
            last_opened_at: now - 60,
            // A server bug or a future field: the list must still not render it.
            token: TOKEN,
            url: `https://battlelab.example/share/${TOKEN}`,
          },
        ],
      },
    }),
  );
  await page.goto(settingsPath("sharing"));
  const row = page.locator("[data-share-row]");
  await expect(row).toHaveCount(1);
  await expect(row).toContainText("Share-mask timing bound");
  // The live link's light, with its words in the row.
  await expect(row.locator(".hud-led.up")).toHaveCount(1);
  await expect(page.locator("body")).not.toContainText(TOKEN);
});

test("the update progress scans the Acquiring strip while the installer runs, not after", async ({
  page,
}) => {
  await base(page);
  let state: "running" | "done" = "running";
  const now = () => Math.floor(Date.now() / 1000);
  await page.route("**/api/update/settings", (r) =>
    r.fulfill({ json: { auto_update: false, channel: "stable", last_auto: null } }),
  );
  await page.route("**/api/update/progress", (r) =>
    r.fulfill({
      json:
        state === "running"
          ? { state: "running", steps: 4, step: "build", step_index: 2, label: "Building the web app", started_at: now() - 30 }
          : { state: "done", steps: 4, step_index: 4, label: "Done", started_at: now() - 200, elapsed_s: 170 },
    }),
  );
  await page.goto(settingsPath("updates"));
  await expect(page.getByTestId("update-progress")).toHaveAttribute("data-state", "running");
  await expect(page.getByTestId("update-acquire")).toBeVisible();
  state = "done";
  await expect(page.getByTestId("update-progress")).toHaveAttribute("data-state", "done", { timeout: 15_000 });
  await expect(page.getByTestId("update-acquire")).toHaveCount(0);
});

test("a roster row's plan usage is the shared Gauge, a meter with its value", async ({ page }) => {
  await base(page);
  await page.route("**/api/agents/usage", (r) =>
    r.fulfill({
      json: {
        budgets: { threshold_pct: 90, notify: true, engines: {} },
        agents: [{ engine: "claude", source: "plan", used_pct: 58, windows: [{ label: "5h plan" }] }],
      },
    }),
  );
  await page.goto(settingsPath("agents"));
  const meter = page.getByRole("meter", { name: "58% of the plan used" });
  await expect(meter).toBeVisible();
  await expect(meter).toHaveAttribute("aria-valuenow", "58");
});

test("Defaults: the chosen default-agent card wears the accent edge", async ({ page }) => {
  await base(page);
  await page.goto("/settings/agents/defaults");
  const chosen = page.locator("label:has(input[type='radio']:checked)").first();
  await expect(chosen).toBeVisible();
  const [edge, accent] = await chosen.evaluate((el) => [
    getComputedStyle(el).borderTopColor,
    (() => {
      const p = document.createElement("span");
      p.style.color = "var(--accent)";
      document.body.appendChild(p);
      const c = getComputedStyle(p).color;
      p.remove();
      return c;
    })(),
  ]);
  expect(edge).toBe(accent);
});
