import { describe, expect, test } from "vitest";
import {
  DRAG_SLOP_PX,
  decideMouseDown,
  exceededSlop,
  forceSelectModifier,
} from "./termSelect";

const down = (o: Partial<Parameters<typeof decideMouseDown>[0]> = {}) => ({
  isTrusted: true,
  button: 0,
  detail: 1,
  shiftKey: false,
  ctrlKey: false,
  altKey: false,
  metaKey: false,
  ...o,
});

describe("decideMouseDown (#617)", () => {
  test("mouse-tracking session: a plain press is deferred until it proves drag or click", () => {
    // The whole point: this no longer depends on the buffer type. claude (>=2.1.178) and opencode
    // both run mouse-tracking on the ALTERNATE screen, where the old buffer guard killed selection.
    expect(decideMouseDown(down(), { mouseTracking: true })).toBe("defer");
  });

  test("no mouse tracking: xterm selects natively — never force (#582)", () => {
    // codex / antigravity. A twin here is a Shift-incremental extend from no anchor: selects nothing.
    expect(decideMouseDown(down(), { mouseTracking: false })).toBe("native");
    expect(decideMouseDown(down({ detail: 2 }), { mouseTracking: false })).toBe(
      "native",
    );
  });

  test("double / triple click force-selects at once — no drag is coming", () => {
    expect(decideMouseDown(down({ detail: 2 }), { mouseTracking: true })).toBe(
      "force-select",
    );
    expect(decideMouseDown(down({ detail: 3 }), { mouseTracking: true })).toBe(
      "force-select",
    );
  });

  test("our own synthetic twin / click replay passes straight through to xterm", () => {
    expect(
      decideMouseDown(down({ isTrusted: false }), { mouseTracking: true }),
    ).toBe("native");
  });

  test("non-left buttons are left alone (context menu, middle paste)", () => {
    expect(decideMouseDown(down({ button: 1 }), { mouseTracking: true })).toBe(
      "native",
    );
    expect(decideMouseDown(down({ button: 2 }), { mouseTracking: true })).toBe(
      "native",
    );
  });

  test("an explicitly modified press is the user's intent, not ours", () => {
    for (const mod of ["shiftKey", "ctrlKey", "altKey", "metaKey"] as const) {
      expect(
        decideMouseDown(down({ [mod]: true }), { mouseTracking: true }),
      ).toBe("native");
    }
  });
});

describe("exceededSlop", () => {
  test("a press that never moves is a click", () => {
    expect(exceededSlop(100, 100, 100, 100)).toBe(false);
    expect(exceededSlop(100, 100, 100 + DRAG_SLOP_PX, 100 + DRAG_SLOP_PX)).toBe(
      false,
    );
  });

  test("movement past the slop on either axis is a drag", () => {
    expect(exceededSlop(100, 100, 100 + DRAG_SLOP_PX + 1, 100)).toBe(true);
    expect(exceededSlop(100, 100, 100, 100 - DRAG_SLOP_PX - 1)).toBe(true);
  });
});

describe("forceSelectModifier", () => {
  // xterm 5.5.0: isMac ? altKey && macOptionClickForcesSelection : shiftKey.
  test("Shift off macOS, Alt on macOS — a Shift-only twin never selected on a Mac", () => {
    expect(forceSelectModifier(false)).toEqual({
      shiftKey: true,
      altKey: false,
    });
    expect(forceSelectModifier(true)).toEqual({
      shiftKey: false,
      altKey: true,
    });
  });
});
