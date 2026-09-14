import { expect, type Page, test } from "@playwright/test";
import { settingsPath } from "../src/routes/settingsTabs";

// Real-browser check that a saved Pulse setting survives remounting the panel. ConfigCtx is
// fetched once at app load; before the fix, PulseSettings never refreshed it after a save, so
// leaving the page and coming back re-seeded the panel from the stale context and the saved
// value appeared lost ("where do I save?"). The mock is stateful — /api/prefs updates the pulse
// block that later /api/config fetches return — exactly like the real server.

const AI_REVIEW = {
  enabled: false,
  base_url: "https://ai.example.io/v1",
  model: "minimax-m2.7",
  interval_minutes: 5,
  max_input_chars: 24000,
  api_key_set: true,
  configured: true,
};

const AUTO_SORT = {
  enabled: false,
  interval_minutes: 30,
  confidence_min: 0.7,
  max_per_pass: 8,
  configured: true,
};

test.beforeEach(async ({ page }) => {
  const pulse = {
    auto_enabled: false,
    interval_minutes: 30,
    window_days: 3,
    scan_depth: "fast",
    configured: true,
  };
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        ai_review: AI_REVIEW,
        auto_sort: AUTO_SORT,
        pulse: { ...pulse },
      },
    }),
  );
  await page.route("**/api/prefs", async (r) => {
    const body = r.request().postDataJSON() as {
      pulse?: Record<string, unknown>;
    } | null;
    Object.assign(pulse, body?.pulse ?? {});
    await r.fulfill({ json: { pulse: { ...pulse } } });
  });
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [] } }),
  );
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [] } }),
  );
  await page.route(/\/api\/projects($|\?)/, (r) =>
    r.fulfill({ json: { projects: [] } }),
  );
  await page.route("**/api/ai/activity", (r) =>
    r.fulfill({ json: { running: [], last: {} } }),
  );
  await page.route("**/api/ai-review/models**", (r) =>
    r.fulfill({ json: { models: ["minimax-m2.7"] } }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions: [],
        next_offset: null,
        total: 0,
        facets: { projects: [], engines: [] },
      },
    }),
  );
});

/** In-app section switch — never `page.goto`, which would reload and re-fetch the config and so
 *  prove nothing about remounting. Desktop has the sidebar; a phone goes back to the index first.
 *
 *  Waits on RENDERED state, not the URL: the router updates the URL before it re-renders, so a
 *  URL assertion can pass while the old page is still on screen, and the next switch then acts on
 *  a page that is about to unmount. */
async function openSection(page: Page, label: string) {
  const nav = page.getByRole("navigation", { name: "Settings", exact: true });
  const phone = page.viewportSize()!.width <= 800;
  if (phone) {
    await page.getByRole("link", { name: "Back to settings" }).click();
    await expect(nav).toBeVisible();
    await nav.getByRole("link", { name: label, exact: true }).click();
    await expect(nav).toBeHidden();
    await expect(page.getByRole("link", { name: "Back to settings" })).toBeVisible();
  } else {
    const link = nav.getByRole("link", { name: label, exact: true });
    await link.click();
    await expect(link).toHaveAttribute("aria-current", "page");
  }
}

test("a saved scan setting flashes Saved. and survives leaving + reopening the page", async ({
  page,
}) => {
  await page.goto(settingsPath("ai-mission-control"));
  const depth = page.getByLabel("Scan depth");
  await expect(depth).toHaveValue("fast");

  await depth.selectOption("slow");
  // Immediate feedback — the section says so instead of leaving the user hunting for a Save button.
  await expect(page.getByText("Saved.")).toBeVisible();

  // Leaving the page unmounts the panel; coming back re-seeds it from the config context.
  await openSection(page, "Appearance");
  await openSection(page, "Mission control");
  await expect(page.getByLabel("Scan depth")).toHaveValue("slow");
});
