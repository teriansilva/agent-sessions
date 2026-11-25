import { createContext, useContext } from "react";

/** The dashboard's last successful reads, retained above the router (#1223).
 *
 *  Same contract as the map's retention (#1007, `docs/invariants/dashboard-retention.md`): it is a
 *  RENDERING optimisation, never a request cache. A retained value decides what is painted first
 *  when the dashboard mounts; it never decides whether to fetch — every entry revalidates.
 *
 *  Values are stamped with the hard scope (`scopeKeyOf`) the server applied when they were read,
 *  and a value from a scope no longer in effect is never handed out. `null` scope means config has
 *  not loaded yet: the server still applied its one real scope, so a read committed while config
 *  is unknown is stamped with the scope known at commit time, and dropped if that is still
 *  unknown. */
export interface DashboardRetention {
  /** The hard scope in force now, or null before `/api/config` has answered. */
  scopeKey: string | null;
  /** The retained value for `key`, only if it was read under the current scope. */
  read: <T>(key: string) => T | undefined;
  /** Retain `value` for `key`, read under `forScope`. Dropped when the scope moved underneath. */
  write: <T>(key: string, value: T, forScope: string | null) => void;
}

export const DashboardRetentionCtx = createContext<DashboardRetention>({
  scopeKey: null,
  read: () => undefined,
  write: () => {},
});

export function useDashboardRetention(): DashboardRetention {
  return useContext(DashboardRetentionCtx);
}
