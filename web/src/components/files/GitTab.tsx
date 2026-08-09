import { GitBranch } from "lucide-react";
import type { GitEntry, GitStatus } from "../../types/api";
import { SendPath } from "./SendPath";
import styles from "./filePanel.module.css";

/** Group order is deliberate: a conflict blocks everything else, so it sorts first. */
const GROUPS: { kind: GitEntry["kind"]; label: string }[] = [
  { kind: "unmerged", label: "Conflicts" },
  { kind: "staged", label: "Staged" },
  { kind: "changed", label: "Changes" },
  { kind: "untracked", label: "Untracked" },
];

/** Status letters reuse the app's existing STATUS vocabulary rather than inventing a palette, and
 *  every one is paired with the letter itself plus a title, so the signal is never colour-only. */
function letterFor(e: GitEntry): { ch: string; cls: string; label: string } {
  if (e.kind === "unmerged")
    return { ch: "U", cls: styles.gitConflict, label: "conflicted" };
  if (e.kind === "untracked")
    return { ch: "?", cls: styles.gitUntracked, label: "untracked" };
  const ch = (e.kind === "staged" ? e.index : e.worktree) || "M";
  if (ch === "A") return { ch, cls: styles.gitAdd, label: "added" };
  if (ch === "D") return { ch, cls: styles.gitDel, label: "deleted" };
  if (ch === "R" || ch === "C")
    return { ch, cls: styles.gitMod, label: "renamed" };
  return { ch, cls: styles.gitMod, label: "modified" };
}

export function GitTab({
  status,
  loading,
  error,
  onOpen,
  onRetry,
  onSendPath,
}: {
  status: GitStatus | null;
  loading: boolean;
  error: string | null;
  onOpen: (entry: GitEntry, trigger: HTMLElement | null) => void;
  onRetry: () => void;
  /** Absolute path → the compose draft (#792). */
  onSendPath?: (path: string) => void;
}) {
  if (loading && !status) {
    return (
      <div
        className={styles.body}
        role="status"
        aria-label="Loading repository state"
      >
        {[0, 1, 2].map((i) => (
          <div
            key={i}
            className={styles.skeleton}
            style={{ width: `${60 - i * 10}%` }}
          />
        ))}
      </div>
    );
  }

  if (error) {
    return (
      <div className={styles.body}>
        <div className={`${styles.state} ${styles.stateBad}`} role="alert">
          <span className={styles.stateTag}>Git // Unavailable</span>
          {error}
          <div>
            <button type="button" className={styles.retry} onClick={onRetry}>
              Retry
            </button>
          </div>
        </div>
      </div>
    );
  }

  // "Not a repository" is a STATE, not a failure — the tab stays and says so rather than vanishing.
  if (!status || !status.repo) {
    return (
      <div className={styles.body}>
        <div className={styles.state}>
          <span className={styles.stateTag}>Git // Not a repository</span>
          This folder is not inside a git working tree. Change the root to a
          repo, or use the FILES tab.
        </div>
      </div>
    );
  }

  const total = status.entries.length;

  return (
    <div className={styles.body} data-git-tab="">
      <div className={styles.branchStrip}>
        <span className={styles.branchIcon} aria-hidden="true">
          <GitBranch size={13} />
        </span>
        <span
          className={styles.branchName}
          title={status.branch ?? "detached HEAD"}
        >
          {status.branch ?? "detached HEAD"}
        </span>
      </div>
      <div className={styles.branchMeta}>
        <span className="hud-tag">
          {/* Absent, not zero: "level with upstream" and "no upstream at all" are different facts,
              and rendering both as 0 would erase the difference. */}
          {status.ahead === null || status.behind === null
            ? status.upstream
              ? "NO DIVERGENCE DATA"
              : "NO UPSTREAM"
            : `AHEAD ${status.ahead} // BEHIND ${status.behind}`}
          {" // "}
          {total} CHANGED
        </span>
      </div>

      {total === 0 && (
        <div className={styles.state}>
          <span className={styles.stateTag}>Git // Clean</span>
          Nothing has changed in this working tree.
        </div>
      )}

      {GROUPS.map(({ kind, label }) => {
        const rows = status.entries.filter((e) => e.kind === kind);
        if (!rows.length) return null;
        return (
          <div key={kind}>
            <div className={styles.groupHead}>
              <span className="hud-tag">
                {label} // {rows.length}
              </span>
            </div>
            {rows.map((e) => {
              const { ch, cls, label: what } = letterFor(e);
              const name = e.path.split("/").pop() || e.path;
              const dir = e.path
                .slice(0, e.path.length - name.length)
                .replace(/\/$/, "");
              return (
                // Container + two sibling controls, never a button inside a button (#792).
                <div
                  key={`${e.kind}:${e.path}`}
                  data-git-row={e.path}
                  data-kind={e.kind}
                  className={styles.row}
                >
                  <button
                    type="button"
                    className={styles.rowMain}
                    title={`${e.path} — ${what}${e.orig_path ? ` (was ${e.orig_path})` : ""}`}
                    onClick={(ev) => onOpen(e, ev.currentTarget)}
                  >
                    <span
                      className={`${styles.gitLetter} ${cls}`}
                      aria-label={what}
                      title={what}
                    >
                      {ch}
                    </span>
                    {/* Filename first, path second and dimmed: the reverse is a wall of
                      "web/src/components/…" that forces horizontal scroll at panel width. */}
                    <span className={styles.rowName}>{name}</span>
                    {dir && <span className={styles.rowNote}>{dir}</span>}
                  </button>
                  {/* Repo-relative in the payload; absolute here, because the draft names files
                    relative to the SESSION cwd, which is not necessarily the repo root. */}
                  {onSendPath && status.repo && (
                    <SendPath
                      path={`${status.repo.replace(/\/$/, "")}/${e.path}`}
                      name={name}
                      onSendPath={onSendPath}
                    />
                  )}
                </div>
              );
            })}
          </div>
        );
      })}

      {status.truncated && (
        <div className={`${styles.state} ${styles.stateWarn}`}>
          <span className={styles.stateTag}>Git // Truncated</span>
          This working tree has more changes than the panel lists.
        </div>
      )}
    </div>
  );
}
