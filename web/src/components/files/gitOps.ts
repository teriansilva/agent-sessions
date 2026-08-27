import type { GitEntry, GitPushTarget, GitStatus } from "../../types/api";

/** What the write side of the GIT tab may do right now, given a status (#806).
 *
 *  Pure, and in its own module for the same reason `gitModes.ts` is: it can be unit-tested without
 *  a DOM, and `GitTab.tsx` keeps exporting only a component (mixing the two breaks fast refresh).
 *
 *  **These are not the enforcement.** Every rule here is also checked server-side in `gitwrite.py`,
 *  which re-reads the repository inside the call — this module exists so a control can be disabled
 *  *with the reason written on it* instead of inviting a click that comes back 409. When the two
 *  disagree, the server wins and the panel renders what it said.
 */
export interface GitOps {
  /** A refusal reason is present iff the matching `can*` is false, and it is user-facing prose. */
  canFetch: boolean;
  fetchReason: string | null;
  canPull: boolean;
  pullReason: string | null;
  canSwitch: boolean;
  switchReason: string | null;
  canCommit: boolean;
  commitReason: string | null;
  canPush: boolean;
  pushReason: string | null;
  stagedCount: number;
  dirtyCount: number;
  conflictCount: number;
}

const NO_REPO = "This folder is not inside a git working tree.";
/** One sentence, used by every operation a conflict blocks — they are all blocked for one reason
 *  and phrasing it four ways would suggest four different problems. */
const CONFLICTS = "Resolve the conflict in the session first.";

export function gitOps(
  status: GitStatus | null,
  push: GitPushTarget | null,
): GitOps {
  if (!status || !status.repo) {
    return {
      canFetch: false,
      fetchReason: NO_REPO,
      canPull: false,
      pullReason: NO_REPO,
      canSwitch: false,
      switchReason: NO_REPO,
      canCommit: false,
      commitReason: NO_REPO,
      canPush: false,
      pushReason: NO_REPO,
      stagedCount: 0,
      dirtyCount: 0,
      conflictCount: 0,
    };
  }
  const conflictCount = status.entries.filter(
    (e) => e.kind === "unmerged",
  ).length;
  const stagedCount = status.entries.filter((e) => e.kind === "staged").length;
  // Every entry, not just the tracked ones: `git switch` carries an untracked file across too,
  // and the server counts the same set, so the number the refusal names matches what it checked.
  const dirtyCount = status.entries.length;

  // A repo with no remotes has nothing to fetch. `candidates` is empty ONLY when the preflight
  // has actually answered — while it is still null we allow the click and let the server speak,
  // rather than disabling a control on the strength of not having asked yet.
  const noRemotes = push !== null && push.candidates.length === 0;

  const pullReason = conflictCount
    ? CONFLICTS
    : !status.branch
      ? "HEAD is detached — check out a branch before pulling."
      : !status.upstream
        ? `\`${status.branch}\` has no upstream to pull from.`
        : null;

  // `git commit` commits the INDEX, and past the entry cap this list is only part of it — so a
  // commit would include files the panel never showed. The server refuses it too; disabling here
  // is what stops the control advertising a count it cannot honour.
  const commitReason = conflictCount
    ? CONFLICTS
    : !status.branch
      ? "HEAD is detached — create a branch here before committing."
      : status.truncated
        ? "Too many changes for the panel to list — commit in the session's terminal, so nothing is committed unseen."
        : stagedCount === 0
          ? "Nothing is staged."
          : null;

  return {
    canFetch: !noRemotes,
    fetchReason: noRemotes ? "This repository has no remotes." : null,
    canPull: !pullReason,
    pullReason,
    canSwitch: dirtyCount === 0,
    switchReason: dirtyCount
      ? `${dirtyCount} uncommitted change${dirtyCount === 1 ? "" : "s"} would follow you onto the other branch — commit or discard them first.`
      : null,
    canCommit: !commitReason,
    commitReason,
    // The push target is resolved by the SERVER, and PUSH stays disabled until it has answered.
    // Enabling it while `push` is still null let a click fire `gitPush` with no destination ever
    // shown — the exact inverse of the "the resolved target is rendered before the operator
    // commits to it" contract (found in review). A pending preflight is not a refusal, so it
    // carries no reason; the control simply is not ready yet.
    canPush: push ? push.ok : false,
    pushReason: push
      ? push.ok
        ? null
        : push.reason
      : "Working out where this branch would push to…",
    stagedCount,
    dirtyCount,
    conflictCount,
  };
}

/** Which row-level actions apply to one entry.
 *
 *  Absent, never disabled-and-inert: a staged row cannot be staged again, and — the one that
 *  matters — an **untracked file cannot be discarded**, because `git restore` recovers from the
 *  index or HEAD and an untracked file is in neither. Offering "discard" there would be an
 *  unrecoverable delete wearing the same glyph, so the control is not drawn at all. The server
 *  refuses it too (`gitwrite.git_discard`); this is what stops the operator ever reaching for it.
 */
export function rowActionsFor(e: GitEntry): {
  stage: boolean;
  unstage: boolean;
  discard: boolean;
} {
  if (e.kind === "staged") return { stage: false, unstage: true, discard: false };
  if (e.kind === "untracked")
    return { stage: true, unstage: false, discard: false };
  if (e.kind === "unmerged")
    return { stage: false, unstage: false, discard: false };
  return { stage: true, unstage: false, discard: true };
}
