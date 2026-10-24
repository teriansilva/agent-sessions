/** The setup wizard's HQ contact step (#1544), in a real browser on desktop and mobile. The step
 *  exists only once an AI endpoint is configured — HQ cannot answer without one — and it appears
 *  live when the AI step's save lands. A pick goes to the same `hq_contact` pref as Settings. The
 *  wizard's local-AI presets fill the Base URL draft and save nothing. */
import { expect, test, type Page } from "@playwright/test";

import { DASHBOARD_PATH } from "../src/lib/routes";

const MODELS = ["qwen3", "llama3.3"];

async function mockApp(page: Page, opts: { aiConfigured: boolean }) {
  const ai = {
    base_url: opts.aiConfigured ? "http://localhost:11434/v1" : "",
    model: opts.aiConfigured ? "qwen3" : "",
    api_key_set: opts.aiConfigured,
    configured: opts.aiConfigured,
  };
  const state = { hq_contact: "random", rev: 0, writes: [] as Record<string, unknown>[] };
  const aiBlock = () => ({
    enabled: false,
    base_url: ai.base_url,
    model: ai.model,
    interval_minutes: 5,
    max_input_chars: 24000,
    request_timeout: null,
    api_key_set: ai.api_key_set,
    configured: ai.configured,
  });

  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        auth_mode: "none",
        username: null,
        terminal_backend: "ws",
        new_session_engines: ["claude"],
        onboarded: false,
        ai_review: aiBlock(),
        hq_contact: state.hq_contact,
        hq_contact_rev: state.rev,
      },
    }),
  );
  await page.route("**/api/prefs", async (r) => {
    if (r.request().method() !== "POST") return r.fulfill({ json: {} });
    const body = (r.request().postDataJSON() ?? {}) as {
      hq_contact?: string;
      ai_review?: { base_url?: string; api_key?: string; model?: string };
    };
    state.writes.push(body);
    if (body.hq_contact) {
      state.hq_contact = body.hq_contact;
      state.rev += 1;
    }
    if (body.ai_review) {
      const p = body.ai_review;
      if (p.base_url !== undefined) ai.base_url = p.base_url;
      if (p.api_key) ai.api_key_set = true;
      if (p.model !== undefined) ai.model = p.model;
      ai.configured = !!ai.base_url && ai.api_key_set;
    }
    return r.fulfill({
      json: { ...body, ai_review: aiBlock(), hq_contact_rev: state.rev },
    });
  });
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [], total: 0, next_offset: null, facets: { projects: [], engines: [] } },
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
  // The provider table the picker renders (#1657): the server's, never a client copy.
  await page.route("**/api/ai-review/providers", (r) =>
    r.fulfill({
      json: {
        providers: [
          { id: "custom", label: "Custom (OpenAI-compatible)", group: "custom", kind: "openai-chat", base_url: "", url_editable: true, vendor: "", thinking_supported: true, auto_model: "", key_hint: "" },
          { id: "ollama", label: "Ollama", group: "self-hosted", kind: "openai-chat", base_url: "http://localhost:11434/v1", url_editable: true, vendor: "", thinking_supported: true, auto_model: "", key_hint: "" },
          { id: "openai", label: "OpenAI", group: "api", kind: "openai-chat", base_url: "https://api.openai.com/v1", url_editable: false, vendor: "OpenAI", thinking_supported: true, auto_model: "gpt-5-mini", key_hint: "sk-…" },
        ],
      },
    }),
  );
  await page.route("**/api/ai-review/endpoint/test", (r) =>
    r.fulfill({ json: { models: MODELS, listing: "ok" } }),
  );
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  return state;
}

async function toAiStep(page: Page) {
  await page.goto(DASHBOARD_PATH, { waitUntil: "domcontentloaded" });
  const dialog = page.getByRole("dialog", { name: /set up battlelab/i });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("heading", { name: "Welcome, Commander." })).toBeVisible();
  await dialog.getByRole("button", { name: /get started/i }).click(); // → security
  await dialog.getByRole("button", { name: /skip.*continue/i }).click(); // → agents
  await expect(dialog.getByText("claude", { exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: /^next$/i }).click(); // → ai
  await expect(dialog.getByRole("heading", { name: "Set up your AI" })).toBeVisible();
  return dialog;
}

async function expectNoHScroll(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  );
  expect(overflow, "page must not scroll horizontally").toBeLessThanOrEqual(1);
}

test("with AI configured, the contact step follows AI and a pick is stored as hq_contact (#1544)", async ({
  page,
}) => {
  const state = await mockApp(page, { aiConfigured: true });
  const dialog = await toAiStep(page);
  const rail = dialog.getByRole("navigation", { name: /setup steps/i });
  await expect(rail.getByText("HQ contact")).toBeVisible();

  // Next sits at the foot of the long AI step; the contact step must open at its own top, title in
  // view, not at the AI step's scroll offset (found in the #1544 capture).
  await dialog.getByRole("button", { name: /^next$/i }).click(); // → contact
  await expect(dialog.getByRole("heading", { name: "Choose your HQ contact" })).toBeInViewport();
  const gallery = dialog.getByRole("radiogroup", { name: /select your primary hq contact/i });
  await expect(gallery.getByRole("radio", { name: /random/i })).toHaveAttribute(
    "aria-checked",
    "true",
  );
  // Anchored: the Random card reads "Today: <name>", so on a day whose contact is Harlan an
  // unanchored match finds that card too (strict-mode failure, date-dependent).
  const harlan = gallery.getByRole("radio", { name: /^general jack harlan/i });
  await harlan.scrollIntoViewIfNeeded();
  // A real tap target, not a sliver: the card is at least the 44px floor both ways.
  const box = await harlan.boundingBox();
  expect(box && box.width >= 44 && box.height >= 44).toBe(true);
  await harlan.click();
  await expect(harlan).toHaveAttribute("aria-checked", "true");
  await expect.poll(() => state.writes.some((w) => w.hq_contact === "us-man")).toBe(true);
  await expectNoHScroll(page);

  await dialog.getByRole("button", { name: /^next$/i }).click(); // → project
  await expect(dialog.getByRole("heading", { name: /create your first project/i })).toBeVisible();
  await dialog.getByRole("button", { name: /back/i }).click();
  await expect(dialog.getByRole("heading", { name: "Choose your HQ contact" })).toBeVisible();
});

test("without AI there is no contact step until the AI save lands; presets fill the URL only (#1544)", async ({
  page,
}) => {
  const state = await mockApp(page, { aiConfigured: false });
  const dialog = await toAiStep(page);
  const rail = dialog.getByRole("navigation", { name: /setup steps/i });
  await expect(rail.getByText("HQ contact")).toHaveCount(0);

  // A preset fills the Base URL draft — and writes nothing. Presets are the self-hosted tiles of
  // the provider picker since #1671.
  const presets = dialog.getByRole("radiogroup", { name: /self-hosted/i });
  await presets.getByRole("radio", { name: "Ollama" }).click();
  const url = dialog.getByRole("textbox", { name: /base url/i });
  await expect(url).toHaveValue("http://localhost:11434/v1");
  await expect(presets.getByRole("radio", { name: "Ollama" })).toHaveAttribute(
    "aria-checked",
    "true",
  );
  expect(state.writes).toEqual([]);
  await expectNoHScroll(page);

  // Saving the connection configures the endpoint; the contact step joins the rail live.
  await dialog.getByLabel(/API key/i).fill("local");
  await dialog.getByRole("button", { name: "Save connection" }).click();
  await expect(rail.getByText("HQ contact")).toBeVisible();
  await dialog.getByRole("button", { name: /^next$/i }).click();
  await expect(dialog.getByRole("heading", { name: "Choose your HQ contact" })).toBeVisible();
  // Decide later leaves the stored choice alone.
  await dialog.getByRole("button", { name: /decide later/i }).click();
  await expect(dialog.getByRole("heading", { name: /create your first project/i })).toBeVisible();
  expect(state.writes.some((w) => "hq_contact" in w)).toBe(false);
});
