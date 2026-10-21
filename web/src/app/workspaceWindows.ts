import { createContext, useContext } from "react";
import type {
  WindowSeed,
  Workspace,
} from "../components/overview/useWorkspace";

/** The map's window workspace, shared at the app level (#936).
 *
 *  Split from `WorkspaceContext.tsx` for the same reason `overviewPrefs.ts` is split from
 *  `OverviewPrefsContext.tsx`: a module that exports both a component and a hook defeats Fast
 *  Refresh, so the provider lives alone in the `.tsx` and everything else lives here.
 *
 *  #208 kept this state inside `OverviewCanvasInner`, which made the workspace a dead end: it
 *  died with the `/overview` mount, and nothing outside the map could reach it. Both of the
 *  things hoisting it fixes are consequences of the move rather than features built on top:
 *
 *  - **Windows survive leaving the map.** Not because anything saves and reloads them on
 *    navigation, but because nothing unmounts them. The `<Terminal>` mounts still come and go
 *    with the route — an unmounted map must not hold live sockets open behind Settings — and
 *    coming back re-opens each window at the rect its record still carries.
 *  - **The sidebar and the session pane can ask for a window**, because the state is now above
 *    both surfaces and the router.
 *
 *  `mapReady` is the one thing that flows the other way. The canvas publishes it when it is
 *  mounted, measured, and big enough to host a window at its floor; it clears on unmount. The
 *  surfaces outside the map read it to decide whether to open a window or navigate — one
 *  boolean, rather than each of them re-deriving `useIsMobile` + `canHostWindow` against a box
 *  they cannot see and could not keep in step with. */
export interface WorkspaceContext extends Workspace {
  /** True while a mounted map can host a window. Only then does a sidebar row open one. */
  mapReady: boolean;
  /** The LAST measurement, which outlives the map's unmount — see `WorkspaceCommands.hostable`. */
  hostable: boolean | null;
  /** Canvas → provider. `true`/`false` is a measurement (it updates `hostable` too); `null` means
   *  "the map is unmounting", which clears `mapReady` and deliberately leaves `hostable` alone —
   *  otherwise every navigation away would record the map as unable to host. */
  setMapReady: (ready: boolean | null) => void;
  /** Convenience for the surfaces outside the map: queue a request and report whether the map
   *  will actually pick it up. `false` means the caller should navigate as it always did. */
  openInMap: (seed: WindowSeed) => boolean;
}

/** What the surfaces OUTSIDE the map need — and nothing else.
 *
 *  Two contexts, and the split is a performance boundary rather than tidiness. The full context
 *  value necessarily changes on every `rect` dispatch, i.e. on every pointer move of a window
 *  drag; the sidebar is a consumer, so a single context would re-render the entire session list
 *  ~60 times a second while a window is being dragged. Stable callbacks do not help — it is the
 *  VALUE's identity that propagates.
 *
 *  This one changes only when `mapReady` flips (map mount / unmount / crossing the host floor),
 *  because every member of it is a stable `useCallback`. */
export interface WorkspaceCommands {
  mapReady: boolean;
  /** Was the map able to host a window the last time one was measured? Survives the map's
   *  unmount, which `mapReady` cannot — and that is the point: the session pane has to decide
   *  whether to OFFER "To map" while the map is not mounted, and the viewport width alone does
   *  not answer it (a 1920×400 desktop window is wide enough and far too short). `null` means
   *  "never measured", which is treated as available: the drain has a fallback, and refusing an
   *  action on a map nobody has opened yet would be worse than a recoverable round trip. */
  hostable: boolean | null;
  /** Is there room under the operator's cap right now? Independent of `mapReady`, deliberately:
   *  the new-session form asks this while the map is unmounted, so a mount-scoped answer would
   *  always be "no" and the hand-back would never happen. */
  hasRoom: boolean;
  /** How many MORE windows the cap admits right now (same arithmetic as `hasRoom`), so a surface
   *  opening several at once (Ask's "Open all in map") can ask for no more than fit — the drain
   *  hands a refused request back to the full-screen route, which is right for one and wrong
   *  for the tail of a batch. */
  room: number;
  /** The sessions that already have a window (by `actionKey`) — including, before hydration, the
   *  stored layout still waiting to be restored. Re-opening one focuses it and costs no room.
   *  Identity changes only when the SET of open sessions does, never on a drag. */
  openKeys: ReadonlySet<string>;
  requestOpen: Workspace["requestOpen"];
  openInMap: (seed: WindowSeed) => boolean;
}

/** The map's route. Exported so the two surfaces that navigate *to* the map — the sidebar's
 *  "+ New session" hand-back and the pane's "To map" chip — spell it once rather than twice. */
export const MAP_PATH = "/overview";

export const WorkspaceCtx = createContext<WorkspaceContext | null>(null);
export const WorkspaceCommandsCtx = createContext<WorkspaceCommands | null>(null);

/** The workspace. Throws outside the provider rather than handing back a silent no-op: the map
 *  itself cannot do its job without it, and a canvas that thinks it published `mapReady` and did
 *  not is the failure mode this feature is least able to explain to the operator. */
export function useWorkspaceCtx(): WorkspaceContext {
  const v = useContext(WorkspaceCtx);
  if (!v)
    throw new Error("useWorkspaceCtx must be used inside <WorkspaceProvider>");
  return v;
}

/** The workspace, or `null` outside the provider.
 *
 *  The surfaces that only *offer* window mode — the sidebar row, the pane's "To map" chip, the
 *  new-session landing — use this one and fall back to navigating. Two reasons, and the second
 *  is the real one:
 *  - they are rendered standalone in unit tests, and a throwing hook would make every one of
 *    those tests declare a provider it does not otherwise need;
 *  - "there is no map" and "the map cannot host a window" already have to produce the same
 *    behaviour (navigate), so collapsing them into one branch removes a state rather than
 *    hiding one. */
export function useMapWindows(): WorkspaceCommands | null {
  return useContext(WorkspaceCommandsCtx);
}
