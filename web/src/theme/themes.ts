// Theme registry (#109). A theme owns two things:
//  1. the chrome palette — CSS custom properties applied via `data-theme` on <html>;
//     the actual values live in index.css under `:root[data-theme="…"]` blocks.
//  2. the terminal look — the xterm.js `ITheme` + font, consumed by Terminal.tsx.
// Dark is the BattleLab default (tactical-HUD, phosphor-amber). Light is the daylight variant.
// The retired `royal` theme migrates to `dark` via coerceTheme (any unknown id → DEFAULT). (#211)

export const THEME_IDS = ["dark", "light"] as const;
export type ThemeId = (typeof THEME_IDS)[number];
export const DEFAULT_THEME: ThemeId = "dark";

/** xterm.js theme subset we set (background/foreground/cursor + selection). */
export interface TerminalTheme {
  fontFamily: string;
  fontSize: number;
  background: string;
  foreground: string;
  cursor: string;
  selectionBackground: string;
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
    },
  },
};

export const THEME_LIST: ThemeMeta[] = THEME_IDS.map((id) => THEMES[id]);

/** The subset of xterm.js `ITheme` we drive from a theme. Kept as a plain shape so
 *  Terminal.tsx can assign it to `term.options.theme`. */
export interface XtermTheme {
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
