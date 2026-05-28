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
  const [projectNames, setProjectNamesState] = useState<Record<string, string>>({});
  const [synced, setSynced] = useState(false);

  useEffect(() => {
    if (synced || !config) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setExpandedState(new Set(config.overview_expanded ?? []));
    // Prefer the new `projects_hidden` key (#174) when present; fall back to the legacy
    // `overview_excluded` so a transition install keeps its existing hides.
    setExcludedState(
      new Set(config.projects_hidden ?? config.overview_excluded ?? []),
    );
    setProjectNamesState({ ...(config.project_names ?? {}) });
    setSynced(true);
  }, [config, synced]);

  const persistExpanded = (next: Set<string>) => {
    setExpandedState(next);
    api.setPrefs({ overview_expanded: [...next] }).catch(() => {});
  };
  const persistHidden = (next: Set<string>) => {
    setExcludedState(next);
    // Server routes `projects_hidden` (or the legacy `overview_excluded`) to the same store;
    // we send the new key so old clients keep seeing the data on their next config load.
    api.setPrefs({ projects_hidden: [...next] }).catch(() => {});
  };
  const value: OverviewPrefs = {
    expanded,
    hiddenProjects: excluded, // same Set under both names (#174)
    excluded,
    projectNames,
    toggle: (cwd) => {
      const next = new Set(expanded);
      if (next.has(cwd)) next.delete(cwd);
      else next.add(cwd);
      persistExpanded(next);
    },
    expandAll: (cwds) => persistExpanded(new Set(cwds)),
    collapseAll: () => persistExpanded(new Set()),
    setExcluded: (cwds) => persistHidden(new Set(cwds)),
    setProjectHidden: (cwd, hidden) => {
      const next = new Set(excluded);
      if (hidden) next.add(cwd);
      else next.delete(cwd);
      persistHidden(next);
    },
    setProjectName: (cwd, name) => {
      const trimmed = name.trim();
      const next = { ...projectNames };
      if (trimmed) next[cwd] = trimmed;
      else delete next[cwd]; // blank clears the custom name
      setProjectNamesState(next);
      api.setPrefs({ project_names: next }).catch(() => {});
    },
  };

  return <OverviewPrefsCtx.Provider value={value}>{children}</OverviewPrefsCtx.Provider>;
}
