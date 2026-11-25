import { beforeEach, describe, expect, it } from "vitest";
import {
  DEFAULT_W,
  MAX_W,
  MIN_W,
  TERM_MIN,
  WIDTH_KEY,
  canDock,
  clampW,
  maxPanelW,
  panelMode,
  readStoredW,
  storeW,
} from "./filePanelLayout";

describe("panel geometry (#783)", () => {
  beforeEach(() => localStorage.clear());

  it("docks only when the pane can afford the panel AND a usable terminal", () => {
    // The arithmetic that makes the app's 800px viewport rule wrong here: with the 320px
    // sidebar an ~801px viewport leaves a ~481px pane, which cannot hold 260 + 360.
    expect(canDock(481)).toBe(false);
    expect(panelMode(481)).toBe("sheet");
    expect(canDock(MIN_W + TERM_MIN)).toBe(true);
    expect(panelMode(MIN_W + TERM_MIN)).toBe("dock");
  });

  it("a narrow DESKTOP pane gets the sheet too — mode follows measurement, not viewport", () => {
    expect(panelMode(500)).toBe("sheet");
    expect(panelMode(1200)).toBe("dock");
  });

  it("never lets the panel squeeze the terminal below its minimum", () => {
    const paneW = 800;
    expect(maxPanelW(paneW)).toBe(paneW - TERM_MIN);
    expect(clampW(9999, paneW)).toBe(paneW - TERM_MIN);
    expect(clampW(9999, paneW) + TERM_MIN).toBeLessThanOrEqual(paneW);
  });

  it("clamps to the hard maximum on a very wide pane", () => {
    expect(clampW(9999, 4000)).toBe(MAX_W);
  });

  it("clamps up to the minimum", () => {
    expect(clampW(10, 1200)).toBe(MIN_W);
  });

  it("round-trips the stored width and falls back on junk", () => {
    storeW(412);
    expect(readStoredW()).toBe(412);
    localStorage.setItem(WIDTH_KEY, "not-a-number");
    expect(readStoredW()).toBe(DEFAULT_W);
  });
});
