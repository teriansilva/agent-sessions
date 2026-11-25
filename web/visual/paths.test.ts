import { describe, expect, it } from "vitest";
import { VISUAL_PATHS, VIEWPORTS, KNOWN_AREA_KEYS } from "./paths";

describe("VISUAL_PATHS registry", () => {
  it("has unique names", () => {
    const names = VISUAL_PATHS.map((p) => p.name);
    expect(new Set(names).size).toBe(names.length);
    expect(KNOWN_AREA_KEYS.size).toBe(names.length);
  });

  it("every entry has a non-empty description + a waitFor", () => {
    for (const p of VISUAL_PATHS) {
      expect(p.description.trim().length).toBeGreaterThan(0);
      expect(p.waitFor).toBeTruthy();
    }
  });

  it("every networkidle wait carries a non-empty reason", () => {
    for (const p of VISUAL_PATHS) {
      if ("kind" in p.waitFor && p.waitFor.kind === "networkidle") {
        expect(p.waitFor.reason.trim().length).toBeGreaterThan(0);
      }
    }
  });

  it("ships multiple screen formats incl. desktop + a small phone", () => {
    expect(Object.keys(VIEWPORTS).length).toBeGreaterThanOrEqual(4);
    expect(VIEWPORTS.desktop.width).toBeGreaterThan(VIEWPORTS["mobile-sm"].width);
  });
});
