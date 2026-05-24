import { expect, test } from "vitest";
import { dragToLines, type ScrollAccum } from "./touchScroll";

test("converts a drag into whole lines by row height", () => {
  const acc: ScrollAccum = { remainder: 0 };
  expect(dragToLines(34, 17, acc)).toBe(2); // 34px / 17px per row = 2 lines
  expect(acc.remainder).toBe(0);
});

test("drag up vs down maps to positive vs negative scroll", () => {
  const acc: ScrollAccum = { remainder: 0 };
  expect(dragToLines(17, 17, acc)).toBe(1); // finger up → scroll toward newer
  expect(dragToLines(-17, 17, acc)).toBe(-1); // finger down → scroll toward older
});

test("sub-line drags accumulate via the carried remainder", () => {
  const acc: ScrollAccum = { remainder: 0 };
  // Three 6px nudges at 18px/row = 18px total → exactly 1 line, no loss to rounding.
  expect(dragToLines(6, 18, acc)).toBe(0);
  expect(dragToLines(6, 18, acc)).toBe(0);
  expect(dragToLines(6, 18, acc)).toBe(1);
  expect(acc.remainder).toBeCloseTo(0, 6);
});

test("unmeasured / invalid row height is a no-op (never NaN scrolls)", () => {
  const acc: ScrollAccum = { remainder: 0 };
  expect(dragToLines(50, 0, acc)).toBe(0);
  expect(dragToLines(50, Number.NaN, acc)).toBe(0);
  expect(acc.remainder).toBe(0);
});
