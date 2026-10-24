import { describe, expect, test } from "vitest";

import type { AgentUsageRow } from "../../types/api";
import {
  forecastLine,
  isActiveMission,
  lowestPlanLeft,
  untilText,
} from "./dashboard";

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

  test("a STALE or refused quota is left out, like the tile leaves it out", () => {
    const rows = [
      row({
        engine: "claude",
        stale: true,
        at: 10,
        windows: [{ label: "weekly", used_pct: 90, resets_at: 1 }],
      }),
      row({
        engine: "gemini",
        at: 99,
        windows: [{ label: "weekly", used_pct: 95, resets_at: 1 }],
        access: {
          state: "denied",
          message: "no",
          observed_at: 1,
          checked_at: 1,
        },
      }),
      row({
        engine: "codex",
        at: 99,
        windows: [{ label: "weekly", used_pct: 20, resets_at: 1 }],
      }),
    ];
    expect(lowestPlanLeft(rows)).toMatchObject({ engine: "codex", left: 80 });
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

describe("forecastLine", () => {
  const NOW = 1_900_000_000;
  const fc = (over: Partial<NonNullable<AgentUsageRow["forecast"]>>) =>
    row({
      forecast: {
        state: "ok",
        window: "week",
        resets_at: null,
        runs_out_at: null,
        pct_at_reset: null,
        rate_per_h: 1,
        ...over,
      },
    });

  test("no forecast, no line", () => {
    expect(forecastLine(row({}), NOW)).toBeNull();
  });

  test("a pace that runs out before the reset warns, with how long and how early", () => {
    const got = forecastLine(
      fc({
        state: "exhausts",
        runs_out_at: NOW + 15 * 3600,
        resets_at: NOW + 48 * 3600,
      }),
      NOW,
    );
    expect(got?.tone).toBe("warn");
    expect(got?.text).toMatch(
      /^week: runs out in ~15h \(.+\) at this pace — 33h before the reset$/,
    );
  });

  test("a lasting pace is muted and says where it lands", () => {
    expect(forecastLine(fc({ pct_at_reset: 64.4 }), NOW)).toEqual({
      text: "week: on pace for ~64% used at the reset",
      tone: "muted",
    });
    expect(forecastLine(fc({ window: null }), NOW)?.text).toBe(
      "on pace to stay under the limit",
    );
  });

  test("a forecast on another window than the meter's names it and carries its own figure", () => {
    // The meter shows the session (nearest its limit); the forecast judges the week (runs out
    // first). Read bare, the week's "runs out" sat under the session's 49 % as if it were its own.
    const windows = [
      { label: "session", used_pct: 51, resets_at: NOW + 3 * 3600 },
      { label: "week", used_pct: 13, resets_at: NOW + 6 * 86400 },
    ];
    const got = forecastLine(
      fc({
        state: "exhausts",
        runs_out_at: NOW + 33 * 3600,
        resets_at: NOW + 6 * 86400,
      }),
      NOW,
    );
    expect(got?.text).toMatch(/^week: /);
    const mixed = forecastLine(
      {
        ...fc({
          state: "exhausts",
          runs_out_at: NOW + 33 * 3600,
          resets_at: NOW + 6 * 86400,
        }),
        windows,
      },
      NOW,
    );
    expect(mixed?.text).toMatch(
      /^week, 87% left: runs out in ~33h \(.+\) at this pace — ~?5d before the reset$/,
    );
    // The meter's own window is named, without repeating the figure the meter already shows.
    const same = forecastLine(
      {
        ...fc({
          state: "exhausts",
          window: "session",
          runs_out_at: NOW + 3600 + 1800,
          resets_at: NOW + 3 * 3600,
        }),
        windows,
      },
      NOW,
    );
    expect(same?.text).toMatch(/^session: runs out in ~2h /);
    // A lasting window lands in % USED; next to its own "% left" it must say so.
    expect(
      forecastLine({ ...fc({ pct_at_reset: 64.4 }), windows }, NOW)?.text,
    ).toBe("week, 87% left: on pace for ~64% used at the reset");
  });

  test("learning is never 'ok'", () => {
    expect(forecastLine(fc({ state: "learning" }), NOW)).toEqual({
      text: "forecast after a few more readings",
      tone: "muted",
    });
  });

  test("out is the down tone", () => {
    expect(forecastLine(fc({ state: "out" }), NOW)?.tone).toBe("over");
  });

  test("durations are coarse", () => {
    expect(untilText(1800)).toBe("under an hour");
    expect(untilText(20 * 3600)).toBe("~20h");
    expect(untilText(5 * 86400)).toBe("~5d");
  });
});
