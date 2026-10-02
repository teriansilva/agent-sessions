import { expect, test, type Page } from "@playwright/test";
import { settingsPath } from "../src/routes/settingsTabs";

// #692: the setup wizard's "Set up your AI" step must (1) persist the endpoint + model and
// (2) surface it in Settings → AI WITHOUT a page reload. Since #956 the wizard hosts the same
// Endpoint & model component Settings uses — Save connection (checked, then saved), then Save
// model — so this also proves the wizard runs that flow and not a copy of the old one.
//
// Real-browser proof (desktop + the mobile ≤800px project): start AT Settings → AI → Endpoint &
// model so its page is mounted UNDER the wizard overlay; save in the wizard, dismiss the overlay
// (no navigation, no reload) and observe the page — through the same ConfigProvider — reflect the
// saved endpoint and model.

const MODELS = ["gpt-4o", "gpt-4o-mini", "o3-mini"];

async function mockApp(page: Page) {
  // Mutable server state: a /api/prefs write flips ai_review unconfigured → configured, and
  // /api/config echoes it — exactly the provider boundary the fix has to cross live.
  const ai = {
    base_url: "",
    model: "",
    request_timeout: null as number | null,
    api_key_set: false,
    configured: false,
  };
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
          max_input_chars: 24000,
          request_timeout: ai.request_timeout,
          api_key_set: ai.api_key_set,
          configured: ai.configured,
        },
      },
    }),
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
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({
      json: {
        engines: [
          { id: "claude", present: true, supports_new: true, bin: "/x/claude" },
        ],
      },
    }),
  );
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({
      json: { folders: [{ cwd: "/home/u/battlelab", label: "battlelab" }] },
    }),
  );
  await page.route(/\/api\/fs\/dirs(\?.*)?$/, (r) =>
    r.fulfill({ json: { path: "/home/u", home: "/home/u", dirs: [] } }),
  );
  await page.route("**/api/ai-review/models**", (r) =>
    r.fulfill({ json: { models: MODELS } }),
  );
  await page.route("**/api/ai-review/endpoint/test", (r) =>
    r.fulfill({ json: { models: MODELS, listing: "ok" } }),
  );
  await page.route("**/api/prefs", async (r) => {
    const body = (r.request().postDataJSON() ?? {}) as {
      onboarded?: boolean;
      ai_review?: {
        base_url?: string;
        api_key?: string;
        model?: string;
        request_timeout?: number | null;
      };
    };
    if (body.onboarded) onboarded = true;
    if (body.ai_review) {
      const p = body.ai_review;
      if (p.base_url !== undefined) ai.base_url = p.base_url;
      if (p.api_key) ai.api_key_set = true;
      if (p.model !== undefined) ai.model = p.model;
      if (p.request_timeout !== undefined) ai.request_timeout = p.request_timeout;
      ai.configured = !!ai.base_url && ai.api_key_set;
    }
    await r.fulfill({
      json: {
        ai_review: {
          enabled: false,
          base_url: ai.base_url,
          model: ai.model,
          interval_minutes: 5,
          max_input_chars: 24000,
          request_timeout: ai.request_timeout,
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
  // Mount Settings → AI → Endpoint & model UNDER the wizard overlay: dismissing the wizard reveals it
  // with no navigation and no reload — a pure shared-ConfigCtx proof.
  await page.goto(settingsPath("ai-endpoint"), { waitUntil: "domcontentloaded" });

  const dialog = page.getByRole("dialog", { name: /set up battlelab/i });
  await expect(dialog).toBeVisible();

  // Walk the wizard to the "Set up your AI" step. (auth_mode:none → the Security step is the
  // login-off "Skip — continue" variant, not the 2FA "Continue" one.)
  await dialog.getByRole("button", { name: /get started/i }).click(); // → security
  await dialog.getByRole("button", { name: /skip.*continue/i }).click(); // → agents
  await expect(dialog.getByText("claude", { exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: /^next$/i }).click(); // → ai

  // Save connection → checked, saved, and the Model field becomes a dropdown from the listing.
  await expect(
    dialog.getByRole("button", { name: /save & validate/i }),
  ).toHaveCount(0);
  await dialog.getByLabel(/Base URL/i).fill("https://api.openai.com/v1");
  await dialog.getByLabel(/API key/i).fill("sk-secret");
  await dialog.getByRole("button", { name: "Save connection" }).click();
  const wizModel = dialog.getByRole("combobox", { name: "Model" });
  await expect(wizModel).toBeVisible();
  await expect(
    dialog.getByText(/✓ Connected — 3 models available/),
  ).toBeVisible();
  // Picking is not saving: Save model stores it.
  await wizModel.selectOption("o3-mini");
  await dialog.getByRole("button", { name: "Save model" }).click();
  await expect(dialog.getByText(/✓ Model saved — active: o3-mini/)).toBeVisible();

  // The AI step must not scroll the page horizontally at this width (≤800px footer wrap, #494).
  await expectNoHScroll(page);

  // Dismiss the wizard (Esc → finish) — reveals the Settings panel already mounted beneath.
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();

  // Settings → AI → Endpoint & model now reflects the wizard's save, WITHOUT a reload.
  await expect(page.getByRole("heading", { name: "Connection" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: /Base URL/i })).toHaveValue(
    "https://api.openai.com/v1",
  );
  const settingsModel = page.getByRole("combobox", { name: "Model" });
  await expect(settingsModel).toBeVisible();
  await expect(settingsModel).toHaveValue("o3-mini");
});

async function expectNoHScroll(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  );
  expect(overflow, "page must not scroll horizontally").toBeLessThanOrEqual(1);
}
