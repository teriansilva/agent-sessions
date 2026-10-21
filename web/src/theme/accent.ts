// Brand accent (#211 Phase 2). The accent is a single `#rrggbb` colour that drives
// `--accent` (and, via color-mix in index.css, the derived accent-soft/glow + CTA tokens)
// plus the xterm cursor. It is orthogonal to the light/dark theme: a user picks a theme
// AND an accent. Presets are offered for one-tap selection; any custom hex is allowed.
// Mirror of the server contract in src/agent_sessions/prefs.py (coerce_accent / DEFAULT_ACCENT).

export const DEFAULT_ACCENT = "#ffb000"; // phosphor-amber — keep in sync with prefs.py

export interface AccentPreset {
  id: string;
  label: string;
  hex: string;
}

// The tactical-HUD product family accents (#211), widened to a 16-colour wheel (#1494) so the
// picker covers the hue circle plus two neutrals. `hex` values are normalized lowercase #rrggbb so
// they compare equal to a coerced custom value. Every preset holds AA for its own ink
// (`onAccentFor`), pinned by accent.test.ts — which is why Indigo is a shade darker than the
// Tailwind indigo it resembles. Order is the order of the swatch grid: warm → cool → neutral.
export const ACCENT_PRESETS: AccentPreset[] = [
  { id: "amber", label: "Amber", hex: "#ffb000" },
  { id: "orange", label: "Orange", hex: "#ff7a1a" },
  { id: "red", label: "Signal Red", hex: "#c02020" },
  { id: "rose", label: "Rose", hex: "#f43f5e" },
  { id: "magenta", label: "Magenta", hex: "#d6409f" },
  { id: "violet", label: "Violet", hex: "#8b5cf6" },
  { id: "indigo", label: "Indigo", hex: "#5b5ff0" },
  { id: "blue", label: "Blue", hex: "#3b82f6" },
  { id: "sky", label: "Sky", hex: "#38bdf8" },
  { id: "cyan", label: "Cyan", hex: "#19b6c9" },
  { id: "teal", label: "Teal", hex: "#14b8a6" },
  { id: "green", label: "Tactical Green", hex: "#3fbf6f" },
  { id: "lime", label: "Lime", hex: "#84cc16" },
  { id: "yellow", label: "Yellow", hex: "#facc15" },
  { id: "slate", label: "Slate", hex: "#8b98a5" },
  { id: "ice", label: "Ice", hex: "#dbe4ee" },
];

/** The preset a colour is, if it is one — the picker names the active accent ("Amber"), and a
 *  custom hex has no name. */
export function accentPresetFor(hex: string): AccentPreset | undefined {
  return ACCENT_PRESETS.find((p) => p.hex === hex);
}

const HEX6 = /^#?([0-9a-f]{6})$/i;
const HEX3 = /^#?([0-9a-f]{3})$/i;

/** Normalize any input to a lowercase `#rrggbb` string, or null if it isn't a hex colour.
 *  Accepts `#rgb` shorthand (expanded) and a missing leading `#`. Same rules as the server's
 *  `coerce_accent` so client and server agree on what's valid. */
export function normalizeAccent(v: unknown): string | null {
  if (typeof v !== "string") return null;
  const s = v.trim();
  const m6 = HEX6.exec(s);
  if (m6) return "#" + m6[1].toLowerCase();
  const m3 = HEX3.exec(s);
  if (m3) {
    return (
      "#" +
      m3[1]
        .toLowerCase()
        .split("")
        .map((c) => c + c)
        .join("")
    );
  }
  return null;
}

/** Narrow any input to a valid accent, falling back to the default. Used at every trust
 *  boundary (localStorage, /api/config, the picker payload). */
export function coerceAccent(v: unknown): string {
  return normalizeAccent(v) ?? DEFAULT_ACCENT;
}

export function isAccent(v: unknown): v is string {
  return normalizeAccent(v) !== null;
}

function srgbToLinear(channel: number): number {
  const c = channel / 255;
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

/** WCAG relative luminance of a `#rrggbb` colour (0 = black, 1 = white). */
export function relativeLuminance(hex: string): number {
  const h = coerceAccent(hex).slice(1);
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return (
    0.2126 * srgbToLinear(r) +
    0.7152 * srgbToLinear(g) +
    0.0722 * srgbToLinear(b)
  );
}

function contrastRatio(l1: number, l2: number): number {
  const hi = Math.max(l1, l2);
  const lo = Math.min(l1, l2);
  return (hi + 0.05) / (lo + 0.05);
}

// The two ink choices for text/icons that sit ON the accent. Near-black (not pure #000) +
// pure white — whichever gives the better contrast against the chosen accent wins.
const ON_ACCENT_DARK = "#0b0b0d";
const ON_ACCENT_LIGHT = "#ffffff";
const DARK_LUM = relativeLuminance(ON_ACCENT_DARK);
const LIGHT_LUM = 1; // white

/** Pick the readable ink colour for text/icons sitting on top of `accentHex`. A light
 *  accent (amber) → dark ink; a dark accent → white ink. Maximizes WCAG contrast. */
export function onAccentFor(accentHex: string): string {
  const lum = relativeLuminance(accentHex);
  return contrastRatio(lum, DARK_LUM) >= contrastRatio(lum, LIGHT_LUM)
    ? ON_ACCENT_DARK
    : ON_ACCENT_LIGHT;
}

/** Contrast ratio of the chosen on-accent ink against the accent — exposed for tests so a
 *  preset that fails AA can't slip in. */
export function onAccentContrast(accentHex: string): number {
  const ink = onAccentFor(accentHex);
  return contrastRatio(relativeLuminance(accentHex), relativeLuminance(ink));
}

/** The grounds accent-coloured TEXT can sit on, and the ink it falls back toward, per mode —
 *  mirrors of tokens.css (--bg-0/-1/-2, --surface-3, --text-1), pinned by contrast.test.ts. */
export const ACCENT_TEXT_GROUNDS = {
  dark: { text: "#e8e9ec", grounds: ["#0d0e10", "#131418", "#1a1c21", "#08090b"] },
  light: { text: "#14161a", grounds: ["#e7e8ea", "#f2f3f5", "#ffffff", "#dfe1e4"] },
} as const;

function mixHex(a: string, b: string, share: number): string {
  const ch = (h: string, i: number) => parseInt(h.slice(1 + 2 * i, 3 + 2 * i), 16);
  return (
    "#" +
    [0, 1, 2]
      .map((i) =>
        Math.round(ch(a, i) * share + ch(b, i) * (1 - share))
          .toString(16)
          .padStart(2, "0"),
      )
      .join("")
  );
}

/** The accent as TEXT (#1494): `--accent` is a fill colour, and as text a pale accent (Ice,
 *  Yellow, Sky) vanishes on the light ground. Returns the accent itself when it already holds AA
 *  on every ground of the mode — so the default amber on dark is untouched — and otherwise the
 *  accent mixed toward the mode's ink by the LARGEST share that does, keeping as much hue as AA
 *  allows. Share 0 is the ink itself, which always passes, so this always returns a colour. */
export function accentTextFor(accentHex: string, mode: "dark" | "light"): string {
  const { text, grounds } = ACCENT_TEXT_GROUNDS[mode];
  return readableAccent(accentHex, text, grounds);
}

/** The grounds NIGHTJAR's chrome lines (`--hud`: rails, notches, ticks, panel edges) sit on, per
 *  mode: the page ground, the atmosphere's ground and a panel face (#1531). */
export const HUD_LINE_GROUNDS = {
  dark: { text: "#e8e9ec", grounds: ["#0d0e10", "#131418", "#090a0c"] },
  light: { text: "#14161a", grounds: ["#e7e8ea", "#f2f3f5", "#e4e6e9"] },
} as const;

/** The chrome LINE colour for an accent (#1531, docs/design.md v2 §3): the accent itself where it
 *  already reads as a graphic (≥ 3:1, WCAG 1.4.11) on every chrome ground of the mode, else the
 *  accent mixed toward the mode's text by the largest share that does. So a custom accent
 *  recolours the whole frame and a pale one (Yellow, Ice) still draws a visible line on light. */
export function hudLineFor(accentHex: string, mode: "dark" | "light"): string {
  const { text, grounds } = HUD_LINE_GROUNDS[mode];
  return readableAccent(accentHex, text, grounds, 3);
}

/** The accent, or the accent mixed toward `ink` by the largest share (1% steps) that holds AA on
 *  EVERY one of `grounds` — for any valid accent, custom ones included. `ink` must itself hold AA
 *  on the grounds (share 0 is the ink), which is what makes this total. Used for the app's
 *  accent text and for an API-session palette's links (#1494). */
export function readableAccent(
  accentHex: string,
  ink: string,
  grounds: readonly string[],
  min: number = 4.5,
): string {
  const accent = coerceAccent(accentHex);
  const text = ink;
  const lumOf = (h: string) => relativeLuminance(h);
  const passes = (h: string) => grounds.every((g) => contrastRatio(lumOf(h), lumOf(g)) >= min);
  for (let pct = 100; pct > 0; pct -= 1) {
    const c = pct === 100 ? accent : mixHex(accent, text, pct / 100);
    if (passes(c)) return c;
  }
  return text;
}
