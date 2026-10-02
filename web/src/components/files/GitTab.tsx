import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  ArrowDownToLine,
  Check,
  ChevronDown,
  GitBranch,
  Minus,
  MoreHorizontal,
  Plus,
  RotateCcw,
  Undo2,
  Upload,
} from "lucide-react";
import { ApiError, api } from "../../lib/api";
import type {
  GitBranches,
  GitEntry,
  GitLog,
  GitLogCommit,
  GitPushTarget,
  GitStatus,
  GitWriteResult,
} from "../../types/api";
import { BranchMenu } from "./BranchMenu";
import {
  commitPlan,
  fingerprintsFor,
  gitOps,
  pendingState,
  revertCommitSummary,
  revertReason,
  worktreePendingState,
  rowActionsFor,
  type CommitMode,
} from "./gitOps";
import { RowMenu, type RowOp } from "./RowMenu";
import { SendPath } from "./SendPath";
import styles from "./filePanel.module.css";

/** Group order is deliberate: a conflict blocks everything else, so it sorts first. */
const GROUPS: { kind: GitEntry["kind"]; label: string }[] = [
  { kind: "unmerged", label: "Conflicts" },
  { kind: "staged", label: "Staged" },
  { kind: "changed", label: "Changes" },
  { kind: "untracked", label: "Untracked" },
];

const MODES: { mode: CommitMode; label: string }[] = [
  { mode: "staged", label: "Staged" },
  { mode: "selected", label: "Selected" },
  { mode: "all", label: "All" },
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

/** The recoverable object ids a discard or revert returned, as one sentence the operator can use. */
function recoverySentence(r: GitWriteResult): string {
  const ids = [
    ...Object.values(r.recoverable ?? {}).flat(),
    ...Object.values(r.staged_recoverable ?? {}),
  ];
  return ids.length
    ? ` Previous contents: ${ids.map((o) => o.slice(0, 12)).join(", ")} — recover with \`git cat-file -p <id>\`.`
    : "";
}

/** Compact age for a commit row, as the mockup has it ("12m", "2h", "3d"): the row is narrow and the
 *  subject is what should get the width. The full time rides on the title. */
function shortAgo(epoch: number): string {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - epoch));
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)}d`;
  return new Date(epoch * 1000).toISOString().slice(0, 10);
}

type ConfirmState =
  | {
      kind: "discard";
      root: string;
      paths: string[];
      /** The fingerprints as they were WHEN THE DIALOG OPENED — an immutable snapshot, not a
       *  lookup done at Confirm time. Recomputing from the latest `status` meant a poll landing
       *  while the dialog was open would bless the NEW bytes: the operator reads one description
       *  and confirms another. The dialog is a promise about what it showed. */
      expect: Record<string, string>;
      trigger: HTMLElement | null;
    }
  | {
      kind: "revert";
      root: string;
      paths: string[];
      expect: Record<string, string>;
      /** The commit the dialog named — the server refuses if HEAD has moved since. */
      head: string;
      trigger: HTMLElement | null;
    }
  | {
      kind: "revert-commit";
      root: string;
      commit: GitLogCommit;
      /** The tip the dialog named — the server refuses if the branch has moved since. */
      head: string;
      branch: string;
      trigger: HTMLElement | null;
    }
  | {
      kind: "unsettled-commit";
      root: string;
      /** The paths whose staged content would revert the last commit. */
      paths: string[];
      message: string;
      stagedFp: string;
      trigger: HTMLElement | null;
    }
  | {
      /** The server could not check for unsettled paths, so a STAGED commit asks rather than
       *  assuming there are none. */
      kind: "unknown-pending-commit";
      root: string;
      message: string;
      stagedFp: string;
      trigger: HTMLElement | null;
    };

export function GitTab({
  root,
  sessionKey,
  status,
  loading,
  error,
  onOpen,
  onRetry,
  onSendPath,
  onStatus,
}: {
  /** The panel's current root — every write is scoped to the repository containing it. */
  root: string;
  /** Named in the discard confirmation: a panel is docked into ONE session and destroying that
   *  session's work from another tab is exactly the mistake worth spelling out. */
  sessionKey: string;
  status: GitStatus | null;
  loading: boolean;
  error: string | null;
  onOpen: (entry: GitEntry, trigger: HTMLElement | null) => void;
  onRetry: () => void;
  /** Absolute path → the compose draft (#792). */
  onSendPath?: (path: string) => void;
  /** Every write answers with the post-write status; handing it up settles the panel from the
   *  SERVER rather than from an optimistic guess (#806). The second argument is the root the
   *  write STARTED against, so the panel can drop a completion that no longer describes what it
   *  is showing. */
  onStatus?: (s: GitStatus, forRoot: string) => void;
}) {
  // Tagged with the root it describes, so changing the panel root RETIRES the previous root's
  // transient surfaces by derivation rather than by a setState inside an effect (which cascades
  // a render, and which the lint rule rightly rejects).
  const [op, setOp] = useState<{
    root: string;
    busy: string | null;
    error: string | null;
    notice: string | null;
  }>({ root, busy: null, error: null, notice: null });
  const busy = op.root === root ? op.busy : null;
  const opError = op.root === root ? op.error : null;
  const notice = op.root === root ? op.notice : null;
  // Tagged with the root each answer describes. Deriving staleness beats clearing it in an
  // effect: a synchronous setState there cascades a render, and — the reason that rule exists —
  // an untagged value briefly describes the PREVIOUS repository after the root changes.
  const [branchesRes, setBranchesRes] = useState<{ root: string; v: GitBranches } | null>(null);
  const [pushRes, setPushRes] = useState<{ root: string; v: GitPushTarget } | null>(null);
  /** An explicit remote the operator picked out of an ambiguous preflight's candidate list.
   *  Tagged with its root: a remote that exists in repo A means nothing in repo B. */
  const [remoteState, setRemoteState] = useState<{ root: string; name: string } | null>(null);
  const remoteChoice = remoteState?.root === root ? remoteState.name : undefined;
  const setRemoteChoice = useCallback((name: string) => setRemoteState({ root, name }), [root]);
  const [menuOpen, setMenuOpen] = useState<{
    el: HTMLElement | null;
    rect: { top: number; bottom: number; left: number; width: number };
  } | null>(null);
  // One row's ⋯ menu on a coarse pointer (#950). Root-tagged like everything else here, and it
  // closes by derivation when its row leaves the status (see `rowMenu` below).
  const [rowMenuState, setRowMenuState] = useState<{
    root: string;
    entry: GitEntry;
    el: HTMLElement;
    rect: { top: number; bottom: number; left: number; right: number };
  } | null>(null);
  // Root-tagged, all of them. A confirmation that survives a root change is a data-loss path: it
  // stores paths, the write uses the CURRENT root, and a same-named path in the new repository
  // gets destroyed instead. The commit draft, the selection and the mode belong to their
  // repository for the same reason, if less dangerously.
  const [msgState, setMsgState] = useState<{ root: string; text: string }>({ root, text: "" });
  const message = msgState.root === root ? msgState.text : "";
  const setMessage = useCallback(
    (text: string) => setMsgState({ root, text }),
    [root],
  );
  const [selState, setSelState] = useState<{ root: string; paths: string[] }>({
    root,
    paths: [],
  });
  const selected = new Set(selState.root === root ? selState.paths : []);
  const toggleSelected = useCallback(
    (path: string) =>
      setSelState((prev) => {
        const current = prev.root === root ? prev.paths : [];
        return {
          root,
          paths: current.includes(path) ? current.filter((p) => p !== path) : [...current, path],
        };
      }),
    [root],
  );
  const [modeState, setModeState] = useState<{ root: string; mode: CommitMode }>({
    root,
    mode: "staged",
  });
  const mode: CommitMode = modeState.root === root ? modeState.mode : "staged";
  const [confirmState, setConfirmState] = useState<ConfirmState | null>(null);
  const confirm = confirmState && confirmState.root === root ? confirmState : null;
  const rowMenu =
    rowMenuState &&
    rowMenuState.root === root &&
    status?.entries.some(
      (x) => x.kind === rowMenuState.entry.kind && x.path === rowMenuState.entry.path,
    )
      ? rowMenuState
      : null;
  const repo = status?.repo ?? null;
  const branch = status?.branch ?? null;
  // Bumped after every successful write so the two read-only side-loads (branches, push target)
  // re-resolve against the new reality instead of describing the previous one.
  const [opTick, setOpTick] = useState(0);

  useEffect(() => {
    if (!repo) return;
    let live = true;
    const ctl = new AbortController();
    api
      .gitBranches(root, { signal: ctl.signal })
      .then((v) => live && setBranchesRes({ root, v }))
      .catch(() => undefined);
    api
      .gitPushTarget(root, remoteChoice, { signal: ctl.signal })
      .then((v) => live && setPushRes({ root, v }))
      .catch((e: unknown) => {
        // Swallowing this left PUSH enabled with no destination shown, which is exactly the
        // "refuse, don't guess" contract inverted. A preflight that failed is a refusal.
        if (!live || (e instanceof DOMException && e.name === "AbortError")) return;
        setPushRes({
          root,
          v: {
            ok: false,
            reason:
              e instanceof ApiError
                ? e.message
                : "Could not work out where this branch would push to.",
            branch: null,
            remote: null,
            target: null,
            expect: null,
            candidates: [],
            set_upstream: false,
          },
        });
      });
    return () => {
      live = false;
      ctl.abort();
    };
  }, [root, repo, branch, opTick, remoteChoice]);

  const branches = repo && branchesRes?.root === root ? branchesRes.v : null;

  // RECENT COMMITS (#950): collapsed by default, and read only while open — the 15s status poll
  // should not drag a log read along with it. Root-tagged, like every other side-load here.
  const [logOpenState, setLogOpenState] = useState<{ root: string; open: boolean }>({
    root,
    open: false,
  });
  const logOpen = logOpenState.root === root && logOpenState.open;
  const [logRes, setLogRes] = useState<{
    root: string;
    head: string | null;
    branch: string | null;
    v: GitLog | null;
    error: string | null;
  } | null>(null);
  const [logRetry, setLogRetry] = useState(0);
  const headSha = status?.head ?? null;
  useEffect(() => {
    if (!repo || !logOpen) return;
    let live = true;
    const ctl = new AbortController();
    const load = async () => {
      try {
        // An old in-flight read can finish after a write or a checkout switch. Never paint it as
        // this checkout's history. A second read crosses the server's mutation epoch; an external
        // writer can still be ahead of status, in which case offer a refresh rather than stale rows.
        for (let attempt = 0; attempt < 2; attempt++) {
          const v = await api.gitLog(root, { signal: ctl.signal });
          if (!live) return;
          if (v.head === headSha && v.branch === branch) {
            setLogRes({ root, head: headSha, branch, v, error: null });
            return;
          }
        }
        setLogRes({
          root,
          head: headSha,
          branch,
          v: null,
          error: "History no longer matches this branch and commit. Refresh to load it again.",
        });
      } catch (e: unknown) {
        if (!live || (e instanceof DOMException && e.name === "AbortError")) return;
        setLogRes({
          root,
          head: headSha,
          branch,
          v: null,
          error: e instanceof ApiError ? e.message : "Could not list the recent commits.",
        });
      }
    };
    void load();
    return () => {
      live = false;
      ctl.abort();
    };
  }, [root, repo, logOpen, headSha, branch, opTick, logRetry]);
  const log =
    repo && logRes?.root === root && logRes.head === headSha && logRes.branch === branch
      ? logRes
      : null;
  const push = repo && pushRes?.root === root ? pushRes.v : null;

  /** One place where a write is run, because every write shares the same four obligations:
   *  say what is in flight, clear the previous refusal, settle from the server's own status, and
   *  surface the server's `detail` verbatim when it refuses. */
  // The root a write was STARTED against. A slow fetch or push in repo A can finish after the
  // operator has moved the panel to repo B — and applying A's status to B is not a cosmetic
  // glitch: the rows would describe A while every control targets B's `root`, so confirming a
  // discard on a path both repos happen to share would destroy the wrong repository's work.
  // Found in review. A completion whose root no longer matches is DROPPED, not applied.
  // The root the panel is CURRENTLY showing, readable from inside an async write that started
  // earlier. This is the belt; `onStatus`'s own `forRoot !== root` check in FilePanel is the
  // braces, and it is the authoritative one because it closes over the root of the render that
  // is actually on screen.
  //
  // `useLayoutEffect`, not `useEffect`, and the difference is the bug. A PASSIVE effect runs
  // after the commit, so between B's render committing and its effects flushing this ref still
  // said A — and a promise settling in that gap passed the guard and installed A's status while
  // every control already targeted B. That window is small and entirely reachable: a resolved
  // promise is a microtask, and a passive effect is not.
  //
  // A layout effect runs synchronously as part of the commit, so no continuation can interleave
  // between the render taking effect and this ref agreeing with it. (Writing the ref during
  // render would also close the gap and is what a first attempt did — but refs must not be
  // touched during render, and the lint rule was right to say so.)
  const rootAtStart = useRef(root);
  useLayoutEffect(() => {
    rootAtStart.current = root;
  }, [root]);
  // Dropping a superseded completion is not the same as forgetting it happened. `busy` is
  // derived as `op.root === root`, so an op left sitting at root A is not gone — it is dormant,
  // and navigating A → B → A makes it current again with `busy` still set and every write
  // disabled for the rest of the session. Retiring the entry when its own root is the one that
  // started it turns the drop into a real end-of-life.
  const retire = useCallback((startedAt: string) => {
    setOp((prev) => (prev.root === startedAt ? { ...prev, busy: null } : prev));
  }, []);
  const run = useCallback(
    async (
      id: string,
      fn: () => Promise<{ status?: GitStatus | null }>,
      report?: (r: never) => string,
    ) => {
      const startedAt = root;
      rootAtStart.current = startedAt;
      setOp({ root: startedAt, busy: id, error: null, notice: null });
      try {
        const res = await fn();
        if (startedAt !== rootAtStart.current) {
          retire(startedAt); // superseded, or the root moved on — but do not leave it busy
          return;
        }
        // Only a status that EXISTS. The server returns `status: null` when the operation landed
        // but the post-write read failed; handing that up replaced the panel's real status with
        // an absence — a successful push blanking the repository it had just pushed. Keeping the
        // previous status is both truer and less alarming: the write happened, the panel simply
        // has nothing newer to show until the next poll.
        if (res.status) onStatus?.(res.status, startedAt);
        setOpTick((n) => n + 1);
        setOp({
          root: startedAt,
          busy: null,
          error: null,
          notice: report ? report(res as never) : null,
        });
      } catch (e: unknown) {
        if (startedAt !== rootAtStart.current) {
          retire(startedAt);
          return;
        }
        // The server's wording IS the feature — "the repository is busy, the agent is running
        // git" and "that branch is not fully merged" are different facts and must read as such.
        setOp({
          root: startedAt,
          busy: null,
          error: e instanceof ApiError ? e.message : "The git operation failed.",
          notice: null,
        });
      }
    },
    [onStatus, retire, root],
  );

  const ops = gitOps(status, push);
  const plan = commitPlan(status, mode, selected);
  const busyAny = busy !== null;
  // A refresh bumps the tick, and until the new status lands these rows describe a state the
  // server may already have moved past. They stay VISIBLE — blanking the panel on every poll
  // would be worse, and the poll is 15s so this window is normally milliseconds — but they must
  // not stay actionable: the agent in this session edits the same tree, so a discard clicked on
  // a pre-refresh row can throw away work written after it. Disabling is the first half; the
  // second is that every write now carries the FINGERPRINT of what it was shown, and the server
  // re-reads under its lock and refuses a row that moved. Disabling alone could never close it —
  // the agent does not take the panel's lock, so the gap is reachable with no concurrent panel
  // use at all.
  const writesLocked = busyAny || loading;
  const pending = pendingState(status);
  const unsettled = pending.kind === "pending" ? pending.paths : [];
  const worktreePending = worktreePendingState(status);
  const unsettledWorktree = worktreePending.kind === "pending" ? worktreePending.paths : [];
  const head = status?.head ?? null;

  /** The `fp` of each named row, as the panel last rendered it. */
  const fpsFor = useCallback(
    (paths: string[]) => {
      return fingerprintsFor(status?.entries ?? [], paths);
    },
    [status],
  );

  const doDiscard = useCallback(
    (paths: string[], expect: Record<string, string>) => {
      setConfirmState(null);
      void run(
        "discard",
        () => api.gitDiscard(root, paths, expect),
        (r: GitWriteResult) => {
          const n = r.discarded?.length ?? paths.length;
          // The oids are the whole reason this is safe to press, and the panel used to keep them
          // to itself — the confirmation said the work was recoverable and then never said HOW.
          return `Discarded ${n} change${n === 1 ? "" : "s"}.${recoverySentence(r)}`;
        },
      ).then(() => {
        // AFTER the write settles, not at unmount. The dialog's own cleanup does restore focus,
        // but the discard is async: the post-write status arrives a moment later, the rows the
        // trigger lived in are gone, and focus lands back on <body>. Restoring once everything
        // has settled is the only point that survives — and the branch trigger is the stable
        // anchor, since the row that opened the dialog usually no longer exists.
        document.querySelector<HTMLElement>("[data-branch-trigger]")?.focus();
      });
    },
    [root, run, setConfirmState],
  );

  const doRevert = useCallback(
    (paths: string[], expect: Record<string, string>, commit: string) => {
      setConfirmState(null);
      void run(
        "revert",
        () => api.gitDiscard(root, paths, expect, "head", commit),
        (r: GitWriteResult) => {
          if (r.worktree === "pending") {
            // A PARTIAL result (reviews 4829, 4833): restoring stopped part-way. A path in
            // `worktree_left` did not finish being restored — it may be missing or hold something
            // else — so say that plainly with how to recover, never "left as it was".
            const back = r.discarded ?? [];
            const left = r.worktree_left ?? [];
            return `Put back ${back.length ? back.join(", ") : "none"} of ${paths.join(", ")}. Restoring ${left.join(", ")} did not finish${r.worktree_reason ? ` (${r.worktree_reason})` : ""}: ${left.length === 1 ? "it" : "they"} may be missing or changed on disk — check before editing. The index was not changed.${recoverySentence(r)}`;
          }
          const base = `Reverted ${paths.join(", ")} to the last commit.${recoverySentence(r)}`;
          if (r.index_durable === false)
            return `${base} The index was updated, but writing it to disk could not be confirmed${r.index_reason ? ` (${r.index_reason})` : ""} — refresh to check.`;
          return r.index === "pending"
            ? `${base} The index was not updated for ${(r.index_left ?? []).join(", ")}${r.index_reason ? ` (${r.index_reason})` : ""} — they stay as staged.`
            : base;
        },
      ).then(() => document.querySelector<HTMLElement>("[data-branch-trigger]")?.focus());
    },
    [root, run, setConfirmState],
  );

  const clearDraft = useCallback((startedAt: string) => {
    // Cleared for the root the commit BELONGED to. Clearing unconditionally wiped the current
    // root's draft when a late commit from a previous one landed.
    setMsgState((prev) => (prev.root === startedAt ? { root: startedAt, text: "" } : prev));
  }, []);

  const doCommitStaged = useCallback(
    (text: string, stagedFp: string) => {
      setConfirmState(null);
      void run(
        "commit",
        async () => {
          const startedAt = root;
          const r = await api.gitCommit(startedAt, text, stagedFp);
          clearDraft(startedAt);
          return r;
        },
        (r: GitWriteResult) =>
          `Committed ${r.files ?? 0} file${r.files === 1 ? "" : "s"} as ${r.commit ?? "HEAD"}.`,
      );
    },
    [clearDraft, root, run, setConfirmState],
  );

  const doCommitPaths = useCallback(
    (text: string, paths: string[], expect: Record<string, string>, commit: string) => {
      void run(
        "commit",
        async () => {
          const startedAt = root;
          const r = await api.gitCommitPaths(startedAt, text, paths, expect, commit);
          clearDraft(startedAt);
          setSelState((prev) => (prev.root === startedAt ? { root: startedAt, paths: [] } : prev));
          return r;
        },
        (r: GitWriteResult) => {
          const made = `Committed ${r.files ?? paths.length} file${(r.files ?? paths.length) === 1 ? "" : "s"} as ${r.commit ?? "HEAD"}.`;
          // The commit EXISTS either way; what the operator needs next depends on this half.
          if (r.index_durable === false)
            return `${made} The index was updated, but writing it to disk could not be confirmed${r.index_reason ? ` (${r.index_reason})` : ""} — refresh to check.`;
          return r.index === "pending"
            ? `${made} The index could not be brought up to it for ${(r.index_left ?? []).join(", ")}${r.index_reason ? ` (${r.index_reason})` : ""} — see INDEX PENDING.`
            : made;
        },
      );
    },
    [clearDraft, root, run],
  );

  if (loading && !status) {
    return (
      <div className={styles.body} role="status" aria-label="Loading repository state">
        {[0, 1, 2].map((i) => (
          <div key={i} className={styles.skeleton} style={{ width: `${60 - i * 10}%` }} />
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
          This folder is not inside a git working tree. Change the root to a repo, or use the FILES
          tab.
        </div>
      </div>
    );
  }

  const total = status.entries.length;
  const repoName = (status.repo ?? "").split("/").filter(Boolean).pop() ?? status.repo;
  // The tree-level refusal (dirty, detached, conflicted) is the same for every commit, so it is
  // written ONCE above the list, as the mockup has it; per-commit reasons ride on each control.
  const logCommits = log?.v?.commits ?? [];
  const logHead = log?.v?.head ?? null;
  const treeRevertReason = logCommits.length
    ? revertReason(status, { ...logCommits[0], parents: 1 })
    : null;

  const commitLabel =
    mode === "staged"
      ? "COMMIT"
      : mode === "selected"
        ? `COMMIT ${plan.count} SELECTED`
        : `COMMIT ALL ${plan.count}`;

  /** REVERT on the branch and HEAD named by the confirmation. Incomplete restoration can mean
   *  concurrent edits or a partial failure; report recovery ids without claiming files stayed put. */
  const doRevertCommit = (commit: GitLogCommit, tip: string, confirmedBranch: string) => {
    setConfirmState(null);
    void run(
      "revert-commit",
      () => api.gitRevert(root, commit.sha, tip, `refs/heads/${confirmedBranch}`),
      (r: GitWriteResult) => `${revertCommitSummary(commit.short, r)}${recoverySentence(r)}`,
    ).then(() => document.querySelector<HTMLElement>("[data-branch-trigger]")?.focus());
  };

  /** The actions a row's ⋯ menu offers — exactly the inline set, in the same order. */
  const opsFor = (e: GitEntry): RowOp[] => {
    const a = rowActionsFor(e);
    const out: RowOp[] = [];
    if (a.stage) out.push("stage");
    if (a.unstage) out.push("unstage");
    if (a.revert && head) out.push("revert");
    if (a.discard) out.push("discard");
    if (onSendPath && status?.repo) out.push("send");
    return out;
  };
  /** A menu pick does what the inline glyph does. The confirmations take the ⋯ trigger as the
   *  control that opened them, so focus returns to the row rather than to a menu that is gone. */
  const pickRowOp = (e: GitEntry, el: HTMLElement, op: RowOp) => {
    setRowMenuState(null);
    if (op === "stage" || op === "unstage") {
      void run("stage", () => api.gitStage(root, [e.path], op === "stage", fpsFor([e.path])));
    } else if (op === "revert" && head) {
      setConfirmState({ kind: "revert", root, paths: [e.path], expect: fpsFor([e.path]), head, trigger: el });
    } else if (op === "discard") {
      setConfirmState({ kind: "discard", root, paths: [e.path], expect: fpsFor([e.path]), trigger: el });
    } else if (op === "send" && onSendPath && status?.repo) {
      onSendPath(`${status.repo.replace(/\/$/, "")}/${e.path}`);
    }
  };

  return (
    <>
      {/* Pinned head: the branch and the two facts the network controls change. */}
      <div className={styles.gitHead}>
        <div className={styles.branchStrip}>
          <button
            type="button"
            className={styles.branchTrigger}
            aria-haspopup="menu"
            aria-expanded={menuOpen !== null}
            data-branch-trigger=""
            title={status.branch ? `Branch: ${status.branch}` : "detached HEAD"}
            onClick={(ev) => {
              // Measured HERE, from the event: the menu positions itself purely from this, so it
              // never reads a ref during render nor writes geometry into state from an effect.
              const el = ev.currentTarget;
              const r = el.getBoundingClientRect();
              setMenuOpen((o) =>
                o
                  ? null
                  : { el, rect: { top: r.top, bottom: r.bottom, left: r.left, width: r.width } },
              );
            }}
          >
            <span className={styles.branchIcon} aria-hidden="true">
              <GitBranch size={13} />
            </span>
            <span className={styles.branchName}>{status.branch ?? "detached HEAD"}</span>
            <ChevronDown size={12} aria-hidden="true" />
          </button>
        </div>
        <div className={styles.branchMeta}>
          <span className="hud-tag">
            {/* Absent, not zero: "level with upstream" and "no upstream at all" are different
                facts, and rendering both as 0 would erase the difference. */}
            {status.ahead === null || status.behind === null
              ? status.upstream
                ? "NO DIVERGENCE DATA"
                : "NO UPSTREAM"
              : `AHEAD ${status.ahead} // BEHIND ${status.behind}`}
            {" // "}
            {total} CHANGED
          </span>
          <div className={styles.ctrlRow}>
            <button
              type="button"
              className={styles.ctrlBtn}
              data-git-op="fetch"
              disabled={writesLocked || !ops.canFetch}
              title={ops.fetchReason ?? "Fetch the remote"}
              onClick={() =>
                void run("fetch", () => api.gitFetch(root), (r: { remote?: string }) =>
                  `Fetched ${r.remote ?? "the remote"}.`,
                )
              }
            >
              <ArrowDownToLine size={12} aria-hidden="true" />
              {busy === "fetch" ? "FETCHING…" : "FETCH"}
            </button>
            <button
              type="button"
              className={`${styles.ctrlBtn} ${styles.ctrlPrimary}`}
              data-git-op="pull"
              disabled={writesLocked || !ops.canPull}
              title={ops.pullReason ?? "Fast-forward from the upstream"}
              onClick={() =>
                void run(
                  "pull",
                  () => api.gitPull(root),
                  (r: { upstream?: string; settled?: boolean; settle_error?: string | null }) =>
                    r.settled === false
                      ? `The branch was updated from ${r.upstream ?? "the upstream"}, but the working tree was not: ${r.settle_error ?? "the panel could not finish"}. The pull itself is done — sort the working tree out in the terminal.`
                      : `Fast-forwarded from ${r.upstream ?? "the upstream"}.`,
                )
              }
            >
              {busy === "pull" ? "PULLING…" : "PULL"}
            </button>
          </div>
        </div>
      </div>

      <div className={styles.body} data-git-tab="">
        {/* A refusal is the feature, so it renders as prose with the reason and the next step —
            never a toast that disappears before it has been read. */}
        {opError && (
          <div className={`${styles.state} ${styles.stateBad}`} role="alert" data-git-error="">
            <span className={styles.stateTag}>Git // Refused</span>
            {opError}
          </div>
        )}
        {notice && !opError && (
          <div className={styles.state} role="status" data-git-notice="">
            <span className={styles.stateTag}>Git // Done</span>
            {notice}
          </div>
        )}

        {/* Not INDEX PENDING: the server could not tell whether it is (review 4829). */}
        {pending.kind === "unknown" && head && (
          <div className={styles.state} role="status" data-git-pending-unknown="">
            <span className={styles.stateTag}>Git // Index state unknown</span>
            The panel could not check whether the last commit ({head.slice(0, 7)}) is fully settled in
            the index, so a STAGED commit asks first. The next refresh checks again.
          </div>
        )}

        {/* Not WORKING TREE PENDING: the server could not compare the files (too many, or too
            large, to hash on a status read). Say so rather than claim nothing is behind. Shown
            only when the index notice above is not already saying the same thing. */}
        {worktreePending.kind === "unknown" && pending.kind !== "unknown" && head && (
          <div className={styles.state} role="status" data-git-worktree-unknown="">
            <span className={styles.stateTag}>Git // Working tree state unknown</span>
            The panel could not check whether files the last commit ({head.slice(0, 7)}) changed
            still hold their earlier version on disk — there were too many, or they were too large,
            to compare. It cannot say whether any is behind; the session&apos;s terminal can.
          </div>
        )}

        {/* INDEX PENDING (#950). Derived from the repository, not remembered, so it survives a
            reload — and for the same reason it cannot tell an unfinished panel commit from a
            reversal staged on purpose. It says both, and SETTLE is offered, never applied. */}
        {(unsettled.length > 0 || unsettledWorktree.length > 0) && head && (
          <div
            className={`${styles.state} ${styles.stateWarn}`}
            role="status"
            data-git-pending=""
          >
            <span className={styles.stateTag}>
              Git // {unsettled.length > 0 ? "Index pending" : "Working tree pending"}
            </span>
            {unsettled.length > 0 && (
              <>
                The last commit ({head.slice(0, 7)}) changed {unsettled.join(", ")}, but the index
                still holds{" "}
                {unsettled.length === 1
                  ? "its previous version — so it reads as a staged change"
                  : "their previous versions — so they read as staged changes"}{" "}
                that would undo that commit. That is what an unfinished commit looks like, and also
                what a reversal staged on purpose looks like.{" "}
              </>
            )}
            {unsettledWorktree.length > 0 && (
              // The worktree half (#950 2b): only a revert that could not write a file produces
              // this, and the file on disk still being the pre-revert version reads as an unstaged
              // change that undoes it. SETTLE writes the committed version, bound to those bytes.
              <span data-git-pending-worktree="">
                In the working tree, {unsettledWorktree.join(", ")}{" "}
                {unsettledWorktree.length === 1 ? "still has its" : "still have their"} version from
                before the last commit ({head.slice(0, 7)}), which reads as an unstaged change that
                undoes it. SETTLE writes the committed version and keeps the old bytes as a git
                object.
              </span>
            )}
            <div className={styles.ctrlRow}>
              <button
                type="button"
                className={`${styles.ctrlBtn} ${styles.ctrlPrimary}`}
                data-git-op="settle"
                disabled={writesLocked}
                title="Bring the index up to the last commit for these paths"
                onClick={() =>
                  void run("settle", () => api.gitSettle(root, head), (r: GitWriteResult) => {
                    const index =
                      r.index_durable === false
                        ? `The index was updated, but writing it to disk could not be confirmed${r.index_reason ? ` (${r.index_reason})` : ""} — refresh to check.`
                        : r.index === "pending"
                          ? `Settled the rest; ${(r.index_left ?? []).join(", ")} changed after the commit and stay as staged.`
                          : "The index now matches the last commit.";
                    const wrote = r.worktree_paths?.length
                      ? ` Wrote the committed ${r.worktree_paths.join(", ")}.`
                      : "";
                    const left =
                      r.worktree === "pending"
                        ? ` ${(r.worktree_left ?? []).join(", ")} could not be written${r.worktree_reason ? ` (${r.worktree_reason})` : ""}.`
                        : "";
                    return `${index}${wrote}${left}${recoverySentence(r)}`;
                  })
                }
              >
                {busy === "settle" ? "SETTLING…" : "SETTLE"}
              </button>
            </div>
          </div>
        )}

        {ops.conflictCount > 0 && (
          <div className={`${styles.state} ${styles.stateWarn}`}>
            <span className={styles.stateTag}>Git // Conflicts</span>
            {ops.conflictCount} path{ops.conflictCount === 1 ? " has" : "s have"} unresolved
            conflicts. Staging, committing, pulling and switching are blocked until they are
            resolved in the session.
          </div>
        )}

        {total === 0 && (
          <div className={styles.state}>
            <span className={styles.stateTag}>Git // Clean</span>
            Nothing has changed in this working tree.
          </div>
        )}

        {GROUPS.map(({ kind, label }) => {
          const rows = status.entries.filter((e) => e.kind === kind);
          if (!rows.length) return null;
          const stageable = rows.filter((e) => rowActionsFor(e).stage).map((e) => e.path);
          const unstageable = rows.filter((e) => rowActionsFor(e).unstage).map((e) => e.path);
          return (
            <div key={kind}>
              <div className={styles.groupHead}>
                <span className="hud-tag">
                  {label} // {rows.length}
                </span>
                {/* "All" acts on the group the operator is looking at, so what it covers is
                    always visible above it rather than being a hidden selection. */}
                {stageable.length > 0 && (
                  <button
                    type="button"
                    className={styles.groupBtn}
                    data-git-op="stage-all"
                    disabled={writesLocked || ops.conflictCount > 0}
                    title={`Stage all ${rows.length} ${label.toLowerCase()}`}
                    onClick={() =>
                      void run("stage", () => api.gitStage(root, stageable, true, fpsFor(stageable)))
                    }
                  >
                    Stage all
                  </button>
                )}
                {unstageable.length > 0 && (
                  <button
                    type="button"
                    className={styles.groupBtn}
                    data-git-op="unstage-all"
                    disabled={writesLocked}
                    title={`Unstage all ${rows.length}`}
                    onClick={() =>
                      void run("stage", () => api.gitStage(root, unstageable, false, fpsFor(unstageable)))
                    }
                  >
                    Unstage all
                  </button>
                )}
              </div>
              {rows.map((e) => {
                const { ch, cls, label: what } = letterFor(e);
                const name = e.path.split("/").pop() || e.path;
                const dir = e.path.slice(0, e.path.length - name.length).replace(/\/$/, "");
                const acts = rowActionsFor(e);
                const menuOps = opsFor(e);
                return (
                  // Container + sibling controls, never a button inside a button (#792).
                  <div
                    key={`${e.kind}:${e.path}`}
                    data-git-row={e.path}
                    data-kind={e.kind}
                    className={styles.row}
                  >
                    {e.kind !== "unmerged" && (
                      // Per PATH, not per row: a path with a staged and an unstaged change shows
                      // one ticked state in both groups, because it commits as one file.
                      <label className={styles.rowCheck} title={`Select ${e.path} to commit`}>
                        <input
                          type="checkbox"
                          checked={selected.has(e.path)}
                          disabled={writesLocked}
                          onChange={() => toggleSelected(e.path)}
                          aria-label={`Select ${e.path}`}
                          data-git-select={e.path}
                        />
                        <span className={styles.checkBox} aria-hidden="true">
                          <Check size={10} strokeWidth={3} />
                        </span>
                      </label>
                    )}
                    <button
                      type="button"
                      className={styles.rowMain}
                      title={`${e.path} — ${what}${e.orig_path ? ` (was ${e.orig_path})` : ""}`}
                      onClick={(ev) => onOpen(e, ev.currentTarget)}
                    >
                      <span className={`${styles.gitLetter} ${cls}`} aria-label={what} title={what}>
                        {ch}
                      </span>
                      {/* Filename first, path second and dimmed: the reverse is a wall of
                        "web/src/components/…" that forces horizontal scroll at panel width. */}
                      <span className={styles.rowName}>{name}</span>
                      {dir && <span className={styles.rowNote}>{dir}</span>}
                    </button>
                    <span className={styles.rowInline}>
                    {acts.stage && (
                      <button
                        type="button"
                        className={styles.rowAct}
                        data-git-op="stage"
                        disabled={writesLocked}
                        title={`Stage ${e.path}`}
                        aria-label={`Stage ${e.path}`}
                        onClick={() => void run("stage", () => api.gitStage(root, [e.path], true, fpsFor([e.path])))}
                      >
                        <Plus size={13} aria-hidden="true" />
                      </button>
                    )}
                    {acts.unstage && (
                      <button
                        type="button"
                        className={styles.rowAct}
                        data-git-op="unstage"
                        disabled={writesLocked}
                        title={`Unstage ${e.path}`}
                        aria-label={`Unstage ${e.path}`}
                        onClick={() => void run("stage", () => api.gitStage(root, [e.path], false, fpsFor([e.path])))}
                      >
                        <Minus size={13} aria-hidden="true" />
                      </button>
                    )}
                    {acts.revert && head && (
                      <button
                        type="button"
                        className={`${styles.rowAct} ${styles.rowActBad}`}
                        data-git-op="revert"
                        disabled={writesLocked}
                        title={`Revert ${e.path} to the last commit`}
                        aria-label={`Revert ${e.path} to the last commit`}
                        onClick={(ev) =>
                          setConfirmState({
                            kind: "revert",
                            root,
                            paths: [e.path],
                            expect: fpsFor([e.path]),
                            head,
                            trigger: ev.currentTarget,
                          })
                        }
                      >
                        <Undo2 size={13} aria-hidden="true" />
                      </button>
                    )}
                    {acts.discard && (
                      <button
                        type="button"
                        className={`${styles.rowAct} ${styles.rowActBad}`}
                        data-git-op="discard"
                        disabled={writesLocked}
                        title={`Discard changes to ${e.path}`}
                        aria-label={`Discard changes to ${e.path}`}
                        onClick={(ev) =>
                          setConfirmState({
                            kind: "discard",
                            root,
                            paths: [e.path],
                            expect: fpsFor([e.path]),
                            trigger: ev.currentTarget,
                          })
                        }
                      >
                        <RotateCcw size={13} aria-hidden="true" />
                      </button>
                    )}
                    {/* Repo-relative in the payload; absolute here, because the draft names files
                      relative to the SESSION cwd, which is not necessarily the repo root. */}
                    {onSendPath && status.repo && (
                      <SendPath
                        path={`${status.repo.replace(/\/$/, "")}/${e.path}`}
                        name={name}
                        onSendPath={onSendPath}
                      />
                    )}
                    </span>
                    {menuOps.length > 0 && (
                      <button
                        type="button"
                        className={styles.rowMenuTrigger}
                        data-row-menu-trigger={e.path}
                        aria-haspopup="menu"
                        aria-expanded={
                          rowMenu?.entry.kind === e.kind && rowMenu.entry.path === e.path
                        }
                        aria-label={`Actions for ${e.path}`}
                        title={`Actions for ${e.path}`}
                        onClick={(ev) => {
                          const el = ev.currentTarget;
                          const r = el.getBoundingClientRect();
                          setRowMenuState((o) =>
                            o && o.root === root && o.entry.kind === e.kind && o.entry.path === e.path
                              ? null
                              : {
                                  root,
                                  entry: e,
                                  el,
                                  rect: { top: r.top, bottom: r.bottom, left: r.left, right: r.right },
                                },
                          );
                        }}
                      >
                        <MoreHorizontal size={14} aria-hidden="true" />
                      </button>
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

        {/* RECENT COMMITS (#950). A revert is a NEW commit on top — history is never rewritten —
            and it needs a clean tree, so on a dirty one every REVERT is disabled with that reason. */}
        <div className={styles.logSection} data-git-log="">
          <div className={styles.groupHead}>
            <button
              type="button"
              className={styles.logToggle}
              aria-expanded={logOpen}
              data-git-log-toggle=""
              onClick={() => setLogOpenState({ root, open: !logOpen })}
            >
              <span className="hud-tag">Recent commits</span>
              <ChevronDown
                size={12}
                aria-hidden="true"
                className={logOpen ? styles.logCaretOpen : undefined}
              />
            </button>
          </div>
          {logOpen && (
            <>
              {log?.error && (
                <div className={`${styles.state} ${styles.stateBad}`} role="alert">
                  <span className={styles.stateTag}>Git // Log unavailable</span>
                  {log.error}
                  <button
                    type="button"
                    className={styles.retry}
                    aria-label="Refresh recent commits"
                    onClick={() => {
                      onRetry();
                      setLogRetry((v) => v + 1);
                    }}
                  >
                    Refresh
                  </button>
                </div>
              )}
              {!log && (
                <div className={styles.skeleton} style={{ width: "50%" }} role="status" aria-label="Loading recent commits" />
              )}
              {log?.v && logCommits.length === 0 && (
                <div className={styles.logHint}>Nothing has been committed on this branch yet.</div>
              )}
              {treeRevertReason && (
                <div className={styles.logHint} data-log-hint="">
                  {treeRevertReason}
                </div>
              )}
              {logCommits.map((c) => {
                const reason = revertReason(status, c);
                return (
                  <div key={c.sha} className={styles.logRow} data-commit-row={c.sha}>
                    <span className={styles.logSha}>{c.short}</span>
                    <span className={styles.logSubject} title={c.subject}>
                      {c.subject}
                    </span>
                    <span
                      className={styles.logWhen}
                      title={c.time ? new Date(c.time * 1000).toLocaleString() : undefined}
                    >
                      {c.time ? shortAgo(c.time) : ""}
                      {c.pushed === false && (
                        <span className={styles.logUnpushed}> · not pushed</span>
                      )}
                    </span>
                    <button
                      type="button"
                      className={styles.groupBtn}
                      data-git-op="revert-commit"
                      data-commit={c.sha}
                      disabled={writesLocked || reason !== null || !logHead || !status.branch}
                      title={reason ?? `Create a new commit that undoes ${c.short}`}
                      aria-label={`Revert ${c.short}: ${c.subject}`}
                      onClick={(ev) => {
                        if (!logHead || !status.branch) return;
                        setConfirmState({
                          kind: "revert-commit",
                          root,
                          commit: c,
                          head: logHead,
                          branch: status.branch,
                          trigger: ev.currentTarget,
                        });
                      }}
                    >
                      Revert
                    </button>
                  </div>
                );
              })}
            </>
          )}
        </div>
      </div>

      {/* Pinned foot: committing is a deliberate trip to a fixed place, not a floating control. */}
      <div className={styles.gitFoot}>
        {/* What the commit is made OF (#950). A radio group, so the three read as one choice. */}
        <div
          className={`${styles.seg} ${styles.commitModes}`}
          role="radiogroup"
          aria-label="What to commit"
          onKeyDown={(ev) => {
            // A radio group is one Tab stop; the arrows (and Home/End) move the choice AND focus.
            const order = MODES.map((x) => x.mode);
            const at = order.indexOf(mode);
            const next =
              ev.key === "ArrowRight" || ev.key === "ArrowDown"
                ? order[(at + 1) % order.length]
                : ev.key === "ArrowLeft" || ev.key === "ArrowUp"
                  ? order[(at - 1 + order.length) % order.length]
                  : ev.key === "Home"
                    ? order[0]
                    : ev.key === "End"
                      ? order[order.length - 1]
                      : null;
            if (!next || writesLocked) return;
            ev.preventDefault();
            setModeState({ root, mode: next });
            ev.currentTarget.querySelector<HTMLElement>(`[data-commit-mode='${next}']`)?.focus();
          }}
        >
          {MODES.map(({ mode: m, label }) => {
            const p = commitPlan(status, m, selected);
            return (
              <button
                key={m}
                type="button"
                role="radio"
                aria-checked={mode === m}
                tabIndex={mode === m ? 0 : -1}
                className={`${styles.segBtn} ${mode === m ? styles.segBtnOn : ""}`}
                data-commit-mode={m}
                disabled={writesLocked}
                title={p.reason ?? `Commit ${label.toLowerCase()} (${p.count})`}
                onClick={() => setModeState({ root, mode: m })}
              >
                {label} {p.count}
              </button>
            );
          })}
        </div>
        <textarea
          className={styles.commitBox}
          data-git-message=""
          rows={2}
          value={message}
          placeholder={
            plan.reason ??
            (mode === "staged"
              ? `Commit ${plan.count} staged file${plan.count === 1 ? "" : "s"}…`
              : mode === "selected"
                ? `Commit ${plan.count} selected file${plan.count === 1 ? "" : "s"} as they are now…`
                : `Commit all ${plan.count} changed file${plan.count === 1 ? "" : "s"}…`)
          }
          aria-label="Commit message"
          disabled={writesLocked || plan.reason !== null}
          onChange={(e) => setMessage(e.target.value)}
        />
        <div className={styles.ctrlRow}>
          <button
            type="button"
            className={`${styles.ctrlBtn} ${styles.ctrlPrimary}`}
            data-git-op="commit"
            disabled={writesLocked || plan.reason !== null || !message.trim()}
            title={
              plan.reason ??
              (mode === "staged"
                ? "Commit the staged changes"
                : mode === "selected"
                  ? "Commit exactly the ticked files, leaving the rest of the index alone"
                  : "Commit every staged and changed file")
            }
            onClick={(ev) => {
              const text = message.trim();
              if (mode === "staged") {
                if (pending.kind === "unknown") {
                  setConfirmState({
                    kind: "unknown-pending-commit",
                    root,
                    message: text,
                    stagedFp: status.staged_fp,
                    trigger: ev.currentTarget,
                  });
                  return;
                }
                // A STAGED commit with unsettled paths would record the reversal of the last
                // commit. That may be intended — so the panel asks, listing them, and never blocks.
                if (unsettled.length > 0) {
                  setConfirmState({
                    kind: "unsettled-commit",
                    root,
                    paths: unsettled,
                    message: text,
                    stagedFp: status.staged_fp,
                    trigger: ev.currentTarget,
                  });
                  return;
                }
                doCommitStaged(text, status.staged_fp);
                return;
              }
              doCommitPaths(text, plan.paths, fpsFor(plan.paths), head ?? "");
            }}
          >
            {busy === "commit" ? "COMMITTING…" : commitLabel}
          </button>
          <button
            type="button"
            className={styles.ctrlBtn}
            data-git-op="push"
            disabled={writesLocked || !ops.canPush}
            // The resolved target, from the server's own preflight — never a hardcoded `origin`.
            title={ops.pushReason ?? `Push to ${push?.target ?? "the resolved remote"}`}
            onClick={() =>
              void run(
                "push",
                () => api.gitPush(root, remoteChoice, push?.expect ?? undefined),
                (r: {
                  target?: string;
                  pushed?: string;
                  settled?: boolean;
                  settle_error?: string | null;
                }) =>
                  // "It failed" and "it worked and the panel did not finish tidying up" are
                  // different facts, and the second one used to read as the first. A retry is
                  // pointless after the remote has the commit; saying so is the difference
                  // between a confusing panel and an operator pushing twice.
                  r.settled === false
                    ? `Pushed ${r.pushed?.slice(0, 8) ?? ""} to ${r.target ?? "the remote"} — the remote HAS it. Local bookkeeping did not finish: ${r.settle_error ?? "unknown"}. Pushing again is safe but unnecessary.`
                    : `Pushed to ${r.target ?? "the remote"}.`,
              )
            }
          >
            <Upload size={12} aria-hidden="true" />
            {busy === "push"
              ? "PUSHING…"
              : push?.ok && push.remote
                ? `PUSH → ${push.remote}`
                : "PUSH"}
          </button>
        </div>
        {push && !push.ok && push.reason && (
          <div className={styles.footNote} data-push-refusal="">
            {push.reason}
            {push.candidates.length > 1 && (
              // The issue's contract says the client "must resend with an explicit remote from
              // that list". Without this control that path was unreachable, so the refusal was
              // a dead end rather than a next step.
              <div className={styles.ctrlRow}>
                {push.candidates.map((c) => (
                  <button
                    key={c}
                    type="button"
                    className={styles.ctrlBtn}
                    data-push-candidate={c}
                    disabled={writesLocked}
                    title={`Resolve the push target against ${c}`}
                    onClick={() => setRemoteChoice(c)}
                  >
                    {c}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
        {push?.ok && push.set_upstream && (
          <div className={styles.footNote}>
            First push — this sets the upstream to {push.target}.
          </div>
        )}
      </div>

      {menuOpen && branches && (
        <BranchMenu
          current={branches.current}
          local={branches.local}
          remote={branches.remote}
          busy={writesLocked}
          anchor={menuOpen.el}
          rect={menuOpen.rect}
          onClose={() => setMenuOpen(null)}
          onSwitch={(b) => {
            setMenuOpen(null);
            void run("switch", () => api.gitSwitch(root, b, false, undefined, status.dirty_fp), (r: { branch?: string }) =>
              `Switched to ${r.branch ?? b}.`,
            );
          }}
          onCreate={(b, from) => {
            setMenuOpen(null);
            void run(
              "switch",
              () => api.gitSwitch(root, b, true, from, status?.dirty_fp),
              () => `Created and switched to ${b}.`,
            );
          }}
          onDelete={(b) => {
            setMenuOpen(null);
            void run("branch-delete", () => api.gitBranchDelete(root, b), () =>
              `Deleted branch ${b}.`,
            );
          }}
        />
      )}

      {rowMenu && (
        <RowMenu
          path={rowMenu.entry.path}
          ops={opsFor(rowMenu.entry)}
          busy={writesLocked}
          anchor={rowMenu.el}
          rect={rowMenu.rect}
          onClose={() => setRowMenuState(null)}
          onPick={(op) => pickRowOp(rowMenu.entry, rowMenu.el, op)}
        />
      )}

      {confirm?.kind === "discard" && (
        <ConfirmDialog
          label="Discard changes"
          // Not "cannot be undone" any more: the bytes being replaced are written to git's object
          // database first and their id comes back with the response, so a late edit is
          // recoverable. Saying otherwise would be scarier than the truth AND less useful — the
          // operator needs to know the id exists to be able to use it.
          tag="Discard // Recoverable by object id"
          goLabel="Discard"
          danger
          dataName="discard"
          returnFocusTo={confirm.trigger}
          onCancel={() => setConfirmState(null)}
          onConfirm={() => doDiscard(confirm.paths, confirm.expect)}
        >
          Throw away {confirm.paths.length} uncommitted change
          {confirm.paths.length === 1 ? "" : "s"} in <strong>{repoName ?? "this repository"}</strong>
          {status.branch ? ` on ${status.branch}` : ""}, docked into session <code>{sessionKey}</code>.
          {confirm.paths.length === 1 ? ` The file is ${confirm.paths[0]}.` : ""} git has no copy of
          this work — it is gone.
        </ConfirmDialog>
      )}
      {confirm?.kind === "revert" && (
        <ConfirmDialog
          label="Revert file"
          tag="Revert // To the last commit"
          goLabel="Revert file"
          danger
          dataName="revert"
          returnFocusTo={confirm.trigger}
          onCancel={() => setConfirmState(null)}
          onConfirm={() => doRevert(confirm.paths, confirm.expect, confirm.head)}
        >
          Put <strong>{confirm.paths.join(", ")}</strong> back to the last commit (
          <code>{confirm.head.slice(0, 7)}</code>) in <strong>{repoName ?? "this repository"}</strong>
          , docked into session <code>{sessionKey}</code> — both the staged and the unstaged change.
          Every replaced version is kept as a git object and listed after.
        </ConfirmDialog>
      )}
      {confirm?.kind === "revert-commit" && (
        <ConfirmDialog
          label="Revert commit"
          tag="Revert // New commit"
          goLabel="Revert"
          dataName="revert-commit"
          returnFocusTo={confirm.trigger}
          onCancel={() => setConfirmState(null)}
          onConfirm={() => doRevertCommit(confirm.commit, confirm.head, confirm.branch)}
        >
          Create a <strong>new commit</strong> on <code>{confirm.branch}</code> that undoes{" "}
          <code>{confirm.commit.short}</code> “{confirm.commit.subject}”. History is not rewritten
          and nothing is pushed.
        </ConfirmDialog>
      )}
      {confirm?.kind === "unsettled-commit" && (
        <ConfirmDialog
          label="Commit staged reversals"
          tag="Commit // Undoes part of the last commit"
          goLabel="Commit anyway"
          dataName="unsettled"
          returnFocusTo={confirm.trigger}
          onCancel={() => setConfirmState(null)}
          onConfirm={() => doCommitStaged(confirm.message, confirm.stagedFp)}
        >
          The staged {confirm.paths.length === 1 ? "version" : "versions"} of{" "}
          <strong>{confirm.paths.join(", ")}</strong>{" "}
          {confirm.paths.length === 1 ? "is the one" : "are the ones"} from before the last commit, so
          committing the index now records {confirm.paths.length === 1 ? "its" : "their"} reversal. If that is not what
          you meant, cancel and use SETTLE instead.
        </ConfirmDialog>
      )}
      {confirm?.kind === "unknown-pending-commit" && (
        <ConfirmDialog
          label="Commit with the index state unknown"
          tag="Commit // Index state not checked"
          goLabel="Commit anyway"
          dataName="unsettled-unknown"
          returnFocusTo={confirm.trigger}
          onCancel={() => setConfirmState(null)}
          onConfirm={() => doCommitStaged(confirm.message, confirm.stagedFp)}
        >
          The panel could not check whether the last commit
          {head ? (
            <>
              {" "}
              (<code>{head.slice(0, 7)}</code>)
            </>
          ) : null}{" "}
          is fully settled in the index, so it cannot tell whether committing the staged changes now
          would record a reversal of part of it. Cancel to let the next refresh check again, or
          commit anyway.
        </ConfirmDialog>
      )}
    </>
  );
}

/** The tab's confirmations (#806, generalised in #950): discard, revert file, and a staged commit
 *  that would undo part of the last commit.
 *
 *  Branch deletion does not confirm because it *cannot* be destructive — the server runs
 *  `git branch -d`, which refuses an unmerged branch. These can be, so each names what it acts on,
 *  and the destructive ones name the session the panel is docked into: a panel is attached to one
 *  session, and throwing away that session's work from a tab opened elsewhere is exactly the
 *  mistake worth spelling out.
 *
 *  `data-<dataName>-confirm` / `data-<dataName>-go` are the hooks the browser tests use.
 */
function ConfirmDialog({
  label,
  tag,
  goLabel,
  danger = false,
  dataName,
  returnFocusTo,
  onCancel,
  onConfirm,
  children,
}: {
  label: string;
  tag: string;
  goLabel: string;
  danger?: boolean;
  dataName: string;
  /** The control that opened this. Focus goes back to it on EVERY close path. */
  returnFocusTo: HTMLElement | null;
  onCancel: () => void;
  onConfirm: () => void;
  children: React.ReactNode;
}) {
  const ref = useRef<HTMLButtonElement>(null);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // `aria-modal` while Tab walks out to the page behind is a false claim — and here it is a
    // dangerous one: FilePanel disables its own trap while this is open, so a keyboard user could
    // reach the tree, change the root, and confirm against a different repository.
    ref.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        onCancel();
        return;
      }
      if (e.key !== "Tab") return;
      const root = box.current;
      if (!root) return;
      const focusable = Array.from(
        root.querySelectorAll<HTMLElement>('button:not([disabled]), [tabindex]:not([tabindex="-1"])'),
      );
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (e.shiftKey && (active === first || !root.contains(active))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (active === last || !root.contains(active))) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      // Restored on UNMOUNT, which is the only moment that works for both paths: doing it in the
      // click handler set focus and then React tore the portal down and blurred it again. On
      // confirm the row usually goes with the write, so the branch trigger — which always
      // exists — is the fallback that keeps a keyboard user inside the panel instead of on <body>.
      if (returnFocusTo && document.contains(returnFocusTo)) {
        returnFocusTo.focus();
        return;
      }
      document.querySelector<HTMLElement>("[data-branch-trigger]")?.focus();
    };
  }, [onCancel, returnFocusTo]);

  return createPortal(
    <>
      <button
        type="button"
        className={styles.confirmScrim}
        aria-label={`Cancel: ${label}`}
        onClick={onCancel}
      />
      <div
        ref={box}
        className={`${styles.confirm} ${danger ? styles.confirmDanger : ""}`}
        role="alertdialog"
        aria-modal="true"
        aria-label={label}
        // `data-git-confirm` is the ONE marker FilePanel stands its sheet trap down for, so a new
        // confirmation cannot be missed from a hand-kept list (#950 added two and was).
        data-git-confirm=""
        {...{ [`data-${dataName}-confirm`]: "" }}
      >
        <div className={styles.confirmHead}>
          <span className="hud-tag">{tag}</span>
        </div>
        <p className={styles.confirmBody}>{children}</p>
        <div className={styles.confirmRow}>
          <button ref={ref} type="button" className={styles.ctrlBtn} onClick={onCancel}>
            Cancel
          </button>
          <button
            type="button"
            className={`${styles.ctrlBtn} ${danger ? styles.ctrlBad : styles.ctrlPrimary}`}
            {...{ [`data-${dataName}-go`]: "" }}
            onClick={onConfirm}
          >
            {goLabel}
          </button>
        </div>
      </div>
    </>,
    document.body,
  );
}
