import { createContext, useContext } from "react";

/** Shared Session Overview view-state (#144): expanded clusters + excluded projects. Lives
 *  at the app level (see OverviewPrefsContext) so a Settings save and the canvas read/write
 *  the SAME state — a change in one is visible in the other immediately, without a reload. */
export interface OverviewPrefs {
  expanded: Set<string>;
  excluded: Set<string>;
  /** Per-cwd custom display names (#148). */
  projectNames: Record<string, string>;
  toggle: (cwd: string) => void;
  expandAll: (cwds: string[]) => void;
  collapseAll: () => void;
  setExcluded: (cwds: string[]) => void;
  /** Set (or, with an empty/blank name, clear) a project's custom name. */
  setProjectName: (cwd: string, name: string) => void;
}

export const OverviewPrefsCtx = createContext<OverviewPrefs | null>(null);

/** Read the shared overview prefs. Falls back to inert defaults outside a provider (so a
 *  unit test can mount a consumer in isolation). */
export function useOverviewPrefs(): OverviewPrefs {
  return (
    useContext(OverviewPrefsCtx) ?? {
      expanded: new Set(),
      excluded: new Set(),
      projectNames: {},
      toggle: () => {},
      expandAll: () => {},
      collapseAll: () => {},
      setExcluded: () => {},
      setProjectName: () => {},
    }
  );
}
