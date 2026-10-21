import { describe, expect, it } from "vitest";
import {
  canHostWindow,
  canOpen,
  CASCADE_STEP,
  cascadeRect,
  CHROME_H,
  clampRect,
  clampWindowCap,
  clipToBounds,
  DEFAULT_SIZE,
  MIN_SIZE,
  tetherAnchor,
  tetherPath,
  tileCapacity,
  tileRects,
  WINDOW_CAP,
  WINDOW_CAP_MAX,
  WINDOW_CAP_MIN,
} from "./workspace";

// The window workspace's geometry rules (#208). These are the decisions a browser test can only
// observe indirectly (a window "looks" inside the map), so they are pinned directly here.

const BOUNDS = { w: 1400, h: 900 };

describe("clampRect", () => {
  it("leaves a window that already fits alone", () => {
    const r = { x: 100, y: 80, w: 720, h: 480 };
    expect(clampRect(r, BOUNDS)).toEqual(r);
  });

  it("pulls a window back inside when it hangs off the right/bottom edge", () => {
    const r = clampRect({ x: 1300, y: 800, w: 720, h: 480 }, BOUNDS);
    expect(r.x).toBe(BOUNDS.w - 720);
    expect(r.y).toBe(BOUNDS.h - 480);
    expect(r.x + r.w).toBeLessThanOrEqual(BOUNDS.w);
    expect(r.y + r.h).toBeLessThanOrEqual(BOUNDS.h);
  });

  it("never lets a window off the top/left, where its chrome would be unreachable", () => {
    const r = clampRect({ x: -400, y: -200, w: 720, h: 480 }, BOUNDS);
    expect(r.x).toBe(0);
    expect(r.y).toBe(0);
  });

  it("enforces the minimum size — a resize cannot starve the agent of columns", () => {
    const r = clampRect({ x: 0, y: 0, w: 120, h: 60 }, BOUNDS);
    expect(r.w).toBe(MIN_SIZE.w);
    expect(r.h).toBe(MIN_SIZE.h);
  });

  it("lets the box beat the minimum — a window wider than the map could never be dragged back", () => {
    const tight = { w: 400, h: 240 };
    const r = clampRect({ x: 0, y: 0, w: DEFAULT_SIZE.w, h: DEFAULT_SIZE.h }, tight);
    expect(r.w).toBe(tight.w);
    expect(r.h).toBe(tight.h);
    expect(r.x).toBe(0);
    expect(r.y).toBe(0);
  });

  it("moves a window that a shrinking viewport left outside", () => {
    const before = clampRect({ x: 600, y: 380, w: 720, h: 480 }, BOUNDS);
    const after = clampRect(before, { w: 900, h: 700 });
    expect(after.x + after.w).toBeLessThanOrEqual(900);
    expect(after.y + after.h).toBeLessThanOrEqual(700);
  });
});

describe("cascadeRect", () => {
  it("lands the first window off its anchor chip", () => {
    const r = cascadeRect({ x: 200, y: 150 }, 0, BOUNDS);
    expect(r.x).toBeGreaterThan(200);
    expect(r.y).toBeGreaterThan(150);
    expect(r.w).toBe(DEFAULT_SIZE.w);
    expect(r.h).toBe(DEFAULT_SIZE.h);
  });

  it("steps each further window so they never land exactly stacked", () => {
    const first = cascadeRect({ x: 100, y: 100 }, 0, BOUNDS);
    const second = cascadeRect({ x: 100, y: 100 }, 1, BOUNDS);
    expect(second.x - first.x).toBe(CASCADE_STEP);
    expect(second.y - first.y).toBe(CASCADE_STEP);
  });

  it("clamps a cascade that would walk off the edge", () => {
    const r = cascadeRect({ x: BOUNDS.w - 20, y: BOUNDS.h - 20 }, 7, BOUNDS);
    expect(r.x + r.w).toBeLessThanOrEqual(BOUNDS.w);
    expect(r.y + r.h).toBeLessThanOrEqual(BOUNDS.h);
  });

  it("falls back to the top-left when the session has no chip on the map", () => {
    const r = cascadeRect(null, 0, BOUNDS);
    expect(r.x).toBeGreaterThanOrEqual(0);
    expect(r.y).toBeGreaterThanOrEqual(0);
  });
});

describe("canOpen", () => {
  it("allows exactly WINDOW_CAP windows", () => {
    expect(canOpen(0)).toBe(true);
    expect(canOpen(WINDOW_CAP - 1)).toBe(true);
    expect(canOpen(WINDOW_CAP)).toBe(false);
    expect(WINDOW_CAP).toBe(8);
  });
});

describe("tether", () => {
  const rect = { x: 500, y: 200, w: 720, h: 480 };

  it("attaches on the side facing the chip, so the line never crosses the window", () => {
    expect(tetherAnchor(rect, { x: 100, y: 300 }).x).toBe(rect.x);
    expect(tetherAnchor(rect, { x: 1390, y: 300 }).x).toBe(rect.x + rect.w);
  });

  it("attaches at the middle of the chrome bar", () => {
    expect(tetherAnchor(rect, { x: 0, y: 0 }).y).toBe(rect.y + CHROME_H / 2);
  });

  it("clips an anchor panned outside the map to the boundary", () => {
    expect(clipToBounds({ x: -300, y: 1200 }, BOUNDS)).toEqual({ x: 0, y: BOUNDS.h });
    expect(clipToBounds({ x: 4000, y: -50 }, BOUNDS)).toEqual({ x: BOUNDS.w, y: 0 });
  });

  it("draws a horizontal cubic between the two endpoints", () => {
    const d = tetherPath({ x: 10, y: 20 }, { x: 110, y: 220 });
    expect(d).toBe("M 10 20 C 60 20, 60 220, 110 220");
  });
});

describe("an unmeasured overlay box", () => {
  it("is not treated as a constraint — a 0×0 box would otherwise yield a 1px window", () => {
    const r = clampRect({ x: 40, y: 40, w: DEFAULT_SIZE.w, h: DEFAULT_SIZE.h }, { w: 0, h: 0 });
    expect(r.w).toBe(DEFAULT_SIZE.w);
    expect(r.h).toBe(DEFAULT_SIZE.h);
    expect(r.x).toBe(0);
    expect(r.y).toBe(0);
  });
});

describe("canHostWindow", () => {
  it("accepts a box that can hold the floor and refuses one that cannot", () => {
    expect(canHostWindow({ w: MIN_SIZE.w, h: MIN_SIZE.h })).toBe(true);
    expect(canHostWindow({ w: MIN_SIZE.w - 1, h: MIN_SIZE.h })).toBe(false);
    expect(canHostWindow({ w: MIN_SIZE.w, h: MIN_SIZE.h - 1 })).toBe(false);
    expect(canHostWindow({ w: 0, h: 0 })).toBe(false);
  });

  it("refuses the narrow-desktop case the breakpoint alone lets through", () => {
    // 801px viewport is "desktop" by the ≤800px breakpoint, but the expanded sidebar leaves
    // ~460px of map — below the floor whose whole point is the agent's column count.
    expect(canHostWindow({ w: 461, h: 560 })).toBe(false);
  });
});

describe("the operator's window cap (#936)", () => {
  it("clamps into range and floors to a whole window", () => {
    expect(clampWindowCap(WINDOW_CAP_MAX + 40)).toBe(WINDOW_CAP_MAX);
    expect(clampWindowCap(0)).toBe(WINDOW_CAP_MIN);
    expect(clampWindowCap(-9)).toBe(WINDOW_CAP_MIN);
    expect(clampWindowCap(4.6)).toBe(5);
  });

  it("falls back to the DEFAULT for a value that is not a number — never to zero", () => {
    // Read-lenient, like every other read path here: a hand-edited `localStorage` entry must
    // not be able to turn the workspace off. Zero would do exactly that, silently.
    for (const junk of [undefined, null, NaN, "lots", {}, []])
      expect(clampWindowCap(junk)).toBe(WINDOW_CAP);
  });

  it("gates opening on the cap it is GIVEN, not on a module constant", () => {
    expect(canOpen(2, 3)).toBe(true);
    expect(canOpen(3, 3)).toBe(false);
    // An out-of-range cap is clamped here too, so no caller can widen the ceiling by passing a
    // bigger number than the stepper allows.
    expect(canOpen(WINDOW_CAP_MAX, 9999)).toBe(false);
    // Default preserved for the call sites that predate the setting.
    expect(canOpen(WINDOW_CAP - 1)).toBe(true);
    expect(canOpen(WINDOW_CAP)).toBe(false);
  });
});

describe("tileRects", () => {
  it("lays a near-square grid across the box", () => {
    expect(tileRects(4, { w: 1400, h: 900 })).toEqual([
      { x: 0, y: 0, w: 700, h: 450 },
      { x: 700, y: 0, w: 700, h: 450 },
      { x: 0, y: 450, w: 700, h: 450 },
      { x: 700, y: 450, w: 700, h: 450 },
    ]);
  });

  it("never tiles below the floor, and never more than fit — capacity is the floor grid", () => {
    expect(tileCapacity({ w: 1200, h: 700 })).toBe(4);
    expect(tileCapacity({ w: 600, h: 330 })).toBe(1);
    const r = tileRects(5, { w: 1200, h: 700 });
    expect(r).toHaveLength(4);
    for (const x of r) {
      expect(x.w).toBeGreaterThanOrEqual(MIN_SIZE.w);
      expect(x.h).toBeGreaterThanOrEqual(MIN_SIZE.h);
    }
  });

  it("no two tiles overlap, at any count and any hostable box (Hermes on #1320)", () => {
    // The first cut wrapped overflow onto the first cells with a cascade step that a full-box cell
    // clamps back to zero: tileRects(8, 600×330) was eight identical rects.
    const boxes = [
      { w: 600, h: 330 },
      { w: 1200, h: 700 },
      { w: 1400, h: 900 },
      { w: 1920, h: 400 },
      { w: 2560, h: 1300 },
    ];
    for (const b of boxes)
      for (let n = 1; n <= 16; n++) {
        const r = tileRects(n, b);
        expect(r.length).toBe(Math.min(n, tileCapacity(b)));
        for (let i = 0; i < r.length; i++)
          for (let j = i + 1; j < r.length; j++) {
            const [p, q] = [r[i], r[j]];
            const overlap = p.x < q.x + q.w && q.x < p.x + p.w && p.y < q.y + q.h && q.y < p.y + p.h;
            expect(overlap, `${n} in ${b.w}×${b.h}: tiles ${i},${j}`).toBe(false);
          }
      }
  });

  it("one window fills the box; zero is nothing", () => {
    expect(tileRects(1, { w: 1000, h: 600 })).toEqual([{ x: 0, y: 0, w: 1000, h: 600 }]);
    expect(tileRects(0, { w: 1000, h: 600 })).toEqual([]);
  });
});
