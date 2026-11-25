import { describe, expect, test } from "vitest";
import { countsText, NEVER_SAVED, oneLineProblem, slotsOf, summaryProblem } from "./planRules";

describe("slotsOf (#1924)", () => {
  test("numbers slots by each project's first appearance, the server's rule", () => {
    const plan = {
      revision: 3,
      document: {
        objectives: [
          { id: "o1", project_id: "p-beta", procedure: { title: "A" } },
          { id: "o2", project_id: "p-alpha", procedure: { title: "B" } },
          { id: "o3", project_id: "p-beta", procedure: { title: "C" } },
        ],
      },
    };
    expect(slotsOf(plan)).toEqual([
      { id: "p1", projectId: "p-beta", objectives: ["o1", "o3"] },
      { id: "p2", projectId: "p-alpha", objectives: ["o2"] },
    ]);
  });
});

describe("field rules", () => {
  test("one line, required, bounded", () => {
    expect(oneLineProblem("Mail", "A slot label", 60)).toBeNull();
    expect(oneLineProblem("  ", "A slot label", 60)).toMatch(/required/);
    expect(oneLineProblem("a\nb", "A slot label", 60)).toMatch(/one line/);
    expect(oneLineProblem("x".repeat(61), "A slot label", 60)).toMatch(/at most 60/);
  });
  test("a summary may have lines, never other control characters", () => {
    expect(summaryProblem("one\n\ttwo")).toBeNull();
    expect(summaryProblem("bad\u001b[2J")).toMatch(/control/);
    expect(summaryProblem("x".repeat(2001))).toMatch(/at most/);
  });
});

test("counts read naturally", () => {
  expect(countsText({ objectives: 1, slots: 1, steps: 4 })).toBe("1 objective · 1 project slot · 4 steps");
  expect(countsText({ objectives: 3, slots: 2, steps: 9 })).toBe("3 objectives · 2 project slots · 9 steps");
});

test("one never-saved list, four entries", () => {
  expect(NEVER_SAVED).toHaveLength(4);
  expect(NEVER_SAVED.join(" ")).toMatch(/playbook defaults/);
});
