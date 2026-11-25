import { describe, expect, test } from "vitest";

import type { AskConversationSummary } from "../../types/api";

import { arrange, dayGroup, stamp, titleMatches } from "./askSwitcherList";

// Thursday 6 Nov 2026, 12:00 local.
const NOW = new Date(2026, 10, 6, 12, 0);
const at = (d: number, h = 9, m = 0) => new Date(2026, 10, d, h, m).getTime() / 1000;
const row = (id: string, title: string, ts: number, unread = false): AskConversationSummary => ({
  id,
  title,
  created_at: ts,
  updated_at: ts,
  unread,
});

describe("titleMatches", () => {
  test("every word, any order, case and accents folded", () => {
    expect(titleMatches("Find the pm-core media generation", "MEDIA pm-core")).toBe(true);
    expect(titleMatches("General Mateo Ríos", "rios mateo")).toBe(true);
    expect(titleMatches("Find the pm-core media generation", "media tts")).toBe(false);
    expect(titleMatches("anything", "   ")).toBe(true);
  });
});

describe("dayGroup", () => {
  test("today, yesterday, the last 7 days, older — by local calendar day", () => {
    expect(dayGroup(at(6, 0, 1), NOW)).toBe("today");
    expect(dayGroup(at(5, 23, 59), NOW)).toBe("yesterday");
    expect(dayGroup(at(5, 0, 0), NOW)).toBe("yesterday");
    expect(dayGroup(at(4, 23, 59), NOW)).toBe("week");
    // 31 Oct is six days back: the seventh day of "Last 7 days". 30 Oct is older.
    expect(dayGroup(new Date(2026, 9, 31, 0, 0).getTime() / 1000, NOW)).toBe("week");
    expect(dayGroup(new Date(2026, 9, 30, 9).getTime() / 1000, NOW)).toBe("older");
  });
});

describe("arrange", () => {
  const rows = [
    row("a", "Hey, what is running", at(6, 9, 22)),
    row("b", "Find media generation", at(6, 9, 10), true),
    row("c", "Give me a status", at(5, 17, 47)),
    row("d", "Old question", new Date(2026, 9, 1).getTime() / 1000, true),
    row("e", "Week question", at(3, 8)),
    row("f", "Ancient", new Date(2026, 8, 1).getTime() / 1000),
  ];

  test("updates pinned on top in the server's order, then the day sections", () => {
    const s = arrange(rows, "", NOW);
    expect(s.map((x) => [x.group, x.rows.map((r) => r.id)])).toEqual([
      ["updates", ["b", "d"]],
      ["today", ["a"]],
      ["yesterday", ["c"]],
      ["week", ["e"]],
      ["older", ["f"]],
    ]);
  });

  test("the search filters before grouping; empty sections are left out", () => {
    const s = arrange(rows, "question", NOW);
    expect(s.map((x) => [x.group, x.rows.map((r) => r.id)])).toEqual([
      ["updates", ["d"]],
      ["week", ["e"]],
    ]);
    expect(arrange(rows, "nothing like it", NOW)).toEqual([]);
  });
});

describe("stamp", () => {
  test("a time under a day heading; an update row says the day too", () => {
    const t = (d: Date) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    expect(stamp(at(6, 9, 22), "today", NOW)).toBe(t(new Date(2026, 10, 6, 9, 22)));
    expect(stamp(at(5, 17, 47), "yesterday", NOW)).toBe(t(new Date(2026, 10, 5, 17, 47)));
    expect(stamp(at(5, 17, 47), "updates", NOW)).toBe(
      `Yesterday ${t(new Date(2026, 10, 5, 17, 47))}`,
    );
    expect(stamp(at(6, 9, 22), "updates", NOW)).toBe(t(new Date(2026, 10, 6, 9, 22)));
    expect(stamp(at(3, 8), "week", NOW)).toMatch(/\d/);
    expect(stamp(at(3, 8), "week", NOW)).not.toBe(t(new Date(2026, 10, 3, 8)));
  });
});
