import { ACCENT_PRESETS, type AccentPreset } from "../theme/accent";

/** Project colours (#361) are the accent wheel (#1508): the same sixteen named colours Settings →
 *  Interface → Accent offers, from the one list in `theme/accent.ts`, so the two pickers cannot
 *  drift. The colour is still a glanceable marker, not a theming surface. "None" maps to the
 *  store's `color: ""`, and the server keeps accepting any hex — a colour from the old six-swatch
 *  palette stays as it is and the picker shows it as the current colour. `ProjectColorPicker`
 *  draws them, and the New project wizard preselects the first one no active project uses. */
export const PROJECT_COLORS: readonly AccentPreset[] = ACCENT_PRESETS;

export const COLOR_PRESETS: readonly string[] = PROJECT_COLORS.map((p) => p.hex);

/** The colour's name ("Sky"), or its hex when it is not one of the sixteen. */
export function projectColorName(color: string): string {
  const c = color.toLowerCase();
  return PROJECT_COLORS.find((p) => p.hex === c)?.label ?? c;
}
