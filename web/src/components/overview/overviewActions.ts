import { createContext, useContext } from "react";
import type { MenuAnchor } from "../sidebar/RowMenu";

/** Mutating-action plumbing for the overview canvas (#361 Phase 4). Group nodes are
 *  rendered by React Flow deep inside the canvas, so the sessions refetch owned by
 *  `useOverviewSessions` reaches them via context rather than node `data` — keeping
 *  `buildOverview` a pure, function-free transform. */
export interface OverviewActions {
  /** Re-fetch the session list (after a create-project mutation changed resolution). */
  refetchSessions: () => void;
  /** Open the map's session menu (#968) for a chip — under its ⋯ (`element`) or at a pointer
   *  (`point`). `opener` is where focus returns when the menu closes. */
  openSessionMenu: (
    sessionKey: string,
    anchor: MenuAnchor,
    opener: HTMLElement | null,
  ) => void;
}

export const OverviewActionsCtx = createContext<OverviewActions>({
  refetchSessions: () => {},
  openSessionMenu: () => {},
});

/** Inert default outside a provider so a unit test can mount a node in isolation. */
export function useOverviewActions(): OverviewActions {
  return useContext(OverviewActionsCtx);
}
