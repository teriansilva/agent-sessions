// Settings → Appearance in NIGHTJAR (#1574), in a real browser.
//
// The MiniApp previews read the canonical tokens through `[data-theme-scope]` (tokens.css): no
// preview palette. The contract (#1574 Architecture): for page theme {dark, light} × preview mode
// {dark, light} × accent {default, custom}, a preview resolves --bg-1, --text-1, --accent,
// --accent-text and --hud to exactly what <html> resolves with that mode and accent in force.
// Resolved, not the raw token text: a probe paints `color: var(--token)` and the browser's
// computed colour is compared, so color-mix() and inherited var() chains count as what they paint.
import { expect, test, type Page } from "@playwright/test";
import { settingsPath } from "../src/routes/settingsTabs";
import { DEFAULT_ACCENT } from "../src/theme/accent";
import { animationsSettled, expectIdleBudget, storeMotionChoice } from "./nightjarShell";
import { TOUCH_FLOOR } from "./touchTarget";

const TOKENS = ["--bg-1", "--text-1", "--accent", "--accent-text", "--hud"] as const;
const CUSTOM = "#3aa0ff";

async function mockSettings(page: Page, accent: string) {
  // The appearance-interface spec's mock (it renders this page), plus the accent pref.
  await page.route("**/api/config", (r) =>
    r.fulfill({ json: { csrf: "x", new_session_engines: [], terminal_backend: "ws", auth_mode: "none" } }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: { accent } }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } } }),
  );
}

async function open(page: Page, theme: "dark" | "light", accent: string) {
  await page.addInitScript(
    ([t, a]) => {
      localStorage.setItem("tr-theme", t);
      localStorage.setItem("tr-accent", a);
    },
    [theme, accent],
  );
  await mockSettings(page, accent);
  await page.goto(settingsPath("appearance"));
  await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
  await expect(page.getByTestId("mini-app").first()).toBeVisible();
}

/** Each token resolved inside `el` (a probe child paints `color: var(token)`). */
const resolveIn = (page: Page, selector: string | null) =>
  page.evaluate(
    ([sel, tokens]) => {
      const hosts = sel ? [...document.querySelectorAll<HTMLElement>(sel)] : [document.body];
      return hosts.map((host) => {
        const out: Record<string, string> = { scope: host.dataset.themeScope ?? "" };
        for (const t of tokens) {
          const probe = document.createElement("span");
          probe.style.color = `var(${t})`;
          host.appendChild(probe);
          out[t] = getComputedStyle(probe).color;
          probe.remove();
        }
        return out;
      });
    },
    [selector, TOKENS] as const,
  );

for (const accent of [DEFAULT_ACCENT, CUSTOM]) {
  test(`a mode preview resolves the tokens <html> does in that mode (accent ${accent}) (#1574)`, async ({
    browser,
  }) => {
    // What <html> resolves with each mode (and this accent) in force: the reference.
    const want: Record<string, Record<string, string>> = {};
    for (const mode of ["dark", "light"] as const) {
      const ctx = await browser.newContext();
      const page = await ctx.newPage();
      await open(page, mode, accent);
      want[mode] = (await resolveIn(page, null))[0];
      await ctx.close();
    }
    expect(want.dark["--bg-1"]).not.toBe(want.light["--bg-1"]);
    // Every preview, on a page of either theme, paints its OWN mode's values.
    for (const pageTheme of ["dark", "light"] as const) {
      const ctx = await browser.newContext();
      const page = await ctx.newPage();
      await open(page, pageTheme, accent);
      const previews = await resolveIn(page, "[data-testid='mini-app']");
      // Dark card, light card, and Match device's pair.
      expect(previews.map((p) => p.scope).sort()).toEqual(["dark", "dark", "light", "light"]);
      for (const p of previews)
        for (const t of TOKENS)
          expect(p[t], `${pageTheme} page, ${p.scope} preview, ${t}`).toBe(want[p.scope][t]);
      await ctx.close();
    }
  });
}

test("the Motion control is a radio group of cards, one per mode, each with its stage (#1574)", async ({
  page,
}) => {
  await storeMotionChoice(page, "full");
  await open(page, "dark", DEFAULT_ACCENT);
  const group = page.getByRole("radiogroup", { name: "Motion" });
  await expect(group.getByRole("radio")).toHaveText([/Match device/, /Full/, /Reduced/, /Off/]);
  await expect(group.getByRole("radio", { name: /Full/ })).toHaveAttribute("aria-checked", "true");
  await group.getByRole("radio", { name: /Off/ }).click();
  await expect(page.locator("html")).toHaveAttribute("data-motion", "off");
  await expect(page.getByTestId("motion-desc")).toContainText("Nothing moves");
  await animationsSettled(page);
  for (const radio of await group.getByRole("radio").all()) {
    const box = (await radio.boundingBox())!;
    expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
});

test("Appearance → Interface idles within budget in Full, its Motion stage running (#1574)", async ({
  page,
  browserName,
}, info) => {
  test.skip(browserName !== "chromium", "CDP metrics");
  test.skip(info.project.name !== "desktop", "one measurement is enough");
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await storeMotionChoice(page, "full");
  await open(page, "dark", DEFAULT_ACCENT);
  await page.waitForTimeout(1500);
  await expectIdleBudget(page, "appearance interface", info);
});

// The endpoint's secret never renders (#1574 Evidence): a stored key is a mask, a typed key is a
// password field, and neither a refused check nor the page text ever echoes it. The value is a
// fake fixture shaped like a real key, so a leak would be visible in any capture.
test("a real-looking API key never renders on Endpoint & model — stored, typed or refused (#1574)", async ({
  page,
}) => {
  const KEY = "sk-live-FAKE-fixture-9f8e7d6c5b4a39281706";
  await mockSettings(page, DEFAULT_ACCENT);
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        ai_review: { enabled: true, base_url: "https://api.example.com/v1", model: "m", api_key_set: true, configured: true },
      },
    }),
  );
  await page.route("**/api/ai-review/endpoint/test", (r) =>
    r.fulfill({ status: 502, json: { detail: "The endpoint refused the key (HTTP 401)." } }),
  );
  await page.goto(settingsPath("ai-endpoint"));
  await expect(page.getByText(/stored/).first()).toBeVisible();
  await page.getByRole("button", { name: "Replace key" }).click();
  const field = page.locator("input[type='password']");
  await field.fill(KEY);
  await expect(field).toHaveAttribute("type", "password");
  await page.getByRole("button", { name: "Test", exact: true }).click();
  await expect(page.getByText("refused the key")).toBeVisible();
  // Nowhere in the rendered text, and in no attribute but the password field's own value.
  expect(await page.locator("body").innerText()).not.toContain(KEY);
  const leaks = await page.evaluate(
    (key) =>
      [...document.querySelectorAll("*")].flatMap((el) =>
        [...el.attributes]
          // The field the key was typed into holds it (React mirrors a controlled value).
          .filter((a) => !(el instanceof HTMLInputElement && el.type === "password" && a.name === "value"))
          .filter((a) => a.value.includes(key))
          .map((a) => `${el.tagName}[${a.name}]`),
      ),
    KEY,
  );
  expect(leaks).toEqual([]);
});
