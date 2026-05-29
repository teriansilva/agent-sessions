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
    for (const c of [t.terminal.background, t.terminal.foreground, t.terminal.cursor]) {
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
    });
  }
  // Dark (default) is the BattleLab near-black ground with the amber cursor.
  expect(xtermTheme("dark").background).toBe("#0d0e10");
  expect(xtermTheme("dark").cursor).toBe("#ffb000");
});
