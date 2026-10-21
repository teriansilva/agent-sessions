import { describe, expect, it } from "vitest";
import { isHoverButton, stripHoverMouseReports } from "./hoverMouse";

const sgr = (b: number, final = "M") => `\x1b[<${b};51;59${final}`;

describe("stripHoverMouseReports (#1285)", () => {
  it.each([35, 39, 43, 47, 51, 55, 59, 63])(
    "drops hover motion %i (no button, any modifier)",
    (b) => {
      expect(isHoverButton(b)).toBe(true);
      expect(stripHoverMouseReports(sgr(b))).toBe("");
    },
  );

  it.each([
    ["press left", sgr(0)],
    ["press right", sgr(2)],
    ["release", sgr(0, "m")],
    ["release reported as button 35", sgr(35, "m")],
    ["drag left", sgr(32)],
    ["drag right", sgr(34)],
    ["ctrl drag", sgr(48)],
    ["wheel up", sgr(64)],
    ["wheel down", sgr(65)],
    ["motion-flagged wheel", sgr(99)],
    ["plain keys", "hello"],
    ["lone Esc", "\x1b"],
    ["arrow key", "\x1b[A"],
    ["incomplete report", "\x1b[<35;51"],
    ["non-SGR CSI", "\x1b[35;51;59M"],
  ])("keeps %s untouched", (_label, data) => {
    expect(stripHoverMouseReports(data)).toBe(data);
  });

  it("strips only the hover reports from a mixed chunk, keeping order", () => {
    const data = `a${sgr(35)}${sgr(0)}\x1b${sgr(43)}b${sgr(0, "m")}${sgr(64)}`;
    expect(stripHoverMouseReports(data)).toBe(
      `a${sgr(0)}\x1bb${sgr(0, "m")}${sgr(64)}`,
    );
  });

  it("drops a burst of hover reports entirely", () => {
    expect(stripHoverMouseReports(sgr(35).repeat(20))).toBe("");
  });
});
