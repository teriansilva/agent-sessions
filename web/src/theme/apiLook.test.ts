import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, test } from "vitest";
import {
  API_BACKGROUND_IDS,
  API_BODY_FONTS,
  API_CODE_FONTS,
  API_PALETTE_IDS,
  API_PALETTES,
  API_TEXT_SIZE_MAX,
  API_TEXT_SIZE_MIN,
  apiLookProps,
  coerceApiLook,
  DEFAULT_API_LOOK,
} from "./apiLook";
import { ACCENT_PRESETS, readableAccent } from "./accent";
import { contrast } from "./termSchemes";

// The SAME fixture tests/test_prefs.py reads, so client and server coerce identically (#1494 P3).
const FX = JSON.parse(
  readFileSync(resolve(process.cwd(), "../tests/fixtures/appearance_ids.json"), "utf8"),
).api_look as {
  default: object;
  palettes: string[];
  backgrounds: string[];
  body_fonts: string[];
  code_fonts: string[];
  text_size: { min: number; max: number };
  read: { in: unknown; out: object; why: string }[];
};

// Every preset, the colour well's extremes, and 500 deterministic custom accents (an LCG, so a
// failure reproduces).
const LINK_ACCENTS: string[] = (() => {
  const out = [...ACCENT_PRESETS.map((p) => p.hex), "#000000", "#ffffff", "#808080", "#7f7f7f", "#0000ff", "#ffff00"];
  let x = 1494;
  for (let i = 0; i < 500; i++) {
    x = (x * 1103515245 + 12345) % 2147483648;
    out.push("#" + (x % 0x1000000).toString(16).padStart(6, "0"));
  }
  return out;
})();

describe("API-session look (#1494 P3)", () => {
  test("the ids and limits are the shared fixture's", () => {
    expect([...API_PALETTE_IDS]).toEqual(FX.palettes);
    expect([...API_BACKGROUND_IDS]).toEqual(FX.backgrounds);
    expect([...API_BODY_FONTS]).toEqual(FX.body_fonts);
    expect([...API_CODE_FONTS]).toEqual(FX.code_fonts);
    expect([API_TEXT_SIZE_MIN, API_TEXT_SIZE_MAX]).toEqual([FX.text_size.min, FX.text_size.max]);
    expect(DEFAULT_API_LOOK).toEqual(FX.default);
    expect(API_PALETTES.map((p) => p.id)).toEqual(FX.palettes.slice(1)); // battlelab sets nothing
  });

  test.each(FX.read.map((c) => [c.why, c] as const))("read coerces per field: %s", (_, c) => {
    expect(coerceApiLook(c.in)).toEqual(c.out);
  });

  test("THE contract: the default look sets nothing on the log, in either mode", () => {
    expect(apiLookProps(DEFAULT_API_LOOK, "dark", "monospace")).toEqual({});
    expect(apiLookProps({ ...DEFAULT_API_LOOK }, "light", "monospace")).toEqual({});
  });

  test("a look sets only what it changes", () => {
    const size = apiLookProps({ ...DEFAULT_API_LOOK, text_size: 16 }, "dark", "x");
    expect(size).toEqual({ style: { "--api-scale": String(16 / 14) } });
    const bg = apiLookProps({ ...DEFAULT_API_LOOK, background: "dots" }, "dark", "x");
    expect(bg).toEqual({ style: {}, "data-api-bg": "dots" });
    const term = apiLookProps({ ...DEFAULT_API_LOOK, code_font: "terminal" }, "dark", '"Fira Code", monospace');
    expect(term.style).toEqual({ "--font-mono": '"Fira Code", monospace' });
    const nord = apiLookProps({ ...DEFAULT_API_LOOK, palette: "nord" }, "light", "x", "#000000");
    const n = API_PALETTES.find((p) => p.id === "nord")!.light;
    // The link is computed from the accent passed in: here a custom black.
    expect((nord.style as Record<string, string>)["--api-link"]).toBe(
      readableAccent("#000000", n.text, [n.bg, n.panel, n.code]),
    );
    expect((nord.style as Record<string, string>)["--text"]).toBe(API_PALETTES.find((p) => p.id === "nord")!.light.text);
  });

  for (const p of API_PALETTES)
    for (const mode of ["dark", "light"] as const)
      test(`${p.label} (${mode}) holds AA for every text pair it paints`, () => {
        const c = p[mode];
        for (const [fg, bg, name] of [
          [c.text, c.bg, "text/ground"],
          [c.text, c.panel, "text/panel"],
          [c.text, c.code, "text/code"],
          [c.muted, c.bg, "muted/ground"],
          [c.muted, c.panel, "muted/panel"],
          [c.you, c.bg, "your-message label/ground"],
        ] as const)
          expect(contrast(fg, bg), name).toBeGreaterThanOrEqual(4.5);
        // Links (--api-link) are derived from the LIVE accent, so they must hold AA for ANY valid
        // accent, not just the presets: the 16, the extremes a colour well reaches (black, white,
        // mid greys), and a fixed pseudo-random sweep of custom hexes, on every ground a link can
        // sit on. (#1499 review: a custom #000000 on High contrast dark was 2.02:1.)
        for (const a of LINK_ACCENTS) {
          // What the log is actually given, not a recomputation: apiLookProps' own output.
          const props = apiLookProps({ ...DEFAULT_API_LOOK, palette: p.id }, mode, "x", a);
          const link = (props.style as Record<string, string>)["--api-link"];
          expect(link, `${a} link is a resolved colour`).toMatch(/^#[0-9a-f]{6}$/);
          for (const g of [c.bg, c.panel, c.code])
            expect(contrast(link, g), `${a} link on ${g}`).toBeGreaterThanOrEqual(4.5);
        }
        // Timestamps and other tertiary text: 3:1, the bar the app's own --text-3 is held to.
        expect(contrast(c.faint, c.bg), "faint/ground").toBeGreaterThanOrEqual(3);
        expect(contrast(c.faint, c.panel), "faint/panel").toBeGreaterThanOrEqual(3);
      });
});
