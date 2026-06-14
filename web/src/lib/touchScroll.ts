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
  focus?: () => void;
  /** xterm's hidden input — blur+focus to (re)open the mobile keyboard on tap. */
  textarea?: HTMLTextAreaElement | null;
}

// A touch that moves less than this (px) is treated as a tap, not a scroll.
const TAP_SLOP = 8;
// Momentum (fling) after lift: velocity in px/ms, decayed each frame; stops below MIN.
const FRICTION = 0.95; // per ~16.7ms frame
const MIN_FLING_V = 0.04; // px/ms — below this, don't start / stop the glide
const STALE_LIFT_MS = 70; // if the finger paused before lifting, no fling

/** Wire one-finger touch scrolling onto `surface` for an xterm-like `term`; returns a
 *  cleanup fn. `surface` is a transparent capture overlay over the terminal: claiming
 *  the touch there (xterm never sees it) is the only thing that scrolls reliably on
 *  Android — its text layer otherwise hijacks the drag. A quick drag scrolls (+ momentum
 *  after lift); a tap (re)opens the keyboard via xterm's textarea. */
export function attachTouchScroll(surface: HTMLElement, term: Scrollable): () => void {
  const acc: ScrollAccum = { remainder: 0 };
  let lastY = 0;
  let startY = 0;
  let moved = false;
  let dragging = false;
  let velocity = 0; // px/ms, signed like dy (smoothed across moves)
  let lastMoveT = 0;
  let fling = 0; // rAF handle for the post-lift glide (0 = none)

  const pxPerRow = () => surface.clientHeight / (term.rows || 24);
  const stopFling = () => {
    if (fling) cancelAnimationFrame(fling);
    fling = 0;
  };
  const scrollByPx = (dyPx: number) => {
    const lines = dragToLines(dyPx, pxPerRow(), acc);
    if (lines !== 0) term.scrollLines(lines);
  };
  // Tap → (re)open the keyboard. Blur+focus so it reopens even when the textarea is
  // already the focused element (a plain focus() would be a no-op and stay closed).
  const focusKeyboard = () => {
    const ta = term.textarea;
    if (ta) {
      ta.blur();
      ta.focus();
    } else {
      term.focus?.();
    }
  };

  const onStart = (e: TouchEvent) => {
    if (e.touches.length !== 1) return; // let multi-touch (pinch-zoom) through
    stopFling(); // a new touch catches/halts an ongoing glide (like native)
    lastY = startY = e.touches[0].clientY;
    lastMoveT = performance.now();
    velocity = 0;
    acc.remainder = 0;
    dragging = true;
    moved = false;
    if (e.cancelable) e.preventDefault(); // claim the gesture (overlay has no selection)
  };
  const onMove = (e: TouchEvent) => {
    if (!dragging || e.touches.length !== 1) return;
    const now = performance.now();
    const y = e.touches[0].clientY;
    const dy = lastY - y; // finger up (dy>0) → scroll toward newer output
    lastY = y;
    const dt = now - lastMoveT;
    lastMoveT = now;
    if (dt > 0) velocity = 0.8 * velocity + 0.2 * (dy / dt); // smoothed px/ms
    if (Math.abs(y - startY) > TAP_SLOP) moved = true;
    scrollByPx(dy);
    if (e.cancelable) e.preventDefault();
  };
  const onEnd = () => {
    if (!dragging) return;
    dragging = false;
    if (!moved) {
      focusKeyboard(); // a tap opens the keyboard
      return;
    }
    // Fling: if the finger was still moving at lift, keep gliding with friction decay.
    if (performance.now() - lastMoveT > STALE_LIFT_MS || Math.abs(velocity) < MIN_FLING_V) return;
    let v = velocity;
    let prev = performance.now();
    const step = (t: number) => {
      const dt = t - prev;
      prev = t;
      scrollByPx(v * dt);
      v *= FRICTION ** (dt / 16.67);
      fling = Math.abs(v) > MIN_FLING_V ? requestAnimationFrame(step) : 0;
    };
    fling = requestAnimationFrame(step);
  };

  surface.addEventListener("touchstart", onStart, { passive: false, capture: true });
  surface.addEventListener("touchmove", onMove, { passive: false, capture: true });
  surface.addEventListener("touchend", onEnd, { passive: true, capture: true });
  surface.addEventListener("touchcancel", onEnd, { passive: true, capture: true });
  return () => {
    stopFling();
    surface.removeEventListener("touchstart", onStart, { capture: true });
    surface.removeEventListener("touchmove", onMove, { capture: true });
    surface.removeEventListener("touchend", onEnd, { capture: true });
    surface.removeEventListener("touchcancel", onEnd, { capture: true });
  };
}
