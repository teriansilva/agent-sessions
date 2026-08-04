import { expect, test } from "@playwright/test";

// Real-browser check that a saved Pulse setting survives remounting the panel. ConfigCtx is
// fetched once at app load; before the fix, PulseSettings never refreshed it after a save, so
// leaving the AI tab and coming back re-seeded the panel from the stale context and the saved
// value appeared lost ("where do I save?"). The mock is stateful — /api/prefs updates the pulse
// block that later /api/config fetches return — exactly like the real server.

const AI_REVIEW = {
  enabled: false,
  base_url: "https://ai.example.io/v1",
  model: "minimax-m2.7",
  interval_minutes: 5,
  prompt: "custom prompt",
  max_input_chars: 24000,
  api_key_set: true,
  configured: true,
  default_prompt: "default prompt from server",
};

const AUTO_SORT = {
  enabled: false,
  interval_minutes: 30,
  confidence_min: 0.7,
  max_per_pass: 8,
  prompt: "default sort prompt",
  configured: true,
  default_prompt: "default sort prompt",
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

test("a saved Pulse setting flashes Saved. and survives leaving + reopening the tab", async ({
  page,
}) => {
  await page.goto("/settings/ai-review");
  const depth = page.getByLabel("Scan depth");
  await expect(depth).toHaveValue("fast");

  await depth.selectOption("slow");
  // Immediate feedback — the section says so instead of leaving the user hunting for a Save button.
  await expect(page.getByText("Saved.")).toBeVisible();

  // Leaving the tab unmounts the panel; coming back re-seeds it from the config context.
  await page.getByRole("tab", { name: "Appearance" }).click();
  await page.getByRole("tab", { name: "AI", exact: true }).click();
  await expect(page.getByLabel("Scan depth")).toHaveValue("slow");
});
