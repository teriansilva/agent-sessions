import { describe, expect, it } from "vitest";
import type { GitEntry, GitPushTarget, GitStatus } from "../../types/api";
import { gitOps, rowActionsFor } from "./gitOps";

function entry(over: Partial<GitEntry> = {}): GitEntry {
  return { path: "a.txt", index: ".", worktree: "M", kind: "changed", oid: null, ...over };
}

function status(over: Partial<GitStatus> = {}): GitStatus {
  return {
    repo: "/home/u/proj",
    branch: "main",
    upstream: "origin/main",
    ahead: 0,
    behind: 0,
    entries: [],
    truncated: false,
    ...over,
  };
}

function target(over: Partial<GitPushTarget> = {}): GitPushTarget {
  return {
    ok: true,
    reason: null,
    branch: "main",
    remote: "origin",
    target: "origin/main",
    expect: "origin/main@0123456789abcdef",
    candidates: ["origin"],
    set_upstream: false,
    ...over,
  };
}

describe("write-side preconditions (#806)", () => {
  it("offers nothing outside a repository, and says why once", () => {
    const o = gitOps(status({ repo: null }), null);
    expect([o.canFetch, o.canPull, o.canSwitch, o.canCommit, o.canPush]).toEqual([
      false, false, false, false, false,
    ]);
    expect(o.switchReason).toMatch(/not inside a git working tree/);
  });

  it("blocks a switch on a dirty tree and names the count the server will name", () => {
    const o = gitOps(status({ entries: [entry(), entry({ path: "b.txt" })] }), target());
    expect(o.canSwitch).toBe(false);
    expect(o.switchReason).toMatch(/2 uncommitted changes/);
  });

  it("counts an untracked file as dirty, because `git switch` carries it across too", () => {
    const o = gitOps(status({ entries: [entry({ kind: "untracked", worktree: "?" })] }), target());
    expect(o.canSwitch).toBe(false);
    expect(o.switchReason).toMatch(/1 uncommitted change\b/);
  });

  it("singular and plural are not the same sentence", () => {
    expect(gitOps(status({ entries: [entry()] }), target()).switchReason).toContain("1 uncommitted change ");
    expect(gitOps(status({ entries: [entry(), entry()] }), target()).switchReason).toContain("2 uncommitted changes");
  });

  it("lets a conflict block pull and commit with ONE reason, not four phrasings", () => {
    const o = gitOps(status({ entries: [entry({ kind: "unmerged" })] }), target());
    expect(o.canPull).toBe(false);
    expect(o.canCommit).toBe(false);
    expect(o.pullReason).toBe(o.commitReason);
    expect(o.conflictCount).toBe(1);
  });

  it("refuses a pull with no upstream, naming the branch", () => {
    const o = gitOps(status({ upstream: null }), target());
    expect(o.canPull).toBe(false);
    expect(o.pullReason).toMatch(/`main` has no upstream/);
  });

  it("refuses a pull on a detached HEAD", () => {
    const o = gitOps(status({ branch: null, upstream: null }), target());
    expect(o.canPull).toBe(false);
    expect(o.pullReason).toMatch(/detached/);
  });

  it("will not offer a commit with an empty index", () => {
    expect(gitOps(status(), target()).canCommit).toBe(false);
    const staged = gitOps(status({ entries: [entry({ kind: "staged", index: "M" })] }), target());
    expect(staged.canCommit).toBe(true);
    expect(staged.stagedCount).toBe(1);
  });

  it("passes the SERVER's push refusal through instead of restating it", () => {
    const o = gitOps(
      status({ upstream: null }),
      target({ ok: false, reason: "several remotes — name one", target: null, candidates: ["origin", "backup"] }),
    );
    expect(o.canPush).toBe(false);
    expect(o.pushReason).toBe("several remotes — name one");
  });

  it("does not disable fetch merely because the preflight has not answered yet", () => {
    // `null` is "not asked", which is not the same fact as "no remotes" — treating them alike
    // greys out the control on first paint, every time.
    expect(gitOps(status(), null).canFetch).toBe(true);
    expect(gitOps(status(), target({ candidates: [] })).canFetch).toBe(false);
  });
});

describe("row actions (#806)", () => {
  it("never offers to discard an untracked file", () => {
    // `git restore` recovers from the index or HEAD; an untracked file is in neither, so this
    // control would be an unrecoverable delete wearing the same glyph.
    expect(rowActionsFor(entry({ kind: "untracked", worktree: "?" })).discard).toBe(false);
    expect(rowActionsFor(entry({ kind: "untracked", worktree: "?" })).stage).toBe(true);
  });

  it("offers unstage — and only unstage — on a staged row", () => {
    expect(rowActionsFor(entry({ kind: "staged", index: "M" }))).toEqual({
      stage: false,
      unstage: true,
      discard: false,
    });
  });

  it("offers stage and discard on a tracked worktree change", () => {
    expect(rowActionsFor(entry())).toEqual({ stage: true, unstage: false, discard: true });
  });

  it("offers nothing on a conflicted row — resolution happens in the session", () => {
    expect(rowActionsFor(entry({ kind: "unmerged" }))).toEqual({
      stage: false,
      unstage: false,
      discard: false,
    });
  });
});

describe("push is gated on the preflight (#825 review)", () => {
  it("is NOT enabled before the preflight has answered", () => {
    // Enabling it while `push` is still null let a click fire `gitPush` with no destination ever
    // shown — the exact inverse of "the resolved target is rendered before the operator commits
    // to it". A pending preflight is not a refusal, so the wording says so.
    const o = gitOps(status(), null);
    expect(o.canPush).toBe(false);
    expect(o.pushReason).toMatch(/Working out where/);
  });

  it("becomes enabled once a target resolves, with no reason attached", () => {
    const o = gitOps(status(), target());
    expect(o.canPush).toBe(true);
    expect(o.pushReason).toBeNull();
  });

  it("stays disabled when the preflight came back ambiguous, carrying its candidates' reason", () => {
    const o = gitOps(
      status({ upstream: null }),
      target({ ok: false, reason: "several remotes (origin, backup)", target: null, candidates: ["origin", "backup"] }),
    );
    expect(o.canPush).toBe(false);
    expect(o.pushReason).toContain("origin, backup");
  });
});

describe("a truncated status cannot be committed (#806, review round 6)", () => {
  it("disables commit when the listing was truncated, and says why", () => {
    // `git commit` commits the INDEX, not the rows. Past the entry cap the panel can only show
    // some of what is staged, so offering the control would mean committing files it never
    // displayed. The server refuses this too; disabling here is what stops the UI advertising a
    // button that is guaranteed to 409.
    const o = gitOps(
      status({ truncated: true, entries: [entry({ kind: "staged", index: "M" })] }),
      target(),
    );
    expect(o.canCommit).toBe(false);
    expect(o.commitReason).toMatch(/too many|truncated|terminal/i);
  });

  it("still offers commit when the same status is complete", () => {
    // The control: the refusal has to be about truncation specifically, not about staging.
    const o = gitOps(
      status({ truncated: false, entries: [entry({ kind: "staged", index: "M" })] }),
      target(),
    );
    expect(o.canCommit).toBe(true);
    expect(o.commitReason).toBeNull();
  });
});
