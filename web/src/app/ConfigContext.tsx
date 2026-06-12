import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { api, gotoChangePassword, setCsrfToken } from "../lib/api";
import type { AppConfig } from "../types/api";
import { ConfigCtx, ConfigRefreshCtx } from "./config";

/** Fetches /api/config once on mount and primes the CSRF token used by mutations.
 *  Children render immediately; consumers treat `null` as "not yet loaded".
 *  First-run forced password change (`must_change_password`) → route to the
 *  server-rendered /change-password before the (non-functional, 403-gated) app loads.
 *  Also provides a refetch via `ConfigRefreshCtx` (Hermes #367): settings panels call it
 *  when a save flips server-derived gating (e.g. ai_review.configured), so consumers like
 *  the sidebar's Review now/exclude controls update without a full reload. */
export function ConfigProvider({ children }: { children: ReactNode }) {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const load = useCallback(() => {
    api
      .config()
      .then((c) => {
        if (c.must_change_password) {
          gotoChangePassword();
          return;
        }
        setCsrfToken(c.csrf);
        setConfig(c);
      })
      .catch(() => {
        /* unauthenticated / offline — sidebar still renders, mutations 403 until login */
      });
  }, []);
  useEffect(() => {
    load();
  }, [load]);
  return (
    <ConfigRefreshCtx.Provider value={load}>
      <ConfigCtx.Provider value={config}>{children}</ConfigCtx.Provider>
    </ConfigRefreshCtx.Provider>
  );
}
