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
  /** xterm's root element — for forwarding wheel to mouse-tracking / alt-screen apps. */
  element?: HTMLElement | null;
  /** Live xterm buffer; we read `active.type` to detect the alternate screen. */
  buffer?: { active?: { type?: string } };
  /** Live xterm modes; `mouseTrackingMode !== 'none'` ⇒ the app consumes scroll itself. */
  modes?: { mouseTrackingMode?: string };
}

// A touch that moves less than this (px) is treated as a tap, not a scroll.
const TAP_SLOP = 8;
// Momentum (fling) after lift: velocity in px/ms, decayed each frame; stops below MIN.
const FRICTION = 0.95; // per ~16.7ms frame
const MIN_FLING_V = 0.04; // px/ms — below this, don't start / stop the glide
const STALE_LIFT_MS = 70; // if the finger paused before lifting, no fling

export interface TouchScrollHandlers {
  /** A tap (touch down+up with no scroll). Coords are clientX/Y. If provided, REPLACES the
   *  default focus-keyboard behavior — the caller decides (e.g. open a link under the tap,
   *  else open the keyboard). */
  onTap?: (clientX: number, clientY: number) => void;
  /** A press-and-hold with the finger still (no scroll) for `longPressMs`. Used to enter
   *  text-selection mode. While selecting, scrolling/tap for this gesture is suppressed. */
  onLongPress?: (clientX: number, clientY: number) => void;
  /** Long-press threshold (ms). Default 450. */
  longPressMs?: number;
}

// Default long-press threshold before selection mode arms.
const LONG_PRESS_MS = 450;

/** Wire one-finger touch scrolling onto `surface` for an xterm-like `term`. Returns
 *  `{ detach, stopMomentum }`: `detach` tears everything down; `stopMomentum` halts an
 *  in-flight post-lift glide WITHOUT detaching — the scroll-to-bottom FAB calls it so a
 *  tap-to-jump can't be dragged back up by leftover fling velocity (the FAB paints above
 *  this overlay and receives the tap itself, so `onStart`'s own `stopFling` never runs for
 *  it — #519 follow-up). `surface` is a transparent capture overlay over the terminal:
 *  claiming the touch there (xterm never sees it) is the only thing that scrolls reliably on
 *  Android — its text layer otherwise hijacks the drag. A quick drag scrolls (+ momentum
 *  after lift); a tap runs `onTap` (or, by default, (re)opens the keyboard); a press-and-hold
 *  runs `onLongPress` (selection mode). */
export function attachTouchScroll(
  surface: HTMLElement,
  term: Scrollable,
  handlers: TouchScrollHandlers = {},
): { detach: () => void; stopMomentum: () => void } {
  const acc: ScrollAccum = { remainder: 0 };
  let lastY = 0;
  let startY = 0;
  let startX = 0;
  let moved = false;
  let dragging = false;
  let longPressed = false; // this gesture became a selection long-press → suppress scroll/tap
  let longPressTimer: ReturnType<typeof setTimeout> | 0 = 0;
  let velocity = 0; // px/ms, signed like dy (smoothed across moves)
  let lastMoveT = 0;
  let fling = 0; // rAF handle for the post-lift glide (0 = none)
  const cancelLongPress = () => {
    if (longPressTimer) clearTimeout(longPressTimer);
    longPressTimer = 0;
  };

  const pxPerRow = () => surface.clientHeight / (term.rows || 24);
  const stopFling = () => {
    if (fling) cancelAnimationFrame(fling);
    fling = 0;
  };
  // Apps that consume scroll themselves — a mouse-tracking TUI (opencode) and/or one drawing in
  // the alternate screen — keep NO xterm scrollback, so term.scrollLines() is a no-op (#414). A
  // desktop wheel still scrolls them because xterm routes the wheel to the app (mouse-wheel
  // reports / alternate-scroll) whenever mouse tracking is on, regardless of buffer. We mirror
  // that exactly: when the app wants the wheel, synthesize one on xterm's screen element and let
  // xterm do its own translation (honoring the app's mouse mode) — no re-encoding of protocols.
  // Otherwise (e.g. claude in the normal buffer with no mouse tracking) scroll xterm's scrollback.
  // Gating on mouseTrackingMode (not just the alt buffer) is the fix: opencode runs in the NORMAL
  // buffer with mouse tracking, so an alt-buffer-only check never engaged for it.
  const appConsumesWheel = () =>
    (term.modes?.mouseTrackingMode ?? "none") !== "none" ||
    term.buffer?.active?.type === "alternate";
  const wheelTarget = () =>
    term.element?.querySelector<HTMLElement>(".xterm-screen") ?? term.element ?? null;
  const scrollByPx = (dyPx: number) => {
    const lines = dragToLines(dyPx, pxPerRow(), acc);
    if (lines === 0) return;
    if (appConsumesWheel()) {
      const target = wheelTarget();
      // deltaY>0 = scroll toward newer output (down), matching positive scrollLines(). Give the
      // synthetic wheel real pointer coords at the screen centre so xterm encodes a valid cell
      // (a bare 0,0 can land outside the screen on a laid-out page).
      if (target) {
        const r = target.getBoundingClientRect();
        target.dispatchEvent(
          new WheelEvent("wheel", {
            deltaY: lines * pxPerRow(),
            deltaMode: 0, // DOM_DELTA_PIXEL — xterm divides by cell height into wheel notches
            clientX: Math.round(r.left + r.width / 2),
            clientY: Math.round(r.top + r.height / 2),
            bubbles: true,
            cancelable: true,
          }),
        );
      }
      return;
    }
    term.scrollLines(lines);
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

  // A second finger joined (pinch / multi-touch): abandon our one-finger gesture entirely so
  // we never fire a long-press, tap, or scroll mid-pinch. Cancels the pending long-press and
  // marks the gesture non-tap + non-dragging, so the rest of it is ignored until all fingers
  // lift and a fresh single-finger touch starts. Restores the "let multi-touch through" contract.
  const abortGesture = () => {
    cancelLongPress();
    moved = true;
    dragging = false;
  };

  const onStart = (e: TouchEvent) => {
    if (e.touches.length !== 1) {
      abortGesture();
      return;
    }
    stopFling(); // a new touch catches/halts an ongoing glide (like native)
    lastY = startY = e.touches[0].clientY;
    startX = e.touches[0].clientX;
    lastMoveT = performance.now();
    velocity = 0;
    acc.remainder = 0;
    dragging = true;
    moved = false;
    longPressed = false;
    // Arm selection mode if the finger stays put. A scroll (movement past TAP_SLOP) or lift
    // cancels it first.
    cancelLongPress();
    if (handlers.onLongPress) {
      longPressTimer = setTimeout(() => {
        if (!dragging || moved) return;
        longPressed = true;
        handlers.onLongPress?.(startX, startY);
      }, handlers.longPressMs ?? LONG_PRESS_MS);
    }
    if (e.cancelable) e.preventDefault(); // claim the gesture (overlay has no selection)
  };
  const onMove = (e: TouchEvent) => {
    if (e.touches.length !== 1) {
      abortGesture(); // a second finger joined mid-drag → bail out of our handling
      return;
    }
    if (!dragging || longPressed) return;
    const now = performance.now();
    const y = e.touches[0].clientY;
    const dy = lastY - y; // finger up (dy>0) → scroll toward newer output
    lastY = y;
    const dt = now - lastMoveT;
    lastMoveT = now;
    if (dt > 0) velocity = 0.8 * velocity + 0.2 * (dy / dt); // smoothed px/ms
    if (Math.abs(y - startY) > TAP_SLOP) {
      moved = true;
      cancelLongPress(); // it's a scroll, not a hold
    }
    scrollByPx(dy);
    if (e.cancelable) e.preventDefault();
  };
  const onEnd = () => {
    cancelLongPress();
    if (!dragging) return;
    dragging = false;
    if (longPressed) return; // selection mode took over; nothing to do on lift
    if (!moved) {
      // A tap: caller-defined (link-or-keyboard) if provided, else just open the keyboard.
      if (handlers.onTap) handlers.onTap(startX, startY);
      else focusKeyboard();
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
  return {
    detach: () => {
      stopFling();
      cancelLongPress();
      surface.removeEventListener("touchstart", onStart, { capture: true });
      surface.removeEventListener("touchmove", onMove, { capture: true });
      surface.removeEventListener("touchend", onEnd, { capture: true });
      surface.removeEventListener("touchcancel", onEnd, { capture: true });
    },
    stopMomentum: stopFling,
  };
}
