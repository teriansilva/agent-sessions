import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  File as FileIcon,
  Folder,
  Link2,
} from "lucide-react";
import { api, ApiError } from "../../lib/api";
import type { FileEntry, FileListing, GitEntry } from "../../types/api";
import { SendPath } from "./SendPath";
import styles from "./filePanel.module.css";

/** A directory's fetch state. `stale` keeps the LAST GOOD listing visible when a refresh fails —
 *  blanking a tree because one poll lost the network is worse than saying so. */
type DirState =
  | { kind: "loading" }
  | { kind: "ok"; listing: FileListing }
  | { kind: "stale"; listing: FileListing; message: string }
  | { kind: "error"; message: string };

type Row =
  | { kind: "entry"; entry: FileEntry; depth: number }
  | { kind: "status"; dir: string; depth: number; state: DirState };

/** Lazy file tree (#783). Only an EXPANDED directory is fetched, and a directory already in
 *  flight is never re-issued — polling must coalesce, not pile up behind an uncancellable worker.
 *
 *  Stale responses cannot repopulate a superseded tree because the parent KEYS this component on
 *  session + root: changing either remounts it, and a reply that lands afterwards is dropped by
 *  the `alive` guard. That is simpler than carrying a token through every fetch. */
export function FileTree({
  root,
  expanded,
  onToggleExpanded,
  onRootListing,
  onOpenFile,
  onSendPath,
  refreshTick,
  gitEntries,
}: {
  root: string;
  /** Status letters come from the SAME feed the GIT tab renders, so browsing and reviewing can
   *  never disagree about what changed. */
  gitEntries?: GitEntry[] | null;
  /** Controlled: the panel owns it so it can be persisted across a close/reopen. */
  expanded: Set<string>;
  onToggleExpanded: (path: string) => void;
  /** Reports the root's listing so the panel can use the SERVER's `parent` boundary. */
  onRootListing: (listing: FileListing) => void;
  onOpenFile: (path: string, trigger: HTMLElement | null) => void;
  /** Absolute path → the compose draft (#792). Absent ⇒ the action is not rendered at all. */
  onSendPath?: (path: string) => void;
  refreshTick: number;
}) {
  // Seeded with the root already loading, so `load` never has to set state synchronously.
  const [dirs, setDirs] = useState<Record<string, DirState>>(() => ({
    [root]: { kind: "loading" },
  }));
  const [selected, setSelected] = useState<string | null>(null);
  const inFlight = useRef<Set<string>>(new Set());
  const alive = useRef(true);
  const listRef = useRef<HTMLDivElement>(null);

  // A root or session change REMOUNTS this component (the parent keys it), so there is no reset
  // effect and no stale-token bookkeeping: the old instance and all of its state simply go away.
  // `alive` only guards a reply that lands after unmount.
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const load = useCallback((path: string) => {
    if (inFlight.current.has(path)) return; // coalesce: one request per directory at a time
    inFlight.current.add(path);
    api
      .filesList(path)
      .then((listing) => {
        if (!alive.current) return; // reply landed after unmount — drop it
        setDirs((prev) => ({ ...prev, [path]: { kind: "ok", listing } }));
      })
      .catch((e: unknown) => {
        if (!alive.current) return;
        const message =
          e instanceof ApiError ? e.message : "Could not read this folder.";
        setDirs((prev) => {
          const was = prev[path];
          const keep =
            was && (was.kind === "ok" || was.kind === "stale")
              ? was.listing
              : null;
          return {
            ...prev,
            // Keep the last good listing rather than blanking the tree on a failed refresh.
            [path]: keep
              ? { kind: "stale", listing: keep, message }
              : { kind: "error", message },
          };
        });
      })
      .finally(() => inFlight.current.delete(path));
  }, []);

  useEffect(() => {
    load(root);
  }, [root, load]);

  // Poll only what is actually on screen, and only while the tab is visible. `load` resolves
  // asynchronously, so nothing here sets state during the effect body.
  useEffect(() => {
    if (refreshTick === 0) return;
    load(root);
    expanded.forEach((p) => load(p));
  }, [refreshTick, root, expanded, load]);

  const toggle = useCallback(
    (path: string) => {
      if (!expanded.has(path)) {
        // Placeholder set from the click handler (allowed) rather than from inside `load`.
        setDirs((prev) =>
          prev[path] ? prev : { ...prev, [path]: { kind: "loading" } },
        );
        load(path);
      }
      onToggleExpanded(path);
    },
    [expanded, load, onToggleExpanded],
  );

  // Restore a persisted expansion: any expanded directory we have not fetched yet is fetched now,
  // which is what actually makes "switching away and back keeps your place" true.
  useEffect(() => {
    expanded.forEach((p) => load(p));
  }, [expanded, load]);

  // Flatten the expanded tree into rows, INCLUDING each directory's own status. Rendering status
  // only for the root meant an expanded child could be loading, failing, stale or truncated with
  // nothing on screen to say so — the opposite of the "every state rendered" contract.
  const rows = useMemo(() => {
    const out: Row[] = [];
    const walk = (dir: string, depth: number) => {
      const st = dirs[dir];
      if (!st) return;
      if (st.kind === "loading" || st.kind === "error") {
        out.push({ kind: "status", dir, depth, state: st });
        return;
      }
      if (st.kind === "stale")
        out.push({ kind: "status", dir, depth, state: st });
      for (const entry of st.listing.entries) {
        out.push({ kind: "entry", entry, depth });
        if (entry.kind === "dir" && expanded.has(entry.path))
          walk(entry.path, depth + 1);
      }
      if (!st.listing.complete || (st.listing.unencodable ?? 0) > 0) {
        out.push({
          kind: "status",
          dir,
          depth,
          state: { kind: "ok", listing: st.listing },
        });
      }
    };
    walk(root, 0);
    return out;
  }, [dirs, expanded, root]);

  // path -> letter, worktree state winning over index so the tree shows what is on disk.
  const gitByPath = useMemo(() => {
    const m = new Map<string, string>();
    for (const e of gitEntries ?? []) {
      const abs = `${root.replace(/\/$/, "")}/${e.path}`;
      const ch =
        e.kind === "untracked"
          ? "?"
          : e.kind === "unmerged"
            ? "U"
            : e.worktree !== "."
              ? e.worktree
              : e.index;
      if (ch && ch !== ".") m.set(abs, ch);
    }
    return m;
  }, [gitEntries, root]);

  const rootState = dirs[root];
  // Hand the root's listing up so the panel can use the SERVER's `parent` boundary for Up.
  const rootListing =
    rootState?.kind === "ok" || rootState?.kind === "stale"
      ? rootState.listing
      : null;
  useEffect(() => {
    if (rootListing) onRootListing(rootListing);
  }, [rootListing, onRootListing]);

  const onRowKey = useCallback(
    (e: React.KeyboardEvent<HTMLButtonElement>, entry: FileEntry) => {
      const isDir = entry.kind === "dir";
      if (e.key === "ArrowRight" && isDir && !expanded.has(entry.path)) {
        e.preventDefault();
        toggle(entry.path);
      } else if (e.key === "ArrowLeft" && isDir && expanded.has(entry.path)) {
        e.preventDefault();
        toggle(entry.path);
      } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const buttons = Array.from(
          // The ROW is a container now (#792) and a <div> cannot take focus — querying
          // `[data-file-row]` returned elements whose `.focus()` silently did nothing, which is
          // how arrow navigation broke. Target the row's own control instead.
          listRef.current?.querySelectorAll<HTMLButtonElement>(
            "[data-row-main]",
          ) ?? [],
        );
        const i = buttons.indexOf(e.currentTarget);
        const next = buttons[i + (e.key === "ArrowDown" ? 1 : -1)];
        next?.focus();
      }
    },
    [expanded, toggle],
  );

  return (
    <div className={styles.body} ref={listRef} data-file-tree="">
      {rootState &&
        rootState.kind !== "loading" &&
        rootState.kind !== "error" &&
        rows.length === 0 && (
          <div className={styles.state}>
            <span className={styles.stateTag}>Files // Empty folder</span>
            This folder has nothing in it. Not a loading state, not an error.
          </div>
        )}

      {rows.map((row) => {
        if (row.kind === "status") {
          const key = `status:${row.dir}:${row.state.kind}`;
          const pad = 10 + row.depth * 12;
          if (row.state.kind === "loading") {
            return (
              <div key={key} role="status" style={{ paddingLeft: pad }}>
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
          if (row.state.kind === "error") {
            return (
              <div
                key={key}
                className={`${styles.state} ${styles.stateBad}`}
                role="alert"
                style={{ paddingLeft: pad }}
              >
                <span className={styles.stateTag}>Files // Unavailable</span>
                {row.state.message}
                <div>
                  <button
                    type="button"
                    className={styles.retry}
                    onClick={() => load(row.dir)}
                  >
                    Retry
                  </button>
                </div>
              </div>
            );
          }
          if (row.state.kind === "stale") {
            return (
              <div
                key={key}
                className={`${styles.state} ${styles.stateWarn}`}
                role="status"
                style={{ paddingLeft: pad }}
              >
                <span className={styles.stateTag}>Files // Refresh failed</span>
                {row.state.message} Showing the last good listing.
              </div>
            );
          }
          const listing = row.state.listing;
          return (
            <div
              key={`${key}:notes`}
              className={`${styles.state} ${styles.stateWarn}`}
              style={{ paddingLeft: pad }}
            >
              {!listing.complete && (
                <>
                  <span className={styles.stateTag}>Files // Truncated</span>
                  This folder has more entries than the panel scans. Showing the
                  first {listing.entries.length}.
                </>
              )}
              {(listing.unencodable ?? 0) > 0 && (
                <>
                  <span className={styles.stateTag}>
                    Files // Undisplayable names
                  </span>
                  {listing.unencodable} entr
                  {listing.unencodable === 1 ? "y is" : "ies are"} hidden: the
                  filename is not valid UTF-8, so it cannot be shown safely.
                </>
              )}
            </div>
          );
        }
        const { entry, depth } = row;
        const isDir = entry.kind === "dir";
        const isLink = entry.kind === "link";
        const open = expanded.has(entry.path);
        return (
          // The row is a CONTAINER, not a control (#792). It used to be the button itself, but a
          // second action cannot be nested inside a button — invalid HTML, and touch gets two
          // overlapping targets with undefined precedence. Two siblings instead.
          <div
            key={entry.path}
            data-file-row=""
            data-kind={entry.kind}
            className={`${styles.row} ${isDir ? styles.rowDir : ""} ${
              selected === entry.path ? styles.rowSelected : ""
            }`}
          >
            <button
              type="button"
              data-row-main=""
              className={styles.rowMain}
              style={{ paddingLeft: 8 + depth * 12 }}
              aria-expanded={isDir ? open : undefined}
              // A link is display-only in phase 1 — it announces itself rather than pretending
              // to be openable and then failing.
              aria-disabled={isLink || undefined}
              title={
                isLink
                  ? `Symlink → ${
                      entry.link_unencodable_target
                        ? "(target name is not valid UTF-8)"
                        : (entry.link_target ?? "?")
                    } (not followed)`
                  : entry.path
              }
              onKeyDown={(e) => onRowKey(e, entry)}
              onClick={(e) => {
                if (isDir) {
                  toggle(entry.path);
                } else if (entry.kind === "file") {
                  setSelected(entry.path);
                  onOpenFile(entry.path, e.currentTarget);
                }
              }}
            >
              <span className={styles.twisty} aria-hidden="true">
                {isDir ? (
                  open ? (
                    <ChevronDown size={12} />
                  ) : (
                    <ChevronRight size={12} />
                  )
                ) : null}
              </span>
              <span
                className={`${styles.rowIcon} ${isDir ? styles.rowIconDir : ""}`}
                aria-hidden="true"
              >
                {isDir ? (
                  <Folder size={13} />
                ) : isLink ? (
                  <Link2 size={13} />
                ) : (
                  <FileIcon size={13} />
                )}
              </span>
              <span className={styles.rowName}>{entry.name}</span>
              {gitByPath.has(entry.path) && (
                <span
                  className={styles.gitLetter}
                  title={`git: ${gitByPath.get(entry.path)}`}
                  aria-label={`git status ${gitByPath.get(entry.path)}`}
                >
                  {gitByPath.get(entry.path)}
                </span>
              )}
              {isLink && (
                <span className={styles.rowNote}>
                  →{" "}
                  {entry.link_unencodable_target
                    ? "(undisplayable)"
                    : (entry.link_target ?? "?")}
                </span>
              )}
            </button>
            {/* A directory has no path worth naming to an agent, and a symlink is display-only in
              phase 1 — so the action exists only where it means something. */}
            {onSendPath && entry.kind === "file" && (
              <SendPath
                path={entry.path}
                name={entry.name}
                onSendPath={onSendPath}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}
