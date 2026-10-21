import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, test } from "vitest";
import {
  coerceRecentWindowDays,
  isRecentWindowDays,
  RECENT_WINDOW_DEFAULT,
  RECENT_WINDOW_MAX,
  RECENT_WINDOW_MIN,
} from "./recentWindow";

// The SAME fixture tests/test_pulse_window.py reads (#1086), resolved from the vitest root (web/).
const FIXTURE = resolve(process.cwd(), "../tests/fixtures/pulse_window_days_cases.json");
const CASES = JSON.parse(readFileSync(FIXTURE, "utf8")) as {
  min: number;
  max: number;
  default: number;
  read: { in: unknown; out: number; why: string }[];
  write_accepted: unknown[];
  write_rejected: unknown[];
};

describe("recent-work window normalization (#1086)", () => {
  test("the shared fixture describes THIS module's constants", () => {
    expect([CASES.min, CASES.max, CASES.default]).toEqual([
      RECENT_WINDOW_MIN,
      RECENT_WINDOW_MAX,
      RECENT_WINDOW_DEFAULT,
    ]);
  });

  test("the shared read table", () => {
    for (const c of CASES.read) {
      expect(coerceRecentWindowDays(c.in), `${JSON.stringify(c.in)}: ${c.why}`).toBe(c.out);
    }
  });

  test("read edges JSON cannot carry", () => {
    for (const v of [Number.NaN, Infinity, -Infinity, undefined]) {
      expect(coerceRecentWindowDays(v)).toBe(RECENT_WINDOW_DEFAULT);
    }
  });

  test("the shared write table", () => {
    for (const v of CASES.write_accepted) expect(isRecentWindowDays(v), String(v)).toBe(true);
    for (const v of CASES.write_rejected)
      expect(isRecentWindowDays(v), JSON.stringify(v)).toBe(false);
  });
});
