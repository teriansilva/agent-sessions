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

export function useConfigRefresh(): () => void {
  return useContext(ConfigRefreshCtx);
}
