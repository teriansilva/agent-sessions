import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useConfig } from "../app/config";
import { api } from "../lib/api";
import { coerceAccent, DEFAULT_ACCENT, normalizeAccent } from "./accent";
import { ACCENT_STORAGE_KEY, applyAccent, readStoredAccent, storeAccent } from "./applyAccent";
import { AccentCtx } from "./accentStore";

/** Owns the active brand accent. Mirrors ThemeProvider (#172): the initial value is the
 *  device cache (already applied pre-paint by the inline boot script). On a brand-new device
 *  (no `tr-accent` in localStorage) we seed from the server once `/api/config` arrives so the
 *  user's accent follows across devices. Once a valid local choice exists, **the local cache
 *  wins** — a stale server value can't override it on every reload. `setAccent` applies +
 *  caches locally and best-effort persists to the server. Must render inside <ConfigProvider>. */
export function AccentProvider({ children }: { children: ReactNode }) {
  const config = useConfig();
  const [accent, setAccentState] = useState<string>(() => readStoredAccent());
  const reconciled = useRef(false);

  // Keep the inline custom properties in sync with state (covers StrictMode remounts + any
  // path where state was set without applying).
  useEffect(() => {
    applyAccent(accent);
  }, [accent]);

  // One-time seed from the server when no *valid* local choice exists yet.
  useEffect(() => {
    if (reconciled.current || !config?.accent) return;
    reconciled.current = true;
    let hasValidLocal = false;
    try {
      hasValidLocal = normalizeAccent(localStorage.getItem(ACCENT_STORAGE_KEY)) !== null;
    } catch {
      /* storage disabled — treat as no local; the seed is then this run only */
    }
    if (hasValidLocal) return; // explicit, valid local choice wins
    const server = coerceAccent(config.accent);
    setAccentState(server);
    storeAccent(server); // also overwrites an invalid cache, if any
  }, [config?.accent]);

  const setAccent = useCallback((hex: string) => {
    const next = coerceAccent(hex);
    setAccentState(next);
    storeAccent(next);
    // Best-effort: a failed persist still applies locally; it just won't follow devices.
    api.setAccent(next).catch(() => {});
  }, []);

  return <AccentCtx.Provider value={{ accent, setAccent }}>{children}</AccentCtx.Provider>;
}

export { DEFAULT_ACCENT };
