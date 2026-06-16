import { type ReactNode, useEffect, useState } from "react";
import { api } from "../lib/api";
import type { GroupBy } from "../lib/overviewGraph";
import { useConfig } from "./config";
import { type OverviewPrefs, OverviewPrefsCtx } from "./overviewPrefs";

// Map clustering mode is device-local (#424 Phase 2), like the sidebar-collapse flag — it is
// NOT part of the server-synced per-user prefs, so it lives in localStorage only.
const GROUPBY_KEY = "tr-overview-groupby";
const readGroupBy = (): GroupBy => {
  const v = localStorage.getItem(GROUPBY_KEY);
  return v === "folder" || v === "agent" || v === "project" ? v : "project";
};

/** Provides the shared overview view-state (#144). Seeded from /api/config once, persisted
 *  per-user on every change (best-effort; local state always applies). */
export function OverviewPrefsProvider({ children }: { children: ReactNode }) {
  const config = useConfig();
  const [expanded, setExpandedState] = useState<Set<string>>(new Set());
  const [excluded, setExcludedState] = useState<Set<string>>(new Set());
  const [projectNames, setProjectNamesState] = useState<Record<string, string>>({});
  const [mode, setModeState] = useState<"all" | "included">("all");
  const [included, setIncludedState] = useState<Set<string>>(new Set());
  const [groupBy, setGroupByState] = useState<GroupBy>(readGroupBy);
  const [synced, setSynced] = useState(false);

  useEffect(() => {
    if (synced || !config) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setExpandedState(new Set(config.overview_expanded ?? []));
    // `projects_hidden` (#174) is the only hide-list key: the legacy `overview_excluded`
    // is retired (#357 Phase 2) — the server union-merges old on-disk values at startup.
    setExcludedState(new Set(config.projects_hidden ?? []));
    setProjectNamesState({ ...(config.project_names ?? {}) });
    setModeState(config.projects_mode === "included" ? "included" : "all"); // #335
    setIncludedState(new Set(config.projects_included ?? []));
    setSynced(true);
  }, [config, synced]);

  const persistExpanded = (next: Set<string>) => {
    setExpandedState(next);
    api.setPrefs({ overview_expanded: [...next] }).catch(() => {});
  };
  const persistHidden = (next: Set<string>) => {
    setExcludedState(next);
    api.setPrefs({ projects_hidden: [...next] }).catch(() => {});
  };
  const persistIncluded = (next: Set<string>) => {
    setIncludedState(next);
    api.setPrefs({ projects_included: [...next] }).catch(() => {});
  };
  // Mirror of the server `prefs.project_visible` resolver — mode-EXCLUSIVE so the map + Settings
  // agree with the server-filtered sidebar/facets (#335).
  const isVisible = (cwd: string) => (mode === "included" ? included.has(cwd) : !excluded.has(cwd));
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
    projectsMode: mode,
    includedProjects: included,
    isVisible,
    setProjectsMode: (m) => {
      setModeState(m);
      api.setPrefs({ projects_mode: m }).catch(() => {});
    },
    groupBy,
    setGroupBy: (g) => {
      setGroupByState(g);
      localStorage.setItem(GROUPBY_KEY, g);
    },
    // Route a show/hide toggle to the list the CURRENT mode consults (#335): the allowlist in
    // `included` mode, the denylist in `all` mode — never both, so they can't drift.
    setProjectVisible: (cwd, visible) => {
      if (mode === "included") {
        const next = new Set(included);
        if (visible) next.add(cwd);
        else next.delete(cwd);
        persistIncluded(next);
      } else {
        const next = new Set(excluded);
        if (visible) next.delete(cwd);
        else next.add(cwd);
        persistHidden(next);
      }
    },
  };

  return <OverviewPrefsCtx.Provider value={value}>{children}</OverviewPrefsCtx.Provider>;
}
