// Settings → Appearance → Terminal sessions → Colour scheme (#1494 P2), in a real browser: a
// chosen scheme paints the live xterm AND the --term-bg ground around it, survives a reload,
// follows the mode when it has a light variant, and a malformed saved value paints BattleLab.
// The network is mocked; the terminal is the in-page bench.
import { expect, test, type Page } from "@playwright/test";
import { settingsPath } from "../src/routes/settingsTabs";
import { expectTerminalShows, pushOutput, setupBench } from "./terminal/harness";
import { TOUCH_FLOOR } from "./touchTarget";

const hexToRgb = (hex: string) => {
  const n = parseInt(hex.slice(1), 16);
  return `rgb(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255})`;
};

const SESSIONS = [{ engine: "claude", uuid: "aaa", title: "Session Alpha" }];

async function openTerminal(page: Page) {
  await page.goto("/s/claude/aaa");
  await expectTerminalShows(page, "HIST claudeaaa END");
  await pushOutput(page, "\r\n\x1b[32mGRN32\x1b[0m\r\n");
  await expectTerminalShows(page, "GRN32");
}

const green = (page: Page) => page.locator(".xterm-rows span", { hasText: "GRN32" }).first();
const viewport = (page: Page) => page.locator(".xterm-viewport").first();
const termBg = (page: Page) =>
  page.evaluate(() =>
    getComputedStyle(document.documentElement).getPropertyValue("--term-bg").trim(),
  );

test.beforeEach(async ({ page }) => {
  await setupBench(page, { sessions: SESSIONS, wipeOnResizeChange: false });
});

test("picking a scheme in Settings paints the terminal and --term-bg, and survives a reload", async ({
  page,
}) => {
  await page.goto(settingsPath("terminal-appearance"));
  const group = page.getByRole("radiogroup", { name: "Colour scheme" });
  await expect(group.getByRole("radio")).toHaveCount(9);
  await expect(group.getByRole("radio", { name: /^BattleLab/ })).toHaveAttribute("aria-checked", "true");
  await group.getByRole("radio", { name: /^Dracula/ }).click();
  expect(await page.evaluate(() => localStorage.getItem("tr-termscheme"))).toBe("dracula");

  await openTerminal(page); // a full navigation: the choice came back from the device cache
  await expect(viewport(page)).toHaveCSS("background-color", hexToRgb("#282a36"));
  await expect(green(page)).toHaveCSS("color", hexToRgb("#50fa7b"));
  expect(await termBg(page)).toBe("#282a36");
});

test("a follows-mode scheme switches variant with the OS under Match device; dark-only stays dark", async ({
  page,
}) => {
  await page.addInitScript(() => {
    localStorage.setItem("tr-theme", "system");
    // Seed once: an init script re-runs on every navigation, and the Nord step below must stick.
    if (!localStorage.getItem("tr-termscheme")) localStorage.setItem("tr-termscheme", "solarized");
  });
  await page.emulateMedia({ colorScheme: "dark" });
  await openTerminal(page);
  await expect(viewport(page)).toHaveCSS("background-color", hexToRgb("#002b36"));
  expect(await termBg(page)).toBe("#002b36");

  await page.emulateMedia({ colorScheme: "light" });
  await expect(viewport(page)).toHaveCSS("background-color", hexToRgb("#fdf6e3"));
  await expect.poll(() => termBg(page)).toBe("#fdf6e3");

  // Nord has no light variant: on the same light OS it stays Nord dark.
  await page.evaluate(() => localStorage.setItem("tr-termscheme", "nord"));
  await openTerminal(page);
  await expect(viewport(page)).toHaveCSS("background-color", hexToRgb("#2e3440"));
});

test("a malformed saved scheme paints BattleLab, and the default leaves --term-bg to tokens.css", async ({
  page,
}) => {
  await page.addInitScript(() => localStorage.setItem("tr-termscheme", "{garbage"));
  await openTerminal(page);
  await expect(viewport(page)).toHaveCSS("background-color", hexToRgb("#0d0e10"));
  await expect(green(page)).toHaveCSS("color", hexToRgb("#4e9a06"));
  const inline = await page.evaluate(() =>
    document.documentElement.style.getPropertyValue("--term-bg"),
  );
  expect(inline).toBe("");
});

test("the measured low-contrast note shows where the policy says, and arrows move the group", async ({
  page,
}) => {
  await page.goto(settingsPath("terminal-appearance"));
  const group = page.getByRole("radiogroup", { name: "Colour scheme" });
  await expect(group.getByRole("radio", { name: /^Gruvbox/ })).toContainText("Some colours low contrast");
  await expect(group.getByRole("radio", { name: /^Dracula/ })).not.toContainText("low contrast");

  await group.getByRole("radio", { name: /^BattleLab/ }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(group.getByRole("radio", { name: /^Solarized/ })).toHaveAttribute("aria-checked", "true");
  await expect(group.getByRole("radio", { name: /^Solarized/ })).toBeFocused();
});

test("at 360px the scheme cards fit, nothing scrolls sideways, every card is ≥44px", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 800 });
  await page.goto(settingsPath("terminal-appearance"));
  const radios = page.getByRole("radiogroup", { name: "Colour scheme" }).getByRole("radio");
  await expect(radios).toHaveCount(9);
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
  for (const r of await radios.all()) {
    const b = (await r.boundingBox())!;
    expect(b.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    expect(b.width).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
});
