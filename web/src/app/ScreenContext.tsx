import { type ReactNode, useEffect, useLayoutEffect, useState } from "react";
import { MAP_PATH } from "../lib/routes";
import { useIsMobile } from "../lib/useIsMobile";
import { SCREEN_SHOWN_EVENT, ScreenCtx, type ScreenShownDetail } from "./screenControl";
import { ScreenExecutor, type ScreenDeps } from "./screenExecutor";
import { useWorkspaceCtx, type WorkspaceContext } from "./workspaceWindows";

/** What the executor needs of the data router. It sits ABOVE `<RouterProvider>`, where no
 *  router hook exists, so it drives the router object itself. */
export interface ScreenRouter {
  basename?: string;
  state: {
    location: { key: string; pathname: string };
    blockers: Map<string, { state: string }>;
  };
  navigate(to: string): Promise<void>;
  subscribe(fn: (state: ScreenRouter["state"]) => void): () => void;
}

const guarded = (state: ScreenRouter["state"]) =>
  [...state.blockers.values()].some((b) => b.state !== "unblocked");

/** Navigate, honouring the app's own unsaved-changes guards (`useBlocker`): a guarded page asks
 *  as it does for any link, and this resolves `held` if the Commander stays. */
async function navigate(router: ScreenRouter, to: string): Promise<"applied" | "held"> {
  const before = router.state.location.key;
  await router.navigate(to);
  if (!guarded(router.state)) return "applied";
  // The page is asking about unsaved changes: HQ's phone drawer and a phone's call panel cover
  // it (and make it inert), so they give way first, or the question could not be answered.
  const detail: ScreenShownDetail = { source: "typed" };
  window.dispatchEvent(new CustomEvent(SCREEN_SHOWN_EVENT, { detail }));
  return new Promise((resolve) => {
    const stop = router.subscribe((state) => {
      if (guarded(state)) return;
      stop();
      resolve(state.location.key === before ? "held" : "applied");
    });
  });
}

/** The router-relative path on screen (the app may live under a basename, #1735). */
function pathOf(router: ScreenRouter): string {
  const base = (router.basename ?? "").replace(/\/+$/, "");
  const { pathname } = router.state.location;
  return base && pathname.startsWith(base) ? pathname.slice(base.length) || "/" : pathname;
}

/** Keyboard focus follows what opened: the session's map window, or the page. After the frame
 *  the route rendered in, and after a closing drawer has handed focus back to its trigger. */
function focusShown(windowKey: string | null): void {
  setTimeout(() => {
    const el = windowKey
      ? document.querySelector(`[data-session-window="${CSS.escape(windowKey)}"]`)
      : document.querySelector("main");
    if (!(el instanceof HTMLElement)) return;
    if (!el.hasAttribute("tabindex")) el.setAttribute("tabindex", "-1");
    el.focus({ preventScroll: true });
  }, 60);
}

/** The app, as the executor reads it: the router, the workspace and the viewport of one render. */
function depsOf(router: ScreenRouter, ws: WorkspaceContext, isMobile: boolean): ScreenDeps {
  return {
    navigate: (to) => navigate(router, to),
    onMap: () => pathOf(router) === MAP_PATH,
    mapCapable: () => !isMobile && ws.hostable !== false,
    workspace: () => ws,
    dispatch: (action) => ws.command(action),
    shown: (source, windowKey) => {
      const detail: ScreenShownDetail = { source };
      window.dispatchEvent(new CustomEvent(SCREEN_SHOWN_EVENT, { detail }));
      focusShown(windowKey);
    },
  };
}

/** Mounts HQ's ONE screen executor ABOVE the router, beside the workspace (#2102): a directive
 *  that navigates unmounts the view it came from, and the ones queued behind it must survive
 *  that. Both the typed stream and the call socket hand their directives to it. */
export function ScreenProvider({ router, children }: { router: ScreenRouter; children: ReactNode }) {
  const ws = useWorkspaceCtx();
  const isMobile = useIsMobile();
  const [executor] = useState(() => new ScreenExecutor(depsOf(router, ws, isMobile)));
  // Rebound before any passive effect (the settle below), so every decision — an event's, a
  // settle's — reads the workspace and the viewport of the render it follows.
  useLayoutEffect(() => {
    executor.bind(depsOf(router, ws, isMobile));
  });
  // The workspace is the authority: it answers each command by id (`acks`), and every render
  // is where the executor reads those answers. Runs after the rebind above, in the same commit.
  const { acks } = ws;
  useEffect(() => {
    executor.settle();
  }, [executor, acks]);
  return <ScreenCtx.Provider value={executor}>{children}</ScreenCtx.Provider>;
}
