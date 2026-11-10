"""Found in git (#1740 P1b): what a mission's sessions changed in their repositories, collected
exactly — never guessed — and put on the same shelf as delivered items.

**Baseline** (`take_baselines`), per (mission, session, repository), recorded when the session
joins the mission: the HEAD commit and branch, and the blob hash of every dirty or untracked,
non-ignored file (`git status --porcelain=v2 -z`, then `hash-object --no-filters`). A session that
joined before this existed (or whose join path did not record one) gets a LATE baseline the first
time the shelf is read; it is marked so, because it can only see changes from then on.

**Collection** (`collect_due`), once per baseline, when the session has left the mission's roster
or the mission is in review or closed: the same snapshot again, compared with the baseline.

* Commits: reachable from the end HEAD and not from the baseline HEAD (`rev-list base..end`), so a
  branch switch is handled by reachability, not by name.
* Files: added, changed or deleted between the two states, by content hash. A file already dirty
  at baseline counts only if its content changed; an ignored file never counts (status omits it).
  A deleted file is listed with no viewer.
* The pull request for the end branch, when the configured forge (`forge.py`, read-only) knows one.

Every path is judged against the mission's boundary (`mission_shelf.path_allowed`): a change under
an excluded folder, or outside the mission's folders, is not reported at all.

What it produces: ONE code-change item per (session, repository) collection, with its commits, its
files (+/- lines) and the per-file patches (in `detail`, served by the diff route), and one FILE
item per changed image / video / audio / document file. Items are deduplicated per mission — a
code change by (repository, base, end, files), a file by (repository, path, git blob id) — and a
second session whose window covers the same change is added to the item's `sessions`, not given a
copy.

Git runs only through `gitpanel`'s hardened runner: the repository is discovered and contained
server-side first, and git reads a sanitized private gitdir that carries none of the repository's
own config, so no filter, hook or textconv it defines ever runs. Read-only; nothing is written to
the repository.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import secrets
import stat
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import gitpanel, mission_shelf, missions
from .fsbrowse import home_root

log = logging.getLogger(__name__)

#: Files reported per session (the issue's cap); past it the code item says how many more.
FOUND_FILES_MAX = 500
#: Commits listed per code item.
COMMITS_MAX = 50
#: Dirty paths a baseline records; past it the baseline is partial and says so.
BASELINE_PATHS_MAX = 5000
#: The largest file hashed for a baseline or a comparison.
HASH_MAX = 64 * 1024 * 1024
#: One file's patch, and all of one code item's patches, as stored.
PATCH_MAX = 64 * 1024
PATCHES_MAX = 1024 * 1024
#: Document types a FOUND file is shelved as (source code is in the code item, not a tile each).
FOUND_DOC_EXTS = frozenset({".md", ".markdown", ".txt", ".rst", ".adoc", ".pdf", ".csv", ".tsv"})
#: Mission states in which every session's changes are final.
CLOSING_STATES = frozenset({"review", "done", "failed", "abandoned"})


# ---------------------------------------------------------------- git, read-only


def _git(repo: gitpanel.Repo, gitdir: str, args: list[str]) -> bytes:
    # LITERAL pathspecs: every path handed to git here is a file name the repository holds, and a
    # file named `*` (or `private/[a]*`) must never widen a per-file diff to the files the
    # boundary excluded.
    return gitpanel._run_git(
        home_root(), ["--literal-pathspecs", *args], cwd=repo.toplevel, gitdir=gitdir
    )


def _repos(b: mission_shelf.Boundary) -> list[gitpanel.Repo]:
    """The repositories of a mission's folders, each discovered and contained by `gitpanel`. A
    folder that is not in a repository contributes nothing (no mtime or filename guessing)."""
    out: list[gitpanel.Repo] = []
    seen: set[str] = set()
    for folder in b.folders:
        try:
            repo = gitpanel.discover_repo(folder)
        except Exception:  # noqa: BLE001, S112 — a refused or unreadable repo contributes nothing
            continue
        if repo is not None and repo.toplevel not in seen:
            seen.add(repo.toplevel)
            out.append(repo)
    return out


def _hash_paths(repo: gitpanel.Repo, gitdir: str, paths: list[str]) -> dict[str, str]:
    """``{path: blob id}`` for the regular files among ``paths`` (``"deleted"`` for a gone one,
    ``"special"`` for a link or other non-file, ``"big"`` past `HASH_MAX`)."""
    out: dict[str, str] = {}
    regular: list[str] = []
    for p in paths:
        try:
            st = os.lstat(os.path.join(repo.toplevel, p))
        except FileNotFoundError:
            out[p] = "deleted"
            continue
        except OSError:
            out[p] = "special"
            continue
        if not stat.S_ISREG(st.st_mode):
            out[p] = "special"
        elif st.st_size > HASH_MAX:
            out[p] = f"big:{st.st_size}:{st.st_mtime_ns}"
        else:
            regular.append(p)
    for i in range(0, len(regular), 200):
        chunk = regular[i : i + 200]
        oids = _git(repo, gitdir, ["hash-object", "--no-filters", "--", *chunk]).decode().split()
        if len(oids) != len(chunk):
            raise gitpanel.GitError("could not hash the changed files", status=400)
        out.update(zip(chunk, oids, strict=True))
    return out


def snapshot(repo: gitpanel.Repo) -> dict:
    """``{head, branch, dirty: {path: blob id | marker}, partial}`` for one repository now."""
    with gitpanel.sanitized_gitdir(repo) as (gitdir, branch):
        blob = _git(
            repo, gitdir, ["status", "--porcelain=v2", "--branch", "--untracked-files=all", "-z"]
        )
        parsed = gitpanel._parse_porcelain_v2(blob, repo.toplevel)
        paths: list[str] = []
        added: list[str] = []
        for e in parsed["entries"]:
            if e.get("undecodable") or e.get("kind") == "ignored":
                continue
            paths.append(e["path"])
            # New since HEAD: untracked, or staged as added / a rename's or copy's target.
            if e.get("kind") == "untracked" or (
                e.get("kind") in ("staged", "renamed") and e.get("index") in ("A", "R", "C")
            ):
                added.append(e["path"])
            if e.get("orig_path"):
                paths.append(e["orig_path"])
        partial = bool(parsed.get("truncated")) or len(paths) > BASELINE_PATHS_MAX
        dirty = _hash_paths(repo, gitdir, sorted(set(paths))[:BASELINE_PATHS_MAX])
        return {
            "head": parsed.get("head") if parsed.get("head") not in (None, "(initial)") else None,
            "branch": parsed.get("branch") or branch,
            "dirty": dirty,
            "untracked": sorted(set(added)),
            "partial": partial,
        }


# ---------------------------------------------------------------- baselines


def _aliases(con, key: str) -> list[str]:
    """Every key one session is known by: its own, and the placeholder or real id a late-bound
    runtime is mapped to (`session_runtime_bindings`, #989). A dispatch records its baseline under
    the launch key; the roster adopts the session it became."""
    out = {key}
    for r in con.execute(
        "SELECT logical_key, physical_key FROM session_runtime_bindings"
        " WHERE logical_key=? OR physical_key=?",
        (key, key),
    ).fetchall():
        out.update((str(r[0]), str(r[1])))
    return sorted(out)


def _has_baseline(con, mission_id: str, session_key: str) -> bool:
    """An OPEN baseline (not yet collected) for this session under any of its keys."""
    return (
        con.execute(
            "SELECT 1 FROM mission_shelf_baselines WHERE mission_id=? AND collected_at IS NULL"
            " AND session_key IN (SELECT value FROM json_each(?)) LIMIT 1",
            (mission_id, json.dumps(_aliases(con, session_key))),
        ).fetchone()
        is not None
    )


def _roster(con, mission_id: str, session_key: str) -> str:
    """``member`` (on the roster now), ``left`` (was, and is not) or ``never`` (no row yet — a
    launch whose session has not been adopted), under any of the session's keys."""
    rows = con.execute(
        "SELECT removed_at FROM mission_sessions WHERE mission_id=?"
        " AND session_key IN (SELECT value FROM json_each(?))",
        (mission_id, json.dumps(_aliases(con, session_key))),
    ).fetchall()
    if any(r["removed_at"] is None for r in rows):
        return "member"
    return "left" if rows else "never"


def prepare_baselines(mission_id: str, *, path: Path | None = None) -> list[tuple[str, dict]]:
    """The snapshots a baseline records, taken NOW (before a launch, off the event loop); recorded
    under a session key later by `record_baselines`. Never raises."""
    try:
        m = missions.get_mission(mission_id, events_limit=1, path=path)
        if m is None:
            return []
        snaps = []
        for repo in _repos(mission_shelf.boundary(m)):
            try:
                snaps.append((repo.toplevel, snapshot(repo)))
            except Exception:  # noqa: BLE001
                log.warning("shelf: no baseline for a repository of %s", mission_id, exc_info=True)
        if not snaps:
            # No repository: a marker row, so the session is known to have none (no late retry).
            snaps = [("", {"head": None, "branch": None, "dirty": {}, "partial": False})]
        return snaps
    except Exception:  # noqa: BLE001
        log.warning("shelf: preparing baselines for %s failed", mission_id, exc_info=True)
        return []


def record_baselines(
    mission_id: str,
    session_key: str,
    snaps: list[tuple[str, dict]],
    *,
    late: bool = False,
    path: Path | None = None,
) -> int:
    """Store prepared snapshots as this session's baselines, once (an existing one wins)."""
    if not snaps:
        return 0
    try:
        now = time.time()
        with missions._write_lock:
            con = missions._ready(path)
            try:
                con.execute("BEGIN IMMEDIATE")
                if _has_baseline(con, mission_id, session_key):
                    con.execute("ROLLBACK")
                    return 0
                # A session back on the roster (re-adopted, or the mission reopened): its earlier
                # window is collected and on the shelf, so the bookkeeping row makes way for the
                # new window's baseline. Shelf items are never touched.
                con.execute(
                    "DELETE FROM mission_shelf_baselines WHERE mission_id=? AND session_key=?"
                    " AND collected_at IS NOT NULL",
                    (mission_id, session_key),
                )
                for repo_path, snap in snaps:
                    con.execute(
                        "INSERT INTO mission_shelf_baselines (mission_id, session_key, repo, head,"
                        " branch, dirty, taken_at, late) VALUES (?,?,?,?,?,?,?,?)",
                        (
                            mission_id,
                            session_key,
                            repo_path,
                            snap["head"],
                            snap["branch"],
                            json.dumps({"files": snap["dirty"], "partial": snap["partial"]}),
                            now,
                            1 if late else 0,
                        ),
                    )
                con.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(Exception):
                    con.execute("ROLLBACK")
                raise
            finally:
                con.close()
        return len(snaps)
    except Exception:  # noqa: BLE001 — a launch or an adopt never fails for the shelf
        log.warning("shelf: recording baselines for %s failed", mission_id, exc_info=True)
        return 0


def take_baselines(
    mission_id: str, session_key: str, *, late: bool = False, path: Path | None = None
) -> int:
    """Snapshot and record, once, for a session joining the mission now. Never raises."""
    try:
        con = missions._ready(path)
        try:
            if _has_baseline(con, mission_id, session_key):
                return 0
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return 0
    return record_baselines(
        mission_id, session_key, prepare_baselines(mission_id, path=path), late=late, path=path
    )


def ensure_baselines(mission_id: str, *, path: Path | None = None) -> None:
    """A LATE baseline for every session on the roster that has no open one, under any of its
    keys — never while the mission is in review or closed (its windows are over)."""
    m = missions.get_mission(mission_id, events_limit=1, path=path)
    if m is None or m.get("state") in CLOSING_STATES:
        return
    con = missions._ready(path)
    try:
        keys = [
            str(r["session_key"])
            for r in con.execute(
                "SELECT session_key FROM mission_sessions"
                " WHERE mission_id=? AND removed_at IS NULL",
                (mission_id,),
            ).fetchall()
        ]
        keys = [k for k in keys if not _has_baseline(con, mission_id, k)]
    finally:
        con.close()
    for key in keys:
        take_baselines(mission_id, key, late=True, path=path)


# ---------------------------------------------------------------- collection

#: Comparisons a baseline gets before it is given up on (and marked so).
COLLECT_ATTEMPTS = 3
#: How long a claimed collection is another reader's before it counts as abandoned.
CLAIM_STALE_S = 300.0


def _open_rows(con, mission_id: str) -> list[dict]:
    return [
        dict(r)
        for r in con.execute(
            "SELECT * FROM mission_shelf_baselines WHERE mission_id=? AND collected_at IS NULL",
            (mission_id,),
        ).fetchall()
    ]


def _due(con, mission: dict) -> tuple[list[dict], list[dict]]:
    """``(due, never_joined)``. A baseline is due when its session has LEFT the roster (under any
    of its keys), or — every one that joined — once the mission is in review or closed. A launch
    whose session is not adopted yet is not due: its window is still open. One that never joined
    by the time the mission closes produced nothing the mission owns."""
    due: list[dict] = []
    never: list[dict] = []
    closing = mission.get("state") in CLOSING_STATES
    for r in _open_rows(con, mission["id"]):
        state = _roster(con, mission["id"], r["session_key"])
        if state == "left" or (closing and state == "member"):
            due.append(r)
        elif closing and state == "never":
            never.append(r)
    return due, never


def pending_sessions(mission_id: str, *, path: Path | None = None) -> list[str]:
    """Sessions whose found items are still to come: an open baseline in a repository, for a
    session that is (or was) on the roster. Named by the roster's key."""
    con = missions._ready(path)
    try:
        out = set()
        for r in _open_rows(con, mission_id):
            if r["repo"] and _roster(con, mission_id, r["session_key"]) != "never":
                out.add(_display_key(con, mission_id, r["session_key"]))
        return sorted(out)
    finally:
        con.close()


#: One collection per mission at a time in this process; the row claim covers the rest.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _mission_lock(mission_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(mission_id, threading.Lock())


def refresh(mission_id: str, *, wait: bool = True, path: Path | None = None) -> None:
    """The found-in-git work a first shelf read does: late baselines, then due collections.
    Bounded and never raises; runs off the loop, outside the store's admission pool. With
    ``wait=False`` (a shelf read) it does nothing while another refresh of the mission runs: that
    one collects, and the shelf's next poll shows it."""
    lock = _mission_lock(mission_id)
    if not (lock.acquire(timeout=30) if wait else lock.acquire(blocking=False)):
        return
    try:
        try:
            ensure_baselines(mission_id, path=path)
        except Exception:  # noqa: BLE001
            log.warning("shelf: late baselines for %s failed", mission_id, exc_info=True)
        collect_due(mission_id, path=path)
    finally:
        lock.release()


def collect_due(mission_id: str, *, path: Path | None = None) -> int:
    """Collect every due baseline (see `_due`). Returns how many were collected. Never raises."""
    try:
        m = missions.get_mission(mission_id, events_limit=1, path=path)
        if m is None:
            return 0
        con = missions._ready(path)
        try:
            due, never = _due(con, m)
        finally:
            con.close()
        for row in never:
            _mark(row, "never joined", path)
        b = mission_shelf.boundary(m)
        done = 0
        for row in due:
            if _claim(row, path):
                done += _collect_one(m, row, b, path=path)
        return done
    except Exception:  # noqa: BLE001
        log.warning("shelf: collecting found items for %s failed", mission_id, exc_info=True)
        return 0


def _display_key(con, mission_id: str, key: str) -> str:
    """The roster's own key for a late-bound session's runtime key (its placeholder)."""
    row = con.execute(
        "SELECT s.session_key FROM mission_sessions s JOIN session_runtime_bindings b"
        " ON b.logical_key = s.session_key WHERE s.mission_id=? AND b.physical_key=? LIMIT 1",
        (mission_id, key),
    ).fetchone()
    return str(row["session_key"]) if row else key


def _claim(row: dict, path: Path | None) -> bool:
    """Take this baseline for one comparison (compare-and-set): two readers, or two instances,
    never collect the same window twice. A claim older than `CLAIM_STALE_S` is abandoned."""
    now = time.time()
    with missions._write_lock:
        con = missions._ready(path)
        try:
            cur = con.execute(
                "UPDATE mission_shelf_baselines SET claimed_at=? WHERE mission_id=?"
                " AND session_key=? AND repo=? AND collected_at IS NULL"
                " AND (claimed_at IS NULL OR claimed_at < ?)",
                (now, row["mission_id"], row["session_key"], row["repo"], now - CLAIM_STALE_S),
            )
            return cur.rowcount == 1
        finally:
            con.close()


def _mark(row: dict, outcome: str, path: Path | None) -> None:
    with missions._write_lock:
        con = missions._ready(path)
        try:
            con.execute(
                "UPDATE mission_shelf_baselines SET collected_at=?, outcome=?, claimed_at=NULL"
                " WHERE mission_id=? AND session_key=? AND repo=? AND collected_at IS NULL",
                (time.time(), outcome, row["mission_id"], row["session_key"], row["repo"]),
            )
        finally:
            con.close()


def _failed(row: dict, path: Path | None) -> None:
    """One comparison failed: released for the next read, until `COLLECT_ATTEMPTS`, then given up
    on with an outcome that says so (never retried on every read forever)."""
    with missions._write_lock:
        con = missions._ready(path)
        try:
            con.execute(
                "UPDATE mission_shelf_baselines SET attempts=attempts+1, claimed_at=NULL,"
                " outcome=CASE WHEN attempts+1 >= ? THEN 'failed' ELSE 'retrying' END,"
                " collected_at=CASE WHEN attempts+1 >= ? THEN ? END"
                " WHERE mission_id=? AND session_key=? AND repo=? AND collected_at IS NULL",
                (
                    COLLECT_ATTEMPTS,
                    COLLECT_ATTEMPTS,
                    time.time(),
                    row["mission_id"],
                    row["session_key"],
                    row["repo"],
                ),
            )
        finally:
            con.close()


def _collect_one(m: dict, row: dict, b: mission_shelf.Boundary, *, path: Path | None) -> int:
    if not row["repo"]:
        _mark(row, "no repository", path)
        return 1
    try:
        repo = gitpanel.discover_repo(row["repo"])
        if repo is None or repo.toplevel != row["repo"]:
            _mark(row, "repository gone", path)
            return 1
        found = _compare(repo, row, b)
        con = missions._ready(path)
        try:
            session = _display_key(con, m["id"], row["session_key"])
        finally:
            con.close()
        if found["files"]:
            found["pr"] = _pull_request(repo, found)
            _store(m["id"], session, repo, found, b, path=path)
    except Exception:  # noqa: BLE001 — retried by the next read, a bounded number of times
        log.warning("shelf: comparing %s failed", row["repo"], exc_info=True)
        _failed(row, path)
        return 0
    _mark(row, "collected", path)
    return 1


def _numstat(blob: bytes) -> dict[str, tuple[int | None, int | None]]:
    out: dict[str, tuple[int | None, int | None]] = {}
    fields = blob.split(b"\x00")
    for f in fields:
        if not f:
            continue
        parts = f.decode("utf-8", "replace").split("\t", 2)
        if len(parts) != 3:
            continue
        add = int(parts[0]) if parts[0].isdigit() else None
        dele = int(parts[1]) if parts[1].isdigit() else None
        out[parts[2]] = (add, dele)
    return out


def _name_status(blob: bytes) -> dict[str, str]:
    fields = [f.decode("utf-8", "replace") for f in blob.split(b"\x00")]
    out: dict[str, str] = {}
    i = 0
    while i + 1 < len(fields):
        status, p = fields[i], fields[i + 1]
        if status:
            out[p] = status[0]
        i += 2
    return out


def _compare(repo: gitpanel.Repo, row: dict, b: mission_shelf.Boundary) -> dict:
    base = json.loads(row["dirty"] or "{}")
    base_files: dict[str, str] = base.get("files") or {}
    end = snapshot(repo)
    files: dict[str, dict] = {}
    commits: list[dict] = []
    history = "linear"
    with gitpanel.sanitized_gitdir(repo) as (gitdir, _branch):
        head0, head1 = row["head"], end["head"]
        if head0 and not _commit_exists(repo, gitdir, head0):
            # The baseline commit is gone from the object store (a reflog expired, a gc): there
            # is no commit window to read, and the exact part — the working tree — still is.
            head0, history = None, "base_missing"
            head1_window = False
        else:
            head1_window = bool(head1 and head0 != head1)
        if head1_window:
            # Reachability, not names: a branch switch lists exactly what end has and base lacks.
            # A repository with no commit at baseline: every commit now is the session's, and
            # its changes are against the empty tree.
            commits = _log(repo, gitdir, f"{head0}..{head1}" if head0 else head1)
            if commits:
                merge_base = (
                    _git(repo, gitdir, ["merge-base", head0, head1]).decode().strip()
                    if head0
                    else ""
                )
                if head0 and not merge_base:
                    # UNRELATED HISTORIES (an orphan branch): the two trees share nothing, so a
                    # diff between them would invent a deletion for every file of the old one.
                    # What end's history added is its own tree, against the empty tree.
                    history = "unrelated"
                base_ref = merge_base or gitpanel.EMPTY_TREE
                stats = _numstat(
                    _git(repo, gitdir, ["diff", "--numstat", "-z", "--no-renames", base_ref, head1])
                )
                kinds = _name_status(
                    _git(
                        repo,
                        gitdir,
                        ["diff", "--name-status", "-z", "--no-renames", base_ref, head1],
                    )
                )
                for p, (a, d) in stats.items():
                    files[p] = {
                        "path": p,
                        "status": kinds.get(p, "M"),
                        "add": a,
                        "del": d,
                        "from": "commit",
                        "range": [base_ref, head1],
                    }
        # The working tree: a path changed between the two snapshots, by content.
        end_files: dict[str, str] = end["dirty"]
        wt_paths = sorted(
            p for p in set(base_files) | set(end_files) if base_files.get(p) != end_files.get(p)
        )
        if wt_paths and head1:
            stats = _numstat(
                _git(
                    repo,
                    gitdir,
                    [
                        "diff",
                        "--numstat",
                        "-z",
                        "--no-renames",
                        head1,
                        "--",
                        *wt_paths[:FOUND_FILES_MAX],
                    ],
                )
            )
        else:
            stats = {}
        untracked = set(end["untracked"])
        for p in wt_paths:
            state = end_files.get(p)
            if p in files:
                # Committed in the window AND still changing in the tree: the commit's diff stays
                # the record; the item says the tree moved on — only when it is dirty NOW (a file
                # dirty at baseline and then committed clean is in `wt_paths` too).
                if p in end_files:
                    files[p]["worktree_too"] = True
                continue
            if state == "deleted":
                status = "D"
            elif state is None:
                status = "M"  # dirty at baseline, back to the commit's content now
            elif p in untracked:
                status = "A"
            else:
                status = "M"
            a, d = stats.get(p, (None, None))
            files[p] = {"path": p, "status": status, "add": a, "del": d, "from": "worktree"}
        # The boundary: nothing under an exclusion or outside the mission's folders is reported.
        kept = [
            f
            for p, f in sorted(files.items())
            if mission_shelf.path_allowed(os.path.realpath(os.path.join(repo.toplevel, p)), b)
            or (
                f["status"] == "D" and mission_shelf.path_allowed(os.path.join(repo.toplevel, p), b)
            )
        ]
        # A commit is listed only when it touched a path the boundary allows (its subject is the
        # mission's to read only then); one that touched nothing (a merge, an empty commit) rides
        # along with a change that is listed.
        allowed = {f["path"] for f in kept}
        commits = [
            c
            for c in commits
            if (allowed and not c["paths"]) or any(q in allowed for q in c["paths"])
        ]
        overflow = max(0, len(kept) - FOUND_FILES_MAX)
        kept = kept[:FOUND_FILES_MAX]
        patches = _patches(repo, gitdir, kept, head1)
        for f in kept:
            if f["status"] != "D":
                f["sha"] = end_files.get(f["path"]) or _blob_at(repo, gitdir, head1, f["path"])
    return {
        "base": row["head"],
        "head": end["head"],
        "branch": end["branch"],
        "commits": [{"sha": c["sha"], "subject": c["subject"]} for c in commits[:COMMITS_MAX]],
        "commits_more": len(commits) > COMMITS_MAX,
        "files": kept,
        "overflow": overflow,
        "patches": patches,
        "late": bool(row.get("late")),
        "history": history,
    }


def _commit_exists(repo: gitpanel.Repo, gitdir: str, sha: str) -> bool:
    out = _git(repo, gitdir, ["rev-parse", "--verify", "-q", f"{sha}^{{commit}}"]).decode()
    return bool(out.strip())


def _log(repo: gitpanel.Repo, gitdir: str, rev: str) -> list[dict]:
    """``[{sha, subject, paths}]`` newest first, at most `COMMITS_MAX` + 1 (the +1 says "more").

    Records are split on a per-call random marker: a commit subject is the repository's text,
    and one carrying the separator byte must not split its record (and lose its path list)."""
    mark = secrets.token_hex(8)
    blob = _git(
        repo,
        gitdir,
        [
            "log",
            f"--max-count={COMMITS_MAX + 1}",
            f"--format=%x1e{mark}%H%x1f%s",
            "--name-only",
            "--no-renames",
            "-z",
            rev,
            "--",
        ],
    )
    out: list[dict] = []
    for rec in blob.split(b"\x1e" + mark.encode()):
        if b"\x1f" not in rec:
            continue
        header, _, names = rec.partition(b"\x00")
        sha, _, subject = header.decode("utf-8", "replace").partition("\x1f")
        paths = [n.decode("utf-8", "replace").lstrip("\n") for n in names.split(b"\x00")]
        out.append({"sha": sha, "subject": subject[:200], "paths": [q for q in paths if q]})
    return out


def _blob_at(repo: gitpanel.Repo, gitdir: str, head: str | None, p: str) -> str | None:
    if not head:
        return None
    with contextlib.suppress(Exception):
        out = _git(repo, gitdir, ["rev-parse", "--verify", "-q", f"{head}:{p}"]).decode().strip()
        return out or None
    return None


def _patches(repo, gitdir: str, kept: list[dict], head: str | None) -> dict[str, dict]:
    """Per-file unified diffs, bounded: committed changes against the merge base, working-tree
    changes against the end commit, a new untracked file as its own content. Never through a
    textconv or an external diff (`--no-ext-diff --no-textconv`, a sanitized gitdir)."""
    out: dict[str, dict] = {}
    total = 0
    for f in kept:
        if total >= PATCHES_MAX:
            out[f["path"]] = {"patch": "", "truncated": True}
            continue
        p = f["path"]
        try:
            if f["from"] == "commit":
                args = [
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--no-renames",
                    "-U3",
                    f["range"][0],
                    f["range"][1],
                    "--",
                    p,
                ]
                text = _git(repo, gitdir, args).decode("utf-8", "replace")
            elif head and f["status"] != "A":
                args = [
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--no-renames",
                    "-U3",
                    head,
                    "--",
                    p,
                ]
                text = _git(repo, gitdir, args).decode("utf-8", "replace")
            else:
                text = _new_file_patch(repo, p)
        except Exception:  # noqa: BLE001 — a file whose diff cannot be read says so
            out[p] = {"patch": "", "truncated": False, "unavailable": True}
            continue
        cut = len(text.encode("utf-8")) > PATCH_MAX
        if cut:
            text = text.encode("utf-8")[:PATCH_MAX].decode("utf-8", "ignore")
        total += len(text)
        out[p] = {"patch": text, "truncated": cut}
    return out


def _new_file_patch(repo, p: str) -> str:
    """An untracked new file as a patch: its own lines, read through a verified descriptor."""
    from . import files

    fd, st, _v = files._open_verified(os.path.join(repo.toplevel, p), directory=False)
    try:
        data = os.pread(fd, PATCH_MAX + 1, 0)
    finally:
        os.close(fd)
    if b"\x00" in data[:8192]:
        return f"Binary file {p} added ({st.st_size} bytes)\n"
    lines = data.decode("utf-8", "replace").splitlines()
    head = f"--- /dev/null\n+++ b/{p}\n@@ -0,0 +1,{len(lines)} @@\n"
    return head + "".join(f"+{line}\n" for line in lines)


# ---------------------------------------------------------------- the forge


def _repo_slug(repo: gitpanel.Repo) -> str:
    url = gitpanel.remote_url(repo.toplevel) or ""
    path = urlsplit(url).path if "://" in url else (url.split(":", 1)[1] if ":" in url else url)
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    parts = [p for p in path.split("/") if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else ""


def _pull_request(repo: gitpanel.Repo, found: dict) -> dict | None:
    """The PR for the end branch, when the configured (read-only) forge knows one. Best-effort:
    no forge, no credential or no answer is simply no link."""
    from . import forge, prefs

    branch, head = found.get("branch"), found.get("head")
    if not branch or not head:
        return None
    try:
        cfg = prefs.get_forge()
        if not cfg.get("enabled") or not cfg.get("base_url"):
            return None
        slug = _repo_slug(repo)
        if not slug:
            return None
        client = forge.ForgeClient(
            kind=cfg["kind"],
            base_url=cfg["base_url"],
            token=cfg.get("token") or "",
            owner=cfg.get("owner") or "",
        )
        fact = client.pull_request(slug, branch, head_sha=head)
    except Exception:  # noqa: BLE001
        return None
    extra = fact.extra or {}
    num = extra.get("number")
    if not fact.observed or not isinstance(num, int):
        return None
    base = cfg["base_url"].rstrip("/")
    url = (
        f"https://github.com/{slug}/pull/{num}"
        if cfg["kind"] == "github"
        else f"{base}/{slug}/pulls/{num}"
    )
    return {
        "number": num,
        "state": extra.get("pr_state") or "",
        "url": url,
        "merged": bool(extra.get("merged")),
    }


# ---------------------------------------------------------------- storing


def _is_oid(v: object) -> bool:
    return isinstance(v, str) and len(v) in (40, 64) and all(c in "0123456789abcdef" for c in v)


def _tileable(v: object) -> bool:
    return _is_oid(v) or (isinstance(v, str) and v.startswith("big:"))


def _digest(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:32]


def _store(
    mission_id: str,
    session: str,
    repo: gitpanel.Repo,
    found: dict,
    b: mission_shelf.Boundary,
    *,
    path: Path | None,
) -> None:
    name = os.path.basename(repo.toplevel)
    files = [{k: v for k, v in f.items() if k not in ("range",)} for f in found["files"]]
    add = sum(f["add"] or 0 for f in files)
    dele = sum(f["del"] or 0 for f in files)
    code_key = _digest(
        repo.toplevel,
        found["base"],
        found["head"],
        [(f["path"], f.get("sha"), f["status"]) for f in files],
    )
    title = (
        found["commits"][-1]["subject"] if found["commits"] else f"Uncommitted changes in {name}"
    )
    meta = {
        "repo": repo.toplevel,
        "repo_name": name,
        "branch": found["branch"],
        "base": found["base"],
        "head": found["head"],
        "commits": found["commits"],
        "commits_more": found["commits_more"],
        "files": files,
        "files_more": found["overflow"],
        "add": add,
        "del": dele,
        "pr": found.get("pr"),
        "late": found["late"],
        # "linear", "unrelated" (an orphan branch: commits diffed against the empty tree) or
        # "base_missing" (the baseline commit is gone: working-tree changes only).
        "history": found.get("history", "linear"),
        "sessions": [session],
        "key": code_key,
    }
    items = [
        (
            "code",
            "code",
            title,
            json.dumps(meta),
            meta,
            json.dumps(found["patches"]),
            code_key,
            None,
        )
    ]
    for f in files:
        if f["status"] == "D" or not _tileable(f.get("sha")):
            # No tile for a link or other special file: only a regular file — hashed by git, or
            # one past `HASH_MAX` keyed by its size and mtime (a large render is normal output,
            # and the raw route serves it up to `FILE_MAX`).
            continue
        full = os.path.join(repo.toplevel, f["path"])
        kind, mime = mission_shelf.classify(full)
        ext = os.path.splitext(full)[1].lower()
        if kind == "document" and ext not in FOUND_DOC_EXTS:
            continue
        fkey = _digest(repo.toplevel, f["path"], f["sha"])
        fmeta = {
            "repo": repo.toplevel,
            "repo_name": name,
            "sessions": [session],
            "key": fkey,
            "code_key": code_key,
        }
        items.append((kind, "path", os.path.basename(full), full, fmeta, None, fkey, mime))
    _insert(mission_id, session, items, path=path)


def _insert(mission_id: str, session: str, items: list, *, path: Path | None) -> None:
    ts = time.time()
    # Files are read BEFORE the store's write lock: hashing up to `FILE_MAX` per file must never
    # hold every other mission write behind it.
    facts: dict[str, tuple[int | None, str | None]] = {}
    for _k, ref_type, _t, ref, _m, _d, key, _mime in items:
        if ref_type == "path":
            size = None
            with contextlib.suppress(OSError):
                size = os.stat(ref).st_size
            facts[key] = (size, _file_sha(ref))
    with missions._write_lock:
        con = missions._ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            for kind, ref_type, title, ref, meta, detail, key, mime in items:
                prior = con.execute(
                    "SELECT id, meta FROM mission_shelf_items WHERE mission_id=? AND source='found'"
                    " AND json_extract(meta, '$.key')=?",
                    (mission_id, key),
                ).fetchone()
                if prior is not None:
                    # Touched by several sessions: one item, every session named.
                    pm = json.loads(prior["meta"])
                    if session not in pm.get("sessions", []):
                        pm["sessions"] = [*pm.get("sessions", []), session]
                        con.execute(
                            "UPDATE mission_shelf_items SET meta=? WHERE id=?",
                            (json.dumps(pm), prior["id"]),
                        )
                        mission_shelf._bump_revision_tx(con, mission_id)
                    continue
                count = con.execute(
                    "SELECT COUNT(*) FROM mission_shelf_items WHERE mission_id=?", (mission_id,)
                ).fetchone()[0]
                if count >= mission_shelf.MISSION_ITEMS_MAX:
                    break
                size, sha = facts.get(key, (None, None))
                seq, _rev = mission_shelf._bump_tx(con, mission_id)
                con.execute(
                    "INSERT INTO mission_shelf_items (id, mission_id, seq, source, kind, title,"
                    " why, session_key, ref_type, ref, mime, size, sha256, meta, created_at,"
                    " detail)"
                    " VALUES (?,?,?,'found',?,?,'',?,?,?,?,?,?,?,?,?)",
                    (
                        mission_shelf.new_item_id(),
                        mission_id,
                        seq,
                        kind,
                        mission_shelf.clean_line(title, mission_shelf.TITLE_MAX)[
                            : mission_shelf.TITLE_MAX
                        ]
                        or "Changes",
                        session,
                        ref_type,
                        ref,
                        mime,
                        size,
                        sha,
                        json.dumps(meta),
                        ts,
                        detail,
                    ),
                )
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(Exception):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def _file_sha(full: str) -> str | None:
    """The file's sha256, read through a verified descriptor (`integrity` compares to it)."""
    from . import files

    try:
        fd, st, _v = files._open_verified(full, directory=False)
    except Exception:  # noqa: BLE001
        return None
    try:
        return (
            mission_shelf._hash_fd(fd, st.st_size) if st.st_size <= mission_shelf.FILE_MAX else None
        )
    finally:
        os.close(fd)


# ---------------------------------------------------------------- after a route


_BACKGROUND: set = set()


def collect_soon(mission_id: str) -> None:
    """Collect in the background after a roster or state change (detach, review, close). Never
    blocks the route; the shelf's next read collects anyway if this one could not."""
    import asyncio

    task = asyncio.get_running_loop().create_task(asyncio.to_thread(refresh, mission_id))
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


def diff(mission_id: str, item_id: str, *, path: Path | None = None) -> dict:
    """A found code change's files and patches, mission-bound (another mission's item, or an item
    that is not a code change, is `MissionError` 404). Takes no path."""
    row = mission_shelf.get_item(mission_id, item_id, path=path)
    if row is None or row["kind"] != "code":
        raise missions.MissionError("unknown shelf item", status=404)
    meta = row.get("meta") or {}
    patches = json.loads(row.get("detail") or "{}")
    m = missions.get_mission(mission_id, events_limit=1, path=path)
    if m is None:
        raise missions.MissionError("unknown mission", status=404)
    b = mission_shelf.boundary(m)
    top = str(meta.get("repo") or "")
    out = []
    for f in meta.get("files") or []:
        # The boundary as it is NOW: a folder excluded since the collection serves no patch.
        full = os.path.join(top, f["path"])
        if not (
            mission_shelf.path_allowed(os.path.realpath(full), b)
            or (f.get("status") == "D" and mission_shelf.path_allowed(full, b))
        ):
            continue
        pt = patches.get(f["path"]) or {}
        out.append(
            {
                **f,
                "patch": pt.get("patch", ""),
                "truncated": bool(pt.get("truncated")),
                "unavailable": bool(pt.get("unavailable")),
            }
        )
    return {"id": item_id, "files": out, "files_more": meta.get("files_more", 0)}
