// Theme registry (#109). A theme owns two things:
//  1. the chrome palette — CSS custom properties applied via `data-theme` on <html>;
//     the actual values live in index.css under `:root[data-theme="…"]` blocks.
//  2. the terminal look — the xterm.js `ITheme` + font, consumed by Terminal.tsx.
// Royal is the default and is byte-for-byte the current TermRoyale identity, so adding
// this registry changes nothing until a user picks another theme.

export const THEME_IDS = ["royal", "dark", "light"] as const;
export type ThemeId = (typeof THEME_IDS)[number];
export const DEFAULT_THEME: ThemeId = "royal";

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
  royal: {
    id: "royal",
    label: "Royal",
    description: "The TermRoyale identity — deep indigo with a gold crown.",
    terminal: {
      fontFamily: MONO,
      fontSize: 13,
      background: "#0d0820",
      foreground: "#ece9f7",
      cursor: "#f5c542",
      selectionBackground: "#3a2d6e",
    },
  },
  dark: {
    id: "dark",
    label: "Dark",
    description: "A neutral slate dark theme.",
    terminal: {
      fontFamily: MONO,
      fontSize: 13,
      background: "#0e1116",
      foreground: "#e6edf3",
      cursor: "#58a6ff",
      selectionBackground: "#2d3a51",
    },
  },
  light: {
    id: "light",
    label: "Light",
    description: "A clean light theme for bright rooms.",
    terminal: {
      fontFamily: MONO,
      fontSize: 13,
      background: "#ffffff",
      foreground: "#1c2024",
      cursor: "#1d4ed8",
      selectionBackground: "#cfe0ff",
    },
  },
};

export const THEME_LIST: ThemeMeta[] = THEME_IDS.map((id) => THEMES[id]);

export function isThemeId(v: unknown): v is ThemeId {
  return typeof v === "string" && (THEME_IDS as readonly string[]).includes(v);
}

/** Narrow any input to a valid ThemeId, falling back to the default. Used at every
 *  trust boundary (localStorage, /api/config, the write endpoint payload). */
export function coerceTheme(v: unknown): ThemeId {
  return isThemeId(v) ? v : DEFAULT_THEME;
}
