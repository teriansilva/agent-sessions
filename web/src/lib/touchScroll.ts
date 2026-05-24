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
