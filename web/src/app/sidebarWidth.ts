/** Desktop sidebar width helpers (#507).
 *
 * Extracted from the App shell so the clamp / persistence logic is unit-testable on its own.
 * The width is device-local (localStorage, like the sidebar-collapse flag) and surfaced to the
 * grid as the `--sidebar-w` CSS variable. The upper bound is viewport-aware so a width saved on
 * a wide monitor can't crowd the pane after moving to a narrow window. */
export const WIDTH_KEY = "tr-sidebar-w";
export const DEFAULT_W = 320;
export const MIN_W = 220;
export const PANE_MIN = 360; // keep at least this much for the terminal/pane column
export const HARD_MAX_W = 560;
export const WIDTH_STEP = 16; // arrow-key nudge

/** Upper bound on the sidebar width — viewport-aware (never crowd the pane below PANE_MIN),
 *  hard-capped at HARD_MAX_W. Falls back to the hard cap when window isn't measurable yet. */
export function maxSidebarW(): number {
  const vw = typeof window !== "undefined" ? window.innerWidth : 0;
  return vw > 0
    ? Math.max(MIN_W, Math.min(HARD_MAX_W, vw - PANE_MIN))
    : HARD_MAX_W;
}

/** Clamp a candidate width to [MIN_W, maxSidebarW()] and round to a whole pixel. */
export function clampW(w: number): number {
  return Math.max(MIN_W, Math.min(maxSidebarW(), Math.round(w)));
}

/** The persisted width (clamped to the current viewport), or the default when unset/invalid. */
export function readStoredW(): number {
  const v = Number(localStorage.getItem(WIDTH_KEY));
  return Number.isFinite(v) && v > 0 ? clampW(v) : DEFAULT_W;
}
