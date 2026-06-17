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

// The tactical-HUD product family accents (#211) + a couple of extras. `hex` values are
// normalized lowercase #rrggbb so they compare equal to a coerced custom value.
export const ACCENT_PRESETS: AccentPreset[] = [
  { id: "amber", label: "Amber", hex: "#ffb000" },
  { id: "red", label: "Signal Red", hex: "#c02020" },
  { id: "cyan", label: "Cyan", hex: "#19b6c9" },
  { id: "green", label: "Tactical Green", hex: "#3fbf6f" },
  { id: "blue", label: "Blue", hex: "#3b82f6" },
  { id: "magenta", label: "Magenta", hex: "#d6409f" },
];

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
  return 0.2126 * srgbToLinear(r) + 0.7152 * srgbToLinear(g) + 0.0722 * srgbToLinear(b);
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
