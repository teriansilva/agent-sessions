import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, test } from "vitest";
import {
  coerceTermScheme,
  contrast,
  DEFAULT_TERM_SCHEME,
  lowContrast,
  resolveTermScheme,
  TERM_SCHEME_IDS,
  TERM_SCHEMES,
  xtermThemeFor,
} from "./termSchemes";
import { THEMES, xtermTheme } from "./themes";

// The SAME fixture tests/test_prefs.py reads (#1494), loaded the way termFont.test.ts loads its own.
const IDS = JSON.parse(
  readFileSync(resolve(process.cwd(), "../tests/fixtures/appearance_ids.json"), "utf8"),
) as { term_schemes: { default: string; ids: string[] } };

const HEX = /^#[0-9a-f]{6}$/;
const variants = TERM_SCHEMES.flatMap((s) => [
  { name: `${s.id}/dark`, t: s.dark },
  ...(s.light ? [{ name: `${s.id}/light`, t: s.light }] : []),
]);

describe("terminal colour schemes (#1494 P2)", () => {
  test("the ids are the shared fixture's, in order, and the registry has one entry per id", () => {
    expect([...TERM_SCHEME_IDS]).toEqual(IDS.term_schemes.ids);
    expect(DEFAULT_TERM_SCHEME).toBe(IDS.term_schemes.default);
    expect(TERM_SCHEMES.map((s) => s.id)).toEqual([...TERM_SCHEME_IDS]);
  });

  test("BattleLab IS today's terminal, in both modes — the default changes nothing", () => {
    expect(resolveTermScheme("battlelab", "dark")).toBe(THEMES.dark.terminal);
    expect(resolveTermScheme("battlelab", "light")).toBe(THEMES.light.terminal);
    expect(xtermThemeFor("battlelab", "dark")).toEqual(xtermTheme("dark"));
    expect(xtermThemeFor("battlelab", "light")).toEqual(xtermTheme("light"));
  });

  test("every variant fills all 16 ANSI slots and its base colours with #rrggbb", () => {
    for (const { name, t } of variants) {
      expect(Object.keys(t.ansi), name).toHaveLength(16);
      for (const c of [t.background, t.foreground, t.selectionBackground, ...Object.values(t.ansi)])
        expect(c, name).toMatch(HEX);
    }
  });

  test("contrast policy, rule 1: every scheme's text reaches 4.5:1 on its own ground", () => {
    for (const { name, t } of variants)
      expect(contrast(t.foreground, t.background), name).toBeGreaterThanOrEqual(4.5);
  });

  test("contrast policy, rule 2: the low-contrast note is measured, and lands where it should", () => {
    // Pinned so a value change that moves a scheme across the 3:1 line is a visible diff here.
    // BattleLab dark is on the list: it is xterm's own Tango palette, kept pixel-identical since
    // #473 (only the LIGHT palette was AA-tuned), and its magenta measures under 3:1. The note is
    // computed for the default exactly as for any other scheme — no exemption.
    expect(variants.filter((v) => lowContrast(v.t)).map((v) => v.name)).toEqual([
      "battlelab/dark",
      "solarized/light",
      "gruvbox/dark",
      "gruvbox/light",
      "catppuccin/light",
    ]);
  });

  test("a follows-mode scheme switches with the mode; a dark-only one stays dark", () => {
    const solarized = TERM_SCHEMES.find((s) => s.id === "solarized")!;
    const nord = TERM_SCHEMES.find((s) => s.id === "nord")!;
    expect(resolveTermScheme("solarized", "light")).toBe(solarized.light);
    expect(resolveTermScheme("solarized", "dark")).toBe(solarized.dark);
    expect(nord.light).toBeUndefined();
    expect(resolveTermScheme("nord", "light")).toBe(nord.dark);
  });

  test("read-time fallback: anything unknown is BattleLab, never a throw", () => {
    for (const bad of ["neon", "", null, undefined, 3, ["nord"], { id: "nord" }, "Nord"])
      expect(coerceTermScheme(bad)).toBe("battlelab");
    expect(coerceTermScheme("dracula")).toBe("dracula");
    // A stale id that reached the resolver anyway still paints the default.
    expect(resolveTermScheme("gone" as never, "dark")).toBe(THEMES.dark.terminal);
  });
});
