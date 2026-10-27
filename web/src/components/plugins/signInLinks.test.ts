import { describe, expect, it } from "vitest";
import { lastUrl, safeSignInUrl } from "./signInLinks";

const buffer = (rows: [string, boolean][]) => ({
  length: rows.length,
  getLine: (y: number) => rows[y] && { isWrapped: rows[y][1], translateToString: () => rows[y][0] },
});

describe("sign-in links", () => {
  it("joins a URL that soft-wraps across rows", () => {
    const buf = buffer([
      ["visit: https://claude.com/oauth?a=1&st", false],
      ["ate=abcdef", true],
      ["Paste code here if prompted > ", false],
    ]);
    expect(lastUrl(buf)).toBe("https://claude.com/oauth?a=1&state=abcdef");
  });

  it("offers the LAST link printed, and none when there is none", () => {
    expect(lastUrl(buffer([["https://a.example/one", false], ["then https://b.example/two", false]]))).toBe("https://b.example/two");
    expect(lastUrl(buffer([["Paste code here > ", false]]))).toBeNull();
  });

  it("only ever opens http(s)", () => {
    expect(safeSignInUrl("javascript:alert(1)")).toBeNull();
    expect(safeSignInUrl("file:///etc/passwd")).toBeNull();
    expect(safeSignInUrl("not a url")).toBeNull();
    expect(safeSignInUrl("https://claude.com/x")).toBe("https://claude.com/x");
  });
});
