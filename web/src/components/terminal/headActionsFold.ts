/** The measured fold's arithmetic (#783, extracted in #1109 for the unit table).
 *
 *  Lives in its own module — not beside `HeadActions` — because a component file that also
 *  exports a function breaks the react-refresh/fast-refresh boundary, and this is the one
 *  piece of the fold worth pinning in a fast unit test (`headActionsFold.test.ts`) separately
 *  from the browser spec that pins the rendered result.
 *
 *  How many of the actions fit a bar that has `avail` px for them. Everything fits →
 *  `widths.length` (nothing folds, no overflow chip is reserved). Otherwise the count that
 *  fits with room for the overflow chip — `moreW` 0 when the host renders that trigger itself
 *  (`foldInto: "external"`) — floored at 1, because the FIRST action never folds: Files leads,
 *  and burying the primary affordance is the trade the pane's own header refuses. */
export function foldCount(
  widths: number[],
  avail: number,
  gap: number,
  moreW: number,
): number {
  const total = widths.reduce((a, b) => a + b, 0) + gap * (widths.length - 1);
  if (total <= avail) return widths.length;
  let used = 0;
  let fit = 0;
  for (let i = 0; i < widths.length; i++) {
    const next = used + widths[i] + (i ? gap : 0);
    if (next + gap + moreW > avail) break;
    used = next;
    fit++;
  }
  return Math.max(1, fit);
}
