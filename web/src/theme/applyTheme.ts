// Applying + persisting the active theme on the client. The chrome theme is a single
// `data-theme` attribute on <html>; index.css does the rest. localStorage is the
// no-flash cache (read synchronously at boot — see the inline script in index.html and
// `bootTheme` below); server-side per-user persistence is layered on in a later PR.

import {
  coerceThemeChoice,
  DEFAULT_THEME,
  resolveTheme,
  type ThemeChoice,
  type ThemeId,
} from "./themes";

export const THEME_STORAGE_KEY = "tr-theme";

/** Set <html data-theme>. Dark is the default (the bare `:root` tokens), but we always
 *  write the attribute so the current theme is observable + the value round-trips. */
export function applyTheme(id: ThemeId): void {
  document.documentElement.dataset.theme = id;
}

/** The media query `system` follows (#1494). Mirrored by the inline boot script in index.html
 *  and by share/main.tsx. */
export const PREFERS_DARK_QUERY = "(prefers-color-scheme: dark)";

/** The device's colour-scheme answer. A browser without matchMedia (or a test environment)
 *  answers dark, the BattleLab default. */
export function prefersDark(): boolean {
  try {
    return window.matchMedia?.(PREFERS_DARK_QUERY).matches ?? true;
  } catch {
    return true;
  }
}

/** The theme choice cached on this device, or the default. Never throws (private-mode/storage
 *  disabled → default). */
export function readStoredTheme(): ThemeChoice {
  try {
    return coerceThemeChoice(localStorage.getItem(THEME_STORAGE_KEY));
  } catch {
    return DEFAULT_THEME;
  }
}

export function storeTheme(id: ThemeChoice): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, id);
  } catch {
    /* storage disabled — the attribute is still applied for this page */
  }
}

/** Apply the device-cached theme. Idempotent with the inline boot script in index.html
 *  (which runs pre-paint); this is the belt-and-suspenders call from main.tsx in case the
 *  inline script was stripped (e.g. a strict CSP). */
export function bootTheme(): void {
  applyTheme(resolveTheme(readStoredTheme(), prefersDark()));
}

/** A page outside the app (the Home Free connect page, the share viewer, #1596): the device's
 *  stored BattleLab choice when this browser has one (the operator on their own device), else the
 *  device's colour scheme — such a page has no settings of its own, and dark-by-default would
 *  ignore a visitor's light device. */
export function bootPageTheme(): void {
  let stored: string | null = null;
  try {
    stored = localStorage.getItem(THEME_STORAGE_KEY);
  } catch {
    /* storage blocked: follow the device */
  }
  applyTheme(resolveTheme(stored === null ? "system" : coerceThemeChoice(stored), prefersDark()));
}
