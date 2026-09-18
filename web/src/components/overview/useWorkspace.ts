import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";
import { isNewSessionPlaceholder } from "../../app/sessionsStore";
import type { TermRole } from "../../lib/termSocket";
import type { FreshSession } from "../../lib/termUrl";
import {
  loadWindowCap,
  loadWorkspace,
  rectOf,
  saveWindowCap,
  saveWorkspace,
  seedOf,
  type StoredWindow,
} from "./windowStore";
import {
  canOpen,
  cascadeRect,
  clampWindowCap,
  type Point,
  type Rect,
  type Size,
  WINDOW_CAP_MAX,
} from "./workspace";

/** The minimum a caller needs to know about a session to ask for a window on it.
 *
 *  Not `Session`: the three callers outside the map have three different amounts of information —
 *  the sidebar has a full row, the pane has a row that may be a 404, and the new-session landing
 *  has nothing but the id it just minted. A seed is the intersection, so none of them has to
 *  fabricate a `Session` to be allowed to ask. The map upgrades a seed to the live row when it
 *  has one. */
export interface WindowSeed {
  /** The engine-qualified session id (`engine:native`). */
  key: string;
  engine: string;
  /** The NATIVE id — what goes in the ws URL, not the qualified key. */
  id: string;
  title: string;
}

/** A queued request to open a window, made from outside the map (#936).
 *
 *  Callers outside the map cannot open a window themselves: `open` needs the chip's projected
 *  anchor and the overlay box, both of which only the mounted, measured canvas knows. So they
 *  queue, and the canvas drains. */
export interface WindowRequest {
  seed: WindowSeed;
  /** Fresh-launch params, when this request came from the new-session flow. Carried on the
   *  record for the life of the window and NEVER persisted — replaying a launch on reload is
   *  the one thing the storage layer must not do. */
  fresh?: FreshSession;
}

/** One open window. `key` is the engine-qualified session id (`engine:uuid`) — the app's own
 *  identity for a session, and the dedupe key that keeps a session to a single window. */
export interface WorkspaceWindow {
  key: string;
  /** The id the SERVER should act on, which after an opencode/codex/agy/kimi converge is the
   *  real id the engine minted rather than the `new-<uuid>` placeholder we launched under.
   *
   *  Two identities, kept apart on purpose — the #867/#127 split, applied to a window:
   *  - `key` is the **transport** identity. It is frozen for the life of the window, because it
   *    is the React key and the socket's key: re-keying it would remount `<Terminal>` and tear
   *    down the live socket of a session that had just started.
   *  - `actionKey` is what everything ELSE reads — the title sync, the tether's anchor, what
   *    gets persisted, and the "is this session already open?" test. Aiming any of those at a
   *    `new-` placeholder targets a session that does not exist. */
  actionKey: string;
  engine: string;
  id: string;
  /** The chrome title. The window's record is the single source of truth for it: it starts as
   *  the name at open time and `syncTitles` keeps it current from the MAP's list (which the
   *  map refreshes on its own revalidations — mutations, the reconcile-triggered refetch
   *  #1037, a route re-entry), so a rename the map has caught up with reaches an open window
   *  and a session that later leaves the map keeps the last name it had. */
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
  /** One-shot launch params for a window opened from the new-session flow (#936). `<Terminal>`
   *  freezes them itself (`freshRef`), so this is only ever read at mount. */
  fresh?: FreshSession;
}

/** How long the "already open" flash on a duplicate open lasts (matches the CSS animation). */
const FLASH_MS = 900;
/** How long the cap notice stays up. */
const NOTICE_MS = 4000;
/** How long after the last change the layout is written. A drag dispatches `rect` on every
 *  pointer move (`SessionWindow.startGesture`), so persisting the SETTLED rect is the same
 *  discipline that keeps `fit()` out of that path (#227/#349). */
const PERSIST_DEBOUNCE_MS = 400;

interface State {
  windows: WorkspaceWindow[];
  focusedKey: string | null;
  flashKey: string | null;
  notice: string | null;
  /** The operator's ceiling (#936). In state rather than read from storage at each decision, so
   *  the reducer stays pure and every transition is testable without a DOM. */
  cap: number;
  /** Requests queued from outside the map, drained by the canvas once it is measured. */
  pending: WindowRequest[];
  /** Requests the drain could NOT honour — the map cannot host a window, or the cap is full.
   *
   *  They are not discarded, because a caller decided this session should be on screen and a
   *  fresh one carries the cwd and bypass the operator just chose. The drain records the refusal
   *  as an explicit OUTCOME at the transition that made it (rather than leaving the caller to
   *  infer it from a notice), and the canvas hands the first one back to the full-screen route.
   *  Transient, never persisted (Hermes on #939, rounds 1 + 2). */
  rejected: WindowRequest[];
  /** The layout read from storage, waiting for a map that can host it. Held rather than applied
   *  at construction because the gates that decide whether a window may open at all —
   *  `canHostWindow`, the mobile breakpoint — are the canvas's to evaluate, and applying a
   *  desktop layout sight-unseen would mount eight terminals on a phone. */
  restorable: StoredWindow[];
  /** True once a restore has been attempted. The persistence effect writes only after this: a
   *  write before it would flush the empty initial state over a layout that had not been read
   *  back yet, which is the "my windows are gone" bug in its most annoying form — silent, and
   *  only visible on the NEXT reload. */
  hydrated: boolean;
}

type Action =
  | {
      type: "open";
      seed: WindowSeed;
      anchor: Point | null;
      bounds: Size;
      fresh?: FreshSession;
      /** An explicit rect (a restore); absent means cascade off the anchor. */
      rect?: Rect;
    }
  | { type: "focus"; key: string }
  | { type: "close"; key: string }
  | { type: "closeAll" }
  | { type: "rect"; key: string; rect: Rect }
  | { type: "role"; key: string; role: TermRole }
  | { type: "reconcile"; key: string; sid: string }
  | { type: "titles"; titles: Map<string, string> }
  | { type: "request"; req: WindowRequest }
  | {
      type: "drain";
      /** Chip anchors, projected by the canvas — the reducer cannot compute them. */
      anchors: Map<string, Point | null>;
      bounds: Size;
      /** Can the measured map host a window at all? `false` rejects the whole queue. */
      canHost: boolean;
    }
  | { type: "clearRejected" }
  | { type: "restore"; bounds: Size }
  | { type: "detach" }
  | { type: "cap"; cap: number }
  | { type: "clearFlash"; key: string }
  | { type: "clearNotice" };

/** Still mid-launch: the engine has not told us the id it minted. */
const isPlaceholderKey = (key: string): boolean => isNewSessionPlaceholder(key);

const topZ = (windows: WorkspaceWindow[]): number =>
  windows.reduce((m, w) => Math.max(m, w.z), 0);

/** Does any window already hold this session, under EITHER identity? A window launched under a
 *  `new-` placeholder answers to its real id the moment it reconciles, so a sidebar click on the
 *  converged session must focus it rather than open a second window on one pty. */
const holderOf = (
  windows: WorkspaceWindow[],
  key: string,
): WorkspaceWindow | undefined =>
  windows.find((w) => w.key === key || w.actionKey === key);

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

/** The open transition, factored out so `restore` can reuse it verbatim.
 *
 *  Restoring is opening — same dedupe, same cap, same clamping — and the moment those were two
 *  code paths one of them would drift. The only difference is where the rect comes from. */
function openInto(
  state: State,
  seed: WindowSeed,
  anchor: Point | null,
  bounds: Size,
  fresh?: FreshSession,
  rect?: Rect,
  /** The ceiling to enforce. Defaults to the operator's, but a RESTORE passes the hard maximum
   *  instead — see the `restore` case. */
  cap: number = state.cap,
): State {
  const key = seed.key;
  // Decision 2 (#208): one window per session, and this is not tidiness. Ownership is keyed
  // on (fp, tab_id), so a second window on the same session IN THE SAME TAB would also be
  // told `owner` — two owners driving one pty's width. Refused here, before a socket exists.
  const held = holderOf(state.windows, key);
  if (held) {
    // Say WHERE it went: without the flash, clicking an already-open row looks like a
    // no-op when its window is behind another one.
    return { ...raise(state, held.key), flashKey: held.key };
  }
  if (!canOpen(state.windows.length, cap)) {
    return {
      ...state,
      notice: `${cap} window${cap === 1 ? "" : "s"} is the limit — close one, or raise the limit in the toolbar.`,
    };
  }
  const win: WorkspaceWindow = {
    key,
    actionKey: key,
    engine: seed.engine,
    id: seed.id,
    title: seed.title,
    rect: rect ?? cascadeRect(anchor, state.windows.length, bounds),
    z: topZ(state.windows) + 1,
    role: "owner",
    fresh,
  };
  return {
    ...state,
    windows: [...state.windows, win],
    focusedKey: key,
    flashKey: null,
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
    case "open":
      return openInto(
        state,
        action.seed,
        action.anchor,
        action.bounds,
        action.fresh,
        action.rect,
      );
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
    case "reconcile": {
      const me = state.windows.find((w) => w.key === action.key);
      if (!me || me.actionKey === action.sid) return state;
      // A window elsewhere already answering to this id can only happen in one narrow race: the
      // session appeared in the list and was opened from there BEFORE our launch reconciled. Two
      // windows on one pty is exactly what decision 2 exists to prevent, and the launch window is
      // the one holding the live master, so the other (a plain attach that would be demoted to
      // read-only anyway) gives way. Dropping the id instead would leave this window permanently
      // unnamed, untethered and unpersistable.
      const other = state.windows.find(
        (w) => w.key !== action.key && (w.key === action.sid || w.actionKey === action.sid),
      );
      const windows = state.windows
        .filter((w) => w !== other)
        .map((w) => (w.key === action.key ? { ...w, actionKey: action.sid } : w));
      return {
        ...state,
        windows,
        focusedKey: other && state.focusedKey === other.key ? action.key : state.focusedKey,
        flashKey: other && state.flashKey === other.key ? null : state.flashKey,
      };
    }
    case "titles": {
      let changed = false;
      const windows = state.windows.map((w) => {
        // Looked up by `actionKey`: the live index is keyed on the id the session actually has,
        // which after a converge is not the placeholder this window still transports on.
        const live = action.titles.get(w.actionKey);
        // Absent from the map (filtered, archived, another layout) → keep what we have. A map
        // filter never closes a window, and it must not un-name one either.
        if (!live || live === w.title) return w;
        changed = true;
        return { ...w, title: live };
      });
      return changed ? { ...state, windows } : state;
    }
    case "request": {
      // Bounded, and deduped against what is already queued: a double-click on a sidebar row
      // must not queue two requests for one session. (`open` would refuse the second anyway;
      // this just keeps the queue honest.)
      if (state.pending.some((p) => p.seed.key === action.req.seed.key)) return state;
      if (state.pending.length >= state.cap) return state;
      return { ...state, pending: [...state.pending, action.req] };
    }
    case "drain": {
      if (!state.pending.length) return state;
      // A map that cannot host refuses the whole queue in one step — same outcome, one code path,
      // so the two ways a request can fail cannot drift apart.
      if (!action.canHost)
        return {
          ...state,
          pending: [],
          rejected: [...state.rejected, ...state.pending],
        };
      let next: State = { ...state, pending: [] };
      const rejected: WindowRequest[] = [];
      for (const req of state.pending) {
        // "Already open" counts as honoured: `openInto` focuses and flashes the existing window,
        // which is exactly what the caller asked for. Only a request that produced no window and
        // had none to focus was actually refused.
        const held = Boolean(holderOf(next.windows, req.seed.key));
        const before = next.windows.length;
        next = openInto(
          next,
          req.seed,
          action.anchors.get(req.seed.key) ?? null,
          action.bounds,
          req.fresh,
        );
        if (!held && next.windows.length === before) rejected.push(req);
      }
      return rejected.length
        ? { ...next, rejected: [...next.rejected, ...rejected] }
        : next;
    }
    case "clearRejected":
      return state.rejected.length ? { ...state, rejected: [] } : state;
    case "restore": {
      if (!state.restorable.length)
        return state.hydrated ? state : { ...state, hydrated: true };
      let next: State = { ...state, restorable: [], hydrated: true };
      for (const w of state.restorable) {
        // Restored in stored stacking order (the codec sorts by z), so the window that was on
        // top opens last and lands on top. Each goes through the SAME open transition, so the
        // one-window-per-session rule and the clamping hold exactly as on a click.
        //
        // The ceiling here is the HARD maximum, deliberately NOT the operator's soft cap. A
        // truncated restore would be followed a moment later by the persistence effect writing
        // the truncated list back — silently deleting part of a layout the operator never asked
        // to lose, and only visible on the load after next. The soft cap gates what may be
        // OPENED; a stored layout is by construction a set of windows that were already open
        // together on this device, and lowering the cap is documented never to close one.
        // `WINDOW_CAP_MAX` still holds the line against a hand-edited storage entry.
        next = openInto(
          next,
          seedOf(w),
          null,
          action.bounds,
          undefined,
          rectOf(w),
          WINDOW_CAP_MAX,
        );
      }
      // A session missing from the map's list is NOT treated as deleted, and that is deliberate:
      // the list is filtered, paginated and scope-stripped, so absence from it says nothing about
      // existence (#867). A window whose session really has gone opens and its own pane reports
      // the connection failure — the same answer a deep link to that session gives, in the place
      // the operator is already looking.
      //
      // A restore is not a focus gesture: nothing should steal the focus ring on arrival, and
      // the notice a capped restore would raise is noise the operator cannot act on yet.
      return { ...next, focusedKey: null, flashKey: null, notice: null };
    }
    case "detach": {
      // The map is unmounting, so no window has a live `<Terminal>` any more — and THAT is what
      // the frozen transport identity was protecting. Freezing `key`/`engine`/`id` past the mount
      // is not just pointless, it is wrong: a window launched under a `new-<uuid>` placeholder
      // would re-mount against the placeholder (a key the server only accepts on the launch path)
      // and, still carrying `fresh`, would send `new=1` again — starting a SECOND session on the
      // next visit to the map. So the moment the mount is gone the record adopts the canonical
      // identity it reconciled to and drops its launch params.
      //
      // A window that never got that far — still an unreconciled placeholder when the map went
      // away — is dropped. It cannot be attached to (the id it would ask for does not exist) and
      // it must not be relaunched, so there is nothing left for it to be. Reaching this needs the
      // operator to leave the map within a second or so of launching.
      let changed = false;
      const windows: WorkspaceWindow[] = [];
      for (const w of state.windows) {
        if (isPlaceholderKey(w.actionKey)) {
          changed = true;
          continue;
        }
        if (w.key === w.actionKey && !w.fresh) {
          windows.push(w);
          continue;
        }
        changed = true;
        const [engine, ...rest] = w.actionKey.split(":");
        windows.push({
          ...w,
          key: w.actionKey,
          engine: engine || w.engine,
          id: rest.join(":") || w.id,
          fresh: undefined,
        });
      }
      if (!changed) return state;
      const keys = new Set(windows.map((w) => w.key));
      return {
        ...state,
        windows,
        focusedKey: null,
        flashKey: null,
        // A pending request for a window that just went away must not be resurrected; one for a
        // window that survived is about to be refused as a duplicate anyway.
        pending: state.pending.filter((p) => !keys.has(p.seed.key)),
      };
    }
    case "cap": {
      const cap = clampWindowCap(action.cap);
      if (cap === state.cap) return state;
      // Lowering below the open count NEVER closes a window. Closing on a settings change is
      // destructive, and the cap is a resource guard, not a security control — it refuses the
      // next open and nothing else.
      return { ...state, cap, notice: null };
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
  open: (
    seed: WindowSeed,
    anchor: Point | null,
    bounds: Size,
    fresh?: FreshSession,
  ) => void;
  /** Ask for a window from OUTSIDE the map (sidebar, session pane, new-session flow). Queued;
   *  the canvas opens it once it is mounted and measured. */
  requestOpen: (seed: WindowSeed, fresh?: FreshSession) => void;
  /** Open everything queued, in one atomic transition, recording what could not be admitted.
   *  `canHost` false refuses the whole queue rather than silently dropping it. */
  drain: (anchors: Map<string, Point | null>, bounds: Size, canHost: boolean) => void;
  /** The canvas has handed the refused requests back to the full-screen route. */
  clearRejected: () => void;
  /** The map is unmounting: no window has a live pane any more, so each record adopts the
   *  canonical identity it reconciled to and drops its launch params. */
  detach: () => void;
  /** Apply the stored layout, or (when there is none) mark the workspace hydrated so it may
   *  start persisting. Idempotent: the second call in a StrictMode double-mount is a no-op. */
  restore: (bounds: Size) => void;
  focus: (key: string) => void;
  close: (key: string) => void;
  closeAll: () => void;
  setRect: (key: string, rect: Rect) => void;
  setRole: (key: string, role: TermRole) => void;
  /** Adopt the real id an engine minted for a `new-` placeholder window (#127/#315). */
  reconcile: (key: string, sid: string) => void;
  setCap: (cap: number) => void;
  /** Adopt renames for open windows. */
  syncTitles: (titles: Map<string, string>) => void;
}

function init(): State {
  return {
    windows: [],
    focusedKey: null,
    flashKey: null,
    notice: null,
    rejected: [],
    cap: loadWindowCap(),
    pending: [],
    restorable: loadWorkspace(),
    hydrated: false,
  };
}

/** The workspace's state machine (#208, hoisted and persisted in #936).
 *
 *  It lives in a PROVIDER above the router now, not inside the map: the records have to outlive
 *  the `/overview` mount (leaving the map and coming back must find the same windows), and the
 *  sidebar has to be able to ask for one. What did NOT move is geometry — the canvas still owns
 *  measurement, projection and the tether, and windows still mount only inside `WindowLayer`.
 *
 *  Persistence is device-local (`localStorage`), which is #872's option B: the workspace is
 *  device-shaped, and choosing it means this whole feature has no server surface at all. */
export function useWorkspace(): Workspace {
  const [state, dispatch] = useReducer(reduce, undefined, init);

  // The transient markers expire in EFFECTS keyed on the value, not in the action that set them:
  // a timer armed inside a dispatch would be armed twice under StrictMode's double invocation,
  // and would leak if the window closed before it fired.
  const { flashKey, notice, windows, cap, hydrated } = state;
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

  // Persist the layout, debounced. `windows` changes on every pointer move of a drag, so the
  // trailing timer is what turns a 200-frame gesture into one write.
  //
  // Gated on `hydrated`: before the stored layout has been read back, the state is legitimately
  // empty, and writing that would erase the layout it is about to restore.
  useEffect(() => {
    if (!hydrated) return;
    const t = setTimeout(() => saveWorkspace(windows), PERSIST_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [windows, hydrated]);

  // The cap is written on change, not debounced — a stepper press is one discrete decision, and
  // the first render must not write the value it just read back (which would be harmless but is
  // noise in a storage inspector).
  const capWritten = useRef(cap);
  useEffect(() => {
    if (capWritten.current === cap) return;
    capWritten.current = cap;
    saveWindowCap(cap);
  }, [cap]);

  const open = useCallback(
    (
      seed: WindowSeed,
      anchor: Point | null,
      bounds: Size,
      fresh?: FreshSession,
    ) => dispatch({ type: "open", seed, anchor, bounds, fresh }),
    [],
  );
  const requestOpen = useCallback(
    (seed: WindowSeed, fresh?: FreshSession) =>
      dispatch({ type: "request", req: { seed, fresh } }),
    [],
  );
  const drain = useCallback(
    (anchors: Map<string, Point | null>, bounds: Size, canHost: boolean) =>
      dispatch({ type: "drain", anchors, bounds, canHost }),
    [],
  );
  const clearRejected = useCallback(() => dispatch({ type: "clearRejected" }), []);
  const restore = useCallback(
    (bounds: Size) => dispatch({ type: "restore", bounds }),
    [],
  );
  const detach = useCallback(() => dispatch({ type: "detach" }), []);
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
  const reconcile = useCallback(
    (key: string, sid: string) => dispatch({ type: "reconcile", key, sid }),
    [],
  );
  const setCap = useCallback((c: number) => dispatch({ type: "cap", cap: c }), []);
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
      requestOpen,
      drain,
      clearRejected,
      restore,
      detach,
      focus,
      close,
      closeAll,
      setRect,
      setRole,
      reconcile,
      setCap,
      syncTitles,
    }),
    [
      state,
      open,
      requestOpen,
      drain,
      clearRejected,
      restore,
      detach,
      focus,
      close,
      closeAll,
      setRect,
      setRole,
      reconcile,
      setCap,
      syncTitles,
    ],
  );
}
