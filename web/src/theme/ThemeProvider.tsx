import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useConfig } from "../app/config";
import { api } from "../lib/api";
import { applyTheme, readStoredTheme, storeTheme, THEME_STORAGE_KEY } from "./applyTheme";
import { coerceTheme, isThemeId, type ThemeId } from "./themes";
import { ThemeCtx } from "./themeStore";

/** Owns the active theme. Initial value is the device cache (already applied pre-paint by
 *  the inline boot script). On first load of a brand-new device (no `tr-theme` in
 *  localStorage), once `/api/config` arrives we seed from the server's value so the user's
 *  preference follows across devices. Once a local choice exists, **the local cache wins**
 *  — a stale server value (from a silent persist failure or a brief touch on another
 *  device) can't flip the theme on every reload (#172). `setTheme` applies + caches
 *  locally and best-effort persists to the server. Must render inside <ConfigProvider>. */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const config = useConfig();
  const [theme, setThemeState] = useState<ThemeId>(() => readStoredTheme());
  const reconciled = useRef(false);

  // Keep the DOM attribute in sync with state (covers the StrictMode remount + any path
  // where state was set without applying).
  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  // One-time **seed** from the server when no *valid* local choice exists yet — fresh
  // device / private window / corrupt cache. After the user picks a theme here, the local
  // cache wins on every subsequent reload; the server still gets updated by `setTheme`
  // below so a future fresh device still picks up the most recent choice. (#172)
  useEffect(() => {
    if (reconciled.current || !config?.theme) return;
    reconciled.current = true;
    // Validate the cached value — a malformed/legacy key (e.g. `"neon"`) is NOT an explicit
    // local choice. Without this, `readStoredTheme()` would coerce the bad value to dark
    // and we'd never adopt the server's valid theme on a fresh device (Hermes #173 review).
    let hasValidLocal = false;
    try {
      hasValidLocal = isThemeId(localStorage.getItem(THEME_STORAGE_KEY));
    } catch {
      /* storage disabled — treat as no local; the seed is then this run only */
    }
    if (hasValidLocal) return; // explicit, valid local choice wins forever
    const server = coerceTheme(config.theme);
    setThemeState(server);
    storeTheme(server); // also overwrites the invalid cache, if any
  }, [config?.theme]);

  const setTheme = useCallback((id: ThemeId) => {
    setThemeState(id);
    storeTheme(id);
    // Best-effort: a failed persist still applies locally; it just won't follow devices.
    api.setTheme(id).catch(() => {});
  }, []);

  return <ThemeCtx.Provider value={{ theme, setTheme }}>{children}</ThemeCtx.Provider>;
}
