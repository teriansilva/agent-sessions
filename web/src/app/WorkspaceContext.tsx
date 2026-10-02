import { type ReactNode, useCallback, useMemo, useState } from "react";
import {
  useWorkspace,
  type WindowSeed,
} from "../components/overview/useWorkspace";
import { WorkspaceCommandsCtx, WorkspaceCtx } from "./workspaceWindows";

/** Mounts the map's window workspace ABOVE the router (#936), deliberately: a provider inside
 *  the routed tree would be remounted by exactly the navigations it exists to survive.
 *
 *  The contract, the context object and the hooks live in `workspaceWindows.ts` — this file
 *  exports a component and nothing else, which is what keeps Fast Refresh working. */
export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const ws = useWorkspace();
  const [mapReady, setMapReadyState] = useState(false);
  // The last MEASUREMENT, which outlives the unmount that clears `mapReady`. `null` until a map
  // has been measured at all.
  const [hostable, setHostable] = useState<boolean | null>(null);
  // Same-value guard: the canvas calls this from a measurement effect that runs on every
  // ResizeObserver tick, and re-setting an identical boolean would re-render the whole shell
  // (sidebar included) on every one of them.
  const setMapReady = useCallback((ready: boolean | null) => {
    setMapReadyState((cur) => (cur === (ready ?? false) ? cur : (ready ?? false)));
    // `null` is the unmount signal, not a measurement: recording it would make every navigation
    // away from the map look like a map that cannot host one.
    if (ready !== null) setHostable((cur) => (cur === ready ? cur : ready));
  }, []);
  const { requestOpen } = ws;
  const openInMap = useCallback(
    (seed: WindowSeed) => {
      if (!mapReady) return false;
      requestOpen(seed);
      return true;
    },
    [mapReady, requestOpen],
  );
  const value = useMemo(
    () => ({ ...ws, mapReady, hostable, setMapReady, openInMap }),
    [ws, mapReady, hostable, setMapReady, openInMap],
  );
  // A COUNT, not the window array: this crosses the cap boundary on open/close, which is a
  // discrete operator action, and never on a drag frame.
  //
  // Before hydration the stored layout counts too. Reloading the new-session form gives the
  // provider a fresh start — `windows` is empty and the whole layout is still in `restorable` —
  // so a bare `windows.length` reads as "plenty of room", the form hands its launch to the map,
  // and restore then fills the cap before the queued open gets there (Hermes on #939, round 2).
  // This is a PRECHECK that avoids a pointless round trip; the drain's explicit rejection is what
  // guarantees the launch survives being wrong here.
  const hasRoom =
    ws.windows.length + (ws.hydrated ? 0 : ws.restorable.length) < ws.cap;
  // The narrow value the shell consumes. It deliberately does NOT spread `ws`: that object gets a
  // new identity on every window rect update, and the sidebar is a consumer — one context would
  // re-render the whole session list on every frame of a window drag.
  const commands = useMemo(
    () => ({ mapReady, hostable, hasRoom, requestOpen, openInMap }),
    [mapReady, hostable, hasRoom, requestOpen, openInMap],
  );
  return (
    <WorkspaceCtx.Provider value={value}>
      <WorkspaceCommandsCtx.Provider value={commands}>
        {children}
      </WorkspaceCommandsCtx.Provider>
    </WorkspaceCtx.Provider>
  );
}
