import { createContext, useContext } from "react";
import type { AppConfig } from "../types/api";

/** SPA bootstrap config (CSRF + new-session engines). `null` until /api/config loads. */
export const ConfigCtx = createContext<AppConfig | null>(null);

export function useConfig(): AppConfig | null {
  return useContext(ConfigCtx);
}

/** Refetch /api/config and update every `useConfig()` consumer (Hermes #367): settings
 *  that flip server-derived gating (e.g. ai_review.configured → the sidebar's Review
 *  now/exclude controls) call this so the UI updates without a reload. */
export const ConfigRefreshCtx = createContext<() => void>(() => {});

/** The same refetch, AWAITABLE (#1407): resolves `true` once THIS read is applied, `false` when
 *  it failed or a newer read superseded it — for a save that must not announce success while the
 *  app still reads the old config. A separate context so fire-and-forget callers are untouched. */
export const ConfigReloadCtx = createContext<() => Promise<boolean>>(async () => true);

export function useConfigReload(): () => Promise<boolean> {
  return useContext(ConfigReloadCtx);
}

export function useConfigRefresh(): () => void {
  return useContext(ConfigRefreshCtx);
}
