import { describe, expect, it } from "vitest";
import { expectsLoopbackCallback, isLoopbackAddress, lastUrl, linkSegments, safeSignInUrl, screenLines } from "./signInLinks";

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

describe("the sign-in output as text (#2106)", () => {
  it("joins soft-wrapped rows, trims, and keeps one blank line between paragraphs", () => {
    const buf = buffer([
      ["", false],
      ["Signing in to GitHub.com...   ", false],
      ["", false],
      ["", false],
      ["visit https://github.com/login/oauth/authorize?client_id=Iv1&redirect_u", false],
      ["ri=http%3A%2F%2F127.0.0.1%3A35067%2Fcallback", true],
      ["", false],
    ]);
    expect(screenLines(buf)).toEqual([
      "Signing in to GitHub.com...",
      "",
      "visit https://github.com/login/oauth/authorize?client_id=Iv1&redirect_uri=http%3A%2F%2F127.0.0.1%3A35067%2Fcallback",
    ]);
  });

  it("splits a line into text and safe links", () => {
    expect(linkSegments("open https://a.example/x now")).toEqual([
      { text: "open ", href: null },
      { text: "https://a.example/x", href: "https://a.example/x" },
      { text: " now", href: null },
    ]);
    expect(linkSegments("no links")).toEqual([{ text: "no links", href: null }]);
  });

  it("spots a loopback callback, named outright or as a redirect_uri", () => {
    expect(expectsLoopbackCallback(["https://github.com/login/oauth/authorize?redirect_uri=http%3A%2F%2F127.0.0.1%3A35067%2Fcallback"])).toBe(true);
    expect(expectsLoopbackCallback(["Starting local login server on http://localhost:1455."])).toBe(true);
    expect(expectsLoopbackCallback(["https://accounts.google.com/o/oauth2/v2/auth?redirect_uri=http://[::1]:8085/oauth2callback"])).toBe(true);
    expect(expectsLoopbackCallback(["https://claude.ai/oauth/authorize?redirect_uri=https%3A%2F%2Fconsole.anthropic.com%2Fcallback"])).toBe(false);
    expect(expectsLoopbackCallback(["Enter the code at https://github.com/login/device"])).toBe(false);
  });

  it("recognises the address the browser ended on, and only a loopback one", () => {
    expect(isLoopbackAddress(" http://127.0.0.1:35067/callback?code=1&state=2 ")).toBe(true);
    expect(isLoopbackAddress("http://localhost:1455/auth/callback?code=x")).toBe(true);
    expect(isLoopbackAddress("http://[::1]:8085/oauth2callback")).toBe(true);
    expect(isLoopbackAddress("http://127.0.0.1/callback")).toBe(false);
    expect(isLoopbackAddress("http://127.0.0.1.evil.example:80/")).toBe(false);
    expect(isLoopbackAddress("abc123#state")).toBe(false);
  });
});
