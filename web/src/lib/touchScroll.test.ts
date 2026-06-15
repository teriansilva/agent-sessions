import { expect, test, vi } from "vitest";
import { attachTouchScroll, dragToLines, type ScrollAccum } from "./touchScroll";

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

// --- gesture wiring: long-press selection vs. multi-touch ---------------------------

function touchEvent(type: string, points: Array<{ clientX: number; clientY: number }>) {
  const ev = new Event(type, { bubbles: true, cancelable: true });
  Object.defineProperty(ev, "touches", { value: points });
  return ev;
}

function fakeTerm() {
  return {
    rows: 24,
    scrollLines: () => {},
    focus: () => {},
    textarea: null,
    element: null,
    buffer: { active: { type: "normal" } },
  };
}

test("a still single-finger hold enters selection mode (long-press fires)", () => {
  vi.useFakeTimers();
  try {
    const surface = document.createElement("div");
    let longPresses = 0;
    const detach = attachTouchScroll(surface, fakeTerm(), {
      onLongPress: () => longPresses++,
      longPressMs: 400,
    });
    surface.dispatchEvent(touchEvent("touchstart", [{ clientX: 10, clientY: 10 }]));
    vi.advanceTimersByTime(500);
    expect(longPresses).toBe(1);
    detach();
  } finally {
    vi.useRealTimers();
  }
});

test("a second finger cancels the pending long-press (pinch never selects)", () => {
  // Regression for the multi-touch transition: the first finger's long-press timer must be
  // cancelled when a second finger joins, or a pinch wrongly drops into selection mode.
  vi.useFakeTimers();
  try {
    const surface = document.createElement("div");
    let longPresses = 0;
    const detach = attachTouchScroll(surface, fakeTerm(), {
      onLongPress: () => longPresses++,
      longPressMs: 400,
    });
    surface.dispatchEvent(touchEvent("touchstart", [{ clientX: 10, clientY: 10 }]));
    // second finger joins before the long-press threshold elapses
    surface.dispatchEvent(
      touchEvent("touchstart", [
        { clientX: 10, clientY: 10 },
        { clientX: 80, clientY: 80 },
      ]),
    );
    vi.advanceTimersByTime(500);
    expect(longPresses).toBe(0);
    detach();
  } finally {
    vi.useRealTimers();
  }
});
