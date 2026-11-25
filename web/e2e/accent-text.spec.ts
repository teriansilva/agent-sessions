// Accent-coloured TEXT stays readable for every accent (#1494). A pale preset (Ice) is ~1.1:1 as text
// on the light ground, and Signal Red is ~3:1 on the dark one, so no text may paint the raw
// --accent: it paints --accent-text, which accentTextFor() keeps at AA. This walks every visible
// text-bearing element on surfaces that use accent text (the new-session landing, Ask, Settings)
// and fails on any whose computed colour IS the raw accent. It also requires that the safe colour
// is actually in use, so it cannot pass by finding nothing.
import { expect, test, type Page } from "@playwright/test";
import { accentTextFor } from "../src/theme/accent";
import { settingsPath } from "../src/routes/settingsTabs";

const hexToRgb = (hex: string) => {
  const n = parseInt(hex.slice(1), 16);
  return `rgb(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255})`;
};

async function mock(page: Page) {
  await page.route("**/api/**", (r) => r.fulfill({ status: 404, json: { detail: "not mocked" } }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: { csrf: "x", new_session_engines: [], terminal_backend: "ws", auth_mode: "none" },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } } }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
}

/** Colours of every visible element that directly holds text. */
const textColours = (page: Page) =>
  page.evaluate(() =>
    Array.from(document.querySelectorAll<HTMLElement>("body *"))
      .filter((el) => {
        const own = Array.from(el.childNodes).some((n) => n.nodeType === 3 && n.textContent!.trim());
        const r = el.getBoundingClientRect();
        return own && r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== "hidden";
      })
      .map((el) => ({ colour: getComputedStyle(el).color, text: el.textContent!.trim().slice(0, 40) })),
  );

for (const [mode, accent, label] of [
  ["light", "#dbe4ee", "Ice"],
  ["dark", "#c02020", "Signal Red"],
] as const) {
  test(`${label} on ${mode}: no visible text paints the raw accent; the safe colour is in use`, async ({
    page,
  }) => {
    await mock(page);
    await page.addInitScript(
      ([m, a]) => {
        localStorage.setItem("tr-theme", m);
        localStorage.setItem("tr-accent", a);
      },
      [mode, accent],
    );
    const safe = hexToRgb(accentTextFor(accent, mode));
    expect(safe).not.toBe(hexToRgb(accent)); // both presets FAIL raw in their mode
    let safeSeen = 0;
    for (const path of ["/", "/ask", settingsPath("appearance")]) {
      await page.goto(path);
      await expect(page.locator("#root *").first()).toBeVisible();
      await page.waitForTimeout(500);
      const colours = await textColours(page);
      const raw = colours.filter((c) => c.colour === hexToRgb(accent));
      expect(raw, `${path}: text painting the raw ${label}`).toEqual([]);
      safeSeen += colours.filter((c) => c.colour === safe).length;
    }
    expect(safeSeen, "accent-coloured text was found at all").toBeGreaterThan(0);
  });
}
