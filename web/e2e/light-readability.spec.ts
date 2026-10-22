// Light-theme readability pass (#473), proven in a real browser against the isolated bench
// (mocked /api + in-page fake terminal server — no backend). Three fixes, each pinned here:
//  1. the terminal's 16-colour ANSI palette is theme-driven (agent output was washed-out on light),
//  2. the surface behind the xterm rows follows --term-bg (a hardcoded near-black painted a dark
//     band over the compose bar on light),
//  3. the topbar/classbar read as distinct surfaces on light (firmer edge, lifted telemetry).
// Dark is pinned unchanged alongside each light assertion. Runs on the desktop AND mobile projects.
import { expect, test, type Page } from "@playwright/test";
import { throttleIfAsked } from "./browserTiming";
import {
  pushOutput,
  setupBench,
  expectTerminalShows,
} from "./terminal/harness";

const SESSIONS = [{ engine: "claude", uuid: "aaa", title: "Session Alpha" }];

const hexToRgb = (hex: string) => {
  const n = parseInt(hex.slice(1), 16);
  return `rgb(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255})`;
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
const ANSI_SAMPLE =
  "\r\n\x1b[90mDIM90\x1b[0m \x1b[32mGRN32\x1b[0m \x1b[33mYLW33\x1b[0m \x1b[36mCYN36\x1b[0m\r\n";

// xterm replaces row spans while painting. A locator assertion re-resolves a detached span.
const ansiSpan = (page: Page, marker: string) =>
  page.locator(".xterm-rows span", { hasText: marker }).first();

test.beforeEach(async ({ page }) => {
  await throttleIfAsked(page);
});

/** The NIGHTJAR v2 bars (#1531, docs/design.md §5), in either theme: opaque --bg-1 surfaces, the
 *  top bar closed by the lit --hud rail and the footer opened by the --hud-dim edge. They read as
 *  distinct surfaces by that edge, not by translucency over the work below. The expected colours
 *  are resolved from the live tokens, because --hud follows the operator's accent. */
async function expectNightjarBars(page: Page) {
  const resolve = (v: string) =>
    page.evaluate((value) => {
      const probe = document.createElement("i");
      probe.style.color = value;
      document.body.append(probe);
      const out = getComputedStyle(probe).color;
      probe.remove();
      return out;
    }, v);
  const topbar = page.locator(".hud-topbar");
  const classbar = page.locator(".hud-classbar");
  await expect(topbar).toHaveCSS("border-bottom-color", await resolve("var(--hud)"));
  await expect(classbar).toHaveCSS("border-top-color", await resolve("var(--hud-dim)"));
  for (const bar of [topbar, classbar]) {
    // Opaque: the --bg-1 colour itself (an `rgb()`, no alpha), never a translucent mix.
    const bg = await bar.evaluate((el) => getComputedStyle(el).backgroundColor);
    expect(bg).toBe(await resolve("var(--bg-1)"));
    expect(bg.startsWith("rgb(")).toBe(true);
  }
}

async function openBench(page: Page, theme: "dark" | "light") {
  // This fixture measures colors; the generic resize repaint would erase its injected sample.
  // Dedicated terminal-bench tests retain the default wipe behavior and pin resize/history.
  await setupBench(page, { sessions: SESSIONS, wipeOnResizeChange: false });
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
    await expect(ansiSpan(page, "DIM90")).toHaveCSS("color", hexToRgb("#585f6a"));
    await expect(ansiSpan(page, "GRN32")).toHaveCSS("color", hexToRgb("#136f2c"));
    await expect(ansiSpan(page, "YLW33")).toHaveCSS("color", hexToRgb("#7d5600"));
    await expect(ansiSpan(page, "CYN36")).toHaveCSS("color", hexToRgb("#0e6c7c"));
  });

  test("topbar + classbar read as distinct surfaces", async ({
    page,
  }) => {
    await openBench(page, "light");
    await expectNightjarBars(page);
    // Light tags are the darker --text-2 (#41454d), readable on the light --bg-1.
    await expect(page.locator(".hud-classbar .hud-tag").first()).toHaveCSS(
      "color",
      hexToRgb("#41454d"),
    );
  });
});

test.describe("dark theme stays as-is (#473 sanity)", () => {
  test("terminal surface + ANSI palette are unchanged; the chrome is the v2 bars", async ({
    page,
  }) => {
    await openBench(page, "dark");
    // Surface = the dark xterm canvas bg (#0d0e10; was the visually identical #0e0e0e).
    expect(await surfaceBehindTerminal(page)).toBe(hexToRgb("#0d0e10"));
    // ANSI = xterm's built-in Tango defaults, now pinned explicitly per theme.
    await pushOutput(page, ANSI_SAMPLE);
    await expectTerminalShows(page, "CYN36");
    await expect(ansiSpan(page, "DIM90")).toHaveCSS("color", hexToRgb("#555753"));
    await expect(ansiSpan(page, "GRN32")).toHaveCSS("color", hexToRgb("#4e9a06"));
    await expect(ansiSpan(page, "YLW33")).toHaveCSS("color", hexToRgb("#c4a000"));
    await expect(ansiSpan(page, "CYN36")).toHaveCSS("color", hexToRgb("#06989a"));
    // The chrome is the NIGHTJAR v2 bars (docs/design.md §5), the same contract as light.
    await expectNightjarBars(page);
    await expect(page.locator(".hud-classbar .hud-tag").first()).toHaveCSS(
      "color",
      hexToRgb("#a7abb3"),
    );
  });
});
