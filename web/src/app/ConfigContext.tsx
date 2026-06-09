import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { api, gotoChangePassword, setCsrfToken } from "../lib/api";
import type { AppConfig } from "../types/api";
import { ConfigCtx } from "./config";

/** Fetches /api/config once on mount and primes the CSRF token used by mutations.
 *  Children render immediately; consumers treat `null` as "not yet loaded".
 *  First-run forced password change (`must_change_password`) → route to the
 *  server-rendered /change-password before the (non-functional, 403-gated) app loads. */
export function ConfigProvider({ children }: { children: ReactNode }) {
  const [config, setConfig] = useState<AppConfig | null>(null);
  useEffect(() => {
    let alive = true;
    api
      .config()
      .then((c) => {
        if (!alive) return;
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
    return () => {
      alive = false;
    };
  }, []);
  return <ConfigCtx.Provider value={config}>{children}</ConfigCtx.Provider>;
}
