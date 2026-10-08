import { describe, expect, it } from "vitest";
import type { Step } from "./playbookDraft";
import { dependencyError, graphLayout } from "./playbookGraph";

const step = (id: string, after: string[] = []): Step => ({
  id,
  title: id,
  actor: { kind: "operator" },
  after,
});
describe("flowchart draft projection", () => {
  it("keeps every step of an invalid draft visible without rewriting it", () => {
    const steps = [
      step("a", ["b"]),
      step("b", ["a"]),
      step("c", ["gone"]),
      step("d"),
    ];
    const before = structuredClone(steps);
    const graph = graphLayout(steps);
    expect(graph.cyclic).toBe(true);
    expect(graph.missing).toBe(true);
    expect([...graph.positions.keys()]).toEqual(["a", "b", "c", "d"]);
    expect(
      new Set([...graph.positions.values()].map((p) => `${p.x},${p.y}`)).size,
    ).toBe(4);
    expect(steps).toEqual(before);
  });
  it("draws joins after every prerequisite and ignores rework for dependency layout", () => {
    const steps = [
      step("review", ["left", "right"]),
      step("left", ["plan"]),
      step("right", ["plan"]),
      step("plan"),
    ];
    steps[0].rework = { to: "plan", when: "approved", max_rounds: 4 };
    const graph = graphLayout(steps);
    expect(graph.cyclic).toBe(false);
    expect(graph.positions.get("review")!.x).toBeGreaterThan(
      graph.positions.get("left")!.x,
    );
    expect(graph.positions.get("review")!.x).toBeGreaterThan(
      graph.positions.get("right")!.x,
    );
    expect(graph.positions.get("left")!.x).toBeGreaterThan(
      graph.positions.get("plan")!.x,
    );
  });
  it("refuses cyclic, self, duplicate, missing and note prerequisite connections", () => {
    const steps = [
      step("plan"),
      step("implement", ["plan"]),
      step("review", ["implement"]),
      { ...step("note"), actor: { kind: "none" } },
    ];
    expect(dependencyError(steps, "review", "plan")).toContain("cycle");
    expect(dependencyError(steps, "plan", "plan")).toContain("itself");
    expect(dependencyError(steps, "plan", "implement")).toContain("already");
    expect(dependencyError(steps, "missing", "plan")).toContain("exist");
    expect(dependencyError(steps, "note", "plan")).toContain("note");
    expect(dependencyError(steps, "plan", "review")).toBe("");
  });
});
