import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { api, setCsrfToken } from "../lib/api";
import type { AppConfig } from "../types/api";
import { ConfigCtx } from "./config";

/** Fetches /api/config once on mount and primes the CSRF token used by mutations.
 *  Children render immediately; consumers treat `null` as "not yet loaded". */
export function ConfigProvider({ children }: { children: ReactNode }) {
  const [config, setConfig] = useState<AppConfig | null>(null);
  useEffect(() => {
    let alive = true;
    api
      .config()
      .then((c) => {
        if (!alive) return;
        setCsrfToken(c.csrf);
        setConfig(c);
      })
      .catch(() => {
        /* unauthenticated / offline — sidebar still renders, mutations 403 until login */
      });
    return () => {
      alive = false;
    };
  }, []);
  return <ConfigCtx.Provider value={config}>{children}</ConfigCtx.Provider>;
}
