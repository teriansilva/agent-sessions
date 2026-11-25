import { describe, expect, test } from "vitest";

import {
  linkDisplay,
  linkError,
  nameFromLink,
  normalizeLink,
  originSaid,
  refusalHint,
  resolveRows,
  sentence,
} from "./repoLinkModel";

describe("a repository link (#2353)", () => {
  test("only an https repository root passes the quick check", () => {
    for (const ok of [
      "https://github.com/owner/repo",
      "https://github.com/owner/repo.git",
      "https://github.com/owner/repo/",
      "https://git.example:8443/team/server",
    ])
      expect(linkError(ok), ok).toBe("");
    expect(linkError("")).toMatch(/repository’s link/);
    expect(linkError("http://github.com/owner/repo")).toMatch(/https:\/\//);
    expect(linkError("git@github.com:owner/repo.git")).toMatch(/https:\/\//);
    expect(linkError("https://github.com/owner/repo/tree/main/pkg")).toMatch(/no \/tree\//);
    expect(linkError("https://github.com/owner/repo/blob/main/package.json")).toMatch(/no \/tree\//);
    expect(linkError("https://github.com/owner")).toMatch(/root link/);
    expect(linkError("https://github.com/owner/repo?tab=x")).toMatch(/root link/);
  });

  test("normalizing drops .git and trailing slashes and lowercases the host only", () => {
    expect(normalizeLink("HTTPS://GitHub.com/Owner/Repo.git/")).toBe("https://github.com/Owner/Repo");
    expect(linkDisplay("https://github.com/owner/repo.git")).toBe("github.com/owner/repo");
    expect(originSaid({ url: "https://github.com/owner/repo", commit: "3f2a9c1e5b7d40a8" })).toBe(
      "github.com/owner/repo @ 3f2a9c1",
    );
  });

  test("a name follows the repository's own name", () => {
    expect(nameFromLink("https://github.com/acme/acme-mcp-server")).toBe("acme");
    expect(nameFromLink("https://github.com/acme/Files.git")).toBe("files");
    expect(nameFromLink("https://github.com/acme")).toBe("");
  });

  test("progress rows: before the step done, the step running, the rest waiting", () => {
    const rows = resolveRows({ step: "release", commit: "3f2a9c1e5b", manifest: "package.json", kind: "npm", package: "@acme/x", version: "1.4.0" });
    expect(rows.map((r) => [r.key, r.state])).toEqual([
      ["commit", "done"],
      ["manifest", "done"],
      ["release", "running"],
      ["repository", "waiting"],
      ["review", "waiting"],
    ]);
    expect(rows[0].what).toBe("default branch head 3f2a9c1");
    expect(rows[1].what).toBe("package.json → @acme/x 1.4.0");
    expect(rows[2].what).toBe("npm @acme/x 1.4.0, exactly");
    expect(resolveRows({})[0].state).toBe("running");
  });

  test("a refusal's hint and words", () => {
    const both = { repository: "r", commit: "c", found: [
      { manifest: "package.json" as const, kind: "npm" as const, package: "a", version: "1.0.0" },
      { manifest: "pyproject.toml" as const, kind: "pypi" as const, package: "a", version: "1.0.0" },
    ] };
    expect(refusalHint(both, "")).toMatch(/choose npm package or PyPI package/);
    expect(refusalHint(null, "npm has no published release a 1.0.0")).toMatch(/registry publishes/);
    expect(refusalHint(null, "something else")).toBe("");
    expect(sentence("this repository declares both")).toBe("This repository declares both.");
    expect(sentence("Done.")).toBe("Done.");
    expect(sentence("npm has no published release a 1.0.0")).toBe("npm has no published release a 1.0.0.");
  });
});
