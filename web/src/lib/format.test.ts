import { afterEach, describe, expect, test, vi } from "vitest";
import { relTime } from "./format";

// #508: relTime now spells units out ("3 mins ago", "1 hour ago") instead of the compact
// "3m"/"1h", with singular/plural agreement. Pin the boundaries + pluralization.
describe("relTime — spelled-out relative time (#508)", () => {
  afterEach(() => vi.useRealTimers());

  function ago(seconds: number): string {
    const now = new Date("2026-06-19T12:00:00Z");
    vi.useFakeTimers();
    vi.setSystemTime(now);
    return relTime(Math.floor(now.getTime() / 1000) - seconds);
  }

  test("under a minute reads 'just now'", () => {
    expect(ago(0)).toBe("just now");
    expect(ago(59)).toBe("just now");
  });

  test("minutes are spelled out and pluralized", () => {
    expect(ago(60)).toBe("1 min ago");
    expect(ago(125)).toBe("2 mins ago");
    expect(ago(59 * 60)).toBe("59 mins ago");
  });

  test("hours are spelled out and pluralized", () => {
    expect(ago(3600)).toBe("1 hour ago");
    expect(ago(2 * 3600 + 30)).toBe("2 hours ago");
    expect(ago(23 * 3600)).toBe("23 hours ago");
  });

  test("days are spelled out and pluralized", () => {
    expect(ago(86400)).toBe("1 day ago");
    expect(ago(5 * 86400)).toBe("5 days ago");
  });

  test("clock skew / future timestamps clamp to 'just now'", () => {
    expect(ago(-30)).toBe("just now");
  });
});
