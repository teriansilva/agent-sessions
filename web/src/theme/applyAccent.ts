// Applying + persisting the active brand accent on the client (#211 Phase 2). The accent
// is applied as two inline custom properties on <html> — `--accent` and `--on-accent` —
// which OVERRIDE the index.css token defaults. Everything else (accent-soft/glow, the CTA
// gradient, borders/LEDs/brackets/focus rings) is derived from `--accent` via color-mix in
// index.css, so these two overrides cascade to the whole accent system. localStorage is the
// no-flash cache (read synchronously at boot — see the inline script in index.html and
// `bootAccent` below); server-side per-user persistence is layered on by AccentProvider.

import { accentTextFor, coerceAccent, DEFAULT_ACCENT, hudLineFor, onAccentFor } from "./accent";
import { prefersDark, readStoredTheme } from "./applyTheme";
import { resolveTheme, type ThemeId } from "./themes";

export const ACCENT_STORAGE_KEY = "tr-accent";

/** The custom properties a custom accent overrides in one mode: `--accent`, `--on-accent`,
 *  `--accent-text` (mode-dependent, #1494) and `--hud` (#1531). `null` for the default accent,
 *  which tokens.css already carries. ONE helper for `<html>` (`applyAccent`) and for any element
 *  that shows another mode through `[data-theme-scope]` (#1574): both mode blocks reset these
 *  tokens, so a scoped preview re-applies them for its own mode. */
export function accentVars(hex: string, mode: ThemeId): Record<string, string> | null {
  const accent = coerceAccent(hex);
  if (accent === DEFAULT_ACCENT) return null;
  return {
    "--accent": accent,
    "--on-accent": onAccentFor(accent),
    "--accent-text": accentTextFor(accent, mode),
    // The chrome line colour (#1531): the accent where it reads on the mode's grounds, else mixed
    // toward the text colour — a pale accent still draws a visible frame on light.
    "--hud": hudLineFor(accent, mode),
  };
}

const ACCENT_PROPS = ["--accent", "--on-accent", "--accent-text", "--hud"] as const;

/** Set the inline accent overrides on <html> (`accentVars`). The default accent is left to
 *  tokens.css (the inline props are cleared) so the unmodified app stays pixel-identical to the
 *  static stylesheet. `mode` is the RESOLVED mode in force. */
export function applyAccent(hex: string, mode: ThemeId = "dark"): void {
  const root = document.documentElement;
  const vars = accentVars(hex, mode);
  for (const k of ACCENT_PROPS) {
    if (vars) root.style.setProperty(k, vars[k]);
    else root.style.removeProperty(k);
  }
}

/** The accent cached on this device, or the default. Never throws. */
export function readStoredAccent(): string {
  try {
    return coerceAccent(localStorage.getItem(ACCENT_STORAGE_KEY));
  } catch {
    return DEFAULT_ACCENT;
  }
}

export function storeAccent(hex: string): void {
  try {
    localStorage.setItem(ACCENT_STORAGE_KEY, coerceAccent(hex));
  } catch {
    /* storage disabled — the inline props are still applied for this page */
  }
}

/** Apply the device-cached accent. Idempotent with the inline boot script in index.html
 *  (which runs pre-paint); belt-and-suspenders call from main.tsx in case the inline
 *  script was stripped (e.g. a strict CSP). */
export function bootAccent(): void {
  applyAccent(readStoredAccent(), resolveTheme(readStoredTheme(), prefersDark()));
}
