import { describe, expect, it } from "vitest";
import {
  resolveScope,
  trailerAreas,
  bodySectionAreas,
  parseDecl,
  touchesUI,
} from "../../scripts/resolve-visual-scope.mjs";

const KNOWN = new Set(["login", "app-home", "new-session"]);

describe("resolve-visual-scope", () => {
  it("commit trailer wins", () => {
    const r = resolveScope("feat: x\n\nVisual-Areas: login, app-home", "", KNOWN);
    expect(r).toEqual({ areas: ["login", "app-home"], source: "commit trailer" });
  });

  it("trailer 'all' / 'none'", () => {
    expect(resolveScope("x\nVisual-Areas: all", "", KNOWN).areas).toEqual(["all"]);
    expect(resolveScope("x\nVisual-Areas: none", "", KNOWN).areas).toEqual([]);
  });

  it("falls back to the PR body section when no trailer", () => {
    const body = "blah\n## Areas affected\n- login\n- new-session\n## Next";
    expect(resolveScope("no trailer here", body, KNOWN)).toEqual({
      areas: ["login", "new-session"],
      source: "PR body",
    });
  });

  it("defaults to all when nothing is declared", () => {
    expect(resolveScope("just a commit", "just a body", KNOWN)).toEqual({
      areas: ["all"],
      source: "default (no declaration)",
    });
  });

  it("drops unknown keys but keeps valid ones; all-unknown falls through to default", () => {
    expect(resolveScope("x\nVisual-Areas: login, bogus", "", KNOWN).areas).toEqual(["login"]);
    expect(resolveScope("x\nVisual-Areas: bogus", "", KNOWN).source).toBe("default (no declaration)");
  });

  it("pure helpers", () => {
    expect(trailerAreas("a\nVisual-Areas: x\nVisual-Areas: y")).toBe("y"); // last wins
    expect(bodySectionAreas("## Areas affected\nlogin")).toBe("login");
    expect(parseDecl("a, b  a")).toEqual(["a", "b"]);
  });

  it("touchesUI flags UI-affecting paths only (diff-fallback classifier, #211)", () => {
    expect(touchesUI(["web/src/app/App.tsx"])).toBe(true);
    expect(touchesUI(["src/agent_sessions/templates/login.html"])).toBe(true);
    expect(touchesUI(["docs/x.css"])).toBe(true); // any *.css/svg/png/html
    expect(touchesUI(["src/agent_sessions/main.py", "tests/test_api.py"])).toBe(false);
    expect(touchesUI(["README.md", "pyproject.toml"])).toBe(false);
    expect(touchesUI([])).toBe(false);
  });
});
