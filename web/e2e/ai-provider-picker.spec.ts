import { expect, type Locator, type Page, test } from "@playwright/test";
import { settingsPath } from "../src/routes/settingsTabs";
import { TOUCH_FLOOR } from "./touchTarget";

// Settings → AI → Endpoint & model: the provider picker and the egress acknowledgement (#1657 P1b,
// #1671), in a real browser on both projects. The network is fully mocked: the catalog, the saved
// block and the server's acknowledgement rules (consent never carries to another recipient; a
// patch's own `egress_acknowledged` binds it to the new one). The SERVER enforces all of this on
// every save, test and send — what is proven here is the page: what it says, what it locks, and
// what it sends.

const CATALOG = [
  { id: "custom", label: "Custom (OpenAI-compatible)", group: "custom", kind: "openai-chat", base_url: "", url_editable: true, vendor: "", thinking_supported: true, auto_model: "", key_hint: "" },
  { id: "ollama", label: "Ollama", group: "self-hosted", kind: "openai-chat", base_url: "http://localhost:11434/v1", url_editable: true, vendor: "", thinking_supported: true, auto_model: "", key_hint: "" },
  { id: "openai", label: "OpenAI", group: "api", kind: "openai-chat", base_url: "https://api.openai.com/v1", url_editable: false, vendor: "OpenAI", thinking_supported: true, auto_model: "gpt-5-mini", key_hint: "sk-…" },
  { id: "mistral", label: "Mistral", group: "api", kind: "openai-chat", base_url: "https://api.mistral.ai/v1", url_editable: false, vendor: "Mistral AI", thinking_supported: false, auto_model: "mistral-medium-latest", key_hint: "" },
];

function originOf(url: string): string {
  const u = new URL(url);
  return `${u.protocol.slice(0, -1)}://${u.hostname}:${u.port || (u.protocol === "https:" ? "443" : "80")}`;
}

type Ack = { provider: string; origin: string; at: string; source: string } | null;

const UNCONFIGURED = {
  enabled: false,
  base_url: "",
  model: "",
  interval_minutes: 5,
  max_input_chars: 24000,
  request_timeout: null,
  api_key_set: false,
  configured: false,
  provider: "custom",
  model_mode: "specific",
  thinking: "auto",
  egress_ack: null as Ack,
};

/** A server that applies the acknowledgement rules to what /api/prefs stores and /api/config
 *  echoes, and records every draft test and save. */
async function server(page: Page, start: Record<string, unknown> = UNCONFIGURED) {
  const ai: Record<string, unknown> = { ...start };
  const tests: Record<string, unknown>[] = [];
  const saves: Record<string, unknown>[] = [];
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        ai_review: { ...ai },
      },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } } }),
  );
  await page.route("**/api/ai-review/providers", (r) => r.fulfill({ json: { providers: CATALOG } }));
  await page.route("**/api/ai-review/models**", (r) => r.fulfill({ json: { models: ["gpt-4.1", "gpt-5-mini"] } }));
  await page.route("**/api/ai-review/endpoint/test", async (r) => {
    tests.push(r.request().postDataJSON());
    await r.fulfill({ json: { models: ["gpt-4.1", "gpt-5-mini"], listing: "ok" } });
  });
  await page.route("**/api/prefs", async (r) => {
    const body = (r.request().postDataJSON() ?? {}) as { ai_review?: Record<string, unknown> };
    const p = body.ai_review ?? {};
    saves.push(p);
    const prevOrigin = ai.base_url ? originOf(String(ai.base_url)) : "";
    const prevProvider = ai.provider;
    const row = CATALOG.find((c) => c.id === (p.provider ?? ai.provider));
    if (typeof p.provider === "string") ai.provider = p.provider;
    if (typeof p.base_url === "string") ai.base_url = row && !row.url_editable ? row.base_url : p.base_url;
    if (typeof p.model === "string") ai.model = p.model;
    for (const k of ["model_mode", "thinking", "request_timeout"]) if (k in p) ai[k] = p[k];
    if ("api_key" in p) ai.api_key_set = p.api_key !== null;
    const moved = ai.provider !== prevProvider || (ai.base_url ? originOf(String(ai.base_url)) : "") !== prevOrigin;
    if (moved) ai.egress_ack = null;
    if (p.egress_acknowledged === true && ai.base_url)
      ai.egress_ack = { provider: String(ai.provider), origin: originOf(String(ai.base_url)), at: "t", source: "operator" };
    ai.configured = !!ai.base_url && ai.api_key_set === true;
    await r.fulfill({ json: { ai_review: { ...ai } } });
  });
  return { tests, saves };
}

const open = (page: Page) => page.goto(settingsPath("ai-endpoint"));
const tile = (page: Page, name: string) => page.getByRole("radio", { name, exact: true });
const ack = (page: Page) => page.getByRole("checkbox", { name: /I understand session content is sent to/ });
const saveConnection = (page: Page) => page.getByRole("button", { name: "Save connection" });
const testBtn = (page: Page) => page.getByRole("button", { name: "Test", exact: true });

test("a hosted provider: fixed endpoint, its own terms, and nothing tested or saved until acknowledged", async ({ page }) => {
  const s = await server(page);
  await open(page);
  await tile(page, "OpenAI").click();
  await expect(page.getByTestId("ai-fixed-endpoint")).toHaveText("api.openai.com · fixed by BattleLab");
  await expect(page.getByLabel(/Base URL/i)).toHaveCount(0);
  await page.getByLabel(/API key/i).fill("sk-o");
  const warning = page.getByTestId("ai-egress");
  await expect(warning).toContainText("Session data leaves this host");
  await expect(warning).toContainText("OpenAI's terms, retention and data location apply");
  await expect(testBtn(page)).toBeDisabled();
  await expect(saveConnection(page)).toBeDisabled();
  await expect(page.getByText("Tick the acknowledgement to test or save. Nothing is sent before.")).toBeVisible();
  expect(s.tests).toEqual([]);

  await ack(page).check();
  await saveConnection(page).click();
  await expect.poll(() => s.saves.length).toBe(1);
  expect(s.tests[0]).toEqual({ base_url: "https://api.openai.com/v1", api_key: "sk-o", provider: "openai", egress_acknowledged: true });
  expect(s.saves[0]).toEqual({ base_url: "https://api.openai.com/v1", provider: "openai", api_key: "sk-o", egress_acknowledged: true });
  await expect(page.getByText(/✓ Connected/)).toBeVisible();
});

test("changing the recipient unticks the acknowledgement — by provider, and by URL", async ({ page }) => {
  await server(page);
  await open(page);
  await page.getByLabel(/Base URL/i).fill("https://a.example.io/v1");
  await ack(page).check();
  await expect(ack(page)).toBeChecked();
  await page.getByLabel(/Base URL/i).fill("https://b.example.io/v1");
  await expect(ack(page)).not.toBeChecked();
  await expect(saveConnection(page)).toBeDisabled();
  await ack(page).check();
  await tile(page, "Mistral").click();
  await expect(ack(page)).not.toBeChecked();
  await expect(page.getByTestId("ai-egress")).toContainText("Mistral AI");
});

test("a remote Custom URL names its host's policy; a loopback preset needs nothing", async ({ page }) => {
  await server(page);
  await open(page);
  await page.getByLabel(/Base URL/i).fill("https://gpu.example.net/v1");
  await expect(page.getByTestId("ai-egress")).toContainText(
    "Whoever runs gpu.example.net decides what is kept and for how long. Check that host's terms and retention policy, not BattleLab's.",
  );
  await tile(page, "Ollama").click();
  await expect(page.getByTestId("ai-egress")).toHaveCount(0);
  await expect(page.getByTestId("ai-egress-local")).toContainText("localhost is this host (loopback)");
  await page.getByLabel(/API key/i).fill("local");
  await expect(saveConnection(page)).toBeEnabled();
});

test("a carried-over (legacy) acknowledgement renders ticked with its note; a URL edit asks again", async ({ page }) => {
  await server(page, {
    ...UNCONFIGURED,
    base_url: "https://legacy.example.io/v1",
    model: "m",
    api_key_set: true,
    configured: true,
    egress_ack: { provider: "custom", origin: "https://legacy.example.io:443", at: "t", source: "legacy" },
  });
  await open(page);
  await expect(ack(page)).toBeChecked();
  await expect(ack(page)).toBeDisabled();
  await expect(page.getByTestId("ai-egress-legacy")).toContainText("Carried over from before this version");
  await page.getByLabel(/Base URL/i).fill("https://elsewhere.example.org/v1");
  await expect(ack(page)).not.toBeChecked();
  await expect(page.getByTestId("ai-egress-legacy")).toHaveCount(0);
  await expect(testBtn(page)).toBeDisabled();
});

test("Thinking has no levels for a provider without one, and says so", async ({ page }) => {
  await server(page, {
    ...UNCONFIGURED,
    provider: "mistral",
    base_url: "https://api.mistral.ai/v1",
    api_key_set: true,
    configured: true,
    egress_ack: { provider: "mistral", origin: "https://api.mistral.ai:443", at: "t", source: "operator" },
  });
  await open(page);
  const thinking = page.getByRole("radiogroup", { name: "Thinking" });
  await expect(thinking.getByRole("radio", { name: "High" })).toBeDisabled();
  await expect(page.getByTestId("ai-thinking-hint")).toContainText("Mistral has no thinking setting");
});

// ---- geometry and accessibility: both themes × desktop and 390 px, coarse pointer -------------

/** WCAG contrast of a text element against the first opaque background behind it. Colours are
 *  resolved by painting them, so `color-mix()` and modern colour syntax need no parser. */
async function contrast(el: Locator): Promise<number> {
  return el.evaluate((node) => {
    const ctx = document.createElement("canvas").getContext("2d")!;
    const rgba = (css: string) => {
      ctx.clearRect(0, 0, 1, 1);
      ctx.fillStyle = "#000";
      ctx.fillStyle = css;
      ctx.fillRect(0, 0, 1, 1);
      return Array.from(ctx.getImageData(0, 0, 1, 1).data);
    };
    let bgNode: Element | null = node;
    let bg = [0, 0, 0, 0];
    while (bgNode) {
      bg = rgba(getComputedStyle(bgNode).backgroundColor);
      if (bg[3] === 255) break;
      bgNode = bgNode.parentElement;
    }
    const fg = rgba(getComputedStyle(node).color);
    const lum = ([r, g, b]: number[]) =>
      [r, g, b]
        .map((v) => v / 255)
        .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4))
        .reduce((acc, c, i) => acc + c * [0.2126, 0.7152, 0.0722][i], 0);
    const [a, b] = [lum(fg), lum(bg)].sort((x, y) => y - x);
    return (a + 0.05) / (b + 0.05);
  });
}

for (const theme of ["dark", "light"] as const) {
  for (const width of [1280, 390]) {
    test.describe(`geometry · ${theme} · ${width}px · coarse pointer`, () => {
      test.use({ hasTouch: true, isMobile: true, viewport: { width, height: 900 } });

      test("every control is a 44 × 44 target, focus reaches the acknowledgement, AA text, no overflow", async ({ page }) => {
        await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
        await server(page);
        await open(page);
        await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
        expect(await page.evaluate(() => matchMedia("(pointer: coarse)").matches)).toBe(true);
        await tile(page, "OpenAI").click();
        await page.getByLabel(/API key/i).fill("sk-o");
        const card = page.locator("section").filter({ has: page.getByTestId("ai-egress") });
        await expect(card).toBeVisible();

        // Tiles, buttons, segments and the acknowledgement label: both dimensions.
        const controls = card.locator('button:visible, [role="radio"]:visible, label:has(input[type="checkbox"])');
        const n = await controls.count();
        expect(n).toBeGreaterThanOrEqual(7); // 4 tiles, Test, Save connection, the acknowledgement
        for (let i = 0; i < n; i++) {
          const box = await controls.nth(i).boundingBox();
          expect(box, `control ${i}`).not.toBeNull();
          expect(box!.height, `control ${i} height`).toBeGreaterThanOrEqual(TOUCH_FLOOR);
          expect(box!.width, `control ${i} width`).toBeGreaterThanOrEqual(TOUCH_FLOOR);
        }

        // Keyboard focus reaches the acknowledgement and is visible.
        await page.getByLabel(/API key/i).focus();
        for (let i = 0; i < 10 && !(await ack(page).evaluate((n) => n === document.activeElement)); i++)
          await page.keyboard.press("Tab");
        await expect(ack(page)).toBeFocused();
        expect(await ack(page).evaluate((n) => getComputedStyle(n).outlineStyle)).not.toBe("none");

        // The warning's text meets AA (4.5:1 — it is small text).
        const warning = page.getByTestId("ai-egress");
        for (const part of [warning.locator("p").first(), warning.locator("p").nth(1), warning.locator("li").first(), warning.locator("label span")])
          expect(await contrast(part)).toBeGreaterThanOrEqual(4.5);

        // Nothing spills out of its card.
        const overflow = await card.evaluate((c) => {
          const r = c.getBoundingClientRect();
          return Array.from(c.querySelectorAll("*"))
            .map((e) => e.getBoundingClientRect())
            .filter((b) => b.width > 0 && (b.right > r.right + 0.5 || b.left < r.left - 0.5)).length;
        });
        expect(overflow).toBe(0);
        await page.screenshot({ path: `test-results/ai-provider-picker-${theme}-${width}.png`, fullPage: true });
      });
    });
  }
}
