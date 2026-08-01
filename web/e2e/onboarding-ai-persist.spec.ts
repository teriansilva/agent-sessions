import { expect, test, type Page } from "@playwright/test";

// #692: the setup wizard's "Set up your AI" step must (1) persist the endpoint + model and
// (2) surface it in Settings → AI WITHOUT a page reload — the bug was Onboarding.saveAi()
// never calling useConfigRefresh(), so the shared ConfigCtx stayed stale and Settings kept
// showing the pre-save (unconfigured) state. It also brings the wizard's Model field to
// parity with Settings: a /models-populated dropdown with a free-text fallback.
//
// Real-browser proof (runs on desktop + the mobile ≤800px project): start AT /settings/ai-review
// so the Settings panel is mounted UNDER the wizard overlay; save in the wizard, dismiss the
// overlay (no navigation, no reload) and observe the Settings panel — through the same
// ConfigProvider — reflect the saved endpoint + the model dropdown. RED before #692 (Settings
// stays stale: empty endpoint, free-text model), GREEN after (refreshConfig crosses the boundary).

const MODELS = ["gpt-4o", "gpt-4o-mini", "o3-mini"];

async function mockApp(page: Page) {
  // Mutable server state: a /api/prefs write flips ai_review unconfigured → configured, and
  // /api/config echoes it — exactly the provider boundary the fix has to cross live.
  const ai = { base_url: "", model: "", api_key_set: false, configured: false };
  let onboarded = false;

  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["claude"],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: [],
        projects_hidden: [],
        onboarded,
        ai_review: {
          enabled: false,
          base_url: ai.base_url,
          model: ai.model,
          interval_minutes: 5,
          prompt: "",
          max_input_chars: 24000,
          request_timeout: null,
          api_key_set: ai.api_key_set,
          configured: ai.configured,
          default_prompt: "",
        },
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) =>
    r.fulfill({
      json: { engines: [{ id: "claude", present: true, supports_new: true, bin: "/x/claude" }] },
    }),
  );
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [{ cwd: "/home/u/battlelab", label: "battlelab" }] } }),
  );
  await page.route(/\/api\/fs\/dirs(\?.*)?$/, (r) =>
    r.fulfill({ json: { path: "/home/u", home: "/home/u", dirs: [] } }),
  );
  await page.route("**/api/ai-review/models**", (r) => r.fulfill({ json: { models: MODELS } }));
  await page.route("**/api/prefs", async (r) => {
    const body = (r.request().postDataJSON() ?? {}) as {
      onboarded?: boolean;
      ai_review?: { base_url?: string; api_key?: string; model?: string };
    };
    if (body.onboarded) onboarded = true;
    if (body.ai_review) {
      const p = body.ai_review;
      if (p.base_url !== undefined) ai.base_url = p.base_url;
      if (p.api_key) ai.api_key_set = true;
      if (p.model !== undefined) ai.model = p.model;
      ai.configured = !!ai.base_url && ai.api_key_set;
    }
    await r.fulfill({
      json: {
        ai_review: {
          base_url: ai.base_url,
          model: ai.model,
          api_key_set: ai.api_key_set,
          configured: ai.configured,
        },
      },
    });
  });
}

test("wizard AI setup persists and Settings reflects it live — no reload (#692)", async ({
  page,
}) => {
  await mockApp(page);
  // Mount the Settings → AI panel UNDER the wizard overlay: dismissing the wizard reveals it
  // with no navigation and no reload — a pure shared-ConfigCtx proof.
  await page.goto("/settings/ai-review", { waitUntil: "domcontentloaded" });

  const dialog = page.getByRole("dialog", { name: /set up battlelab/i });
  await expect(dialog).toBeVisible();

  // Walk the wizard to the "Set up your AI" step. (auth_mode:none → the Security step is the
  // login-off "Skip — continue" variant, not the 2FA "Continue" one.)
  await dialog.getByRole("button", { name: /get started/i }).click(); // → security
  await dialog.getByRole("button", { name: /skip.*continue/i }).click(); // → agents
  await expect(dialog.getByText("claude", { exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: /^next$/i }).click(); // → ai

  // Fill + Save & validate → the Model field becomes a dropdown populated from /models.
  await dialog.getByPlaceholder(/api\.openai\.com/i).fill("https://api.openai.com/v1");
  await dialog.getByPlaceholder(/never echoed/i).fill("sk-secret");
  await dialog.getByRole("button", { name: /save & validate/i }).click();
  const wizModel = dialog.getByRole("combobox", { name: /model/i });
  await expect(wizModel).toBeVisible();
  await expect(dialog.getByText(/endpoint validated — 3 models/i)).toBeVisible();
  await wizModel.selectOption("o3-mini");

  // The AI step must not scroll the page horizontally at this width (≤800px footer wrap, #494).
  await expectNoHScroll(page);

  // Dismiss the wizard (Esc → finish) — reveals the Settings panel already mounted beneath.
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();

  // Settings → AI now reflects the wizard-saved endpoint + the /models dropdown, WITHOUT reload.
  await expect(page.getByRole("heading", { name: /ai endpoint/i })).toBeVisible();
  await expect(page.getByRole("textbox", { name: /endpoint base url/i })).toHaveValue(
    "https://api.openai.com/v1",
  );
  const settingsModel = page.getByRole("combobox", { name: /model/i });
  await expect(settingsModel).toBeVisible();
  await expect(settingsModel).toHaveValue("o3-mini");
});

async function expectNoHScroll(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  );
  expect(overflow, "page must not scroll horizontally").toBeLessThanOrEqual(1);
}
