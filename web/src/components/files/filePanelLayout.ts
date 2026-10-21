/** File-panel geometry (#783).
 *
 * Mirrors `app/sidebarWidth.ts`: device-local width, clamped against what the pane can actually
 * spare. The one thing that is NOT copied from there is the mode decision.
 *
 * **Dock-vs-sheet keys on available PANE width, not the viewport breakpoint.** The app's 800px
 * rule is the wrong signal here: with the 320px sidebar an ~801px viewport leaves a ~481px pane,
 * while a 340px panel plus the terminal's 360px minimum needs ~700px. Keying on the viewport
 * would dock the panel into a pane that cannot hold it and squeeze the terminal below its
 * minimum. So a narrow *desktop* pane gets the sheet too — the mode follows the measurement.
 */
export const WIDTH_KEY = "tr-filepanel-w";
export const DEFAULT_W = 340;
export const MIN_W = 260;
export const MAX_W = 560;
/** The terminal never goes below this; it is the hero surface, not the thing that yields. */
export const TERM_MIN = 360;
export const WIDTH_STEP = 16;

/** Widest the panel may be for a given pane width, or 0 when the pane cannot dock at all. */
export function maxPanelW(paneW: number): number {
  const spare = paneW - TERM_MIN;
  return spare >= MIN_W ? Math.min(MAX_W, spare) : 0;
}

/** Can this pane hold a docked panel *and* a usable terminal? */
export function canDock(paneW: number): boolean {
  return maxPanelW(paneW) > 0;
}

/** "dock" when the pane can afford both; "sheet" otherwise (narrow desktop pane included). */
export function panelMode(paneW: number): "dock" | "sheet" {
  return canDock(paneW) ? "dock" : "sheet";
}

export function clampW(w: number, paneW: number): number {
  const max = maxPanelW(paneW);
  if (max <= 0) return DEFAULT_W; // sheet mode: width is unused, keep the stored value sane
  return Math.max(MIN_W, Math.min(max, Math.round(w)));
}

export function readStoredW(): number {
  try {
    const v = Number(localStorage.getItem(WIDTH_KEY));
    return Number.isFinite(v) && v > 0 ? v : DEFAULT_W;
  } catch {
    return DEFAULT_W;
  }
}

export function storeW(w: number): void {
  try {
    localStorage.setItem(WIDTH_KEY, String(Math.round(w)));
  } catch {
    /* private mode / quota — the width is a nicety, never a hard failure */
  }
}
