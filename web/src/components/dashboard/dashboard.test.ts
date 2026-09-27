import { describe, expect, test } from "vitest";

import type { AgentUsageRow } from "../../types/api";
import { isActiveMission, lowestPlanLeft } from "./dashboard";

function row(over: Partial<AgentUsageRow>): AgentUsageRow {
  return {
    engine: "claude",
    source: "plan",
    windows: [],
    at: 1000,
    checked_at: 1000,
    stale: false,
    limit_tokens: 0,
    manual_used: 0,
    used_pct: null,
    ...over,
  };
}

describe("lowestPlanLeft (#1123, review 75977)", () => {
  test("only PLAN windows compete; tokens, manual and none never enter it", () => {
    const rows = [
      row({
        engine: "opencode",
        source: "tokens",
        used_pct: 99,
        limit_tokens: 10,
      }),
      row({
        engine: "gemini",
        source: "manual",
        used_pct: 99,
        manual_used: 9,
        limit_tokens: 10,
      }),
      row({ engine: "kimi", source: "none" }),
      row({
        engine: "codex",
        windows: [{ label: "weekly", used_pct: 29, resets_at: 5 }],
      }),
    ];
    expect(lowestPlanLeft(rows)).toMatchObject({
      engine: "codex",
      left: 71,
      label: "weekly",
    });
  });

  test("an agent is judged on its WORST window", () => {
    const r = row({
      windows: [
        { label: "5-hour", used_pct: 10, resets_at: 1 },
        { label: "weekly", used_pct: 82, resets_at: 2 },
      ],
    });
    expect(lowestPlanLeft([r])).toMatchObject({
      left: 18,
      label: "weekly",
      resets_at: 2,
    });
  });

  test("a STALE lower figure still wins, and says it is stale", () => {
    const rows = [
      row({
        engine: "claude",
        stale: true,
        at: 10,
        windows: [{ label: "weekly", used_pct: 90, resets_at: 1 }],
      }),
      row({
        engine: "codex",
        at: 99,
        windows: [{ label: "weekly", used_pct: 20, resets_at: 1 }],
      }),
    ];
    expect(lowestPlanLeft(rows)).toMatchObject({
      engine: "claude",
      left: 10,
      stale: true,
      at: 10,
    });
  });

  test("a failed last check marks the figure stale too", () => {
    const r = row({
      error: "not logged in",
      windows: [{ label: "weekly", used_pct: 5, resets_at: 1 }],
    });
    expect(lowestPlanLeft([r])?.stale).toBe(true);
  });

  test("a tie goes to the fresher figure", () => {
    const rows = [
      row({
        engine: "a",
        at: 10,
        windows: [{ label: "weekly", used_pct: 50, resets_at: 1 }],
      }),
      row({
        engine: "b",
        at: 20,
        windows: [{ label: "weekly", used_pct: 50, resets_at: 1 }],
      }),
    ];
    expect(lowestPlanLeft(rows)?.engine).toBe("b");
  });

  test("no plan quota at all is null — never a made-up number", () => {
    expect(
      lowestPlanLeft([row({ source: "none" }), row({ windows: [] })]),
    ).toBeNull();
    expect(lowestPlanLeft([])).toBeNull();
  });
});

test("active missions are dispatching + running; review is not active", () => {
  expect(isActiveMission({ state: "dispatching" })).toBe(true);
  expect(isActiveMission({ state: "running" })).toBe(true);
  expect(isActiveMission({ state: "review" })).toBe(false);
  expect(isActiveMission({ state: "draft" })).toBe(false);
});
