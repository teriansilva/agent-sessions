import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useConfig } from "../app/config";
import { api } from "../lib/api";
import { applyTheme, readStoredTheme, storeTheme } from "./applyTheme";
import { coerceTheme, type ThemeId } from "./themes";
import { ThemeCtx } from "./themeStore";

/** Owns the active theme. Initial value is the device cache (already applied pre-paint by
 *  the inline boot script); once /api/config arrives we reconcile to the server value once
 *  (per-user, so the choice follows the user to a new device). `setTheme` applies + caches
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

  // One-time reconcile to the server's stored theme when config loads.
  useEffect(() => {
    if (reconciled.current || !config?.theme) return;
    reconciled.current = true;
    const server = coerceTheme(config.theme);
    setThemeState((prev) => {
      if (server !== prev) storeTheme(server);
      return server;
    });
  }, [config?.theme]);

  const setTheme = useCallback((id: ThemeId) => {
    setThemeState(id);
    storeTheme(id);
    // Best-effort: a failed persist still applies locally; it just won't follow devices.
    api.setTheme(id).catch(() => {});
  }, []);

  return <ThemeCtx.Provider value={{ theme, setTheme }}>{children}</ThemeCtx.Provider>;
}
