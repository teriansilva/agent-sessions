import { createContext, useContext } from "react";
import type { AppConfig } from "../types/api";

/** SPA bootstrap config (CSRF + new-session engines). `null` until /api/config loads. */
export const ConfigCtx = createContext<AppConfig | null>(null);

export function useConfig(): AppConfig | null {
  return useContext(ConfigCtx);
}
