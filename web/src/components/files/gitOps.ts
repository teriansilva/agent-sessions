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
const DETACHED_COMMIT = "HEAD is detached — create a branch here before committing.";
const TRUNCATED_COMMIT =
  "Too many changes for the panel to list — commit in the session's terminal, so nothing is committed unseen.";

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
  const commitReason =
    commitBlocker(status, conflictCount) ?? (stagedCount === 0 ? "Nothing is staged." : null);

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

/** The refusals every commit mode shares — conflicts, a detached HEAD, a truncated listing. */
function commitBlocker(status: GitStatus, conflictCount: number): string | null {
  if (conflictCount) return CONFLICTS;
  if (!status.branch) return DETACHED_COMMIT;
  if (status.truncated) return TRUNCATED_COMMIT;
  return null;
}

/** What a commit is made OF (#950). */
export type CommitMode = "staged" | "selected" | "all";

export interface CommitPlan {
  /** The paths a SELECTED / ALL commit sends. Empty for STAGED, which commits the index as is. */
  paths: string[];
  /** The number the control shows for this mode. */
  count: number;
  /** Present iff this mode cannot commit right now, as user-facing prose. */
  reason: string | null;
}

/** The paths each commit mode would send, and why a mode cannot commit.
 *
 *  * **STAGED** is today's commit: the index as it stands.
 *  * **SELECTED** is exactly the ticked paths, committed as they are now — the rest of the index is
 *    left alone. Selection is per PATH, not per row: what commits is the path's current worktree
 *    content. A path whose staged version is neither the last commit's nor the file as it is now
 *    is refused by the server (stage it first) — a failed settlement from there could not be
 *    recognised afterwards (review 4829).
 *  * **ALL** is every staged and changed path. An untracked file joins only when it is ticked, so
 *    a stray build artefact is never committed because nobody unticked it.
 */
export function commitPlan(
  status: GitStatus | null,
  mode: CommitMode,
  selected: ReadonlySet<string>,
): CommitPlan {
  if (!status || !status.repo) return { paths: [], count: 0, reason: NO_REPO };
  const entries = status.entries;
  const conflictCount = entries.filter((e) => e.kind === "unmerged").length;
  const blocker = commitBlocker(status, conflictCount);
  const unique = (list: GitEntry[]) => [...new Set(list.map((e) => e.path))];
  const names = (list: GitEntry[]) => [...new Set(list.flatMap(namesOf))];
  // A name that is not UTF-8 is shown lossily and refused by the server as a write target
  // (review 4833), so no commit mode may send it.
  const writable = (list: GitEntry[]) => list.filter((e) => !e.undecodable);

  if (mode === "staged") {
    const count = entries.filter((e) => e.kind === "staged").length;
    return { paths: [], count, reason: blocker ?? (count === 0 ? "Nothing is staged." : null) };
  }
  if (mode === "selected") {
    const rows = writable(entries.filter((e) => e.kind !== "unmerged" && selected.has(e.path)));
    const count = unique(rows).length;
    return {
      paths: names(rows),
      count,
      reason: blocker ?? (count === 0 ? "Tick the files to commit." : null),
    };
  }
  const all = entries.filter(
    (e) =>
      e.kind === "staged" ||
      e.kind === "changed" ||
      (e.kind === "untracked" && selected.has(e.path)),
  );
  const rows = writable(all);
  const count = unique(rows).length;
  // ALL means every change: quietly leaving an unnameable one out would not be ALL.
  const unnameable = all.length - rows.length;
  return {
    paths: names(rows),
    count,
    reason:
      blocker ??
      (unnameable > 0
        ? `${unnameable} changed file name${unnameable === 1 ? " is" : "s are"} not valid UTF-8, so the panel cannot name ${unnameable === 1 ? "it" : "them"} safely. Commit in this session's terminal.`
        : count === 0
          ? "Nothing has changed."
          : null),
  };
}

/** The names a row commits. A staged RENAME is two paths to git — the new name and the old one's
 *  deletion — so it sends both (review 4829: sending only the new name committed a copy, and left
 *  the old name's deletion staged). It stays ONE file in every count, because to the operator it
 *  is one row. A copy (`C`) leaves its source alone, so it sends only its own name. */
function namesOf(e: GitEntry): string[] {
  return e.kind === "staged" && e.index === "R" && e.orig_path ? [e.path, e.orig_path] : [e.path];
}

/** The fingerprint each named path is bound to, as the panel last rendered it: a row's own name,
 *  and — for a staged rename — its old name too, bound to the SAME row, so the server can refuse
 *  either side having moved. A name no row carries gets no fingerprint. */
export function fingerprintsFor(
  entries: readonly GitEntry[],
  paths: readonly string[],
): Record<string, string> {
  const want = new Set(paths);
  const out: Record<string, string> = {};
  for (const e of entries) {
    if (!e.fp) continue;
    for (const name of namesOf(e)) if (want.has(name)) out[name] = e.fp;
  }
  return out;
}

/** Whether the index still holds the last commit's parent for some paths (#950).
 *
 *  THREE states, not two: `unsettled: null` is the server saying it could NOT check (a read or an
 *  output budget failed). Folding that into "none" let a STAGED commit go straight through when an
 *  unfinished commit might be sitting in the index (review 4829), so it stays its own state and the
 *  panel asks. With no commit on the branch there is no last commit to be pending on. */
export type PendingState =
  | { kind: "none" }
  | { kind: "pending"; paths: string[] }
  | { kind: "unknown" };

export function pendingState(status: GitStatus | null): PendingState {
  if (!status?.head) return { kind: "none" };
  if (status.unsettled === null) return { kind: "unknown" };
  const paths = status.unsettled ?? [];
  return paths.length > 0 ? { kind: "pending", paths } : { kind: "none" };
}

/** Which row-level actions apply to one entry.
 *
 *  Absent, never disabled-and-inert: a staged row cannot be staged again, and — the one that
 *  matters — an **untracked file cannot be discarded**, because `git restore` recovers from the
 *  index or HEAD and an untracked file is in neither. Offering "discard" there would be an
 *  unrecoverable delete wearing the same glyph, so the control is not drawn at all. The server
 *  refuses it too (`gitwrite.git_discard`); this is what stops the operator ever reaching for it.
 *
 *  **REVERT** (#950) is the staged row's counterpart to discard: the file goes back to the last
 *  commit, index and worktree together. A path the last commit does not carry is refused by the
 *  server (reverting a newly added file would delete it) rather than guessed at here.
 */
export function rowActionsFor(e: GitEntry): {
  stage: boolean;
  unstage: boolean;
  discard: boolean;
  revert: boolean;
} {
  // Its name is shown lossily and the server refuses it as a target (review 4833).
  if (e.undecodable) return { stage: false, unstage: false, discard: false, revert: false };
  if (e.kind === "staged") return { stage: false, unstage: true, discard: false, revert: true };
  if (e.kind === "untracked")
    return { stage: true, unstage: false, discard: false, revert: false };
  if (e.kind === "unmerged")
    return { stage: false, unstage: false, discard: false, revert: false };
  return { stage: true, unstage: false, discard: true, revert: false };
}
