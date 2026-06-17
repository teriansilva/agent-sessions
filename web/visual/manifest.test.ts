import { describe, expect, it } from "vitest";
import { parseAreasArg, shotFile, emptyManifest } from "./manifest";

const KNOWN = new Set(["login", "app-home", "new-session"]);

describe("parseAreasArg", () => {
  it("empty / none → kind none", () => {
    expect(parseAreasArg("", KNOWN).kind).toBe("none");
    expect(parseAreasArg("  ", KNOWN).kind).toBe("none");
    expect(parseAreasArg("none", KNOWN).kind).toBe("none");
    expect(parseAreasArg(undefined, KNOWN).kind).toBe("none");
  });

  it("all → every known key", () => {
    const r = parseAreasArg("all", KNOWN);
    expect(r).toEqual({ kind: "ok", areas: expect.arrayContaining([...KNOWN]) });
  });

  it("csv of known keys → ok", () => {
    expect(parseAreasArg("login, app-home", KNOWN)).toEqual({
      kind: "ok",
      areas: ["login", "app-home"],
    });
  });

  it("unknown key → invalid with the offenders", () => {
    const r = parseAreasArg("login,nope", KNOWN);
    expect(r).toEqual({ kind: "invalid", unknown: ["nope"] });
  });
});

describe("manifest helpers", () => {
  it("shotFile is area__viewport.png", () => {
    expect(shotFile("app-home", "mobile")).toBe("app-home__mobile.png");
  });

  it("emptyManifest records base url + areas + the viewport set", () => {
    const m = emptyManifest("https://x", "abc1234", ["login"]);
    expect(m.base_url).toBe("https://x");
    expect(m.head_sha).toBe("abc1234");
    expect(m.resolved_areas).toEqual(["login"]);
    expect(m.viewports.length).toBeGreaterThanOrEqual(4);
    expect(m.paths).toEqual([]);
  });
});
