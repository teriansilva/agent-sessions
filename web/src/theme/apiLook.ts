// The API-session look (#1494 P3): how the conversation in an API session reads — codex-api,
// claude-api, opencode-api (StructuredPane) and chat (ChatPane). Terminal sessions are not
// affected, and neither is the pane's chrome (head, composer): the look is applied to the
// conversation LOG only.
//
// The contract that matters most: **the default look sets nothing.** `apiLookProps(DEFAULT)` is
// `{}` — no class, no attribute, no inline property — so a default pane runs on today's CSS path
// unchanged, which e2e/api-look-defaults.spec.ts holds against a computed-style baseline recorded
// from main. A non-default look works by RE-POINTING the existing tokens (--text, --line,
// --surface-3, …) inside the log, plus three log-only hooks the pane CSS reads with today's value
// as the fallback: --api-you, --api-rule and --api-scale.
//
// Ids mirror prefs.API_* via tests/fixtures/appearance_ids.json.

import type { CSSProperties } from "react";
import { DEFAULT_ACCENT, readableAccent } from "./accent";
import { isTermFontFamily, TERM_FONT_FAMILY_MAX_LEN } from "./termFont";
import type { ThemeId } from "./themes";

export const API_PALETTE_IDS = ["battlelab", "graphite", "solarized", "nord", "sepia", "contrast"] as const;
export const API_BACKGROUND_IDS = ["none", "grid", "dots", "ruled", "scanlines"] as const;
export const API_BODY_FONTS = ["sans", "serif", "mono", "custom"] as const;
export const API_CODE_FONTS = ["app", "terminal", "custom"] as const;
export const API_TEXT_SIZE_MIN = 12;
export const API_TEXT_SIZE_MAX = 18;
export const API_TEXT_SIZE_DEFAULT = 14;
export const API_FONT_CUSTOM_MAX_LEN = TERM_FONT_FAMILY_MAX_LEN;

export type ApiPaletteId = (typeof API_PALETTE_IDS)[number];
export type ApiBackgroundId = (typeof API_BACKGROUND_IDS)[number];
export type ApiBodyFont = (typeof API_BODY_FONTS)[number];
export type ApiCodeFont = (typeof API_CODE_FONTS)[number];

export interface ApiLook {
  palette: ApiPaletteId;
  background: ApiBackgroundId;
  body_font: ApiBodyFont;
  code_font: ApiCodeFont;
  text_size: number;
  /** Only meaningful (and only kept) while the matching font is `custom`. */
  body_font_custom?: string;
  code_font_custom?: string;
}

export const DEFAULT_API_LOOK: ApiLook = {
  palette: "battlelab",
  background: "none",
  body_font: "sans",
  code_font: "app",
  text_size: API_TEXT_SIZE_DEFAULT,
};

/** One mode's colours. `faint` is timestamps and other tertiary text (held to 3:1, like the app's
 *  own --text-3); every other text pair is held to AA by apiLook.test.ts. */
export interface ApiPaletteColours {
  bg: string;
  panel: string;
  text: string;
  muted: string;
  faint: string;
  line: string;
  lineStrong: string;
  code: string;
  you: string;
  rule: string;
}

export interface ApiPalette {
  id: Exclude<ApiPaletteId, "battlelab">;
  label: string;
  dark: ApiPaletteColours;
  light: ApiPaletteColours;
}

/** BattleLab is not in this list: it IS the app's tokens, so it has no colours of its own to set. */
export const API_PALETTES: ApiPalette[] = [
  {
    id: "graphite",
    label: "Graphite",
    dark: { bg: "#16171a", panel: "#202226", text: "#d9dbe0", muted: "#9aa0a9", faint: "#80868f", line: "#2c2f35", lineStrong: "#3a3e45", code: "#101114", you: "#7aa2ff", rule: "#6b717b" },
    light: { bg: "#f5f5f6", panel: "#ffffff", text: "#1d1f23", muted: "#555a63", faint: "#6b7079", line: "#dcdee2", lineStrong: "#c4c7cd", code: "#eceef1", you: "#2f5fd0", rule: "#8a9099" },
  },
  {
    id: "solarized",
    label: "Solarized",
    dark: { bg: "#002b36", panel: "#073642", text: "#93a1a1", muted: "#909f9f", faint: "#6f8287", line: "#0d4654", lineStrong: "#1c5b6b", code: "#00212b", you: "#b58900", rule: "#2aa198" },
    light: { bg: "#fdf6e3", panel: "#eee8d5", text: "#073642", muted: "#4a5f66", faint: "#5f7178", line: "#e3dcc5", lineStrong: "#cfc7ae", code: "#f5efdc", you: "#7a5a00", rule: "#2aa198" },
  },
  {
    id: "nord",
    label: "Nord",
    dark: { bg: "#2e3440", panel: "#3b4252", text: "#eceff4", muted: "#c0c8d4", faint: "#a3adbd", line: "#434c5e", lineStrong: "#4c566a", code: "#272c36", you: "#88c0d0", rule: "#81a1c1" },
    light: { bg: "#eceff4", panel: "#e5e9f0", text: "#2e3440", muted: "#434c5e", faint: "#566074", line: "#d1d7e0", lineStrong: "#b8c1cf", code: "#d8dee9", you: "#2f6178", rule: "#5e81ac" },
  },
  {
    id: "sepia",
    label: "Sepia",
    dark: { bg: "#1f1a14", panel: "#2a231b", text: "#e9dcc6", muted: "#bfae93", faint: "#a19077", line: "#3c3226", lineStrong: "#4d4133", code: "#17130e", you: "#e0a458", rule: "#8c7a5f" },
    light: { bg: "#f4ecd8", panel: "#fbf5e6", text: "#3b2f20", muted: "#5f5039", faint: "#6f5f46", line: "#ddd0b2", lineStrong: "#c9b993", code: "#ebe1c8", you: "#8a4b0f", rule: "#a8916c" },
  },
  {
    id: "contrast",
    label: "High contrast",
    dark: { bg: "#000000", panel: "#0a0a0a", text: "#ffffff", muted: "#d6d6d6", faint: "#b8b8b8", line: "#6a6a6a", lineStrong: "#9a9a9a", code: "#000000", you: "#ffd400", rule: "#ffffff" },
    light: { bg: "#ffffff", panel: "#ffffff", text: "#000000", muted: "#1f1f1f", faint: "#3d3d3d", line: "#8a8a8a", lineStrong: "#000000", code: "#f0f0f0", you: "#5a3a00", rule: "#000000" },
  },
];

export const API_PALETTE_LABELS: Record<ApiPaletteId, string> = {
  battlelab: "BattleLab",
  ...(Object.fromEntries(API_PALETTES.map((p) => [p.id, p.label])) as Record<
    Exclude<ApiPaletteId, "battlelab">,
    string
  >),
};

export const API_BACKGROUND_LABELS: Record<ApiBackgroundId, string> = {
  none: "None",
  grid: "Grid",
  dots: "Dots",
  ruled: "Ruled",
  scanlines: "Scanlines",
};

/** Body-font stacks. `sans` is absent on purpose: it is the app's own face, so it sets nothing. */
export const API_BODY_STACKS: Record<Exclude<ApiBodyFont, "sans" | "custom">, string> = {
  serif: '"Iowan Old Style", Charter, Georgia, "DejaVu Serif", serif',
  mono: "var(--font-mono)",
};

const oneOf = <T extends string>(list: readonly T[], v: unknown, fallback: T): T =>
  typeof v === "string" && (list as readonly string[]).includes(v) ? (v as T) : fallback;

/** A custom stack is held to the terminal font's rules (same sanitizer, same length cap). */
export function isApiFontStack(v: unknown): boolean {
  return isTermFontFamily(v) && (v as string).length <= API_FONT_CUSTOM_MAX_LEN;
}

/** The read boundary (localStorage, /api/config). Every field coerces ON ITS OWN, so one bad
 *  field falls back to its default while the others keep their saved values; a non-object is the
 *  default look. A `custom` font without a usable stack falls back to that font's default. */
export function coerceApiLook(v: unknown): ApiLook {
  if (!v || typeof v !== "object" || Array.isArray(v)) return { ...DEFAULT_API_LOOK };
  const o = v as Record<string, unknown>;
  const size =
    typeof o.text_size === "number" &&
    Number.isInteger(o.text_size) &&
    o.text_size >= API_TEXT_SIZE_MIN &&
    o.text_size <= API_TEXT_SIZE_MAX
      ? o.text_size
      : API_TEXT_SIZE_DEFAULT;
  const look: ApiLook = {
    palette: oneOf(API_PALETTE_IDS, o.palette, DEFAULT_API_LOOK.palette),
    background: oneOf(API_BACKGROUND_IDS, o.background, DEFAULT_API_LOOK.background),
    body_font: oneOf(API_BODY_FONTS, o.body_font, DEFAULT_API_LOOK.body_font),
    code_font: oneOf(API_CODE_FONTS, o.code_font, DEFAULT_API_LOOK.code_font),
    text_size: size,
  };
  if (look.body_font === "custom") {
    if (isApiFontStack(o.body_font_custom)) look.body_font_custom = o.body_font_custom as string;
    else look.body_font = DEFAULT_API_LOOK.body_font;
  }
  if (look.code_font === "custom") {
    if (isApiFontStack(o.code_font_custom)) look.code_font_custom = o.code_font_custom as string;
    else look.code_font = DEFAULT_API_LOOK.code_font;
  }
  return look;
}

export function isDefaultApiLook(l: ApiLook): boolean {
  return (
    l.palette === DEFAULT_API_LOOK.palette &&
    l.background === DEFAULT_API_LOOK.background &&
    l.body_font === DEFAULT_API_LOOK.body_font &&
    l.code_font === DEFAULT_API_LOOK.code_font &&
    l.text_size === DEFAULT_API_LOOK.text_size
  );
}

export function apiPalette(id: ApiPaletteId): ApiPalette | undefined {
  return API_PALETTES.find((p) => p.id === id);
}

/** The props a conversation log takes for a look: an inline token set and a background id.
 *  EMPTY for the default look — see the contract at the top of this file. `mode` is the RESOLVED
 *  mode; `termFamily` is the terminal's face, for the "Terminal font" code option; `accent` is the
 *  live brand accent, which the palette's links are derived from. */
export function apiLookProps(
  look: ApiLook,
  mode: ThemeId,
  termFamily: string,
  accent: string = DEFAULT_ACCENT,
): { style?: CSSProperties; "data-api-bg"?: ApiBackgroundId } {
  if (isDefaultApiLook(look)) return {};
  const v: Record<string, string> = {};
  const p = apiPalette(look.palette)?.[mode];
  if (p) {
    Object.assign(v, {
      backgroundColor: p.bg,
      color: p.text,
      "--text": p.text,
      "--text-1": p.text,
      "--text-muted": p.muted,
      "--text-2": p.muted,
      "--text-3": p.faint,
      "--line": p.line,
      "--border-subtle": p.line,
      "--line-strong": p.lineStrong,
      "--border": p.lineStrong,
      "--surface-3": p.code,
      "--bg-1": p.panel,
      "--panel": p.panel,
      "--surface-2": p.panel,
      "--api-you": p.you,
      "--api-rule": p.rule,
      // Links keep as much of the LIVE accent as AA allows on this palette's grounds — computed
      // for the actual accent, so a custom one (black, white, anything) is covered too.
      "--api-link": readableAccent(accent, p.text, [p.bg, p.panel, p.code]),
    });
  }
  if (look.body_font === "custom" && look.body_font_custom) v.fontFamily = look.body_font_custom;
  else if (look.body_font !== "sans" && look.body_font !== "custom")
    v.fontFamily = API_BODY_STACKS[look.body_font];
  // The code face is the token every code surface in the log already reads.
  if (look.code_font === "terminal") Object.assign(v, { "--font-mono": termFamily });
  else if (look.code_font === "custom" && look.code_font_custom)
    Object.assign(v, { "--font-mono": look.code_font_custom });
  if (look.text_size !== API_TEXT_SIZE_DEFAULT)
    Object.assign(v, { "--api-scale": String(look.text_size / API_TEXT_SIZE_DEFAULT) });
  const out: { style?: CSSProperties; "data-api-bg"?: ApiBackgroundId } = {
    style: v as CSSProperties,
  };
  if (look.background !== "none") out["data-api-bg"] = look.background;
  return out;
}
