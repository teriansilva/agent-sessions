import { afterEach, beforeEach, describe, expect, test } from "vitest";
import { clampW, DEFAULT_W, maxSidebarW, MIN_W, readStoredW, WIDTH_KEY } from "./sidebarWidth";

function setViewport(w: number): void {
  Object.defineProperty(window, "innerWidth", { value: w, configurable: true, writable: true });
}

// #507: the desktop sidebar width is persisted device-local and clamped to a viewport-aware
// range so a wide saved value can't crowd the pane on a smaller window.
describe("sidebarWidth (#507)", () => {
  beforeEach(() => {
    localStorage.clear();
    setViewport(1400);
  });
  afterEach(() => localStorage.clear());

  test("maxSidebarW is viewport-aware and hard-capped at 560", () => {
    setViewport(1400); // 1400 − 360 = 1040 → capped to 560
    expect(maxSidebarW()).toBe(560);
    setViewport(800); // 800 − 360 = 440 (below cap)
    expect(maxSidebarW()).toBe(440);
    setViewport(500); // 500 − 360 = 140 → below MIN → MIN_W
    expect(maxSidebarW()).toBe(MIN_W);
  });

  test("clampW clamps to [MIN_W, maxSidebarW] and rounds to a pixel", () => {
    setViewport(1400);
    expect(clampW(100)).toBe(MIN_W);
    expect(clampW(9999)).toBe(560);
    expect(clampW(321.6)).toBe(322);
  });

  test("readStoredW returns the default when unset", () => {
    expect(readStoredW()).toBe(DEFAULT_W);
  });

  test("readStoredW returns the persisted width, clamped to the current viewport", () => {
    localStorage.setItem(WIDTH_KEY, "420");
    setViewport(1400);
    expect(readStoredW()).toBe(420);
    // A width saved on a wide monitor is clamped down on a narrow window (700 − 360 = 340).
    setViewport(700);
    expect(readStoredW()).toBe(340);
  });

  test("readStoredW ignores a garbage / non-positive stored value", () => {
    localStorage.setItem(WIDTH_KEY, "not-a-number");
    expect(readStoredW()).toBe(DEFAULT_W);
    localStorage.setItem(WIDTH_KEY, "-50");
    expect(readStoredW()).toBe(DEFAULT_W);
  });
});
