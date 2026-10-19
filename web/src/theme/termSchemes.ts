// Terminal colour schemes (#1494 P2). A scheme is the terminal's COLOUR only — ground, ink,
// selection and the 16 ANSI slots — exactly what `TerminalTheme` holds. The font (#866) and size
// (#859) stay their own axes, and the cursor is always the brand accent (set by the consumer).
//
// `battlelab` is today's terminal, resolved from THEMES[mode].terminal, so the default changes
// nothing. A scheme with a `light` variant follows the mode; one without is dark only and stays
// dark in light mode (the operator chose it; the card says so).
//
// The third-party ANSI values are the published ones, untouched. They are held to a measured
// policy rather than AA (themes are other people's designs): the foreground must reach 4.5:1 on
// its own ground, or the scheme does not ship; and a chromatic ANSI colour under 3:1 puts a
// visible "Some colours low contrast" note on the card (`lowContrast`). Both pinned by
// termSchemes.test.ts. The rule applies to BattleLab too: its dark variant is xterm's Tango (#473
// kept it pixel-identical), whose magenta is under 3:1, so the default card says so in dark mode. Ids mirror prefs.TERM_SCHEMES via tests/fixtures/appearance_ids.json.

import { THEMES, type TerminalTheme, type ThemeId, type XtermTheme } from "./themes";

export const TERM_SCHEME_IDS = [
  "battlelab",
  "solarized",
  "gruvbox",
  "catppuccin",
  "one",
  "github",
  "dracula",
  "nord",
  "tokyonight",
] as const;
export type TermSchemeId = (typeof TERM_SCHEME_IDS)[number];
export const DEFAULT_TERM_SCHEME: TermSchemeId = "battlelab";

export interface TermScheme {
  id: TermSchemeId;
  label: string;
  dark: TerminalTheme;
  /** Absent = dark only. */
  light?: TerminalTheme;
}

export const TERM_SCHEMES: TermScheme[] = [
  {
    id: "battlelab",
    label: "BattleLab",
    dark: THEMES.dark.terminal,
    light: THEMES.light.terminal,
  },
  {
  // https://ethanschoonover.com/solarized/.
  // Light's foreground is base01 (#586e75), the spec's emphasized body tone: base00 measures
  // 4.1:1 on base3 and would miss the 4.5:1 floor every scheme here is held to.
    id: "solarized",
    label: "Solarized",
    dark: {
    background: "#002b36",
    foreground: "#839496",
    cursor: "#839496",
    selectionBackground: "#073642",
    ansi: {
      black: "#073642",
      red: "#dc322f",
      green: "#859900",
      yellow: "#b58900",
      blue: "#268bd2",
      magenta: "#d33682",
      cyan: "#2aa198",
      white: "#eee8d5",
      brightBlack: "#002b36",
      brightRed: "#cb4b16",
      brightGreen: "#586e75",
      brightYellow: "#657b83",
      brightBlue: "#839496",
      brightMagenta: "#6c71c4",
      brightCyan: "#93a1a1",
      brightWhite: "#fdf6e3",
    },
  },
    light: {
    background: "#fdf6e3",
    foreground: "#586e75",
    cursor: "#586e75",
    selectionBackground: "#eee8d5",
    ansi: {
      black: "#073642",
      red: "#dc322f",
      green: "#859900",
      yellow: "#b58900",
      blue: "#268bd2",
      magenta: "#d33682",
      cyan: "#2aa198",
      white: "#eee8d5",
      brightBlack: "#002b36",
      brightRed: "#cb4b16",
      brightGreen: "#586e75",
      brightYellow: "#657b83",
      brightBlue: "#839496",
      brightMagenta: "#6c71c4",
      brightCyan: "#93a1a1",
      brightWhite: "#fdf6e3",
    },
  },
  },
  {
  // gruvbox (morhetz).
    id: "gruvbox",
    label: "Gruvbox",
    dark: {
    background: "#282828",
    foreground: "#ebdbb2",
    cursor: "#ebdbb2",
    selectionBackground: "#504945",
    ansi: {
      black: "#282828",
      red: "#cc241d",
      green: "#98971a",
      yellow: "#d79921",
      blue: "#458588",
      magenta: "#b16286",
      cyan: "#689d6a",
      white: "#a89984",
      brightBlack: "#928374",
      brightRed: "#fb4934",
      brightGreen: "#b8bb26",
      brightYellow: "#fabd2f",
      brightBlue: "#83a598",
      brightMagenta: "#d3869b",
      brightCyan: "#8ec07c",
      brightWhite: "#ebdbb2",
    },
  },
    light: {
    background: "#fbf1c7",
    foreground: "#3c3836",
    cursor: "#3c3836",
    selectionBackground: "#d5c4a1",
    ansi: {
      black: "#fbf1c7",
      red: "#cc241d",
      green: "#98971a",
      yellow: "#d79921",
      blue: "#458588",
      magenta: "#b16286",
      cyan: "#689d6a",
      white: "#7c6f64",
      brightBlack: "#928374",
      brightRed: "#9d0006",
      brightGreen: "#79740e",
      brightYellow: "#b57614",
      brightBlue: "#076678",
      brightMagenta: "#8f3f71",
      brightCyan: "#427b58",
      brightWhite: "#3c3836",
    },
  },
  },
  {
  // Mocha (dark) / Latte (light).
    id: "catppuccin",
    label: "Catppuccin",
    dark: {
    background: "#1e1e2e",
    foreground: "#cdd6f4",
    cursor: "#cdd6f4",
    selectionBackground: "#45475a",
    ansi: {
      black: "#45475a",
      red: "#f38ba8",
      green: "#a6e3a1",
      yellow: "#f9e2af",
      blue: "#89b4fa",
      magenta: "#f5c2e7",
      cyan: "#94e2d5",
      white: "#bac2de",
      brightBlack: "#585b70",
      brightRed: "#f38ba8",
      brightGreen: "#a6e3a1",
      brightYellow: "#f9e2af",
      brightBlue: "#89b4fa",
      brightMagenta: "#f5c2e7",
      brightCyan: "#94e2d5",
      brightWhite: "#a6adc8",
    },
  },
    light: {
    background: "#eff1f5",
    foreground: "#4c4f69",
    cursor: "#4c4f69",
    selectionBackground: "#ccd0da",
    ansi: {
      black: "#5c5f77",
      red: "#d20f39",
      green: "#40a02b",
      yellow: "#df8e1d",
      blue: "#1e66f5",
      magenta: "#ea76cb",
      cyan: "#179299",
      white: "#acb0be",
      brightBlack: "#6c6f85",
      brightRed: "#d20f39",
      brightGreen: "#40a02b",
      brightYellow: "#df8e1d",
      brightBlue: "#1e66f5",
      brightMagenta: "#ea76cb",
      brightCyan: "#179299",
      brightWhite: "#bcc0cc",
    },
  },
  },
  {
  // Atom One Dark / One Light.
    id: "one",
    label: "One",
    dark: {
    background: "#282c34",
    foreground: "#abb2bf",
    cursor: "#abb2bf",
    selectionBackground: "#3e4451",
    ansi: {
      black: "#282c34",
      red: "#e06c75",
      green: "#98c379",
      yellow: "#e5c07b",
      blue: "#61afef",
      magenta: "#c678dd",
      cyan: "#56b6c2",
      white: "#abb2bf",
      brightBlack: "#5c6370",
      brightRed: "#e06c75",
      brightGreen: "#98c379",
      brightYellow: "#e5c07b",
      brightBlue: "#61afef",
      brightMagenta: "#c678dd",
      brightCyan: "#56b6c2",
      brightWhite: "#ffffff",
    },
  },
    light: {
    background: "#fafafa",
    foreground: "#383a42",
    cursor: "#383a42",
    selectionBackground: "#e5e5e6",
    ansi: {
      black: "#383a42",
      red: "#e45649",
      green: "#50a14f",
      yellow: "#c18401",
      blue: "#0184bc",
      magenta: "#a626a4",
      cyan: "#0997b3",
      white: "#fafafa",
      brightBlack: "#4f525e",
      brightRed: "#e06c75",
      brightGreen: "#98c379",
      brightYellow: "#e5c07b",
      brightBlue: "#61afef",
      brightMagenta: "#c678dd",
      brightCyan: "#56b6c2",
      brightWhite: "#ffffff",
    },
  },
  },
  {
  // GitHub Primer dark / light.
    id: "github",
    label: "GitHub",
    dark: {
    background: "#0d1117",
    foreground: "#e6edf3",
    cursor: "#e6edf3",
    selectionBackground: "#264f78",
    ansi: {
      black: "#484f58",
      red: "#ff7b72",
      green: "#3fb950",
      yellow: "#d29922",
      blue: "#58a6ff",
      magenta: "#bc8cff",
      cyan: "#39c5cf",
      white: "#b1bac4",
      brightBlack: "#6e7681",
      brightRed: "#ffa198",
      brightGreen: "#56d364",
      brightYellow: "#e3b341",
      brightBlue: "#79c0ff",
      brightMagenta: "#d2a8ff",
      brightCyan: "#56d4dd",
      brightWhite: "#ffffff",
    },
  },
    light: {
    background: "#ffffff",
    foreground: "#1f2328",
    cursor: "#1f2328",
    selectionBackground: "#b6e3ff",
    ansi: {
      black: "#24292f",
      red: "#cf222e",
      green: "#116329",
      yellow: "#4d2d00",
      blue: "#0969da",
      magenta: "#8250df",
      cyan: "#1b7c83",
      white: "#6e7781",
      brightBlack: "#57606a",
      brightRed: "#a40e26",
      brightGreen: "#1a7f37",
      brightYellow: "#633c01",
      brightBlue: "#218bff",
      brightMagenta: "#a475f9",
      brightCyan: "#3192aa",
      brightWhite: "#8c959f",
    },
  },
  },
  {
  // draculatheme.com.
    id: "dracula",
    label: "Dracula",
    dark: {
    background: "#282a36",
    foreground: "#f8f8f2",
    cursor: "#f8f8f2",
    selectionBackground: "#44475a",
    ansi: {
      black: "#21222c",
      red: "#ff5555",
      green: "#50fa7b",
      yellow: "#f1fa8c",
      blue: "#bd93f9",
      magenta: "#ff79c6",
      cyan: "#8be9fd",
      white: "#f8f8f2",
      brightBlack: "#6272a4",
      brightRed: "#ff6e6e",
      brightGreen: "#69ff94",
      brightYellow: "#ffffa5",
      brightBlue: "#d6acff",
      brightMagenta: "#ff92df",
      brightCyan: "#a4ffff",
      brightWhite: "#ffffff",
    },
  },
  },
  {
  // nordtheme.com.
    id: "nord",
    label: "Nord",
    dark: {
    background: "#2e3440",
    foreground: "#d8dee9",
    cursor: "#d8dee9",
    selectionBackground: "#434c5e",
    ansi: {
      black: "#3b4252",
      red: "#bf616a",
      green: "#a3be8c",
      yellow: "#ebcb8b",
      blue: "#81a1c1",
      magenta: "#b48ead",
      cyan: "#88c0d0",
      white: "#e5e9f0",
      brightBlack: "#4c566a",
      brightRed: "#bf616a",
      brightGreen: "#a3be8c",
      brightYellow: "#ebcb8b",
      brightBlue: "#81a1c1",
      brightMagenta: "#b48ead",
      brightCyan: "#8fbcbb",
      brightWhite: "#eceff4",
    },
  },
  },
  {
  // Tokyo Night (night).
    id: "tokyonight",
    label: "Tokyo Night",
    dark: {
    background: "#1a1b26",
    foreground: "#c0caf5",
    cursor: "#c0caf5",
    selectionBackground: "#33467c",
    ansi: {
      black: "#15161e",
      red: "#f7768e",
      green: "#9ece6a",
      yellow: "#e0af68",
      blue: "#7aa2f7",
      magenta: "#bb9af7",
      cyan: "#7dcfff",
      white: "#a9b1d6",
      brightBlack: "#414868",
      brightRed: "#f7768e",
      brightGreen: "#9ece6a",
      brightYellow: "#e0af68",
      brightBlue: "#7aa2f7",
      brightMagenta: "#bb9af7",
      brightCyan: "#7dcfff",
      brightWhite: "#c0caf5",
    },
  },
  },
];

export function termScheme(id: TermSchemeId): TermScheme {
  return TERM_SCHEMES.find((s) => s.id === id)!;
}

export function isTermSchemeId(v: unknown): v is TermSchemeId {
  return typeof v === "string" && (TERM_SCHEME_IDS as readonly string[]).includes(v);
}

/** The read boundary (localStorage, /api/config): anything unknown is the default, never a throw. */
export function coerceTermScheme(v: unknown): TermSchemeId {
  return isTermSchemeId(v) ? v : DEFAULT_TERM_SCHEME;
}

/** The colours a scheme paints in a mode. `mode` is the RESOLVED mode (ThemeStore.theme), so
 *  Match device flips a follows-mode scheme with the OS. */
export function resolveTermScheme(id: TermSchemeId, mode: ThemeId): TerminalTheme {
  const s = termScheme(coerceTermScheme(id));
  return mode === "light" && s.light ? s.light : s.dark;
}

/** The flat xterm `ITheme` for a scheme in a mode — what Terminal.tsx assigns. */
export function xtermThemeFor(id: TermSchemeId, mode: ThemeId): XtermTheme {
  const t = resolveTermScheme(id, mode);
  return {
    background: t.background,
    foreground: t.foreground,
    cursor: t.cursor,
    selectionBackground: t.selectionBackground,
    ...t.ansi,
  };
}

function lin(c: number): number {
  const v = c / 255;
  return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
}
function luminance(hex: string): number {
  const h = hex.slice(1);
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}
export function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const CHROMATIC = ["red", "green", "yellow", "blue", "magenta", "cyan"] as const;

/** True when any of the six chromatic normal colours measures under 3:1 on the ground — the
 *  card's "Some colours low contrast" note. Computed, never hand-set. */
export function lowContrast(t: TerminalTheme): boolean {
  return CHROMATIC.some((c) => contrast(t.ansi[c], t.background) < 3);
}
