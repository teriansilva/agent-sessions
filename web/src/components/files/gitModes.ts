import type { GitEntry } from "../../types/api";

/** Which viewer modes apply to a row (#784).
 *
 *  A mode that does not apply is **absent**, never a disabled control the user can press to no
 *  effect: a deleted path has nothing left to read, and an untracked one has nothing to compare
 *  against. Lives in its own module so `GitTab.tsx` exports only a component — mixing the two
 *  breaks fast refresh.
 */
export function modesFor(e: GitEntry): { diff: boolean; content: boolean; defaultDiff: boolean } {
  const ch = e.kind === "staged" ? e.index : e.worktree;
  if (e.kind === "untracked") return { diff: false, content: true, defaultDiff: false };
  if (ch === "D") return { diff: true, content: false, defaultDiff: true };
  // A conflicted file defaults to CONTENT: the working copy WITH its markers is the view that
  // actually helps; stage-2-vs-stage-3 is the secondary question.
  if (e.kind === "unmerged") return { diff: true, content: true, defaultDiff: false };
  return { diff: true, content: true, defaultDiff: true };
}
