import type { Node } from "@xyflow/react";
import { afterEach, describe, expect, test } from "vitest";
import {
  applyPins,
  decodePins,
  emptyPins,
  loadPins,
  MAX_PINS_PER_LAYOUT,
  POSITIONS_KEY,
  savePins,
  withoutLayout,
  withPin,
} from "./mapLayout";

const group = (groupKey: string, x = 0, y = 0): Node => ({
  id: `group:${groupKey}`,
  type: "projectGroup",
  position: { x, y },
  data: { groupKey },
});
const chip = (id: string, parentId: string): Node => ({
  id,
  type: "session",
  parentId,
  position: { x: 12, y: 56 },
  data: {},
});

afterEach(() => localStorage.clear());

describe("decodePins — read-lenient (#968)", () => {
  test("nothing stored, unparseable JSON, and non-object roots all decode to empty layouts", () => {
    for (const raw of [null, "", "{nope", "[]", "42", '"x"', "null"]) {
      expect(decodePins(raw)).toEqual(emptyPins());
    }
  });

  test("a bad entry drops itself and never the rest of the layout", () => {
    const raw = JSON.stringify({
      project: {
        "project:p1": { x: 10.4, y: -20.6 },
        "project:nan": { x: "NaN", y: 1 },
        "project:inf": { x: 1e300, y: 1 },
        "project:missing": { x: 1 },
        "project:array": [1, 2],
        "": { x: 1, y: 1 },
      },
      folder: "not an object",
      agent: { "agent:claude": { x: 5, y: 6 } },
      bogus: { "k": { x: 1, y: 1 } },
    });
    expect(decodePins(raw)).toEqual({
      folder: {},
      project: { "project:p1": { x: 10, y: -21 } },
      agent: { "agent:claude": { x: 5, y: 6 } },
    });
  });

  test("a hand-edited layout larger than the bound is cut at the bound", () => {
    const big = Object.fromEntries(
      Array.from({ length: MAX_PINS_PER_LAYOUT + 50 }, (_, i) => [`k${i}`, { x: i, y: i }]),
    );
    const pins = decodePins(JSON.stringify({ folder: big }));
    expect(Object.keys(pins.folder)).toHaveLength(MAX_PINS_PER_LAYOUT);
  });
});

describe("withPin / withoutLayout", () => {
  test("pins one layout without touching the others, rounding the coordinate", () => {
    const a = withPin(emptyPins(), "agent", "agent:claude", { x: 1.5, y: 2.49 });
    expect(a.agent).toEqual({ "agent:claude": { x: 2, y: 2 } });
    expect(a.folder).toEqual({});
    expect(a.project).toEqual({});
  });

  test("a non-finite drop position is refused rather than stored", () => {
    const all = emptyPins();
    expect(withPin(all, "folder", "/a", { x: Number.NaN, y: 0 })).toBe(all);
  });

  test("at the bound the OLDEST pin goes, and re-pinning refreshes a key's age", () => {
    let all = emptyPins();
    for (let i = 0; i < MAX_PINS_PER_LAYOUT; i++) all = withPin(all, "folder", `k${i}`, { x: i, y: 0 });
    all = withPin(all, "folder", "k0", { x: 99, y: 99 }); // k0 is now the newest
    all = withPin(all, "folder", "fresh", { x: 1, y: 1 });
    const keys = Object.keys(all.folder);
    expect(keys).toHaveLength(MAX_PINS_PER_LAYOUT);
    expect(keys).not.toContain("k1");
    expect(all.folder.k0).toEqual({ x: 99, y: 99 });
    expect(all.folder.fresh).toEqual({ x: 1, y: 1 });
  });

  test("Reset clears the current layout only", () => {
    let all = withPin(emptyPins(), "project", "project:p1", { x: 1, y: 1 });
    all = withPin(all, "folder", "/a", { x: 2, y: 2 });
    const reset = withoutLayout(all, "project");
    expect(reset.project).toEqual({});
    expect(reset.folder).toEqual({ "/a": { x: 2, y: 2 } });
  });
});

describe("storage", () => {
  test("round-trips, and an all-empty set removes the key instead of storing it", () => {
    const all = withPin(emptyPins(), "project", "project:p1", { x: 3, y: 4 });
    savePins(all);
    expect(loadPins()).toEqual(all);
    savePins(emptyPins());
    expect(localStorage.getItem(POSITIONS_KEY)).toBeNull();
  });
});

describe("applyPins", () => {
  test("moves pinned clusters only; chips stay parent-relative and follow their cluster", () => {
    const nodes = [group("project:p1", 0, 0), chip("claude:a", "group:project:p1"), group("project:p2", 400, 0)];
    const out = applyPins(nodes, { "project:p1": { x: 700, y: 300 } });
    expect(out[0].position).toEqual({ x: 700, y: 300 });
    expect(out[1]).toBe(nodes[1]); // untouched chip, same object
    expect(out[2]).toBe(nodes[2]); // unpinned cluster, same object
  });

  test("returns the same array when nothing applies — including pins for clusters not on the map", () => {
    const nodes = [group("project:p1", 5, 5)];
    expect(applyPins(nodes, {})).toBe(nodes);
    expect(applyPins(nodes, { "project:gone": { x: 1, y: 1 } })).toBe(nodes);
    expect(applyPins(nodes, { "project:p1": { x: 5, y: 5 } })).toBe(nodes);
  });
});
