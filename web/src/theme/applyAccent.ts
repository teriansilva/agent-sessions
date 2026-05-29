// Applying + persisting the active brand accent on the client (#211 Phase 2). The accent
// is applied as two inline custom properties on <html> — `--accent` and `--on-accent` —
// which OVERRIDE the index.css token defaults. Everything else (accent-soft/glow, the CTA
// gradient, borders/LEDs/brackets/focus rings) is derived from `--accent` via color-mix in
// index.css, so these two overrides cascade to the whole accent system. localStorage is the
// no-flash cache (read synchronously at boot — see the inline script in index.html and
// `bootAccent` below); server-side per-user persistence is layered on by AccentProvider.

import { coerceAccent, DEFAULT_ACCENT, onAccentFor } from "./accent";

export const ACCENT_STORAGE_KEY = "tr-accent";

/** Set the inline `--accent` + `--on-accent` overrides on <html>. The default accent is
 *  left to index.css (clear the inline props) so the unmodified app stays pixel-identical
 *  to the static stylesheet; a custom accent sets both props. */
export function applyAccent(hex: string): void {
  const accent = coerceAccent(hex);
  const root = document.documentElement;
  if (accent === DEFAULT_ACCENT) {
    root.style.removeProperty("--accent");
    root.style.removeProperty("--on-accent");
    return;
  }
  root.style.setProperty("--accent", accent);
  root.style.setProperty("--on-accent", onAccentFor(accent));
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
  applyAccent(readStoredAccent());
}
