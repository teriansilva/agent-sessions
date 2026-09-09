/** Geometry + policy for the map's window workspace (#208).
 *
 *  Pure and React-free — no @xyflow, no DOM — for the same reason `overviewGraph.ts` is: the
 *  rules that decide where a window LANDS (and whether it may open at all) are the part worth
 *  pinning in a fast unit test, separately from the browser test that proves the pointer
 *  handling. Every coordinate here is **overlay-local** (origin = the overlay box's top-left),
 *  never viewport-absolute: the overlay sits inside the app shell, so the map's origin is never
 *  the viewport's, and a tether computed in viewport coordinates is right only at the origin.
 */

/** DEFAULT ceiling on simultaneously open windows. Each one is a full xterm + WebSocket +
 *  scrollback buffer, so this is a resource guard for the operator's own box — NOT a security
 *  control (a client-side cap is trivially bypassable; nothing may rely on it as one).
 *
 *  The operator can move it (#936). It stays a *constant default* rather than becoming a plain
 *  variable because the reason it is 8 has not changed — 8 is what a typical box carries without
 *  the map getting sluggish — and a fresh install should still land there. */
export const WINDOW_CAP = 8;

/** The range the operator may choose from. The floor is 1 (a workspace of one is still a
 *  workspace; 0 would silently disable a feature the map advertises), and the ceiling is 16 —
 *  double the default, which is generous for the resource this guards without pretending a
 *  browser will hold an arbitrary number of live ptys. */
export const WINDOW_CAP_MIN = 1;
export const WINDOW_CAP_MAX = 16;

/** Coerce anything to a usable cap. Lenient by construction, exactly like the terminal font
 *  size's read path (#859): the value comes back out of `localStorage`, which the operator can
 *  edit, and a workspace that refuses to open ANY window because a stored value is `"lots"` is a
 *  worse failure than one that quietly uses the default. Non-finite / non-numeric → the default;
 *  anything else → floored to an integer and clamped into range.
 *
 *  Rounding is `floor(x + 0.5)`, never `Math.round`, for the same reason `termSize` spells it
 *  that way: the two disagree on negative half values, and one spelling per repo is one fewer
 *  thing to get wrong. */
export function clampWindowCap(value: unknown): number {
  // Only a number, or a string that is one. NOT a bare `Number(value)` coercion: `Number(null)`
  // and `Number([])` are both `0`, which would clamp to the MINIMUM and quietly leave the
  // operator a one-window workspace because a storage entry was missing or malformed. Those
  // inputs mean "no value", and no value means the default.
  const n =
    typeof value === "number"
      ? value
      : typeof value === "string" && value.trim() !== ""
        ? Number(value)
        : NaN;
  if (!Number.isFinite(n)) return WINDOW_CAP;
  const i = Math.floor(n + 0.5);
  return Math.min(Math.max(i, WINDOW_CAP_MIN), WINDOW_CAP_MAX);
}

/** Opening size. Roomy enough that the agent gets a usable grid at the default font size. */
export const DEFAULT_SIZE: Size = { w: 720, h: 480 };

/** Floor on a window's size, enforced on open AND on resize. This is load-bearing rather than
 *  cosmetic: the terminal font size is a DEVICE-global pref (#859/#860) and it is what decides
 *  the agent's column count, so a narrower pane hands a column-laid-out TUI a grid it cannot
 *  lay out against (the #859 failure, one window at a time). */
export const MIN_SIZE: Size = { w: 560, h: 320 };

/** Per-already-open-window offset, so a second window on the same anchor doesn't land exactly
 *  on top of the first. */
export const CASCADE_STEP = 24;

/** The window chrome bar height. Mirrored in sessionWindow.module.css. */
export const CHROME_H = 30;

/** Gap between the anchor chip and the window that cascades off it. */
const ANCHOR_GAP = 16;

export interface Point {
  x: number;
  y: number;
}

export interface Size {
  w: number;
  h: number;
}

export interface Rect extends Point, Size {}

const clamp = (v: number, lo: number, hi: number): number =>
  Math.min(Math.max(v, lo), hi);

/** Round to a whole pixel — window rects go straight into inline styles and an SVG path, and
 *  sub-pixel churn is diff noise nobody can see. */
const px = (v: number): number => Math.round(v);

/** Fit a window inside the overlay box.
 *
 *  Size first: never below `MIN_SIZE`, but never larger than the box either — on a small map
 *  area the minimum loses, because a window wider than its container cannot be dragged back
 *  into view. Position second, against the ALREADY-CLAMPED size, so the whole window (chrome
 *  included) stays inside. A box smaller than the window collapses the range to `0`, which
 *  parks it at the top-left rather than off-screen.
 *
 *  Called on open, on drag, on resize AND **on every render** — the window record keeps the size
 *  and position the OPERATOR chose, and this fits that intent to the box currently available. A
 *  shrinking map therefore moves a window without destroying its layout: grow the map back and
 *  the window returns to the size it had, because nothing overwrote it on the way down.
 *
 *  The box beating `MIN_SIZE` is the one deliberate exception to the floor, and it is a
 *  reachability trade: a window wider than its container could never be dragged back into view.
 *  It only arises for a window that is ALREADY open (`canHostWindow` refuses to open one in a
 *  box this small), and it is not destructive, so the floor is restored along with the box. */
export function clampRect(rect: Rect, bounds: Size): Rect {
  // A box that has not been measured yet (or is display:none) is not a constraint — clamping
  // into it would produce a 1px window rather than a window waiting for a layout. Keep the
  // requested size at its floor and let the next measurement place it.
  if (bounds.w <= 0 || bounds.h <= 0)
    return {
      x: 0,
      y: 0,
      w: px(Math.max(rect.w, MIN_SIZE.w)),
      h: px(Math.max(rect.h, MIN_SIZE.h)),
    };
  const w = Math.min(Math.max(rect.w, MIN_SIZE.w), Math.max(bounds.w, 1));
  const h = Math.min(Math.max(rect.h, MIN_SIZE.h), Math.max(bounds.h, 1));
  return {
    x: px(clamp(rect.x, 0, Math.max(0, bounds.w - w))),
    y: px(clamp(rect.y, 0, Math.max(0, bounds.h - h))),
    w: px(w),
    h: px(h),
  };
}

/** Where a newly opened window lands: cascaded off its anchor chip, then clamped.
 *
 *  `anchor` is the chip's projected position in overlay-local coordinates, or `null` when the
 *  chip isn't on the map (a filtered or collapsed session can still be opened from elsewhere) —
 *  in which case the cascade starts at the box's top-left. */
export function cascadeRect(
  anchor: Point | null,
  openCount: number,
  bounds: Size,
  size: Size = DEFAULT_SIZE,
): Rect {
  const step = openCount * CASCADE_STEP;
  const base = anchor ?? { x: 0, y: 0 };
  return clampRect(
    {
      x: base.x + ANCHOR_GAP + step,
      y: base.y + ANCHOR_GAP + step,
      w: size.w,
      h: size.h,
    },
    bounds,
  );
}

/** Can this overlay box hold a window at its floor?
 *
 *  The workspace needs room, and "desktop" alone does not guarantee it: at an 801px viewport the
 *  breakpoint still says desktop while the expanded sidebar leaves ~460px of map, which is BELOW
 *  `MIN_SIZE.w`. Opening there would hand the agent a column count the floor exists to prevent,
 *  so the map falls back to navigating instead — the behaviour it has always had. */
export function canHostWindow(bounds: Size): boolean {
  return bounds.w >= MIN_SIZE.w && bounds.h >= MIN_SIZE.h;
}

/** May another window open? The one past the cap mounts nothing — it says so instead (#208).
 *
 *  `cap` is passed in rather than read from a module constant (#936): the ceiling is now the
 *  operator's, and a pure predicate that reads device state behind the caller's back is the kind
 *  of thing that makes a reducer untestable. Defaulted so every existing call site keeps its
 *  meaning.
 *
 *  Lowering the cap below the number of ALREADY-open windows is deliberately not this function's
 *  problem: it refuses the next open and nothing else. Closing windows on a settings change would
 *  be destructive, and the cap is a resource guard, never a security control. */
export function canOpen(openCount: number, cap: number = WINDOW_CAP): boolean {
  return openCount < clampWindowCap(cap);
}

/** Where a window's tether attaches: the middle of its chrome bar, on the edge nearest the
 *  anchor, so the line never crosses the window it points at. */
export function tetherAnchor(rect: Rect, from: Point): Point {
  const y = rect.y + CHROME_H / 2;
  return from.x <= rect.x ? { x: rect.x, y } : { x: rect.x + rect.w, y };
}

/** Keep a tether endpoint inside the overlay box.
 *
 *  An anchor projected outside the visible map (panned away, zoomed past the edge) is clipped
 *  to the boundary rather than drawn across the toolbar and the minimap. */
export function clipToBounds(p: Point, bounds: Size): Point {
  return {
    x: px(clamp(p.x, 0, Math.max(0, bounds.w))),
    y: px(clamp(p.y, 0, Math.max(0, bounds.h))),
  };
}

/** Horizontal cubic between chip and window — the control points sit on the midpoint x, so the
 *  line leaves the chip and meets the window horizontally whatever the vertical offset. */
export function tetherPath(from: Point, to: Point): string {
  const mx = px((from.x + to.x) / 2);
  return `M ${px(from.x)} ${px(from.y)} C ${mx} ${px(from.y)}, ${mx} ${px(to.y)}, ${px(to.x)} ${px(to.y)}`;
}
