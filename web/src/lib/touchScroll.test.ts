import { expect, test, vi } from "vitest";
import {
  appConsumesWheel,
  attachTouchScroll,
  dragToLines,
  type ScrollAccum,
} from "./touchScroll";

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

function touchEvent(
  type: string,
  points: Array<{ clientX: number; clientY: number }>,
) {
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
    const { detach } = attachTouchScroll(surface, fakeTerm(), {
      onLongPress: () => longPresses++,
      longPressMs: 400,
    });
    surface.dispatchEvent(
      touchEvent("touchstart", [{ clientX: 10, clientY: 10 }]),
    );
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
    const { detach } = attachTouchScroll(surface, fakeTerm(), {
      onLongPress: () => longPresses++,
      longPressMs: 400,
    });
    surface.dispatchEvent(
      touchEvent("touchstart", [{ clientX: 10, clientY: 10 }]),
    );
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

test("exposes detach + stopMomentum; stopMomentum halts an in-flight fling", () => {
  // The scroll-to-bottom FAB paints above the touch overlay and takes the tap itself, so the
  // overlay's onStart (which would stopFling) never runs for it. attachTouchScroll therefore
  // exposes stopMomentum so the FAB's jump-to-tail can cancel leftover glide velocity that would
  // otherwise drag the view straight back off the tail. Here: build velocity with quick moves,
  // lift to start the fling, then assert stopMomentum freezes further scrolling.
  vi.useFakeTimers();
  const rafSpy = vi.spyOn(globalThis, "requestAnimationFrame");
  try {
    const surface = document.createElement("div");
    Object.defineProperty(surface, "clientHeight", {
      value: 480,
      configurable: true,
    });
    let scrolled = 0;
    const term = { ...fakeTerm(), scrollLines: (n: number) => (scrolled += n) };
    const api = attachTouchScroll(surface, term, {});
    expect(typeof api.detach).toBe("function");
    expect(typeof api.stopMomentum).toBe("function");

    const move = (type: string, y: number, empty = false) =>
      surface.dispatchEvent(
        touchEvent(type, empty ? [] : [{ clientX: 10, clientY: y }]),
      );
    move("touchstart", 400);
    for (let y = 380; y >= 200; y -= 20) {
      vi.advanceTimersByTime(8); // fast, steady drag → non-trivial velocity
      move("touchmove", y);
    }
    move("touchend", 0, true); // lift while moving → starts the momentum fling (rAF)
    expect(rafSpy).toHaveBeenCalled(); // a glide is scheduled

    // Cancel momentum, then let time pass: no further scrolling may accrue.
    api.stopMomentum();
    const after = scrolled;
    vi.advanceTimersByTime(500);
    expect(scrolled).toBe(after); // fling frozen — the FAB's jump-to-tail can't be undone
    api.detach();
  } finally {
    rafSpy.mockRestore();
    vi.useRealTimers();
  }
});

// --- app-consuming (mouse-tracking / alt-screen) scroll: FAB parity for claude (#559) --------

// A session whose app owns the scroll: a mouse-tracking TUI with a real .xterm-screen element so
// the forwarded synthetic wheels have a target.
function fakeAppTerm() {
  const element = document.createElement("div");
  const screen = document.createElement("div");
  screen.className = "xterm-screen";
  element.appendChild(screen);
  return {
    rows: 24,
    scrollLines: () => {},
    focus: () => {},
    textarea: null,
    element,
    buffer: { active: { type: "normal" as const } },
    modes: { mouseTrackingMode: "any" },
    screen,
  };
}

test("appConsumesWheel: mouse-tracking OR alt-screen consumes the wheel; a plain normal buffer does not", () => {
  // claude/opencode: mouse tracking on → app owns the scroll.
  expect(
    appConsumesWheel({
      modes: { mouseTrackingMode: "any" },
      buffer: { active: { type: "normal" } },
    }),
  ).toBe(true);
  // alt-screen even without mouse tracking.
  expect(
    appConsumesWheel({
      modes: { mouseTrackingMode: "none" },
      buffer: { active: { type: "alternate" } },
    }),
  ).toBe(true);
  // codex/gemini inline: no mouse tracking, normal buffer → xterm keeps real scrollback.
  expect(
    appConsumesWheel({
      modes: { mouseTrackingMode: "none" },
      buffer: { active: { type: "normal" } },
    }),
  ).toBe(false);
  expect(appConsumesWheel({})).toBe(false); // defensive: unknown modes/buffer
});

test("app-consuming session: a touch drag forwards a wheel to the app and reports the scroll direction", () => {
  const surface = document.createElement("div");
  Object.defineProperty(surface, "clientHeight", {
    value: 480,
    configurable: true,
  }); // 20px/row
  const term = fakeAppTerm();
  const dirs: number[] = [];
  let wheels = 0;
  term.screen.addEventListener("wheel", () => wheels++);
  const api = attachTouchScroll(surface, term, {
    onAppScroll: (d) => dirs.push(d),
  });
  const move = (type: string, y: number, empty = false) =>
    surface.dispatchEvent(
      touchEvent(type, empty ? [] : [{ clientX: 10, clientY: y }]),
    );
  move("touchstart", 100);
  move("touchmove", 160); // finger DOWN 60px → scroll UP into history (lines < 0)
  expect(wheels).toBeGreaterThan(0); // forwarded a synthetic wheel to the app (not scrollLines)
  expect(dirs.some((d) => d < 0)).toBe(true); // reported an "up" notch → FAB should show
  api.detach();
});

test("jumpToTail forwards a downward wheel burst for an app-consuming session; no-op for a scrollback session", () => {
  const surface = document.createElement("div");
  Object.defineProperty(surface, "clientHeight", {
    value: 480,
    configurable: true,
  });
  const term = fakeAppTerm();
  const deltas: number[] = [];
  term.screen.addEventListener("wheel", (e) =>
    deltas.push((e as WheelEvent).deltaY),
  );
  const api = attachTouchScroll(surface, term, {});
  api.jumpToTail(5); // fewer than a screenful → floored to term.rows so a tap always moves
  expect(deltas.length).toBeGreaterThanOrEqual(term.rows);
  expect(deltas.every((d) => d > 0)).toBe(true); // all downward — toward the live tail
  api.detach();

  // A plain scrollback session (codex): no mouse tracking, normal buffer → jumpToTail does nothing
  // (the FAB uses term.scrollToBottom() there instead).
  const plainSurface = document.createElement("div");
  const plainScreen = document.createElement("div");
  plainScreen.className = "xterm-screen";
  const plainEl = document.createElement("div");
  plainEl.appendChild(plainScreen);
  let plainWheels = 0;
  plainScreen.addEventListener("wheel", () => plainWheels++);
  const plainApi = attachTouchScroll(
    plainSurface,
    { ...fakeTerm(), element: plainEl },
    {},
  );
  plainApi.jumpToTail(50);
  expect(plainWheels).toBe(0);
  plainApi.detach();
});
