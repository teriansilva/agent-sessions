// Settings → Appearance → API sessions (#1494 P3), in a real browser: a look chosen in Settings
// shows in the live preview, then paints a REAL StructuredPane and ChatPane log — and only the
// log: the composer stays chrome. It survives a reload, a partly-bad saved value keeps its good
// fields, a custom stack is refused with a reason, and long content never scrolls the page
// sideways at 360 or 390 px (preview and real panes). The defaults are held separately, against
// a baseline from main, by api-look-defaults.spec.ts.
import { expect, test, type Page } from "@playwright/test";
import { settingsPath } from "../src/routes/settingsTabs";
import { mockApiPanes, openChat, openStructured } from "./apiLookFixture";
import { TOUCH_FLOOR } from "./touchTarget";

const hexToRgb = (hex: string) => {
  const n = parseInt(hex.slice(1), 16);
  return `rgb(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255})`;
};
// Nord, dark variant (theme/apiLook.ts).
const NORD = { bg: "#2e3440", text: "#eceff4", you: "#88c0d0" };

const group = (page: Page, name: string) => page.getByRole("radiogroup", { name });
const log = (page: Page) => page.getByTestId("api-log");
const composerStyle = (page: Page) =>
  page.locator("textarea").first().evaluate((el) => {
    const cs = getComputedStyle(el);
    return { bg: cs.backgroundColor, color: cs.color, size: cs.fontSize, family: cs.fontFamily };
  });

async function chooseNordDotsSerif16(page: Page) {
  await page.goto(settingsPath("api-appearance"));
  await expect(page.getByRole("heading", { name: "API sessions", level: 2 })).toBeVisible();
  await group(page, "Palette").getByRole("radio", { name: /^Nord/ }).click();
  await group(page, "Background").getByRole("radio", { name: /^Dots/ }).click();
  await group(page, "Text font").getByRole("radio", { name: "Serif" }).click();
  await page.getByRole("button", { name: "Bigger conversation text" }).click();
  await page.getByRole("button", { name: "Bigger conversation text" }).click();
}

test("a look chosen in Settings shows in the preview, then paints real API panes — logs only", async ({
  page,
}) => {
  await mockApiPanes(page, { theme: "dark" });
  // The composer BEFORE: the look must not touch the pane's chrome.
  await openStructured(page);
  const composerBefore = await composerStyle(page);

  await chooseNordDotsSerif16(page);
  const preview = page.getByTestId("api-look-preview");
  await expect(preview).toContainText("Nord · Dots · Serif · 16 px");
  const previewLog = preview.locator("[data-api-bg]");
  await expect(previewLog).toHaveAttribute("data-api-bg", "dots");
  await expect(previewLog).toHaveCSS("background-color", hexToRgb(NORD.bg));

  for (const open of [openStructured, openChat]) {
    await open(page);
    await expect(log(page)).toHaveAttribute("data-api-bg", "dots");
    await expect(log(page)).toHaveCSS("background-color", hexToRgb(NORD.bg));
    await expect(log(page)).toHaveCSS("font-family", /Iowan Old Style/);
    // "you" takes the palette's colour; turn text scales 14 → 16 px.
    const you = log(page).locator("[class*='who']").first();
    await expect(you).toHaveCSS("color", hexToRgb(NORD.you));
    const txt = log(page).locator("[class*='txt']").first();
    await expect(txt).toHaveCSS("font-size", "16px");
    await expect(txt).toHaveCSS("color", hexToRgb(NORD.text));
  }
  await openStructured(page);
  expect(await composerStyle(page)).toEqual(composerBefore);

  // It is this device's choice: a reload keeps it.
  await page.reload();
  await expect(log(page)).toHaveAttribute("data-api-bg", "dots");
  await expect(log(page)).toHaveCSS("background-color", hexToRgb(NORD.bg));
});

test("Reset API sessions returns the log to the default path: no attribute, no inline style", async ({
  page,
}) => {
  await mockApiPanes(page, { theme: "light" });
  await chooseNordDotsSerif16(page);
  await page.getByRole("button", { name: "Reset API sessions" }).click();
  await expect(page.getByRole("button", { name: "Reset API sessions" })).toBeDisabled();
  await openChat(page);
  await expect(log(page)).not.toHaveAttribute("data-api-bg", /.*/);
  expect(await log(page).getAttribute("style")).toBeNull();
});

test("a partly-bad saved look keeps its good fields and drops only the bad one", async ({ page }) => {
  await mockApiPanes(page, { theme: "dark" });
  await page.addInitScript(() =>
    localStorage.setItem(
      "tr-apilook",
      JSON.stringify({ palette: "nord", background: "plaid", body_font: "serif", code_font: "app", text_size: 99 }),
    ),
  );
  await openStructured(page);
  await expect(log(page)).toHaveCSS("background-color", hexToRgb(NORD.bg)); // palette kept
  await expect(log(page)).not.toHaveAttribute("data-api-bg", /.*/); // bad background dropped
  await expect(log(page).locator("[class*='txt']").first()).toHaveCSS("font-size", "14px"); // bad size → 14
});

test("a custom stack is refused with a reason; a good one applies to code", async ({ page }) => {
  await mockApiPanes(page, { theme: "dark" });
  await page.goto(settingsPath("api-appearance"));
  await group(page, "Code font").getByRole("radio", { name: "Custom…" }).click();
  const field = page.getByLabel("Custom code font stack");
  await field.fill("x;}body{color:red");
  await field.press("Enter");
  await expect(page.getByRole("alert")).toContainText("Not a usable font stack");
  await field.fill('"Fira Code", monospace');
  await field.press("Enter");
  await expect(page.getByRole("alert")).toHaveCount(0);
  await openStructured(page);
  await expect(log(page).locator("pre").first()).toHaveCSS("font-family", /Fira Code/);
});

test("the palette group moves with the arrow keys and is one Tab stop", async ({ page }) => {
  await mockApiPanes(page, { theme: "dark" });
  await page.goto(settingsPath("api-appearance"));
  const radios = group(page, "Palette").getByRole("radio");
  await expect(radios.and(page.locator('[tabindex="0"]'))).toHaveCount(1);
  await radios.first().focus();
  await page.keyboard.press("ArrowRight");
  await expect(group(page, "Palette").getByRole("radio", { name: /^Graphite/ })).toHaveAttribute(
    "aria-checked",
    "true",
  );
});

for (const width of [360, 390]) {
  test(`long content never scrolls the page sideways at ${width}px — preview and real panes`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 800 });
    await mockApiPanes(page, { theme: "dark", long: true });
    await page.addInitScript(() =>
      localStorage.setItem(
        "tr-apilook",
        JSON.stringify({ palette: "sepia", background: "grid", body_font: "mono", code_font: "app", text_size: 18 }),
      ),
    );
    const sideways = () =>
      page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    await page.goto(settingsPath("api-appearance"));
    await expect(page.getByTestId("api-look-preview")).toBeVisible();
    expect(await sideways()).toBeLessThanOrEqual(0);
    for (const r of await page.getByRole("radiogroup").getByRole("radio").all()) {
      const b = (await r.boundingBox())!;
      expect(b.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    }
    for (const open of [openStructured, openChat]) {
      await open(page);
      expect(await sideways()).toBeLessThanOrEqual(0);
      // The wide line scrolls inside its own block.
      const pre = log(page).locator("pre, [class*='code']").first();
      const scrolls = await pre.evaluate((el) => el.scrollWidth > el.clientWidth || getComputedStyle(el).whiteSpace !== "pre");
      expect(scrolls).toBe(true);
    }
  });
}

// #1499 review: links were a fixed mix of the raw accent, so a CUSTOM accent could vanish —
// black on High contrast dark measured 2.02:1. The link is now derived from the live accent.
for (const [mode, accent] of [
  ["dark", "#000000"],
  ["light", "#ffffff"],
] as const) {
  test(`a custom ${accent} accent keeps API links at AA on High contrast ${mode}`, async ({ page }) => {
    await mockApiPanes(page, { theme: mode });
    await page.addInitScript(
      ([a]) => {
        localStorage.setItem("tr-accent", a);
        localStorage.setItem(
          "tr-apilook",
          JSON.stringify({ palette: "contrast", background: "none", body_font: "sans", code_font: "app", text_size: 14 }),
        );
      },
      [accent],
    );
    await openStructured(page);
    const ratio = await log(page).evaluate((logEl) => {
      const link = logEl.querySelector("a");
      if (!link) return -1;
      const rgb = (c: string) => (c.match(/[\d.]+/g) ?? []).slice(0, 3).map(Number);
      const lum = ([r, g, b]: number[]) =>
        [r, g, b]
          .map((v) => v / 255)
          .map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4))
          .reduce((s, v, i) => s + v * [0.2126, 0.7152, 0.0722][i], 0);
      // The link sits on the reply panel; measure against the nearest painted ground.
      let el: Element | null = link;
      let bg = "rgba(0, 0, 0, 0)";
      while (el && (bg === "rgba(0, 0, 0, 0)" || bg === "transparent")) {
        bg = getComputedStyle(el).backgroundColor;
        el = el.parentElement;
      }
      const [a, b] = [lum(rgb(getComputedStyle(link).color)), lum(rgb(bg))].sort((x, y) => y - x);
      return (a + 0.05) / (b + 0.05);
    });
    expect(ratio, "a link was found and measured").toBeGreaterThan(0);
    expect(ratio).toBeGreaterThanOrEqual(4.5);
  });
}
