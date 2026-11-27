import { describe, expect, test } from "vitest";

import {
  declaredWords,
  goCommandWhere,
  goDependencyWords,
  goVersionWords,
  installedIntegrity,
  linkDisplay,
  linkError,
  nameFromLink,
  normalizeLink,
  originSaid,
  refusalHint,
  resolveRows,
  sentence,
  toolchainWords,
  updatedFlash,
  updateSteps,
  upToDateWords,
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

const MODULE = "git.b4mad.industries/agentic-forges/forgejo-mcp/v3";

describe("a Go module from a repository link (#2427)", () => {
  test("the progress rows are worded for Go: Module (the toolchain's first download), Commit match", () => {
    const at = (step: "commit" | "manifest" | "release" | "repository" | "review", extra = {}) =>
      resolveRows({ step, commit: "9c3fa38c6708", manifest: "go.mod", kind: "go", package: MODULE, ...extra });
    const release = at("release");
    expect(release.map((r) => r.label)).toEqual(["Commit", "Manifest", "Module", "Commit match", "Review"]);
    expect(release[1].what).toBe(`go.mod → ${MODULE}`);
    expect(release[2].state).toBe("running");
    expect(release[2].what).toMatch(/^Downloading the Go toolchain \(first time only\)/);
    const review = at("review", { version: "v3.3.0" });
    expect(review[2].what).toBe(`${MODULE} v3.3.0, from the module proxy`);
    expect(review[2].state).toBe("done");
    expect(review[4].what).toMatch(/nothing built/);
    // An npm link keeps its own rows.
    expect(resolveRows({ step: "release", kind: "npm", package: "a", version: "1.0.0" })[2].label).toBe("Release");
  });

  test("Update from source puts the installed commit beside the head just read", () => {
    const rows = resolveRows({ step: "manifest", commit: "4b1d2e7f0a93" }, "9c3fa38c6708");
    expect(rows[0].what).toBe("default branch head 4b1d2e7 · installed 9c3fa38");
    expect(resolveRows({ step: "commit" }, "9c3fa38c6708")[0].what).not.toMatch(/installed/);
  });

  test("the Go facts in words", () => {
    expect(declaredWords({ manifest: "go.mod", kind: "go", package: MODULE, version: "" })).toBe(`Go module ${MODULE}`);
    expect(goVersionWords({ ref: "refs/tags/v3.3.0", version: "v3.3.0" })).toBe("the tag at this commit");
    expect(goVersionWords({ ref: "", version: "v0.0.0-20261127-9c3fa38c6708" })).toMatch(/pseudo-version/);
    expect(goCommandWhere(MODULE, MODULE)).toBe("the module root");
    expect(goCommandWhere(`${MODULE}/cmd/server`, MODULE)).toBe("cmd/server");
    expect(toolchainWords({ version: "1.27.4", sha256: "x", platform: "linux/amd64" })).toBe(
      "go1.27.4 · BattleLab’s own, sha256-verified · cgo off",
    );
    expect(toolchainWords({ version: "go1.27.4", sha256: "x", platform: "" })).toMatch(/^go1\.27\.4 ·/);
    // The computed count, in the copy the review asked for (not "pinned by go.sum").
    expect(goDependencyWords(46)).toBe("46 modules in its build · each verified against sum.golang.org");
    expect(goDependencyWords(1)).toBe("1 module in its build · each verified against sum.golang.org");
  });

  test("a Go refusal's hint names the way forward", () => {
    const go = { repository: "github.com/someone/x-mirror", commit: "7d41c9e", found: [
      { manifest: "go.mod" as const, kind: "go" as const, package: "git.example.org/x", version: "" },
    ] };
    expect(refusalHint(go, "BattleLab can't build this Go module: the module builds several commands (cmd/a, cmd/b), and BattleLab never picks one for you")).toMatch(
      /exactly one command per server/,
    );
    expect(refusalHint(go, "the Go module proxy's git.example.org/x is not at this repository's commit (7d41c9e)")).toBe(
      "A mirror is accepted only when the module’s own repository has the same commit. Link git.example.org/x itself, or a commit it has published. Nothing was built or registered.",
    );
  });

  test("update words: up to date, updated, the old hash only when that source is installed", () => {
    const src = { kind: "go", package: MODULE, version: "v3.3.0" };
    expect(upToDateWords(src, { url: "https://codeberg.org/goern/forgejo-mcp", commit: "9c3fa38c6708" })).toBe(
      "Already up to date at v3.3.0 (9c3fa38)",
    );
    expect(updatedFlash({ kind: "go", version: "v3.4.0" })).toBe(
      "Updated to v3.4.0 — run the checks; new sessions don't get this server until a check passes.",
    );
    expect(installedIntegrity({ integrity: "h1:a", source: src }, { ...src })).toBe("h1:a");
    expect(installedIntegrity({ integrity: "h1:a", source: { ...src, version: "v3.2.0" } }, src)).toBe("");
    expect(installedIntegrity(null, src)).toBe("");
  });

  test("the update's three steps: Check says the existing check still counts when nothing changed", () => {
    const steps = updateSteps("forgejo-mcp", "https://codeberg.org/goern/forgejo-mcp", "go", false);
    expect(steps.map(([b]) => b)).toEqual(["Source", "Review", "Check"]);
    expect(steps[1][1]).toMatch(/builds exactly the new module version and hash/);
    expect(steps[2][1]).toMatch(/needs its own passing check/);
    expect(updateSteps("x", "https://h/o/r", "npm", false)[1][1]).toMatch(/fetches exactly the new lock/);
    expect(updateSteps("x", "https://h/o/r", "go", true)[2][1]).toMatch(/keeps its install and its passing check/);
  });
});
