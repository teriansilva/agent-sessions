import { describe, expect, it } from "vitest";
import { classifyLink, isPublicShareLink } from "./linkEntry";

const O = "https://battlelab.example";
const MID = "msn_" + "a".repeat(32);

describe("classifyLink (#1232)", () => {
  it("classifies a session link, path-only or absolute on the same origin", () => {
    const want = { kind: "session", engine: "claude", id: "8f2c-01", path: "/s/claude/8f2c-01" };
    expect(classifyLink("/s/claude/8f2c-01", O)).toEqual(want);
    expect(classifyLink(`${O}/s/claude/8f2c-01/`, O)).toEqual(want);
  });

  it("classifies a mission link and rebuilds its path from the id alone", () => {
    expect(classifyLink(`/mission?m=${MID}&x=1`, O)).toEqual({
      kind: "mission",
      id: MID,
      path: `/mission?m=${MID}`,
    });
  });

  it.each([
    ["another origin", "https://evil.example/s/claude/abc"],
    ["a new-<uuid> placeholder", "/s/opencode/new-1234"],
    ["an engine that is not a slug", "/s/Claude!/abc"],
    ["a traversal-shaped id", "/s/claude/..%2Fetc"],
    ["an extra path segment", "/s/claude/abc/files"],
    ["a malformed mission id", "/mission?m=msn_zz"],
    ["a mission path with no id", "/mission"],
    ["any other route", "/settings/appearance"],
    ["junk", "http://["],
  ])("rejects %s", (_, raw) => {
    expect(classifyLink(raw, O)).toBeNull();
  });
});

describe("isPublicShareLink (#1404)", () => {
  const O = "https://lab.example";
  it("knows a same-origin public link, and nothing else", () => {
    expect(isPublicShareLink(`${O}/p/${"A".repeat(43)}`, O)).toBe(true);
    expect(isPublicShareLink("/p/abc_DEF-1", O)).toBe(true);
    expect(isPublicShareLink(`${O}/p/`, O)).toBe(false);
    expect(isPublicShareLink(`https://evil.example/p/${"A".repeat(43)}`, O)).toBe(false);
    expect(isPublicShareLink(`${O}/s/claude/abc`, O)).toBe(false);
    expect(isPublicShareLink(`${O}/p/a/b`, O)).toBe(false);
    // a public link is never classified as an in-app route
    expect(classifyLink(`${O}/p/${"A".repeat(43)}`, O)).toBeNull();
  });
});
