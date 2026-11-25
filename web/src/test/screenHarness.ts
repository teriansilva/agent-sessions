/** A test harness for HQ's screen executor: the REAL executor against the REAL workspace reducer,
 *  with React's part made explicit (#2102). Not a mock of either.
 *
 *  - Actions go through the workspace's own reducer in dispatch order (`actual`).
 *  - The executor reads what was last RENDERED (`rendered`), which lags `actual` until `commit`.
 *  - `canvas` is what the mounted, measured map does in its effects, to quiescence: restore the
 *    stored layout, drain the queue, hand a lone refusal back to its full-screen route (several
 *    stay listed). It runs only while the map is the route on screen.
 *
 *  So "delivered before React commits" is literal here: `execute` several directives, await
 *  `idle`, and nothing has been rendered in between. */
import { matchRoute } from "../components/ask/askMatches";
import {
  initWorkspace,
  workspaceReducer,
  type WorkspaceAction,
  type WorkspaceState,
} from "../components/overview/useWorkspace";
import { CAP_KEY, WORKSPACE_KEY, type StoredWindow } from "../components/overview/windowStore";
import { MAP_PATH } from "../lib/routes";
import { ScreenExecutor, type ScreenDeps } from "../app/screenExecutor";

export const BOUNDS = { w: 1600, h: 1000 };
export const key = (n: number | string) => `claude:s${n}`;
export const id = (n: number) => `sd_${String(n).padStart(16, "0")}`;
export const stored = (n: number | string, minimized = false): StoredWindow => ({
  key: key(n),
  engine: "claude",
  id: `s${n}`,
  title: `Session ${n}`,
  x: 40,
  y: 60,
  w: 640,
  h: 420,
  z: 1,
  ...(minimized ? { minimized: true } : {}),
});

export interface HarnessOptions {
  /** The layout in storage when the page loads. */
  layout?: StoredWindow[];
  path?: string;
  cap?: number;
  /** Can a map host a window on this device? `false`: a phone. */
  capable?: boolean;
  /** A reload in the same tab: the tab's session storage is kept. */
  sameTab?: boolean;
}

export function harness(opts: HarnessOptions = {}) {
  localStorage.clear();
  if (!opts.sameTab) sessionStorage.clear();
  if (opts.layout?.length) localStorage.setItem(WORKSPACE_KEY, JSON.stringify(opts.layout));
  if (opts.cap !== undefined) localStorage.setItem(CAP_KEY, String(opts.cap));
  const capable = opts.capable ?? true;
  let actual: WorkspaceState = initWorkspace();
  let rendered = actual;
  const io = {
    path: opts.path ?? "/dashboard",
    navigations: [] as string[],
    /** An unsaved-changes guard is up and the Commander stays. */
    held: false,
    shown: [] as (string | null)[],
    dispatched: [] as WorkspaceAction[],
  };
  const dispatch = (a: WorkspaceAction) => {
    io.dispatched.push(a);
    actual = workspaceReducer(actual, a);
  };
  const deps: ScreenDeps = {
    navigate: async (to) => {
      io.navigations.push(to);
      if (io.held) return "held";
      io.path = to;
      return "applied";
    },
    onMap: () => io.path === MAP_PATH,
    mapCapable: () => capable,
    workspace: () => rendered,
    dispatch,
    shown: (_source, window) => void io.shown.push(window),
  };
  const ex = new ScreenExecutor(deps);
  /** React renders: the executor now reads this state and its acknowledgements. */
  const commit = () => {
    rendered = actual;
    ex.settle();
  };
  /** Render, and let the map's effects run until nothing more changes. */
  const canvas = () => {
    for (let i = 0; i < 16; i += 1) {
      commit();
      const s = rendered;
      if (io.path !== MAP_PATH) return;
      if (!s.hydrated && capable) dispatch({ type: "restore", bounds: BOUNDS });
      else if (s.queue.length)
        dispatch({ type: "drain", anchors: new Map(), bounds: BOUNDS, canHost: capable });
      else if (s.rejected.length === 1) {
        io.path = matchRoute(s.rejected[0].seed.key);
        dispatch({ type: "clearRejected" });
      } else if (s.rejected.length) dispatch({ type: "refuseRejected" });
      if (actual === s) return;
    }
    throw new Error("the canvas did not settle");
  };
  /** Let the executor apply everything it has taken, and only then render and run the map's
   *  effects. Nothing is rendered while it works — except where it WAITS for the map, which it
   *  does before leaving it: there, and only there, the map gets to answer first. */
  const flush = async () => {
    let idle = false;
    void ex.idle().then(() => (idle = true));
    for (let i = 0; !idle; i += 1) {
      if (i > 10_000) throw new Error("the executor never became idle");
      await Promise.resolve();
      if (!idle && ex.waitingForMap) canvas();
    }
    canvas();
  };
  const outcome = (n: number) => ex.snapshot().get(id(n))?.outcome;
  /** The windows as the workspace holds them: mounted ones, or the stored layout before the map
   *  has mounted. `[key, minimized]`, sorted. */
  const windows = (): [string, boolean][] =>
    (actual.hydrated
      ? actual.windows.map((w): [string, boolean] => [w.actionKey, Boolean(w.minimized)])
      : actual.restorable.map((w): [string, boolean] => [w.key, Boolean(w.minimized)])
    ).sort((a, b) => a[0].localeCompare(b[0]));
  return { ex, io, dispatch, commit, canvas, flush, outcome, windows, state: () => actual };
}

export type Harness = ReturnType<typeof harness>;
