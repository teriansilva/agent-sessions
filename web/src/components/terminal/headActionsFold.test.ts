import { describe, expect, test } from "vitest";
import { foldSelection } from "./headActionsFold";

/** The fold arithmetic's unit table (#1109 P4, extended in #1329). The behaviour it decides —
 *  which chips stay on the bar and which fold into the ONE overflow menu — is pinned in the browser
 *  by `e2e/overview-window-chrome.spec.ts` (fold at a narrow window, single ⋯ menu); this table
 *  pins the ARITHMETIC itself, so a change to the budget model has to say what it costs.
 *
 *  Shapes used throughout: four labelled chips of 70px each (Files / Repaint / Recap /
 *  Hand off-sized), extended with two 50px chips (the text-size pair) where a case needs six.
 *  GAP is the real 6px; MORE_W is 34 (the self-hosted "…" chip) or 0 (a window's fold, whose
 *  trigger is the chrome's own ⋯, outside the slot's flow).
 *
 *  Boundaries, worked out: FOUR totals 280 + 3×6 = 298. A kth chip fits while its running
 *  width + GAP + MORE_W stays within `avail` — so with the trigger reserved the boundaries sit
 *  40px (34 + one gap) later than without. FOUR's per-count boundaries:
 *    external: 4 @ ≥298 · 3 @ ≥228 · 2 @ ≥152 · 1 below
 *    self:     4 @ ≥298 · 3 @ ≥262 · 2 @ ≥186 · 1 below (the 4-chip fold never reserves:
 *              the everything-fits branch returns first) */

const GAP = 6;
const SELF_MORE = 34;
const EXTERNAL_MORE = 0;

/** Four 70px chips → 280 + 3×6 = 298 total. */
const FOUR = [70, 70, 70, 70];
/** Six chips → 4×70 + 2×50 = 380 + 5×6 = 410 total. */
const SIX = [70, 70, 70, 70, 50, 50];
/** No action is menu-first — the #783/#1109 behaviour, unchanged. */
const NONE = (n: number) => Array(n).fill(false);

const kept = (sel: boolean[]) => sel.filter(Boolean).length;

describe("foldSelection", () => {
  test("everything fits → nothing folds, and the overflow chip costs nothing", () => {
    expect(kept(foldSelection(FOUR, 298, GAP, SELF_MORE, NONE(4)))).toBe(4);
    expect(kept(foldSelection(FOUR, 299, GAP, SELF_MORE, NONE(4)))).toBe(4);
    expect(kept(foldSelection(FOUR, 1000, GAP, EXTERNAL_MORE, NONE(4)))).toBe(4);
  });

  test("the overflow chip is reserved only once something actually overflows", () => {
    // 297: the fold begins — the 4th chip plus the reserved "…" (34) misses by one, so three
    // stay on the bar.
    expect(kept(foldSelection(FOUR, 297, GAP, SELF_MORE, NONE(4)))).toBe(3);
    // The 3rd chip's own boundary, trigger included: 70+6+70+6+70 + 6 + 34 = 262.
    expect(kept(foldSelection(FOUR, 262, GAP, SELF_MORE, NONE(4)))).toBe(3);
    expect(kept(foldSelection(FOUR, 261, GAP, SELF_MORE, NONE(4)))).toBe(2);
    expect(kept(foldSelection(FOUR, 186, GAP, SELF_MORE, NONE(4)))).toBe(2);
    expect(kept(foldSelection(FOUR, 185, GAP, SELF_MORE, NONE(4)))).toBe(1);
  });

  test("an external fold reserves nothing for a trigger it does not render", () => {
    // Same bars, no "…" chip of its own: every boundary sits exactly 40px lower (34 + gap).
    expect(kept(foldSelection(FOUR, 297, GAP, EXTERNAL_MORE, NONE(4)))).toBe(3); // 4th needs 304 > 297
    expect(kept(foldSelection(FOUR, 261, GAP, EXTERNAL_MORE, NONE(4)))).toBe(3); // the self fold would be 2 here
    expect(kept(foldSelection(FOUR, 228, GAP, EXTERNAL_MORE, NONE(4)))).toBe(3);
    expect(kept(foldSelection(FOUR, 227, GAP, EXTERNAL_MORE, NONE(4)))).toBe(2);
    expect(kept(foldSelection(FOUR, 152, GAP, EXTERNAL_MORE, NONE(4)))).toBe(2);
    expect(kept(foldSelection(FOUR, 151, GAP, EXTERNAL_MORE, NONE(4)))).toBe(1);
  });

  test("the fold is ordered from the END — the first action never folds", () => {
    // 200px: only the first two 70px chips fit (146 + trigger = 186; a third needs 262).
    expect(kept(foldSelection(SIX, 200, GAP, SELF_MORE, NONE(6)))).toBe(2);
    // Even a bar that cannot hold ONE chip keeps it: the floor of 1.
    expect(kept(foldSelection(SIX, 10, GAP, SELF_MORE, NONE(6)))).toBe(1);
    expect(kept(foldSelection(FOUR, 0, GAP, SELF_MORE, NONE(4)))).toBe(1);
  });

  test("a measured reserve shrinks the fold accordingly (the window chrome model)", () => {
    // A 720px window bar with a ~200px facts run + ~190px of fixed chrome: ~330 for chips —
    // the four labelled chips stay, the text-size pair folds.
    expect(kept(foldSelection(SIX, 330, GAP, EXTERNAL_MORE, NONE(6)))).toBe(4);
    // The 560px minimum with the same chrome: ~180 → two chips, the pair and the rest fold.
    expect(kept(foldSelection(SIX, 180, GAP, EXTERNAL_MORE, NONE(6)))).toBe(2);
  });

  test("a menu-first action folds before any trailing one (#1329)", () => {
    // The 3rd chip is menu-first (Recap in a window). At 228 three chips fit with the
    // trigger reserved — and the menu-first one gives way FIRST, so the 4th stays on the bar.
    expect(foldSelection(FOUR, 228, GAP, EXTERNAL_MORE, [false, false, true, false])).toEqual([
      true,
      true,
      false,
      true,
    ]);
    // With a tighter bar the trailing chip folds too — the menu-first one stays folded, and
    // the 4th (which the trailing fold would have kept at 228) now gives way as well.
    expect(foldSelection(FOUR, 152, GAP, EXTERNAL_MORE, [false, false, true, false])).toEqual([
      true,
      true,
      false,
      false,
    ]);
    // At 228 a three-chip bar fits either way — but the menu-first action gives way, so
    // the 3rd folds and the 4th stays, where the trailing fold would have dropped the 4th.
    expect(foldSelection(FOUR, 228, GAP, EXTERNAL_MORE, [false, false, true, false])).toEqual([
      true,
      true,
      false,
      true,
    ]);
    expect(foldSelection(FOUR, 228, GAP, EXTERNAL_MORE, [false, true, false, false])).toEqual([
      true,
      false,
      true,
      true,
    ]);
    // Everything fits → even a menu-first action stays a chip.
    expect(kept(foldSelection(FOUR, 298, GAP, EXTERNAL_MORE, [false, true, true, false]))).toBe(4);
  });
});
