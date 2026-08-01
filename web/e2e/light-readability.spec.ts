// Light-theme readability pass (#473), proven in a real browser against the isolated bench
// (mocked /api + in-page fake terminal server — no backend). Three fixes, each pinned here:
//  1. the terminal's 16-colour ANSI palette is theme-driven (agent output was washed-out on light),
//  2. the surface behind the xterm rows follows --term-bg (a hardcoded near-black painted a dark
//     band over the compose bar on light),
//  3. the topbar/classbar read as distinct surfaces on light (firmer edge, lifted telemetry).
// Dark is pinned unchanged alongside each light assertion. Runs on the desktop AND mobile projects.
import { expect, test, type Page } from "@playwright/test";
import { pushOutput, setupBench, expectTerminalShows } from "./terminal/harness";

const SESSIONS = [{ engine: "claude", uuid: "aaa", title: "Session Alpha" }];

const hexToRgb = (hex: string) => {
  const n = parseInt(hex.slice(1), 16);
  return `rgb(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255})`;
};

/** Trailing alpha of a computed colour — works for both `rgba(r, g, b, a)` and
 *  `color(srgb r g b / a)` serializations of a translucent color-mix. */
const alphaOf = (color: string) => {
  const m = color.match(/([\d.]+)\)$/);
  return m ? Number(m[1]) : NaN;
};

/** Background of the nearest painted ancestor of the xterm rows — the surface that shows
 *  through in the slack FitAddon leaves below the last row (the #473 "dark band"). */
const surfaceBehindTerminal = (page: Page) =>
  page.evaluate(() => {
    let el = document.querySelector(".xterm")?.parentElement ?? null;
    while (el) {
      const bg = getComputedStyle(el).backgroundColor;
      if (bg && bg !== "rgba(0, 0, 0, 0)" && bg !== "transparent") return bg;
      el = el.parentElement;
    }
    return null;
  });

// Colourised the way agent CLIs actually colourise: bright-black secondary text, green ✓,
// yellow SHAs, cyan file paths (SGR 90/32/33/36 → ANSI slots 8/2/3/6).
const ANSI_SAMPLE = "\r\n\x1b[90mDIM90\x1b[0m \x1b[32mGRN32\x1b[0m \x1b[33mYLW33\x1b[0m \x1b[36mCYN36\x1b[0m\r\n";

const ansiColorOf = (page: Page, marker: string) =>
  page.locator(".xterm-rows span", { hasText: marker }).first().evaluate((el) => getComputedStyle(el).color);

async function openBench(page: Page, theme: "dark" | "light") {
  await setupBench(page, { sessions: SESSIONS });
  await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
  await page.goto("/s/claude/aaa");
  await expectTerminalShows(page, "HIST claudeaaa END");
}

test.describe("light theme (#473)", () => {
  test("no dark surface behind the terminal rows", async ({ page }) => {
    await openBench(page, "light");
    // --term-bg light = the light xterm canvas bg (#f2f3f5) — pre-fix this was #0e0e0e.
    expect(await surfaceBehindTerminal(page)).toBe(hexToRgb("#f2f3f5"));
  });

  test("agent-style ANSI output renders dark-on-light", async ({ page }) => {
    await openBench(page, "light");
    await pushOutput(page, ANSI_SAMPLE);
    await expectTerminalShows(page, "CYN36");
    // LIGHT_ANSI (themes.ts) — pre-fix these rendered xterm's dark-oriented Tango defaults.
    expect(await ansiColorOf(page, "DIM90")).toBe(hexToRgb("#585f6a"));
    expect(await ansiColorOf(page, "GRN32")).toBe(hexToRgb("#136f2c"));
    expect(await ansiColorOf(page, "YLW33")).toBe(hexToRgb("#7d5600"));
    expect(await ansiColorOf(page, "CYN36")).toBe(hexToRgb("#0e6c7c"));
  });

  test("topbar + classbar read as distinct surfaces", async ({ page }, testInfo) => {
    await openBench(page, "light");
    const topbar = page.locator(".hud-topbar");
    const classbar = page.locator(".hud-classbar");
    // The separating edge is the opaque --border (light #c9ccd2), not the 12% --line hairline.
    await expect(topbar).toHaveCSS("border-bottom-color", hexToRgb("#c9ccd2"));
    await expect(classbar).toHaveCSS("border-top-color", hexToRgb("#c9ccd2"));
    // Telemetry tags lift one step: --text-3 → --text-2 (#41454d).
    await expect(classbar.locator(".hud-tag").last()).toHaveCSS("color", hexToRgb("#41454d"));
    // Chrome fill: desktop rises 55% → 92%; mobile keeps the #228 96% degrade (the light
    // override is desktop-scoped so it must NOT beat the mobile rule's near-opaque fill).
    const alpha = alphaOf(await topbar.evaluate((el) => getComputedStyle(el).backgroundColor));
    expect(alpha).toBeCloseTo(testInfo.project.name === "mobile" ? 0.96 : 0.92, 2);
  });
});

test.describe("dark theme stays as-is (#473 sanity)", () => {
  test("terminal surface + ANSI palette + chrome are unchanged", async ({ page }, testInfo) => {
    await openBench(page, "dark");
    // Surface = the dark xterm canvas bg (#0d0e10; was the visually identical #0e0e0e).
    expect(await surfaceBehindTerminal(page)).toBe(hexToRgb("#0d0e10"));
    // ANSI = xterm's built-in Tango defaults, now pinned explicitly per theme.
    await pushOutput(page, ANSI_SAMPLE);
    await expectTerminalShows(page, "CYN36");
    expect(await ansiColorOf(page, "DIM90")).toBe(hexToRgb("#555753"));
    expect(await ansiColorOf(page, "GRN32")).toBe(hexToRgb("#4e9a06"));
    expect(await ansiColorOf(page, "YLW33")).toBe(hexToRgb("#c4a000"));
    expect(await ansiColorOf(page, "CYN36")).toBe(hexToRgb("#06989a"));
    // Chrome keeps the dark frosted look: --line edge, --text-3 tags, 55%/96% fill.
    const topbar = page.locator(".hud-topbar");
    await expect(topbar).toHaveCSS("border-bottom-color", "rgba(255, 255, 255, 0.08)");
    await expect(page.locator(".hud-classbar .hud-tag").last()).toHaveCSS("color", hexToRgb("#6f747d"));
    const alpha = alphaOf(await topbar.evaluate((el) => getComputedStyle(el).backgroundColor));
    expect(alpha).toBeCloseTo(testInfo.project.name === "mobile" ? 0.96 : 0.55, 2);
  });
});
