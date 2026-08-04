import { expect, test } from "vitest";
import {
  coerceTheme,
  DEFAULT_THEME,
  isThemeId,
  THEME_IDS,
  THEME_LIST,
  THEMES,
  xtermTheme,
} from "./themes";

test("registry is dark/light and dark is the default (royal retired) (#211)", () => {
  expect([...THEME_IDS]).toEqual(["dark", "light"]);
  expect(DEFAULT_THEME).toBe("dark");
  expect(THEME_LIST.map((t) => t.id)).toEqual([...THEME_IDS]);
});

test("each theme is self-consistent with a usable terminal palette", () => {
  for (const id of THEME_IDS) {
    const t = THEMES[id];
    expect(t.id).toBe(id);
    expect(t.label.length).toBeGreaterThan(0);
    expect(t.description.length).toBeGreaterThan(0);
    for (const c of [
      t.terminal.background,
      t.terminal.foreground,
      t.terminal.cursor,
    ]) {
      expect(c).toMatch(/^#[0-9a-f]{6}$/i);
    }
    expect(t.terminal.fontSize).toBeGreaterThan(0);
    expect(t.terminal.fontFamily).toMatch(/monospace/);
  }
});

test("isThemeId narrows only known ids", () => {
  expect(isThemeId("dark")).toBe(true);
  expect(isThemeId("light")).toBe(true);
  expect(isThemeId("royal")).toBe(false); // retired
  expect(isThemeId("bogus")).toBe(false);
  expect(isThemeId(null)).toBe(false);
  expect(isThemeId(42)).toBe(false);
});

test("legacy `royal` migrates to dark (#211)", () => {
  expect(coerceTheme("royal")).toBe("dark");
});

test("coerceTheme accepts valid ids and falls back to the default otherwise", () => {
  expect(coerceTheme("light")).toBe("light");
  expect(coerceTheme("bogus")).toBe(DEFAULT_THEME);
  expect(coerceTheme(null)).toBe(DEFAULT_THEME);
  expect(coerceTheme(undefined)).toBe(DEFAULT_THEME);
});

test("xtermTheme maps a theme's terminal palette to the xterm ITheme subset", () => {
  for (const id of THEME_IDS) {
    const it = xtermTheme(id);
    const t = THEMES[id].terminal;
    expect(it).toEqual({
      background: t.background,
      foreground: t.foreground,
      cursor: t.cursor,
      selectionBackground: t.selectionBackground,
      ...t.ansi,
    });
  }
  // Dark (default) is the BattleLab near-black ground with the amber cursor.
  expect(xtermTheme("dark").background).toBe("#0d0e10");
  expect(xtermTheme("dark").cursor).toBe("#ffb000");
});

// The 16 ANSI slots xterm's ITheme accepts — all required on every theme (#473): a partial
// palette would silently fall back to xterm's dark-oriented default for the missing slots.
const ANSI_KEYS = [
  "black",
  "red",
  "green",
  "yellow",
  "blue",
  "magenta",
  "cyan",
  "white",
  "brightBlack",
  "brightRed",
  "brightGreen",
  "brightYellow",
  "brightBlue",
  "brightMagenta",
  "brightCyan",
  "brightWhite",
] as const;

test("every theme ships a complete 16-colour ANSI palette (#473)", () => {
  for (const id of THEME_IDS) {
    const ansi = THEMES[id].terminal.ansi;
    expect(Object.keys(ansi).sort()).toEqual([...ANSI_KEYS].sort());
    for (const k of ANSI_KEYS) expect(ansi[k]).toMatch(/^#[0-9a-f]{6}$/i);
  }
});

test("dark ANSI palette is exactly xterm's built-in default (Tango) — pixel-identical (#473)", () => {
  expect(THEMES.dark.terminal.ansi).toEqual({
    black: "#2e3436",
    red: "#cc0000",
    green: "#4e9a06",
    yellow: "#c4a000",
    blue: "#3465a4",
    magenta: "#75507b",
    cyan: "#06989a",
    white: "#d3d7cf",
    brightBlack: "#555753",
    brightRed: "#ef2929",
    brightGreen: "#8ae234",
    brightYellow: "#fce94f",
    brightBlue: "#729fcf",
    brightMagenta: "#ad7fa8",
    brightCyan: "#34e2e2",
    brightWhite: "#eeeeec",
  });
});

// WCAG relative-luminance contrast (same math as contrast.test.ts, which guards the chrome
// tokens in index.css; this guards the terminal palette in themes.ts).
function lum(hex: string): number {
  const n = parseInt(hex.slice(1), 16);
  const ch = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((v) => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2];
}
function ratio(a: string, b: string): number {
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

test("light ANSI palette holds WCAG AA (>=4.5:1) on the light terminal ground (#473)", () => {
  const t = THEMES.light.terminal;
  for (const k of ANSI_KEYS) {
    expect(
      ratio(t.ansi[k], t.background),
      `light ansi.${k} on ${t.background}`,
    ).toBeGreaterThanOrEqual(4.5);
  }
});
