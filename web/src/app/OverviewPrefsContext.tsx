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
  const [mode, setModeState] = useState<"all" | "included">("all");
  const [included, setIncludedState] = useState<Set<string>>(new Set());
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
    // Server routes `projects_hidden` (or the legacy `overview_excluded`) to the same store;
    // we send the new key so old clients keep seeing the data on their next config load.
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
