/** The touch-target floor every e2e spec asserts (docs/design.md §8: 44 px wherever a finger is the
 *  pointer), with a float tolerance.
 *
 *  NIGHTJAR's arrival and deploy animations (#1531) put elements under a transform, and a box read
 *  mid-transform comes back as float residue: a 44 px control measured at 43.99997 px failed three
 *  CI runs in one day (compose-path, file-panel, mission-console-decisions). `PX_EPSILON` absorbs
 *  that residue and nothing more: a real violation is at least one device pixel short (≥ 0.33 css
 *  px on the 3× phones, 0.38 on the mobile project's 2.625×), thirty times the tolerance.
 *
 *  Assert `toBeGreaterThanOrEqual(TOUCH_FLOOR)`; a bare `toBeGreaterThanOrEqual(44)` is refused by
 *  src/theme/touchTargetFloor.test.ts. */
export const TOUCH_MIN = 44;
export const PX_EPSILON = 0.01;
export const TOUCH_FLOOR = TOUCH_MIN - PX_EPSILON;
