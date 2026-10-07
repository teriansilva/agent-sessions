/** Device-local persistence for the map's window workspace (#936, delivering #872).
 *
 *  `localStorage`, not a server pref — #872's option B, and the reason is not laziness. The
 *  workspace is device-shaped: a layout of eight 720×480 windows means nothing on a phone, and
 *  the cap guards *this browser's* memory and socket budget. The map's grouping mode already
 *  works exactly this way (`OverviewPrefsContext`), so there is precedent, and choosing it
 *  deletes the entire server half of the change — no new `/api/prefs` key, no validator, no
 *  config exposure, no new authorization surface.
 *
 *  Everything here is READ-LENIENT and WRITE-STRICT-ENOUGH, the same asymmetry `termSize` uses
 *  (#859): a read never throws and never returns something the workspace cannot use, because the
 *  operator can edit `localStorage` and a corrupt entry must not be able to strand the map. The
 *  write side is where the rules live.
 */
import { isNewSessionPlaceholder } from "../../app/sessionsStore";
import type { WorkspaceWindow } from "./useWorkspace";
import { clampWindowCap, MIN_SIZE, type Rect } from "./workspace";

/** The layout. `tr-overview-*`, beside `tr-overview-groupby`, so the map's device-local keys
 *  read as one family. */
export const WORKSPACE_KEY = "tr-overview-workspace";
/** The operator's ceiling. Its own key: a layout and a limit change for different reasons, and
 *  clearing one must not clear the other. */
export const CAP_KEY = "tr-overview-window-cap";

/** What a window costs to store. Deliberately NOT the whole `WorkspaceWindow`:
 *  - `role` is the server's live verdict — storing it would resurrect a stale "READ-ONLY" badge
 *    on a window whose owner has long since gone away;
 *  - `fresh` is a *launch*, and replaying a launch on reload is the one thing this must never do;
 *  - `flash` / focus are transient markers with no meaning outside the session that set them.
 *  `title` IS stored, so a restored window has a name before the session list has loaded. */
export interface StoredWindow {
  key: string;
  engine: string;
  id: string;
  title: string;
  x: number;
  y: number;
  w: number;
  h: number;
  z: number;
  /** Parked in the tray. Optional so a layout written before minimize existed still reads. */
  minimized?: boolean;
}

/** Is this a key we may persist?
 *
 *  Two rejections, and the second is the load-bearing one:
 *  - the shape must be `<engine>:<native>` with nothing exotic in it, because the key comes back
 *    out of storage and goes into a WebSocket URL;
 *  - it must not be an `<engine>:new-<uuid>` placeholder. A placeholder window is mid-LAUNCH;
 *    restoring one would re-run `new=1` against an id the engine has already replaced, which is
 *    a session started by a page reload nobody asked for. The server draws the same line —
 *    `canonical_key` rejects the shape and only the ws launch path accepts it (#867/#127).
 */
export function isPersistableKey(key: unknown): key is string {
  if (typeof key !== "string") return false;
  if (isNewSessionPlaceholder(key)) return false;
  return /^[a-z0-9_-]{1,32}:[A-Za-z0-9._-]{1,128}$/.test(key);
}

const num = (v: unknown): number | null => {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
};

/** One stored entry → a record the workspace can restore, or `null` to drop it.
 *
 *  Geometry is only sanity-bounded here, never fitted: the overlay box is not known at read
 *  time, and `clampRect` runs against the real box on every render anyway. What this rules out
 *  is the value that could not survive that trip — a NaN width, a negative position, a size
 *  below the floor that would hand the agent an unusable column count. */
function decodeOne(raw: unknown): StoredWindow | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  if (!isPersistableKey(r.key)) return null;
  if (typeof r.engine !== "string" || !r.engine) return null;
  if (typeof r.id !== "string" || !r.id) return null;
  const x = num(r.x);
  const y = num(r.y);
  const w = num(r.w);
  const h = num(r.h);
  const z = num(r.z);
  if (x === null || y === null || w === null || h === null) return null;
  return {
    key: r.key,
    engine: r.engine,
    id: r.id,
    title: typeof r.title === "string" ? r.title.slice(0, 200) : r.id,
    x: Math.max(0, Math.floor(x + 0.5)),
    y: Math.max(0, Math.floor(y + 0.5)),
    w: Math.max(MIN_SIZE.w, Math.floor(w + 0.5)),
    h: Math.max(MIN_SIZE.h, Math.floor(h + 0.5)),
    z: z === null ? 1 : Math.max(0, Math.floor(z + 0.5)),
    ...(r.minimized === true ? { minimized: true } : {}),
  };
}

/** Decode a stored payload. Pure, so the rejection cases are unit-testable without a DOM.
 *
 *  Skip-and-continue is the whole policy: one bad entry drops itself, never the layout. A stored
 *  workspace outlives the map it described — sessions get archived, projects get reorganised,
 *  engines get uninstalled — so "some of this no longer parses" is the normal case, not an
 *  error state. Duplicate keys collapse to the first, because the workspace's own invariant is
 *  one window per session and a duplicate would be refused a moment later anyway. */
export function decodeWorkspace(raw: string | null): StoredWindow[] {
  if (!raw) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) return [];
  const out: StoredWindow[] = [];
  const seen = new Set<string>();
  // Bounded by the MAXIMUM the operator could ever have configured, not by their current cap:
  // decoding is not the place to enforce a limit that may have been lowered since the write, and
  // `restore` re-applies the live cap anyway. The bound exists so a hand-edited 10,000-entry
  // array cannot make the read loop the expensive part of a page load.
  for (const item of parsed.slice(0, 64)) {
    const w = decodeOne(item);
    if (!w || seen.has(w.key)) continue;
    seen.add(w.key);
    out.push(w);
  }
  // Restore in stacking order, so the window that was on top is opened last and lands on top.
  return out.sort((a, b) => a.z - b.z);
}

/** Windows → the payload. Placeholders drop out here rather than at the read side, so a
 *  mid-launch window never reaches storage in the first place. `actionKey` is what gets stored:
 *  after an opencode/codex converge it is the id the session actually has, and the frozen
 *  transport `key` would restore a session that no longer exists under that name. */
export function encodeWorkspace(windows: WorkspaceWindow[]): StoredWindow[] {
  const out: StoredWindow[] = [];
  for (const w of windows) {
    const key = w.actionKey;
    if (!isPersistableKey(key)) continue;
    const [engine, ...rest] = key.split(":");
    const id = rest.join(":");
    if (!engine || !id) continue;
    out.push({
      key,
      engine,
      id,
      title: w.title,
      x: w.rect.x,
      y: w.rect.y,
      w: w.rect.w,
      h: w.rect.h,
      z: w.z,
      ...(w.minimized ? { minimized: true } : {}),
    });
  }
  return out;
}

/** Storage itself is fallible — a private-mode browser throws on write, a full quota throws on
 *  write, and a disabled-storage browser throws on READ. None of that may take the map down, so
 *  both directions swallow. A workspace that cannot persist is a workspace that behaves exactly
 *  as it did before #936. */
export function loadWorkspace(): StoredWindow[] {
  try {
    return decodeWorkspace(localStorage.getItem(WORKSPACE_KEY));
  } catch {
    return [];
  }
}

export function saveWorkspace(windows: WorkspaceWindow[]): void {
  try {
    const payload = encodeWorkspace(windows);
    // An empty workspace REMOVES the key rather than storing `[]`. "Nothing is open" and "this
    // device has never used the map" restore identically, and not leaving an empty array behind
    // keeps the storage inspector honest.
    if (!payload.length) localStorage.removeItem(WORKSPACE_KEY);
    else localStorage.setItem(WORKSPACE_KEY, JSON.stringify(payload));
  } catch {
    /* quota, private mode, storage disabled — never fatal */
  }
}

export function loadWindowCap(): number {
  try {
    const raw = localStorage.getItem(CAP_KEY);
    return raw === null ? clampWindowCap(undefined) : clampWindowCap(raw);
  } catch {
    return clampWindowCap(undefined);
  }
}

export function saveWindowCap(cap: number): void {
  try {
    localStorage.setItem(CAP_KEY, String(clampWindowCap(cap)));
  } catch {
    /* see saveWorkspace */
  }
}

/** A stored window → the seed the workspace opens from. One place that mapping lives, so the
 *  restore path and the request path hand the reducer the same shape. */
export function seedOf(w: StoredWindow): {
  key: string;
  engine: string;
  id: string;
  title: string;
} {
  return { key: w.key, engine: w.engine, id: w.id, title: w.title };
}

/** The rect a stored window asks for. Fitted to the real box by `clampRect` at render time, as
 *  every other window's is — the record keeps the operator's intent (#208). */
export function rectOf(w: StoredWindow): Rect {
  return { x: w.x, y: w.y, w: w.w, h: w.h };
}
