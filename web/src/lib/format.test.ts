import { afterEach, describe, expect, test, vi } from "vitest";
import { projectColor, relTime } from "./format";

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

// #285: deterministic per-project accent — a stable hash of the project key (entity id or
// folder cwd) → golden-angle hue, emitted as a light-dark() pair so the same hue gets a
// theme-appropriate lightness. Pin determinism, spread, shape, and degenerate inputs.
describe("projectColor — deterministic per-project accent (#285)", () => {
  const SHAPE = /^light-dark\(hsl\((\d{1,3}) 55% 40%\), hsl\((\d{1,3}) 60% 58%\)\)$/;

  test("same key → identical colour on every call (stable across reloads)", () => {
    expect(projectColor("/home/u/proj")).toBe(projectColor("/home/u/proj"));
    expect(projectColor("p-abc123")).toBe(projectColor("p-abc123"));
  });

  test("emits a light-dark(hsl…) pair sharing one hue", () => {
    const m = projectColor("/home/u/proj").match(SHAPE);
    expect(m).not.toBeNull();
    expect(m![1]).toBe(m![2]); // same hue in both themes — only lightness differs
  });

  test("near-identical sibling paths land on distinct hues", () => {
    const colors = new Set(
      Array.from({ length: 12 }, (_, i) => projectColor(`/home/u/proj${i}`)),
    );
    expect(colors.size).toBe(12);
  });

  test("entity ids and cwds are both valid keys and differ from each other", () => {
    expect(projectColor("p-1")).toMatch(SHAPE);
    expect(projectColor("p-1")).not.toBe(projectColor("p-2"));
  });

  test("degenerate keys — empty, root, unicode — still yield a well-formed colour", () => {
    for (const key of ["", "/", "p-", "/home/ü/prøj with spaces"]) {
      const m = projectColor(key).match(SHAPE);
      expect(m).not.toBeNull();
      expect(Number(m![1])).toBeLessThanOrEqual(360);
    }
  });
});
