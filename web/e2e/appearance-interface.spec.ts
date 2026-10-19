// Settings → Appearance → Interface (#1494), proven in a real browser: the mode picker with
// Match device, the 16-accent grid, the hex field that fits its value, arrow-key radio groups,
// and the one thing jsdom cannot show at all — a live terminal repainting when the OS flips its
// colour scheme under Match device. The network is mocked; the terminal is the in-page bench.
import { expect, test, type Page } from "@playwright/test";
import { settingsPath } from "../src/routes/settingsTabs";
import {
  expectTerminalShows,
  pushOutput,
  setupBench,
} from "./terminal/harness";

const hexToRgb = (hex: string) => {
  const n = parseInt(hex.slice(1), 16);
  return `rgb(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255})`;
};

async function mockSettings(page: Page) {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
      },
    }),
  );
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [] } }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [] } }),
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
}

const html = (page: Page) => page.locator("html");

test("/settings/appearance is Interface, with the mode cards and the accent grid", async ({
  page,
}) => {
  await mockSettings(page);
  await page.goto(settingsPath("appearance"));
  await expect(page.getByRole("heading", { name: "Interface", level: 2 })).toBeVisible();
  const modes = page.getByRole("radiogroup", { name: "Mode" });
  await expect(modes.getByRole("radio")).toHaveCount(3);
  await expect(page.getByRole("radiogroup", { name: "Accent" }).getByRole("radio")).toHaveCount(16);
});

test("Match device follows the OS live, and data-theme only ever says dark or light", async ({
  page,
}) => {
  await mockSettings(page);
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto(settingsPath("appearance"));
  await expect(html(page)).toHaveAttribute("data-theme", "dark"); // the default choice

  await page.getByRole("radio", { name: /Match device/ }).click();
  await expect(html(page)).toHaveAttribute("data-theme", "light");
  expect(await page.evaluate(() => localStorage.getItem("tr-theme"))).toBe("system");

  await page.emulateMedia({ colorScheme: "dark" });
  await expect(html(page)).toHaveAttribute("data-theme", "dark");
  await page.emulateMedia({ colorScheme: "light" });
  await expect(html(page)).toHaveAttribute("data-theme", "light");

  // The boot script resolves it pre-paint too: a reload on a light OS starts light.
  await page.reload();
  await expect(html(page)).toHaveAttribute("data-theme", "light");
});

test("an OS flip under Match device repaints an OPEN terminal and its --term-bg, no reload", async ({
  page,
}) => {
  // The bench measures colours; the generic resize repaint would wipe the injected sample.
  await setupBench(page, {
    sessions: [{ engine: "claude", uuid: "aaa", title: "Session Alpha" }],
    wipeOnResizeChange: false,
  });
  await page.addInitScript(() => localStorage.setItem("tr-theme", "system"));
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/s/claude/aaa");
  await expectTerminalShows(page, "HIST claudeaaa END");
  await pushOutput(page, "\r\n\x1b[32mGRN32\x1b[0m\r\n");
  await expectTerminalShows(page, "GRN32");

  const green = page.locator(".xterm-rows span", { hasText: "GRN32" }).first();
  const viewport = page.locator(".xterm-viewport").first();
  const termBg = () =>
    page.evaluate(() =>
      getComputedStyle(document.documentElement).getPropertyValue("--term-bg").trim(),
    );

  // Dark: Tango green on the dark canvas (themes.ts DARK_ANSI / THEMES.dark.terminal).
  await expect(green).toHaveCSS("color", hexToRgb("#4e9a06"));
  await expect(viewport).toHaveCSS("background-color", hexToRgb("#0d0e10"));
  expect(await termBg()).toBe("#0d0e10");

  await page.emulateMedia({ colorScheme: "light" });

  // Light, on the SAME mounted terminal: LIGHT_ANSI green on the light canvas.
  await expect(html(page)).toHaveAttribute("data-theme", "light");
  await expect(green).toHaveCSS("color", hexToRgb("#136f2c"));
  await expect(viewport).toHaveCSS("background-color", hexToRgb("#f2f3f5"));
  await expect.poll(termBg).toBe("#f2f3f5");
});

test("the mode and accent groups move with the arrow keys", async ({ page }) => {
  await mockSettings(page);
  await page.goto(settingsPath("appearance"));

  const dark = page.getByRole("radio", { name: /^Dark/ });
  await dark.focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("radio", { name: /^Light/ })).toBeFocused();
  await expect(page.getByRole("radio", { name: /^Light/ })).toHaveAttribute("aria-checked", "true");
  await expect(html(page)).toHaveAttribute("data-theme", "light");

  // Scoped to the group with exact names: "Ice" is a substring of "Match device".
  const swatch = (name: string) =>
    page.getByRole("radiogroup", { name: "Accent" }).getByRole("radio", { name, exact: true });
  await swatch("Amber").focus();
  await page.keyboard.press("ArrowRight");
  await expect(swatch("Orange")).toHaveAttribute("aria-checked", "true");
  await expect(page.getByTestId("accent-name")).toHaveText("Orange");
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("ArrowLeft"); // wraps from Amber to the last swatch
  await expect(swatch("Ice")).toHaveAttribute("aria-checked", "true");
});

test("the hex field shows its whole value (the old box clipped the last digit)", async ({ page }) => {
  await mockSettings(page);
  await page.goto(settingsPath("appearance"));
  const field = page.getByLabel("Accent hex value");
  await expect(field).toHaveValue("#ffb000");
  const fits = await field.evaluate((el) => (el as HTMLInputElement).scrollWidth <= (el as HTMLInputElement).clientWidth);
  expect(fits).toBe(true);
  const box = await field.boundingBox();
  expect(box!.height).toBeGreaterThanOrEqual(44);
});

test("every Interface target is ≥44px and nothing scrolls sideways at 360px", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 800 });
  await mockSettings(page);
  await page.goto(settingsPath("appearance"));
  await expect(page.getByRole("heading", { name: "Interface", level: 2 })).toBeVisible();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
  for (const radio of await page
    .getByRole("radiogroup", { name: /^(Mode|Accent)$/ })
    .getByRole("radio")
    .all()) {
    const b = (await radio.boundingBox())!;
    expect(b.height).toBeGreaterThanOrEqual(44);
    expect(b.width).toBeGreaterThanOrEqual(44);
  }
});
