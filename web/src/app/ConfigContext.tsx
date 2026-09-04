import { useCallback, useEffect, useRef, useState } from "react";
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
  /** WHICH READ THIS IS (#900 review 5, finding 8).
   *
   *  Every response was applied, in whatever order it arrived. Two saves and two refreshes is
   *  not an exotic sequence — each save triggers one — and an OLDER response landing last rolled
   *  the whole config back to the state before the newer save. In the playbook editor that is
   *  visible and expensive: the panel re-seeds from config when it is not dirty, and the newer
   *  save had just cleared `dirty`, so the editor showed the block the operator had already
   *  replaced and the next save persisted it.
   *
   *  A monotonic ticket, compared at resolution — the same idiom the mission console's row reads
   *  use. Refs rather than state: the value has to be readable from inside a promise that was
   *  created before the newer one existed. */
  const issued = useRef(0);
  const load = useCallback(() => {
    const tk = ++issued.current;
    api
      .config()
      .then((c) => {
        // ONLY THE LATEST *ISSUED* READ IS APPLIED (#900 review 7, finding 8).
        //
        // Comparing against the latest APPLIED ticket answered a different question — "is this
        // the newest response so far" — and that let a failure roll the config back: request B
        // is issued after a save, B fails, and A (issued BEFORE the save, so carrying pre-save
        // values) resolves afterwards and is applied. The panel then shows the block the
        // operator had already replaced, and re-seeds from it because the save had cleared
        // `dirty`.
        //
        // A response that a newer request has superseded describes a moment we have already
        // asked to move past, whether or not that newer request succeeded. Keeping what we had
        // is the honest outcome; the next refresh — every save issues one — resolves it.
        if (tk !== issued.current) return;
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
