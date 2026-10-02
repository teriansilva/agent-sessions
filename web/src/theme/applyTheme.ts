// Applying + persisting the active theme on the client. The chrome theme is a single
// `data-theme` attribute on <html>; index.css does the rest. localStorage is the
// no-flash cache (read synchronously at boot — see the inline script in index.html and
// `bootTheme` below); server-side per-user persistence is layered on in a later PR.

import { coerceTheme, DEFAULT_THEME, type ThemeId } from "./themes";

export const THEME_STORAGE_KEY = "tr-theme";

/** Set <html data-theme>. Dark is the default (the bare `:root` tokens), but we always
 *  write the attribute so the current theme is observable + the value round-trips. */
export function applyTheme(id: ThemeId): void {
  document.documentElement.dataset.theme = id;
}

/** The theme cached on this device, or the default. Never throws (private-mode/storage
 *  disabled → default). */
export function readStoredTheme(): ThemeId {
  try {
    return coerceTheme(localStorage.getItem(THEME_STORAGE_KEY));
  } catch {
    return DEFAULT_THEME;
  }
}

export function storeTheme(id: ThemeId): void {
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
  applyTheme(readStoredTheme());
}
