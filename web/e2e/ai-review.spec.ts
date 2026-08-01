import { expect, test } from "@playwright/test";

// Real-browser checks for the AI session review surface (#356 PR 1, manual slice):
// the Settings → AI Review panel (write-only key, model dropdown via the server proxy,
// prompt save) and the sidebar row (summary line + amber intervention badge). Network is
// fully mocked — the suite never talks to a backend or a real AI endpoint.

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

const NOW = Math.floor(Date.now() / 1000);

const SESSIONS = {
  sessions: [
    {
      id: "claude:aaaaaaaa-0000-0000-0000-000000000001",
      engine: "claude",
      uuid: "aaaaaaaa-0000-0000-0000-000000000001",
      short_uuid: "aaaaaaaa",
      cwd: "/home/u/infra",
      project: { kind: "folder", id: "/home/u/infra", name: "/home/u/infra" },
      last_mtime: NOW,
      first_user_message: "fix the runner",
      title: "Fix CI runner fork-EAGAIN limits",
      sticky: false,
      archived: false,
      ai_summary: "Editing systemd limits; tests rerunning after thread cap",
      ai_title: "Fix CI runner fork-EAGAIN limits",
      intervention_required: true,
      intervention_reason: "waiting on permission prompt",
      reviewed_at: NOW,
      review_excluded: false,
    },
  ],
  next_offset: null,
  total: 1,
  facets: { projects: [{ kind: "folder", id: "/home/u/infra", name: "/home/u/infra" }], engines: ["claude"] },
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        ai_review: AI_REVIEW,
        auto_sort: AUTO_SORT,
      },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  // The auto-sort section resolves near-miss project names via /api/projects on mount.
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/sessions**", (r) => r.fulfill({ json: SESSIONS }));
  await page.route("**/api/ai-review/models**", (r) =>
    r.fulfill({ json: { models: ["minimax-m2.7", "qwen3-vl", "gpt-oss-120b"] } }),
  );
});

test("settings: AI Review panel — write-only key, proxied model dropdown, prompt save", async ({
  page,
}) => {
  let prefsBody: unknown = null;
  await page.route("**/api/prefs", async (r) => {
    prefsBody = r.request().postDataJSON();
    await r.fulfill({ json: { ai_review: AI_REVIEW } });
  });

  await page.goto("/settings/ai-review");
  await expect(page.getByRole("heading", { name: "AI endpoint" })).toBeVisible();

  // Endpoint config renders from /api/config; the key is write-only (empty field + SET badge).
  await expect(page.getByLabel(/Endpoint base URL/i)).toHaveValue("https://ai.example.io/v1");
  const key = page.getByLabel(/API key/i);
  await expect(key).toHaveValue("");
  await expect(page.getByText("set", { exact: true })).toBeVisible();

  // Model dropdown is populated through the server-side proxy (the key never left the server).
  const model = page.getByRole("combobox", { name: "Model" });
  await expect(model).toHaveValue("minimax-m2.7");
  await model.selectOption("qwen3-vl");
  await expect.poll(() => prefsBody).toEqual({ ai_review: { model: "qwen3-vl" } });

  // Prompt editor: save a draft. Scope to the Session review section — the Auto-sort section
  // below it has its own Save/Reset for the classifier prompt (#459).
  const review = page.getByRole("region", { name: "Session review" });
  const prompt = review.getByRole("textbox", { name: "Review prompt" });
  await expect(prompt).toHaveValue("custom prompt");
  await prompt.fill("watch my fleet");
  await review.getByRole("button", { name: "Save", exact: true }).click();
  await expect.poll(() => prefsBody).toEqual({ ai_review: { prompt: "watch my fleet" } });
});

test("settings: a plain visit with a stored config stays quiet — no phantom dirty/validating state (#543)", async ({
  page,
}) => {
  await page.goto("/settings/ai-review");
  // Mount probe done: the dropdown is populated through the proxy.
  await expect(page.getByRole("combobox", { name: "Model" })).toHaveValue("minimax-m2.7");
  // The status line reports explicit actions only — a plain visit must show neither the
  // save-style validation lifecycle nor an unsaved-changes warning (#543).
  await expect(page.getByText(/Validating endpoint/i)).toBeHidden();
  await expect(page.getByText(/Endpoint validated/i)).toBeHidden();
  await expect(page.getByText(/Unsaved changes/i)).toBeHidden();
  // The write-only key field opts out of password-manager autofill — browsers ignore
  // "off" and would fill the app's login password here, dirtying the form.
  await expect(page.getByLabel(/API key/i)).toHaveAttribute("autocomplete", "new-password");
});

test("settings: Remove key clears the stored secret and refetches /api/config", async ({
  page,
}) => {
  // Hermes #367: the blank field means "unchanged" — clearing needs the explicit Remove
  // key action (api_key: null), and a configured-flip must refetch the shared config so
  // sidebar gating updates without a reload.
  let prefsBody: unknown = null;
  let configCalls = 0;
  const cleared = { ...AI_REVIEW, api_key_set: false, configured: false };
  await page.route("**/api/config", (r) => {
    configCalls += 1;
    return r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        ai_review: configCalls > 1 ? cleared : AI_REVIEW,
      },
    });
  });
  await page.route("**/api/prefs", async (r) => {
    prefsBody = r.request().postDataJSON();
    await r.fulfill({ json: { ai_review: cleared } });
  });

  await page.goto("/settings/ai-review");
  await expect(page.getByText("set", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Remove key" }).click();
  await expect.poll(() => prefsBody).toEqual({ ai_review: { api_key: null } });
  // The SET badge and the Remove action are gone; the shared config was refetched.
  await expect(page.getByText("set", { exact: true })).toBeHidden();
  await expect(page.getByRole("button", { name: "Remove key" })).toBeHidden();
  await expect.poll(() => configCalls).toBeGreaterThan(1);
});

test("sidebar: summary line + amber intervention badge with the reason as tooltip", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name === "mobile", "sidebar is off-canvas on mobile — desktop covers the row surface");
  await page.goto("/");
  await expect(
    page.getByText("Editing systemd limits; tests rerunning after thread cap"),
  ).toBeVisible();
  const badge = page.getByRole("img", { name: /intervention required/i });
  await expect(badge).toBeVisible();
  await expect(badge).toHaveAttribute("title", "waiting on permission prompt");
});

test("mobile: AI Review settings panel renders at phone width", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "phone-width layout check");
  await page.route("**/api/prefs", (r) => r.fulfill({ json: { ai_review: AI_REVIEW } }));
  await page.goto("/settings/ai-review");
  await expect(page.getByRole("heading", { name: "AI endpoint" })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Model" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Review prompt" })).toBeVisible();
});
