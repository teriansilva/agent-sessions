import { type ReactNode, useEffect, useState } from "react";
import { api } from "../lib/api";
import { useConfig } from "./config";
import { type OverviewPrefs, OverviewPrefsCtx } from "./overviewPrefs";

/** Provides the shared overview view-state (#144). Seeded from /api/config once, persisted
 *  per-user on every change (best-effort; local state always applies). */
export function OverviewPrefsProvider({ children }: { children: ReactNode }) {
  const config = useConfig();
  const [expanded, setExpandedState] = useState<Set<string>>(new Set());
  const [excluded, setExcludedState] = useState<Set<string>>(new Set());
  const [synced, setSynced] = useState(false);

  useEffect(() => {
    if (synced || !config) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setExpandedState(new Set(config.overview_expanded ?? []));
    setExcludedState(new Set(config.overview_excluded ?? []));
    setSynced(true);
  }, [config, synced]);

  const persistExpanded = (next: Set<string>) => {
    setExpandedState(next);
    api.setPrefs({ overview_expanded: [...next] }).catch(() => {});
  };
  const value: OverviewPrefs = {
    expanded,
    excluded,
    toggle: (cwd) => {
      const next = new Set(expanded);
      if (next.has(cwd)) next.delete(cwd);
      else next.add(cwd);
      persistExpanded(next);
    },
    expandAll: (cwds) => persistExpanded(new Set(cwds)),
    collapseAll: () => persistExpanded(new Set()),
    setExcluded: (cwds) => {
      const next = new Set(cwds);
      setExcludedState(next);
      api.setPrefs({ overview_excluded: [...next] }).catch(() => {});
    },
  };

  return <OverviewPrefsCtx.Provider value={value}>{children}</OverviewPrefsCtx.Provider>;
}
