import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

async function underBase(base: string) {
  vi.stubEnv("BASE_URL", base);
  vi.resetModules();
  return {
    base: await import("./appBase"),
    links: await import("./linkEntry"),
    api: await import("./api"),
  };
}

describe("app base (#1735)", () => {
  it("the local build has no base: URLs are unchanged", async () => {
    const { base, links, api } = await underBase("/");
    expect(base.APP_BASENAME).toBe("");
    expect(base.appUrl("/login")).toBe("/login");
    expect(base.stripBase("/s/claude/x")).toBe("/s/claude/x");
    expect(api.loginRedirectUrl({ pathname: "/s/claude/x", search: "" })).toBe(
      "/login?next=%2Fs%2Fclaude%2Fx",
    );
    expect(links.classifyLink("/s/claude/abc", "https://h")?.path).toBe("/s/claude/abc");
  });

  it("the relay build keeps every URL under /connect", async () => {
    const { base, links, api } = await underBase("/connect/");
    expect(base.APP_BASENAME).toBe("/connect");
    expect(base.appUrl("/change-password")).toBe("/connect/change-password");
    expect(base.stripBase("/connect/s/claude/x")).toBe("/s/claude/x");
    expect(base.stripBase("/connect")).toBe("/");
    expect(base.stripBase("/elsewhere")).toBe("/elsewhere");
    expect(api.loginRedirectUrl({ pathname: "/connect/dashboard", search: "" })).toBe(
      "/connect/login?next=%2Fconnect%2Fdashboard",
    );
    expect(links.classifyLink("https://h/connect/s/claude-api/abc", "https://h")).toEqual({
      kind: "session",
      engine: "claude-api",
      id: "abc",
      path: "/s/claude-api/abc",
    });
    const m = `msn_${"a".repeat(32)}`;
    expect(links.classifyLink(`/connect/mission?m=${m}`, "https://h")?.path).toBe(`/mission?m=${m}`);
  });
});
