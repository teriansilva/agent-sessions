import { describe, expect, test } from "vitest";

import { WHO_COLORS, whoColor, whoIndex, whoInitial } from "./who";

describe("who-colours and initials (#1931)", () => {
  test("a name keeps its colour, whatever its case, spacing or normal form", () => {
    expect(whoColor("Jana")).toBe(whoColor("jana"));
    expect(whoColor("  Jana ")).toBe(whoColor("Jana"));
    expect(whoColor("Renée")).toBe(whoColor("Renée")); // NFC vs NFD
    expect(whoColor("Jana")).toMatch(/^var\(--who-[1-8]\)$/);
  });

  test("names spread over every colour", () => {
    const seen = new Set<number>();
    for (let i = 0; i < 200; i++) seen.add(whoIndex(`visitor-${i}`));
    expect(seen.size).toBe(WHO_COLORS);
    for (const i of seen) expect(i >= 1 && i <= WHO_COLORS).toBe(true);
  });

  test("the initial is the first grapheme, upper-cased; empty is ?", () => {
    expect(whoInitial("jana")).toBe("J");
    expect(whoInitial("  émile")).toBe("É");
    expect(whoInitial("émile")).toBe("É"); // a combining mark stays with its letter
    expect(whoInitial("👩‍💻 dev")).toBe("👩‍💻");
    expect(whoInitial("")).toBe("?");
    expect(whoInitial("   ")).toBe("?");
    expect(whoInitial(null)).toBe("?");
  });
});
