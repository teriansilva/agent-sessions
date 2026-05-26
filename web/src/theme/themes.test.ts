import { expect, test } from "vitest";
import {
  coerceTheme,
  DEFAULT_THEME,
  isThemeId,
  THEME_IDS,
  THEME_LIST,
  THEMES,
} from "./themes";

test("registry has royal/dark/light and royal is the default", () => {
  expect([...THEME_IDS]).toEqual(["royal", "dark", "light"]);
  expect(DEFAULT_THEME).toBe("royal");
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
  expect(isThemeId("royal")).toBe(true);
  expect(isThemeId("bogus")).toBe(false);
  expect(isThemeId(null)).toBe(false);
  expect(isThemeId(42)).toBe(false);
});

test("coerceTheme accepts valid ids and falls back to the default otherwise", () => {
  expect(coerceTheme("light")).toBe("light");
  expect(coerceTheme("bogus")).toBe(DEFAULT_THEME);
  expect(coerceTheme(null)).toBe(DEFAULT_THEME);
  expect(coerceTheme(undefined)).toBe(DEFAULT_THEME);
});
