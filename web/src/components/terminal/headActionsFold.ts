/** The measured fold's arithmetic (#783, extracted in #1109 for the unit table).
 *
 *  Lives in its own module — not beside `HeadActions` — because a component file that also
 *  exports a function breaks the react-refresh/fast-refresh boundary, and this is the one
 *  piece of the fold worth pinning in a fast unit test (`headActionsFold.test.ts`) separately
 *  from the browser spec that pins the rendered result.
 *
 *  `foldSelection` answers WHICH actions stay on the bar, not just how many (#1329): a
 *  `menuFirst` action is one whose canonical home is the overflow menu, so it folds BEFORE any
 *  trailing action — a window's chrome bar is slim by default and only surfaces those chips
 *  when the bar has room to spare. The result is a per-action boolean, so folding a middle
 *  `menuFirst` action leaves no hole: the flex row packs whatever is left.
 *
 *  Rules that survive from #783/#1109:
 *  - everything fits → nothing folds, and the overflow trigger is not reserved;
 *  - once something folds the trigger is reserved (`moreW` is 0 when the host renders that
 *    trigger itself, `foldInto: "external"`);
 *  - the FIRST action never folds — Files leads, and burying the primary affordance is the
 *    trade the pane's own header refuses — so the floor is one visible action. */
export function foldSelection(
  widths: number[],
  avail: number,
  gap: number,
  moreW: number,
  menuFirst: boolean[],
): boolean[] {
  const n = widths.length;
  const visible = widths.map(() => true);
  const width = () => {
    let sum = 0;
    let count = 0;
    for (let i = 0; i < n; i++) {
      if (!visible[i]) continue;
      sum += widths[i];
      count++;
    }
    return sum + gap * Math.max(0, count - 1);
  };
  if (width() <= avail) return visible;
  // Once something folds the trigger is rendered, so the budget must also carry the gap
  // between the last remaining chip and that trigger — the #783 boundary is
  // `width + gap + moreW <= avail`.
  const budget = avail - moreW - gap;
  // Menu-first actions fold first, from the last of them to the first: a `menuFirst` action
  // is one the overflow menu already carries under a stable name, so it is the one that gives way.
  for (let i = n - 1; i >= 0; i--) {
    if (!menuFirst[i] || !visible[i]) continue;
    visible[i] = false;
    if (width() <= budget) return visible;
  }
  // Then the rest fold from the END, exactly as #783 did — never the first action.
  for (let i = n - 1; i >= 1; i--) {
    if (!visible[i]) continue;
    visible[i] = false;
    if (width() <= budget) return visible;
  }
  return visible;
}
