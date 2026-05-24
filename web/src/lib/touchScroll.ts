// Touch-scroll math for the terminal. xterm doesn't scroll its scrollback on a
// one-finger drag (it captures touch for selection), which is why the terminal felt
// "stuck" on phones. We translate a drag into whole xterm line scrolls ourselves; this
// pure helper carries the sub-line remainder so slow drags still accumulate smoothly.

export interface ScrollAccum {
  /** Fractional lines left over from previous moves, carried into the next. */
  remainder: number;
}

/** Lines to scroll for a touch-drag of `dyPx` (one move), given the pixel height of a
 *  terminal row. Convention: `dyPx > 0` = finger moved up = scroll the buffer toward
 *  newer output (positive xterm `scrollLines`). The remainder is updated in place so a
 *  sequence of small drags accumulates to whole lines instead of being lost to rounding.
 */
export function dragToLines(dyPx: number, pxPerRow: number, acc: ScrollAccum): number {
  if (!(pxPerRow > 0)) return 0; // unmeasured row height → no-op (also guards NaN)
  const total = acc.remainder + dyPx / pxPerRow;
  const lines = Math.trunc(total);
  acc.remainder = total - lines;
  return lines;
}

interface Scrollable {
  rows: number;
  scrollLines: (n: number) => void;
}

/** Wire one-finger touch scrolling onto `host` for an xterm-like `term`. Returns a
 *  cleanup fn. Listeners are attached in the **capture** phase so they run before
 *  xterm's own screen/selection touch handling, and `touchmove` is non-passive so we
 *  can `preventDefault` (the page must not pan under the drag). A tap (no movement)
 *  is left untouched so it still focuses the terminal / opens the keyboard. */
export function attachTouchScroll(host: HTMLElement, term: Scrollable): () => void {
  const acc: ScrollAccum = { remainder: 0 };
  let lastY = 0;
  let dragging = false;

  const onStart = (e: TouchEvent) => {
    if (e.touches.length !== 1) return;
    lastY = e.touches[0].clientY;
    acc.remainder = 0;
    dragging = true;
  };
  const onMove = (e: TouchEvent) => {
    if (!dragging || e.touches.length !== 1) return;
    const y = e.touches[0].clientY;
    const dy = lastY - y; // finger up (dy>0) → scroll toward newer output
    lastY = y;
    const lines = dragToLines(dy, host.clientHeight / (term.rows || 24), acc);
    if (lines !== 0) term.scrollLines(lines);
    if (e.cancelable) e.preventDefault();
  };
  const onEnd = () => {
    dragging = false;
  };

  host.addEventListener("touchstart", onStart, { passive: true, capture: true });
  host.addEventListener("touchmove", onMove, { passive: false, capture: true });
  host.addEventListener("touchend", onEnd, { passive: true, capture: true });
  host.addEventListener("touchcancel", onEnd, { passive: true, capture: true });
  return () => {
    host.removeEventListener("touchstart", onStart, { capture: true });
    host.removeEventListener("touchmove", onMove, { capture: true });
    host.removeEventListener("touchend", onEnd, { capture: true });
    host.removeEventListener("touchcancel", onEnd, { capture: true });
  };
}
