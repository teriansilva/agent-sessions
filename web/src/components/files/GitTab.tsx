import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  ArrowDownToLine,
  ChevronDown,
  GitBranch,
  Minus,
  Plus,
  RotateCcw,
  Upload,
} from "lucide-react";
import { ApiError, api } from "../../lib/api";
import type {
  GitBranches,
  GitEntry,
  GitPushTarget,
  GitStatus,
} from "../../types/api";
import { BranchMenu } from "./BranchMenu";
import { gitOps, rowActionsFor } from "./gitOps";
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
  // Root-tagged, all three. A discard confirmation that survives a root change is a data-loss
  // path: it stores paths, `doDiscard` uses the CURRENT root, and a same-named path in the new
  // repository gets destroyed instead. The commit draft and the remote choice belong to their
  // repository for the same reason, if less dangerously.
  const [msgState, setMsgState] = useState<{ root: string; text: string }>({ root, text: "" });
  const message = msgState.root === root ? msgState.text : "";
  const setMessage = useCallback(
    (text: string) => setMsgState({ root, text }),
    [root],
  );
  const [confirmState, setConfirmState] = useState<{
    root: string;
    paths: string[];
    /** The fingerprints as they were WHEN THE DIALOG OPENED — an immutable snapshot, not a
     *  lookup done at Confirm time. Recomputing from the latest `status` meant a poll landing
     *  while the dialog was open would bless the NEW bytes: the operator reads one description
     *  and confirms another. The dialog is a promise about what it showed. */
    expect: Record<string, string>;
    trigger: HTMLElement | null;
  } | null>(null);
  const confirm = confirmState && confirmState.root === root ? confirmState : null;
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
      fn: () => Promise<{ status: GitStatus | null }>,
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

  /** The `fp` of each named row, as the panel last rendered it. */
  const fpsFor = useCallback(
    (paths: string[]) => {
      const want = new Set(paths);
      const out: Record<string, string> = {};
      for (const e of status?.entries ?? []) if (want.has(e.path) && e.fp) out[e.path] = e.fp;
      return out;
    },
    [status],
  );

  const doDiscard = useCallback(
    (paths: string[], expect: Record<string, string>) => {
      setConfirmState(null);
      void run(
        "discard",
        () => api.gitDiscard(root, paths, expect),
        (r: { discarded?: string[]; recoverable?: Record<string, string[]> }) => {
          const n = r.discarded?.length ?? paths.length;
          // The oids are the whole reason this is safe to press, and the panel used to keep them
          // to itself — the confirmation said the work was recoverable and then never said HOW.
          // One id per line is enough to paste into `git cat-file -p`.
          const ids = Object.values(r.recoverable ?? {}).flat();
          const how = ids.length
            ? ` Previous contents: ${ids.map((o) => o.slice(0, 12)).join(", ")} — recover with \`git cat-file -p <id>\`.`
            : "";
          return `Discarded ${n} change${n === 1 ? "" : "s"}.${how}`;
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
    [root, run],
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
                return (
                  // Container + sibling controls, never a button inside a button (#792).
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
                      <span className={`${styles.gitLetter} ${cls}`} aria-label={what} title={what}>
                        {ch}
                      </span>
                      {/* Filename first, path second and dimmed: the reverse is a wall of
                        "web/src/components/…" that forces horizontal scroll at panel width. */}
                      <span className={styles.rowName}>{name}</span>
                      {dir && <span className={styles.rowNote}>{dir}</span>}
                    </button>
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

      {/* Pinned foot: committing is a deliberate trip to a fixed place, not a floating control. */}
      <div className={styles.gitFoot}>
        <textarea
          className={styles.commitBox}
          data-git-message=""
          rows={2}
          value={message}
          placeholder={
            ops.canCommit
              ? `Commit ${ops.stagedCount} staged file${ops.stagedCount === 1 ? "" : "s"}…`
              : (ops.commitReason ?? "Nothing is staged.")
          }
          aria-label="Commit message"
          disabled={writesLocked || !ops.canCommit}
          onChange={(e) => setMessage(e.target.value)}
        />
        <div className={styles.ctrlRow}>
          <button
            type="button"
            className={`${styles.ctrlBtn} ${styles.ctrlPrimary}`}
            data-git-op="commit"
            disabled={writesLocked || !ops.canCommit || !message.trim()}
            title={ops.commitReason ?? "Commit the staged changes"}
            onClick={() =>
              void run(
                "commit",
                async () => {
                  const startedAt = root;
                  const r = await api.gitCommit(startedAt, message.trim(), status.staged_fp);
                  // Cleared for the root the commit BELONGED to. Clearing unconditionally wiped
                  // the current root's draft when a late commit from a previous one landed.
                  setMsgState((prev) => (prev.root === startedAt ? { root: startedAt, text: "" } : prev));
                  return r;
                },
                (r: { commit?: string; files?: number }) =>
                  `Committed ${r.files ?? 0} file${r.files === 1 ? "" : "s"} as ${r.commit ?? "HEAD"}.`,
              )
            }
          >
            {busy === "commit" ? "COMMITTING…" : "COMMIT"}
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

      {confirm && (
        <DiscardConfirm
          paths={confirm.paths}
          repo={repoName ?? "this repository"}
          branch={status.branch}
          sessionKey={sessionKey}
          returnFocusTo={confirm.trigger}
          onCancel={() => setConfirmState(null)}
          onConfirm={() => doDiscard(confirm.paths, confirm.expect)}
        />
      )}
    </>
  );
}

/** The one confirmation this feature has (#806).
 *
 *  Branch deletion does not confirm because it *cannot* be destructive — the server runs
 *  `git branch -d`, which refuses an unmerged branch. Discard can be, so it names the repository,
 *  the branch, the file count, and the session the panel is docked into: a panel is attached to
 *  one session, and throwing away that session's uncommitted work from a tab you opened somewhere
 *  else is exactly the mistake worth spelling out.
 */
function DiscardConfirm({
  paths,
  repo,
  branch,
  sessionKey,
  returnFocusTo,
  onCancel,
  onConfirm,
}: {
  paths: string[];
  repo: string;
  branch: string | null;
  sessionKey: string;
  /** The control that opened this. Focus goes back to it on EVERY close path. */
  returnFocusTo: HTMLElement | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const ref = useRef<HTMLButtonElement>(null);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // `aria-modal` while Tab walks out to the page behind is a false claim — and here it is a
    // dangerous one: FilePanel disables its own trap while this is open, so a keyboard user could
    // reach the tree, change the root, and confirm a discard against a different repository.
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
      // confirm the row usually goes with the discard, so the branch trigger — which always
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
        aria-label="Cancel discarding"
        onClick={onCancel}
      />
      <div
        ref={box}
        className={`${styles.confirm} ${styles.confirmDanger}`}
        role="alertdialog"
        aria-modal="true"
        aria-label="Discard changes"
        data-discard-confirm=""
      >
        <div className={styles.confirmHead}>
          {/* Not "cannot be undone" any more: the bytes being replaced are written to git's
              object database first and their id comes back with the response, so a late edit is
              recoverable. Saying otherwise would be scarier than the truth AND less useful —
              the operator needs to know the id exists to be able to use it. */}
          <span className="hud-tag">Discard // Recoverable by object id</span>
        </div>
        <p className={styles.confirmBody}>
          Throw away {paths.length} uncommitted change{paths.length === 1 ? "" : "s"} in{" "}
          <strong>{repo}</strong>
          {branch ? ` on ${branch}` : ""}, docked into session <code>{sessionKey}</code>.
          {paths.length === 1 ? ` The file is ${paths[0]}.` : ""} git has no copy of this work — it
          is gone.
        </p>
        <div className={styles.confirmRow}>
          <button ref={ref} type="button" className={styles.ctrlBtn} onClick={onCancel}>
            Cancel
          </button>
          <button
            type="button"
            className={`${styles.ctrlBtn} ${styles.ctrlBad}`}
            data-discard-go=""
            onClick={onConfirm}
          >
            Discard
          </button>
        </div>
      </div>
    </>,
    document.body,
  );
}
