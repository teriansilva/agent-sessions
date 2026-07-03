// Theme registry (#109). A theme owns two things:
//  1. the chrome palette — CSS custom properties applied via `data-theme` on <html>;
//     the actual values live in index.css under `:root[data-theme="…"]` blocks.
//  2. the terminal look — the xterm.js `ITheme` + font, consumed by Terminal.tsx.
// Dark is the BattleLab default (tactical-HUD, phosphor-amber). Light is the daylight variant.
// The retired `royal` theme migrates to `dark` via coerceTheme (any unknown id → DEFAULT). (#211)

export const THEME_IDS = ["dark", "light"] as const;
export type ThemeId = (typeof THEME_IDS)[number];
export const DEFAULT_THEME: ThemeId = "dark";

/** The 16-colour ANSI palette a theme ships (#473). xterm.js only themes these 16 base
 *  slots via `ITheme` — 256-colour / truecolor output passes through untouched — but the
 *  16 are what agent CLIs colourise with (secondary text, paths, SHAs, ✓ markers), so
 *  leaving them at xterm's dark-oriented default washes out on a light ground. All 16
 *  fields are required so a partial palette can't silently fall back to that default. */
export interface TerminalAnsiPalette {
  black: string;
  red: string;
  green: string;
  yellow: string;
  blue: string;
  magenta: string;
  cyan: string;
  white: string;
  brightBlack: string;
  brightRed: string;
  brightGreen: string;
  brightYellow: string;
  brightBlue: string;
  brightMagenta: string;
  brightCyan: string;
  brightWhite: string;
}

/** xterm.js theme subset we set (background/foreground/cursor + selection + ANSI 16). */
export interface TerminalTheme {
  fontFamily: string;
  fontSize: number;
  background: string;
  foreground: string;
  cursor: string;
  selectionBackground: string;
  ansi: TerminalAnsiPalette;
}

export interface ThemeMeta {
  id: ThemeId;
  label: string;
  description: string;
  /** Consumed by Terminal.tsx (PR C) to theme the xterm canvas + font. */
  terminal: TerminalTheme;
}

// Same monospace stack the terminal has always used; kept per-theme so a future theme
// could ship a different face without touching Terminal.tsx.
const MONO = 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';

// Dark ANSI = xterm.js's built-in default (the Tango palette), spelled out verbatim so the
// dark terminal renders pixel-identical to before the palette became theme-driven (#473).
const DARK_ANSI: TerminalAnsiPalette = {
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
};

// Light ANSI: dark-on-light, tuned so every colour holds WCAG AA (>=4.5:1) against the light
// terminal ground #f2f3f5 (locked by themes.test.ts). Hue identity is preserved — bright
// variants stay a touch more vivid than their normal counterparts — but "bright" means
// *emphasis*, so on a light ground the greys invert: brightWhite is the darkest grey.
const LIGHT_ANSI: TerminalAnsiPalette = {
  black: "#14161a",
  red: "#a11c1c",
  green: "#136f2c",
  yellow: "#7d5600",
  blue: "#0b57ad",
  magenta: "#8f2f96",
  cyan: "#0e6c7c",
  white: "#5d636d",
  brightBlack: "#585f6a",
  brightRed: "#bc2020",
  brightGreen: "#177f33",
  brightYellow: "#8a5f00",
  brightBlue: "#0c66c8",
  brightMagenta: "#7a3fd4",
  brightCyan: "#0f7a8d",
  brightWhite: "#454b55",
};

export const THEMES: Record<ThemeId, ThemeMeta> = {
  dark: {
    id: "dark",
    label: "Dark",
    description: "BattleLab tactical-HUD — near-black ground, phosphor-amber accent.",
    terminal: {
      fontFamily: MONO,
      fontSize: 13,
      background: "#0d0e10",
      foreground: "#e8e9ec",
      cursor: "#ffb000",
      selectionBackground: "#33332a",
      ansi: DARK_ANSI,
    },
  },
  light: {
    id: "light",
    label: "Light",
    description: "BattleLab daylight — paper-grey ground, same amber accent.",
    terminal: {
      fontFamily: MONO,
      fontSize: 13,
      background: "#f2f3f5",
      foreground: "#14161a",
      cursor: "#ffb000",
      selectionBackground: "#ffe2a8",
      ansi: LIGHT_ANSI,
    },
  },
};

export const THEME_LIST: ThemeMeta[] = THEME_IDS.map((id) => THEMES[id]);

/** The subset of xterm.js `ITheme` we drive from a theme. Kept as a plain shape so
 *  Terminal.tsx can assign it to `term.options.theme`. The 16 ANSI slots sit flat on
 *  `ITheme` (black … brightWhite), so the palette spreads in alongside the base four. */
export interface XtermTheme extends TerminalAnsiPalette {
  background: string;
  foreground: string;
  cursor: string;
  selectionBackground: string;
}

export function xtermTheme(id: ThemeId): XtermTheme {
  const t = THEMES[id].terminal;
  return {
    background: t.background,
    foreground: t.foreground,
    cursor: t.cursor,
    selectionBackground: t.selectionBackground,
    ...t.ansi,
  };
}

export function isThemeId(v: unknown): v is ThemeId {
  return typeof v === "string" && (THEME_IDS as readonly string[]).includes(v);
}

/** Narrow any input to a valid ThemeId, falling back to the default. Used at every
 *  trust boundary (localStorage, /api/config, the write endpoint payload). */
export function coerceTheme(v: unknown): ThemeId {
  return isThemeId(v) ? v : DEFAULT_THEME;
}
