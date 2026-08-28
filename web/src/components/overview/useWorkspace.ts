import { useCallback, useEffect, useMemo, useReducer } from "react";
import type { TermRole } from "../../lib/termSocket";
import type { Session } from "../../types/api";
import {
  canOpen,
  cascadeRect,
  type Point,
  type Rect,
  type Size,
  WINDOW_CAP,
} from "./workspace";

/** One open window. `key` is the engine-qualified session id (`engine:uuid`) — the app's own
 *  identity for a session, and the dedupe key that keeps a session to a single window. */
export interface WorkspaceWindow {
  key: string;
  engine: string;
  id: string;
  /** The chrome title. The window's record is the single source of truth for it: it starts as
   *  the name at open time and `syncTitles` keeps it current, so a rename reaches an open
   *  window and a session that later leaves the map keeps the last name it had. */
  title: string;
  /** The size and position the OPERATOR chose. Fitted to the overlay box at RENDER time rather
   *  than rewritten here, so a map that shrinks and grows again returns the window to the layout
   *  it had — nothing overwrote it on the way down. */
  rect: Rect;
  /** Stacking order; the highest is the focused window. */
  z: number;
  /** The pane's own #184/#293 verdict, mirrored into the chrome. Starts optimistic ("owner"),
   *  exactly as the pane does, and corrects itself when the server's role frame lands. */
  role: TermRole;
}

/** How long the "already open" flash on a duplicate open lasts (matches the CSS animation). */
const FLASH_MS = 900;
/** How long the cap notice stays up. */
const NOTICE_MS = 4000;

interface State {
  windows: WorkspaceWindow[];
  focusedKey: string | null;
  flashKey: string | null;
  notice: string | null;
}

type Action =
  | { type: "open"; session: Session; anchor: Point | null; bounds: Size }
  | { type: "focus"; key: string }
  | { type: "close"; key: string }
  | { type: "closeAll" }
  | { type: "rect"; key: string; rect: Rect }
  | { type: "role"; key: string; role: TermRole }
  | { type: "titles"; titles: Map<string, string> }
  | { type: "clearFlash"; key: string }
  | { type: "clearNotice" };

const INITIAL: State = {
  windows: [],
  focusedKey: null,
  flashKey: null,
  notice: null,
};

const topZ = (windows: WorkspaceWindow[]): number =>
  windows.reduce((m, w) => Math.max(m, w.z), 0);

function raise(state: State, key: string): State {
  const w = state.windows.find((x) => x.key === key);
  if (!w) return state;
  // Already on top → keep the SAME window array, so pressing inside the focused window does not
  // hand the mounted pane a new object to re-render against.
  if (w.z === topZ(state.windows)) {
    return state.focusedKey === key ? state : { ...state, focusedKey: key };
  }
  const top = topZ(state.windows) + 1;
  return {
    ...state,
    focusedKey: key,
    windows: state.windows.map((x) => (x.key === key ? { ...x, z: top } : x)),
  };
}

/** The whole workspace transition, in one pure function.
 *
 *  This is a reducer rather than a set of `setState` calls **because the outcome of an open is
 *  part of the transition**. The first cut decided "opened / focused / capped" by mutating a
 *  holder object inside a functional updater and reading it afterwards — which React does not
 *  promise: an updater may be deferred, batched, or (under StrictMode) invoked twice, so the
 *  code after `setWindows` could act on a decision that had not been made, or on one made
 *  twice. A capped open then skipped its notice and focused a key with no window behind it.
 *  Here the notice and the flash are decided in the same atomic step as the window list. */
function reduce(state: State, action: Action): State {
  switch (action.type) {
    case "open": {
      const key = action.session.id;
      // Decision 2 (#208): one window per session, and this is not tidiness. Ownership is keyed
      // on (fp, tab_id), so a second window on the same session IN THE SAME TAB would also be
      // told `owner` — two owners driving one pty's width. Refused here, before a socket exists.
      if (state.windows.some((w) => w.key === key)) {
        // Say WHERE it went: without the flash, clicking an already-open chip looks like a
        // no-op when its window is behind another one.
        return { ...raise(state, key), flashKey: key };
      }
      if (!canOpen(state.windows.length)) {
        return {
          ...state,
          notice: `${WINDOW_CAP} windows is the limit — close one before opening another.`,
        };
      }
      const s = action.session;
      const win: WorkspaceWindow = {
        key,
        engine: s.engine,
        id: s.uuid,
        title: s.title || s.short_uuid,
        rect: cascadeRect(action.anchor, state.windows.length, action.bounds),
        z: topZ(state.windows) + 1,
        role: "owner",
      };
      return {
        ...state,
        windows: [...state.windows, win],
        focusedKey: key,
        flashKey: null,
      };
    }
    case "focus":
      return raise(state, action.key);
    case "close": {
      // Unmounting the window unmounts <Terminal>, which closes its socket and detaches its
      // document-level listeners. There is nothing else to tear down here.
      const windows = state.windows.filter((w) => w.key !== action.key);
      if (windows.length === state.windows.length) return state;
      return {
        ...state,
        windows,
        focusedKey: state.focusedKey === action.key ? null : state.focusedKey,
        flashKey: state.flashKey === action.key ? null : state.flashKey,
      };
    }
    case "closeAll":
      return state.windows.length
        ? { ...state, windows: [], focusedKey: null, flashKey: null }
        : state;
    case "rect":
      return {
        ...state,
        windows: state.windows.map((w) =>
          w.key === action.key ? { ...w, rect: action.rect } : w,
        ),
      };
    case "role": {
      let changed = false;
      const windows = state.windows.map((w) => {
        if (w.key !== action.key || w.role === action.role) return w;
        changed = true;
        return { ...w, role: action.role };
      });
      return changed ? { ...state, windows } : state;
    }
    case "titles": {
      let changed = false;
      const windows = state.windows.map((w) => {
        const live = action.titles.get(w.key);
        // Absent from the map (filtered, archived, another layout) → keep what we have. A map
        // filter never closes a window, and it must not un-name one either.
        if (!live || live === w.title) return w;
        changed = true;
        return { ...w, title: live };
      });
      return changed ? { ...state, windows } : state;
    }
    case "clearFlash":
      return state.flashKey === action.key ? { ...state, flashKey: null } : state;
    case "clearNotice":
      return state.notice ? { ...state, notice: null } : state;
  }
}

export interface Workspace extends State {
  /** Open a session, or focus and flash the window it already has. The decision (open / focus /
   *  cap) is made inside the transition — callers read the resulting state, never a return
   *  value, because only the reducer can see the current window list. */
  open: (session: Session, anchor: Point | null, bounds: Size) => void;
  focus: (key: string) => void;
  close: (key: string) => void;
  closeAll: () => void;
  setRect: (key: string, rect: Rect) => void;
  setRole: (key: string, role: TermRole) => void;
  /** Adopt renames for open windows. */
  syncTitles: (titles: Map<string, string>) => void;
}

/** The workspace's state machine (#208).
 *
 *  Deliberately NOT persisted: windows live for the life of the `/overview` mount. Persisting
 *  the layout needs a server-side prefs key (the `/api/prefs` allowlist 422s an unknown one),
 *  which is a follow-up rather than part of this slice. */
export function useWorkspace(): Workspace {
  const [state, dispatch] = useReducer(reduce, INITIAL);

  // The transient markers expire in EFFECTS keyed on the value, not in the action that set them:
  // a timer armed inside a dispatch would be armed twice under StrictMode's double invocation,
  // and would leak if the window closed before it fired.
  const { flashKey, notice } = state;
  useEffect(() => {
    if (!flashKey) return;
    const t = setTimeout(
      () => dispatch({ type: "clearFlash", key: flashKey }),
      FLASH_MS,
    );
    return () => clearTimeout(t);
  }, [flashKey]);
  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => dispatch({ type: "clearNotice" }), NOTICE_MS);
    return () => clearTimeout(t);
  }, [notice]);

  const open = useCallback(
    (session: Session, anchor: Point | null, bounds: Size) =>
      dispatch({ type: "open", session, anchor, bounds }),
    [],
  );
  const focus = useCallback((key: string) => dispatch({ type: "focus", key }), []);
  const close = useCallback((key: string) => dispatch({ type: "close", key }), []);
  const closeAll = useCallback(() => dispatch({ type: "closeAll" }), []);
  const setRect = useCallback(
    (key: string, rect: Rect) => dispatch({ type: "rect", key, rect }),
    [],
  );
  const setRole = useCallback(
    (key: string, role: TermRole) => dispatch({ type: "role", key, role }),
    [],
  );
  const syncTitles = useCallback(
    (titles: Map<string, string>) => dispatch({ type: "titles", titles }),
    [],
  );
  // Memoized, and every action is a stable `useCallback`: the canvas destructures the actions it
  // needs into its own dependency arrays, so a map re-render (a chip drag, a refetch) does not
  // hand eight mounted terminals a fresh set of props.
  return useMemo(
    () => ({
      ...state,
      open,
      focus,
      close,
      closeAll,
      setRect,
      setRole,
      syncTitles,
    }),
    [state, open, focus, close, closeAll, setRect, setRole, syncTitles],
  );
}
