import { createContext, useContext } from "react";

/** Mutating-action plumbing for the overview canvas (#361 Phase 4). Group nodes are
 *  rendered by React Flow deep inside the canvas, so the sessions refetch owned by
 *  `useOverviewSessions` reaches them via context rather than node `data` — keeping
 *  `buildOverview` a pure, function-free transform. */
export interface OverviewActions {
  /** Re-fetch the session list (after a create-project mutation changed resolution). */
  refetchSessions: () => void;
}

export const OverviewActionsCtx = createContext<OverviewActions>({
  refetchSessions: () => {},
});

/** Inert default outside a provider so a unit test can mount a node in isolation. */
export function useOverviewActions(): OverviewActions {
  return useContext(OverviewActionsCtx);
}
