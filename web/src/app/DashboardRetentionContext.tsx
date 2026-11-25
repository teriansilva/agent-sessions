import { useCallback, useEffect, useMemo, useRef } from "react";
import type { ReactNode } from "react";

import { useConfig } from "./config";
import {
  DashboardRetentionCtx,
  type DashboardRetention,
} from "./dashboardRetentionStore";
import { scopeKeyOf } from "./overviewSessionsStore";

/** Retains the dashboard's last successful reads in shell-owned memory (#1223).
 *
 *  Mounted ABOVE `RouterProvider` for the reason `OverviewSessionsProvider` is (#1007): the
 *  dashboard route is lazy-loaded inside `<Routes>`, so every navigation away unmounted its five
 *  reads and coming back repainted skeletons over data the operator saw a moment ago.
 *
 *  DATA ONLY. Polling, cancellation and the "refreshing" state stay with the route — its unmount is
 *  what stops the polls. The values live in a ref, not state: they are read once, when a source
 *  mounts, so a poll landing must not re-render the whole app.
 *
 *  Nothing here is persisted: the map dies with the page, and nothing survives a reload or
 *  logout. This file exports a component and nothing else, which keeps Fast Refresh working. */
export function DashboardRetentionProvider({
  children,
}: {
  children: ReactNode;
}) {
  const cfg = useConfig();
  const scopeKey = cfg ? scopeKeyOf(cfg) : null;
  const values = useRef(new Map<string, { value: unknown; scope: string }>());
  const scopeRef = useRef(scopeKey);
  useEffect(() => {
    scopeRef.current = scopeKey;
  }, [scopeKey]);

  // Compared against THIS render's scope — a mount in the same render as a scope change must not
  // be handed the old scope's rows because the ref has not caught up yet.
  const read = useCallback(
    <T,>(key: string): T | undefined => {
      const hit = values.current.get(key);
      // Another scope's rows are wrong data, not stale data: never handed out (#1007).
      return hit && scopeKey !== null && hit.scope === scopeKey
        ? (hit.value as T)
        : undefined;
    },
    [scopeKey],
  );

  const write = useCallback(
    <T,>(key: string, value: T, forScope: string | null) => {
      const now = scopeRef.current;
      // Read under a known scope that has since moved: it describes a boundary no longer in
      // effect, so it is not retained under the new one.
      if (forScope !== null && forScope !== now) return;
      // Read before config answered: the server applied its one real scope, which is the one
      // known now. Still unknown → nothing to stamp it with, so it is not retained.
      if (now === null) return;
      values.current.set(key, { value, scope: now });
    },
    [],
  );

  const value = useMemo<DashboardRetention>(
    () => ({ scopeKey, read, write }),
    [scopeKey, read, write],
  );
  return (
    <DashboardRetentionCtx.Provider value={value}>
      {children}
    </DashboardRetentionCtx.Provider>
  );
}
