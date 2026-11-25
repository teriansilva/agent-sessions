import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";
import { isNewSessionPlaceholder } from "../../app/sessionsStore";
import type { TermRole } from "../../lib/termSocket";
import type { FreshSession } from "../../lib/termUrl";
import {
  loadWindowCap,
  loadWorkspace,
  rectOf,
  saveStoredWorkspace,
  saveWindowCap,
  saveWorkspace,
  seedOf,
  type StoredWindow,
} from "./windowStore";
import {
  canOpen,
  cascadeRect,
  clampWindowCap,
  tileCapacity,
  tileRects,
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
  /** Set on a REFUSED request only (#1648): why the drain could not open it. `cap` = the map was
   *  at its window cap; `host` = the map could not host a window at all (too small, a phone). The
   *  full-screen hand-back says "the map was full" for `cap` only. */
  reason?: "cap" | "host";
  /** The acknowledgements this request owes (#2102): every caller that asked with an id is told,
   *  by that id, what the drain did with it (`Workspace.acks`). */
  acks?: string[];
}

/** What became of ONE command a caller queued or applied with an id (#2102). Explicit and per
 *  request, so nothing a caller is waiting on can stay unanswered, and no later request can be
 *  taken for an earlier one. */
export interface WorkspaceAck {
  id: string;
  /** `applied`: the workspace changed as asked. `noop`: there was nothing to do it to.
   *  `refused`: the map could not do it. `cancelled`: withdrawn before it ran. */
  result: "applied" | "noop" | "refused" | "cancelled";
  /** `cap` / `host`: why the map refused. `parked`: every window is minimised (Arrange).
   *  `empty` / `absent`: no window (at all / for that session). */
  reason?: "cap" | "host" | "parked" | "empty" | "absent";
  /** A refused open that was the drain's ONLY refusal: the map hands that one back to its
   *  full-screen route. Several stay listed on the map. */
  alone?: boolean;
  /** The sessions an applied Arrange parked in the tray (they did not fit at the floor). */
  parked?: string[];
}

/** One entry of the ordered queue the canvas drains (#2102). Opens need the canvas (an anchor,
 *  the measured box) and so does Arrange; minimize, close and close-all do not, but once
 *  anything is queued they QUEUE BEHIND it, so the drain replays every command in the order it
 *  was asked for. That one rule is what makes a batch delivered before a render end exactly
 *  where the same commands, applied one at a time with a drain between each, would end. */
export type QueuedCommand =
  | { kind: "open"; req: WindowRequest }
  | { kind: "arrange"; ack?: string }
  | { kind: "minimize" | "close"; key: string; ack?: string }
  | { kind: "closeAll"; ack?: string };

/** How many acknowledgements are kept for their callers to read. */
const ACKS_KEPT = 64;

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
  /** Parked in the map's tray. The window is HIDDEN, never unmounted: unmounting `<Terminal>`
   *  closes its socket, and a minimized session is one the operator still wants live. It keeps
   *  its rect and its place under the cap. Any raise (a click on its tray chip, a re-open from
   *  the sidebar or Ask) brings it back. */
  minimized?: boolean;
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
  /** Requests queued from outside the map, drained by the canvas once it is measured. Always
   *  the open requests of `queue`, in order: kept as its own list because the canvas resolves an
   *  anchor for each. */
  pending: WindowRequest[];
  /** Everything waiting for the canvas, in the order it was asked for (`QueuedCommand`). */
  queue: QueuedCommand[];
  /** What became of the commands that carried an id, oldest first, the newest `ACKS_KEPT`. */
  acks: WorkspaceAck[];
  /** The highest sequence number among the actions applied so far that carried one. A caller
   *  that numbers its own dispatches reads here how far the committed state has got with them,
   *  which is what lets it predict from a state that lags its dispatches (#2102). */
  seq: number;
  /** The box the canvas last measured, for a prediction that has to tile without a canvas. */
  bounds: Size | null;
  /** Requests the drain could NOT honour — the map cannot host a window, or the cap is full.
   *
   *  They are not discarded, because a caller decided this session should be on screen and a
   *  fresh one carries the cwd and bypass the operator just chose. The drain records the refusal
   *  as an explicit OUTCOME at the transition that made it (rather than leaving the caller to
   *  infer it from a notice), and the canvas hands the first one back to the full-screen route.
   *  Transient, never persisted (Hermes on #939, rounds 1 + 2). */
  rejected: WindowRequest[];
  /** A refused BATCH, kept for the map to list (Hermes on #1320). There is one screen, so only a
   *  single refusal can be handed back to its full-screen route; when several are refused at once
   *  the operator stays on the map and every one is listed with its own way in, until dismissed. */
  refused: WindowRequest[];
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
  | { type: "minimize"; key: string; ack?: string }
  | {
      type: "arrange";
      bounds: Size;
    }
  | { type: "close"; key: string; ack?: string }
  | { type: "closeAll"; ack?: string }
  | { type: "rect"; key: string; rect: Rect }
  | { type: "role"; key: string; role: TermRole }
  | { type: "reconcile"; key: string; sid: string }
  | { type: "titles"; titles: Map<string, string> }
  | { type: "request"; req: WindowRequest }
  | { type: "requestArrange"; ack?: string }
  /** Withdraw queued commands by their ids: each is acknowledged `cancelled` and never runs. */
  | { type: "cancel"; acks: string[] }
  | {
      type: "drain";
      /** Chip anchors, projected by the canvas — the reducer cannot compute them. */
      anchors: Map<string, Point | null>;
      bounds: Size;
      /** Can the measured map host a window at all? `false` rejects the whole queue. */
      canHost: boolean;
    }
  | { type: "clearRejected" }
  | { type: "refuseRejected" }
  | { type: "dismissRefused" }
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
  // Raising a parked window brings it back: every way of asking for a session on screen — its
  // tray chip, a sidebar click, Ask's "Open in map" — goes through here.
  if (w.minimized) {
    const top = topZ(state.windows) + 1;
    return {
      ...state,
      focusedKey: key,
      windows: state.windows.map((x) =>
        x.key === key ? { ...x, z: top, minimized: false } : x,
      ),
    };
  }
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

/** Replace the queue, keeping `pending` (its open requests, in order) in step. */
function requeue(state: State, queue: QueuedCommand[]): State {
  return {
    ...state,
    queue,
    pending: queue.flatMap((c) => (c.kind === "open" ? [c.req] : [])),
  };
}

const queued = (state: State, command: QueuedCommand): State =>
  requeue(state, [...state.queue, command]);

/** Record what became of the command `id` (none: the caller did not ask to be told). */
function acked(state: State, id: string | undefined, ack: Omit<WorkspaceAck, "id">): State {
  if (!id) return state;
  return { ...state, acks: [...state.acks, { ...ack, id }].slice(-ACKS_KEPT) };
}

/** The queue once the map has unmounted (`detach`): a request for a window that survived is
 *  dropped (it would only be refused as a duplicate), and its callers are told. */
function requeued(state: State, survivors: Set<string>): Pick<State, "queue" | "pending" | "acks"> {
  let next = state;
  const queue = state.queue.filter((c) => {
    if (c.kind !== "open" || !survivors.has(c.req.seed.key)) return true;
    for (const id of c.req.acks ?? []) next = acked(next, id, { result: "cancelled" });
    return false;
  });
  const { pending, acks } = requeue(next, queue);
  return { queue, pending, acks };
}

type Outcome = [State, Omit<WorkspaceAck, "id">];
const ABSENT: Omit<WorkspaceAck, "id"> = { result: "noop", reason: "absent" };

// The three transitions below act on the windows AND, while the map has not mounted since the
// page loaded, on the stored layout still waiting to be restored (#2102): before hydration that
// layout is the workspace's only record of its windows, and a command that looked at `windows`
// alone would report nothing to do and then watch the restore bring the window back.

/** Park one window in the tray. */
function minimizeKey(state: State, key: string): Outcome {
  const w = holderOf(state.windows, key);
  const stored = !state.hydrated && state.restorable.some((r) => r.key === key);
  if (!w && !stored) return [state, ABSENT];
  const live = w && !w.minimized;
  const kept = stored && state.restorable.some((r) => r.key === key && !r.minimized);
  if (!live && !kept) return [state, { result: "applied" }]; // parked already: as asked
  return [
    {
      ...state,
      windows: live
        ? state.windows.map((x) => (x.key === w.key ? { ...x, minimized: true } : x))
        : state.windows,
      restorable: kept
        ? state.restorable.map((r) => (r.key === key ? { ...r, minimized: true } : r))
        : state.restorable,
      focusedKey: w && state.focusedKey === w.key ? null : state.focusedKey,
      flashKey: w && state.flashKey === w.key ? null : state.flashKey,
    },
    { result: "applied" },
  ];
}

/** Close one window. Unmounting it unmounts <Terminal>, which closes its socket and detaches its
 *  document-level listeners; there is nothing else to tear down. */
function closeKey(state: State, key: string): Outcome {
  const held = holderOf(state.windows, key);
  const stored = !state.hydrated && state.restorable.some((r) => r.key === key);
  if (!held && !stored) return [state, ABSENT];
  return [
    {
      ...state,
      windows: held ? state.windows.filter((w) => w !== held) : state.windows,
      restorable: stored ? state.restorable.filter((r) => r.key !== key) : state.restorable,
      focusedKey: held && state.focusedKey === held.key ? null : state.focusedKey,
      flashKey: held && state.flashKey === held.key ? null : state.flashKey,
    },
    { result: "applied" },
  ];
}

function closeEvery(state: State): Outcome {
  const stored = !state.hydrated && state.restorable.length > 0;
  if (!state.windows.length && !stored) return [state, { result: "noop", reason: "empty" }];
  return [
    {
      ...state,
      windows: [],
      restorable: stored ? [] : state.restorable,
      focusedKey: null,
      flashKey: null,
    },
    { result: "applied" },
  ];
}

/** Arrange: tile the windows on screen, sorted by name; the parked ones stay in the tray. Tiles
 *  never overlap: windows past what the box holds at the floor (`tileCapacity`) are PARKED, so
 *  every window stays reachable — on screen, or as a chip in the tray (Hermes on #1320). With
 *  no window on screen there is nothing to tile, and it says which kind of nothing. */
function tile(state: State, bounds: Size): Outcome {
  const shown = state.windows
    .filter((w) => !w.minimized)
    .sort(
      (a, b) =>
        a.title.localeCompare(b.title, undefined, { sensitivity: "base" }) ||
        a.key.localeCompare(b.key),
    );
  if (!shown.length)
    return [state, { result: "noop", reason: state.windows.length ? "parked" : "empty" }];
  const tiled = shown.slice(0, tileCapacity(bounds));
  const rects = tileRects(tiled.length, bounds);
  const base = topZ(state.windows);
  const placed = new Map(tiled.map((w, i) => [w.key, { rect: rects[i], z: base + 1 + i }]));
  const parked = new Set(shown.slice(tiled.length).map((w) => w.key));
  return [
    {
      ...state,
      bounds,
      windows: state.windows.map((w) => {
        const p = placed.get(w.key);
        if (p) return { ...w, rect: p.rect, z: p.z };
        return parked.has(w.key) ? { ...w, minimized: true } : w;
      }),
      focusedKey: state.focusedKey && parked.has(state.focusedKey) ? null : state.focusedKey,
    },
    {
      result: "applied",
      parked: shown.slice(tiled.length).map((w) => w.actionKey),
    },
  ];
}

/** Where the workspace will be once the canvas has caught up: the stored layout restored and the
 *  queue replayed, by the SAME transitions the canvas will run. For a caller that must decide now
 *  (is there room, does this session have a window, is anything on screen) from a workspace whose
 *  queue has not drained (#2102). The box only decides where tiles land and how many fit, never
 *  how many windows there are. */
export function settledWorkspace(state: State): State {
  const bounds = state.bounds ?? { w: 1600, h: 1000 };
  let next = state;
  if (!next.hydrated) next = step(next, { type: "restore", bounds });
  if (next.queue.length)
    next = step(next, { type: "drain", anchors: new Map(), bounds, canHost: true });
  return next;
}

/** One action, with the bookkeeping every action shares: a numbered one records how far the
 *  state has got with its caller's dispatches (`State.seq`). */
function reduce(state: State, action: Action & { seq?: number }): State {
  const next = step(state, action);
  return action.seq === undefined || action.seq <= next.seq ? next : { ...next, seq: action.seq };
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
function step(state: State, action: Action): State {
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
    // Focus, minimize and close take a window's own key OR the session it holds (`holderOf`):
    // a caller outside the map (HQ's `show`, #2102) knows the session, not the transport key a
    // window launched under.
    case "focus": {
      const w = holderOf(state.windows, action.key);
      return w ? raise(state, w.key) : state;
    }
    // Minimize, close and close-all apply at once ONLY while nothing is queued for the canvas.
    // Behind a queued open or Arrange they queue too, and the drain replays the lot in order
    // (`QueuedCommand`): applied now, a close would run BEFORE the open it was asked after.
    case "minimize":
    case "close": {
      if (state.queue.length)
        return queued(state, { kind: action.type, key: action.key, ack: action.ack });
      const [next, ack] = (action.type === "close" ? closeKey : minimizeKey)(state, action.key);
      return acked(next, action.ack, ack);
    }
    case "closeAll": {
      if (state.queue.length) return queued(state, { kind: "closeAll", ack: action.ack });
      const [next, ack] = closeEvery(state);
      return acked(next, action.ack, ack);
    }
    case "requestArrange":
      return queued(state, { kind: "arrange", ack: action.ack });
    case "arrange":
      return tile(state, action.bounds)[0];
    case "cancel": {
      const drop = new Set(action.acks);
      let next = state;
      const queue: QueuedCommand[] = [];
      for (const c of state.queue) {
        const ids = c.kind === "open" ? (c.req.acks ?? []) : c.ack ? [c.ack] : [];
        const hit = ids.filter((i) => drop.has(i));
        if (!hit.length) {
          queue.push(c);
          continue;
        }
        for (const id of hit) next = acked(next, id, { result: "cancelled" });
        // An open others still wait on stays queued, without the withdrawn ids.
        const left = ids.filter((i) => !drop.has(i));
        if (c.kind === "open" && left.length) queue.push({ ...c, req: { ...c.req, acks: left } });
      }
      return queue.length === state.queue.length && next === state ? state : requeue(next, queue);
    }
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
      const { req } = action;
      const last = state.queue[state.queue.length - 1];
      // Deduped against the request queued RIGHT before it: a double-click on a sidebar row must
      // not queue two requests for one session. Only an adjacent one — behind anything else
      // (a minimize, a close, an Arrange) a second request is a new intent with its own place in
      // the order, and merging it into the earlier one would run it too early.
      if (last?.kind === "open" && last.req.seed.key === req.seed.key) {
        if (!req.acks?.length) return state;
        const merged = { ...last.req, acks: [...(last.req.acks ?? []), ...req.acks] };
        return requeue(state, [...state.queue.slice(0, -1), { kind: "open", req: merged }]);
      }
      // Bounded: a runaway caller cannot grow the queue without limit. Refused, and said so.
      if (state.pending.length >= Math.max(state.cap, WINDOW_CAP_MAX)) {
        let next = state;
        for (const id of req.acks ?? [])
          next = acked(next, id, { result: "refused", reason: "cap" });
        return next;
      }
      return queued(state, { kind: "open", req });
    }
    case "drain": {
      if (!state.queue.length) return state;
      // ONE replay of the queue, in the order it was asked for. Every command goes through the
      // same transition it would have gone through alone, and each one that carried an id is
      // acknowledged with what actually happened.
      let next: State = requeue({ ...state, bounds: action.bounds }, []);
      const rejected: WindowRequest[] = [];
      const refusals: string[] = [];
      for (const c of state.queue) {
        if (c.kind === "open") {
          const { req } = c;
          // A map that cannot host refuses every open — same outcome, one code path, so the two
          // ways a request can fail cannot drift apart.
          //
          // "Already open" counts as honoured: `openInto` focuses and flashes the existing
          // window (bringing a parked one back), which is exactly what the caller asked for.
          // Only a request that produced no window and had none to focus was actually refused.
          const held = Boolean(holderOf(next.windows, req.seed.key));
          const before = next.windows.length;
          if (action.canHost)
            next = openInto(
              next,
              req.seed,
              action.anchors.get(req.seed.key) ?? null,
              action.bounds,
              req.fresh,
            );
          const opened = action.canHost && (held || next.windows.length > before);
          if (!opened) {
            rejected.push({ ...req, reason: action.canHost ? "cap" : "host" });
            refusals.push(...(req.acks ?? []));
          }
          for (const id of req.acks ?? [])
            next = acked(
              next,
              id,
              opened
                ? { result: "applied" }
                : { result: "refused", reason: action.canHost ? "cap" : "host" },
            );
        } else if (c.kind === "arrange") {
          const [tiled, ack] = action.canHost
            ? tile(next, action.bounds)
            : [next, { result: "refused", reason: "host" } as const];
          next = acked(tiled, c.ack, ack);
        } else {
          const [done, ack] =
            c.kind === "closeAll"
              ? closeEvery(next)
              : (c.kind === "close" ? closeKey : minimizeKey)(next, c.key);
          next = acked(done, c.ack, ack);
        }
      }
      if (!rejected.length) return next;
      next = { ...next, rejected: [...next.rejected, ...rejected] };
      // The canvas hands a LONE refusal back to its full-screen route; its caller is told so.
      if (next.rejected.length === 1)
        next = {
          ...next,
          acks: next.acks.map((a) => (refusals.includes(a.id) ? { ...a, alone: true } : a)),
        };
      return next;
    }
    case "clearRejected":
      return state.rejected.length ? { ...state, rejected: [] } : state;
    case "refuseRejected":
      return state.rejected.length
        ? { ...state, rejected: [], refused: [...state.refused, ...state.rejected] }
        : state;
    case "dismissRefused":
      return state.refused.length ? { ...state, refused: [] } : state;
    case "restore": {
      if (!state.restorable.length)
        return state.hydrated ? state : { ...state, hydrated: true };
      let next: State = { ...state, restorable: [], hydrated: true, bounds: action.bounds };
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
        if (w.minimized)
          next = {
            ...next,
            windows: next.windows.map((x) =>
              x.key === w.key ? { ...x, minimized: true } : x,
            ),
          };
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
        ...requeued(state, keys),
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
  /** Several were refused at once: keep them all (`refused`) for the map to list. */
  refuseRejected: () => void;
  /** The operator dismissed the refused-batch list. */
  dismissRefused: () => void;
  /** The map is unmounting: no window has a live pane any more, so each record adopts the
   *  canonical identity it reconciled to and drops its launch params. */
  detach: () => void;
  /** Apply the stored layout, or (when there is none) mark the workspace hydrated so it may
   *  start persisting. Idempotent: the second call in a StrictMode double-mount is a no-op. */
  restore: (bounds: Size) => void;
  focus: (key: string) => void;
  /** Park a window in the map's tray — hidden, still live (`WorkspaceWindow.minimized`). */
  minimize: (key: string) => void;
  /** Tile the windows on screen into the box, sorted by name; any that do not fit at the floor
   *  are parked in the tray. */
  arrange: (bounds: Size) => void;
  /** Ask for Arrange from outside the map: the canvas runs it once it is mounted and measured. */
  requestArrange: () => void;
  /** The queue's own actions, for a caller that numbers its dispatches and asks to be told, by
   *  id, what became of each (HQ's screen executor, #2102). Stable. */
  command: (action: WorkspaceCommand) => void;
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

/** The pure transition and its starting state, for tests that drive the REAL reducer without a
 *  canvas (the screen executor's, #2102). Nothing in the app imports these. */
export { reduce as workspaceReducer, init as initWorkspace };
export type WorkspaceAction = Action & { seq?: number };
/** What `Workspace.command` takes: the actions that queue for, or are ordered against, the canvas. */
export type WorkspaceCommand = Extract<
  WorkspaceAction,
  { type: "request" | "minimize" | "close" | "closeAll" | "requestArrange" | "cancel" }
>;
export type { State as WorkspaceState };

function init(): State {
  return {
    windows: [],
    focusedKey: null,
    flashKey: null,
    notice: null,
    rejected: [],
    refused: [],
    cap: loadWindowCap(),
    pending: [],
    queue: [],
    acks: [],
    seq: 0,
    bounds: null,
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

  // Before hydration the stored layout IS the workspace's record of its windows. A command from
  // outside the map can change it (HQ closing or parking a window while the map has not mounted
  // since the page loaded, #2102), and that change must reach storage at once: the debounced
  // write above is gated on `hydrated`, so without this the next load would restore the window.
  // Only a CHANGE is written — never the layout that was just read back.
  const { restorable } = state;
  const restorableWritten = useRef(restorable);
  useEffect(() => {
    if (restorableWritten.current === restorable) return;
    restorableWritten.current = restorable;
    if (!hydrated) saveStoredWorkspace(restorable);
  }, [restorable, hydrated]);

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
  const refuseRejected = useCallback(() => dispatch({ type: "refuseRejected" }), []);
  const dismissRefused = useCallback(() => dispatch({ type: "dismissRefused" }), []);
  const restore = useCallback(
    (bounds: Size) => dispatch({ type: "restore", bounds }),
    [],
  );
  const detach = useCallback(() => dispatch({ type: "detach" }), []);
  const focus = useCallback((key: string) => dispatch({ type: "focus", key }), []);
  const minimize = useCallback(
    (key: string) => dispatch({ type: "minimize", key }),
    [],
  );
  const arrange = useCallback(
    (bounds: Size) => dispatch({ type: "arrange", bounds }),
    [],
  );
  const requestArrange = useCallback(() => dispatch({ type: "requestArrange" }), []);
  const command = useCallback((action: WorkspaceCommand) => dispatch(action), []);
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
      refuseRejected,
      dismissRefused,
      restore,
      detach,
      focus,
      minimize,
      arrange,
      requestArrange,
      command,
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
      refuseRejected,
      dismissRefused,
      restore,
      detach,
      focus,
      minimize,
      arrange,
      requestArrange,
      command,
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
