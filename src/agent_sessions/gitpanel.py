"""Read-only git state for the session file panel (#784).

A **security boundary**, like :mod:`agent_sessions.files`, and for a sharper reason: this module
runs a subprocess against a repository the agent is actively writing to. "The repo's config is
trusted" is not an assumption available here. Each claim below was measured on git 2.43.0.

**Being shell-free does not stop repository-controlled code from running.** A read-only subcommand
happily executes programs the repository's own config names:

====================================  ===========================  ==========================
config the repo controls              runs during                  stopped by
====================================  ===========================  ==========================
``diff.<drv>.textconv``               ``git diff``                 ``--no-textconv``
external diff driver                  ``git diff``                 ``--no-ext-diff``
``core.fsmonitor`` hook               ``status`` **and** ``diff``  ``-c core.fsmonitor=false``
``filter.<drv>.clean`` /``.process``  ``status`` **and** ``diff``  nothing — see below
====================================  ===========================  ==========================

That last row drove the design, through two wrong answers.

The first was to redirect *attributes*: ``--attr-source=<empty tree>`` so no ``.gitattributes``
can bind a path to a driver. It does not cover ``$GIT_DIR/info/attributes`` or
``core.attributesFile``, and with either of those in play the filter still ran.

The second was to disable the *drivers*: enumerate every ``filter.*`` the config defines and pass
``-c filter.<drv>.clean=`` for each. That does stop execution, but it has to read the repository's
config to know the names — which leaves an enumerate-then-use gap (a driver written between the
two commands is not in the flags) and an unbounded preflight (a config naming 120_000 filters
expands into an argv that never finishes building).

Both failures are properties of *consulting* repository-controlled metadata at all. So it is not
consulted. :func:`sanitized_gitdir` assembles a private git directory holding only what reading
requires — the object store by symlink, copies of ``index``/``HEAD``/refs, an **empty** ``info/``,
and a ``config`` written here — and every command runs against that. A driver the repository
defines is not disabled; it is never read, so there is nothing to race. It also makes "never
writes to the repository" structural: a status refresh writes to the copy.

**``git diff`` is not invoked at all.** A unified diff is assembled here instead, from
``cat-file`` blobs (objects as stored, no conversion) and the descriptor-verified worktree bytes
phase 1 already provides. Git is used for exactly three subcommands: ``rev-parse``, ``status``,
``cat-file``.

**The ceiling is not the metadata boundary.** ``GIT_CEILING_DIRECTORIES`` bounds how far git walks
*upward*; it says nothing about where a ``.git`` file it finds *points*. A worktree under the root
whose ``.git`` reads ``gitdir: <outside>`` returned a contained ``--show-toplevel`` while
``--absolute-git-dir`` sat outside and ``status`` happily read config, index, refs and objects
there. So discovery is walked **here, before git runs at all**, and ``include.path`` /
``includeIf`` / ``objects/info/alternates`` are validated too — each reaches outside the root
independently.

**A contained gitdir does not mean contained children.** ``.git/objects`` can simply *be* a
symlink to an external store: the gitdir passes every check above, the snapshot links through, and
``cat-file`` returns blobs from outside the root — a working diff over files the browser is not
allowed to see. So each metadata child is resolved and contained before use, and the resolved path
is what gets consumed. Files are taken with ``O_NOFOLLOW`` rather than checked-then-opened, since
the name can be swapped between the two.

No shell, ever. No writes to the repository, ever.
"""

from __future__ import annotations

import os
import re
import select
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from .files import FsError, contained_path, executor
from .fsbrowse import home_root

# The well-known empty tree. `--attr-source` points attribute lookup at it instead of the working
# copy, so an in-tree `.gitattributes` binds nothing. Defence in depth now rather than the
# mechanism: with a sanitized gitdir there is no driver for an attribute to bind to.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

GIT_MAX_ENTRIES = 5000
GIT_TIMEOUT_S = 10.0
GIT_MAX_STDOUT = 4 * 1024 * 1024  # hard byte budget, enforced WHILE reading
_READ_CHUNK = 64 * 1024
_STATUS_TTL_S = 1.0

# Diff bounds, enforced on each side BEFORE comparison — a response cap applied after difflib has
# already run bounds nothing.
DIFF_SOURCE_MAX_BYTES = 2 * 1024 * 1024
DIFF_SOURCE_MAX_LINES = 50_000
DIFF_MAX_LINES = 20_000
DIFF_MAX_BYTES = 512 * 1024
# An ENFORCEABLE bound, not a stopwatch. `difflib` does its expensive matching before it yields
# the first line, so checking elapsed time inside the loop can only *label* a slow comparison —
# it cannot interrupt one, and the work happens on a bounded file-panel worker. Instead the
# comparison is refused up front when it would be too large: trim the common prefix/suffix (which
# is O(n) and removes essentially all of a normal edit), then compare only if the remaining
# rectangle is under budget. Real edits sit far below it; a pathological pair is answered with a
# coarse whole-block replacement, which is O(n) and honest about what it is.
DIFF_MAX_CELLS = 2_000_000

#: git's own wording, emitted after a line whose side has no final newline.
NO_NEWLINE_MARKER = "\\ No newline at end of file"

_GIT_BIN: str | None = None
_GIT_BIN_RESOLVED = False


def git_bin() -> str | None:
    """Resolved once. ``None`` ⇒ the tab renders "git is not installed", never a 500."""
    global _GIT_BIN, _GIT_BIN_RESOLVED
    if not _GIT_BIN_RESOLVED:
        _GIT_BIN = shutil.which("git")
        _GIT_BIN_RESOLVED = True
    return _GIT_BIN


def reset_git_bin_for_test() -> None:
    global _GIT_BIN_RESOLVED
    _GIT_BIN_RESOLVED = False


def _child_env(root: str) -> dict[str, str]:
    """An **allowlist**, not a scrubbed copy of the parent.

    A denylist only removes what this module thought to name; anything it forgot — and git has a
    lot of redirect variables — would be inherited. Building the environment from nothing means a
    variable has to be added deliberately to have any effect.
    """
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", root),
        "LANG": "C",
        "LC_ALL": "C",
        # Never block on credentials, never take optional locks (the panel polls; the agent may be
        # mid-commit), never page.
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_CEILING_DIRECTORIES": root,
    }


class GitError(FsError):
    """A git request failed in a way the UI should state honestly."""


#: `--attr-source` landed in git 2.40. It is defence in depth here rather than the mechanism (the
#: sanitized gitdir defines no driver for an attribute to bind to), so an older git is *supported*
#: rather than refused — the flag is dropped after the first refusal and not offered again.
_ATTR_SOURCE = True


def _run_git(root: str, args: list[str], *, cwd: str, gitdir: str | None = None) -> bytes:
    """Run one read-only git command with a **literal argv list** and bounded output.

    ``subprocess.run(capture_output=True)`` is deliberately not used: it buffers the entire output
    before any code could truncate it, so an enormous status or object is fully resident before the
    advertised cap could apply. stdout is read incrementally against a hard budget and the child is
    killed *and reaped* on breach or timeout — never left to finish in the background.
    """
    exe = git_bin()
    if not exe:
        raise GitError("git is not installed on this host", status=501)
    argv = [
        exe,
        "-c",
        "core.fsmonitor=false",
        *(
            # Attributes from an EMPTY tree, so the WORK TREE's own `.gitattributes` binds
            # nothing. The gitdir's `info/attributes` is handled structurally instead — see
            # `sanitized_gitdir`, which gives git an `info/` that is simply empty.
            [f"--attr-source={EMPTY_TREE}"] if _ATTR_SOURCE else []
        ),
        "--no-optional-locks",
    ]
    if gitdir is not None:
        argv += [f"--git-dir={gitdir}", f"--work-tree={cwd}"]
    argv += ["-C", cwd, *args]
    proc = subprocess.Popen(  # noqa: S603 - literal argv, no shell, allowlisted subcommands
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_child_env(root),
        cwd=cwd,
    )
    out = bytearray()
    err = bytearray()
    deadline = time.monotonic() + GIT_TIMEOUT_S
    # `proc.stdout.read()` BLOCKS, so a deadline checked around it never fires for a silent or
    # stalled child — measured: a child sleeping 1s returned successfully under a 50ms budget.
    # And draining stdout to EOF before touching stderr deadlocks a child that fills the stderr
    # pipe. Both are fixed by selecting over BOTH pipes with the remaining time as the timeout.
    streams = [p for p in (proc.stdout, proc.stderr) if p is not None]
    for p in streams:
        os.set_blocking(p.fileno(), False)
    try:
        while streams:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GitError("git took too long and was stopped", status=504)
            ready, _, _ = select.select(streams, [], [], min(remaining, 0.25))
            for p in ready:
                try:
                    # `os.read`, not `p.read`: a non-blocking BufferedReader returns **None** on
                    # EAGAIN, which is falsy — indistinguishable from the b"" that means EOF, so
                    # a spurious wakeup would drop a live stream. `os.read` raises instead.
                    chunk = os.read(p.fileno(), _READ_CHUNK)
                except BlockingIOError:
                    continue
                if not chunk:
                    streams.remove(p)
                    continue
                if p is proc.stdout:
                    out += chunk
                    if len(out) > GIT_MAX_STDOUT:
                        raise GitError(
                            "git produced more output than the panel will read", status=413
                        )
                elif len(err) < 8192:
                    err += chunk
        proc.wait(timeout=max(0.05, deadline - time.monotonic()))
    except GitError:
        proc.kill()
        proc.wait()
        raise
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise GitError("git took too long and was stopped", status=504) from None
    finally:
        for p in (proc.stdout, proc.stderr):
            if p is not None:
                p.close()
    if proc.returncode not in (0, 1):
        if _ATTR_SOURCE and b"attr-source" in bytes(err[:4096]):
            # An earlier version failed CLOSED here, because safety depended on the flag. It no
            # longer does — the sanitized gitdir defines no driver — so refusing to run on git
            # 2.39 would be a hard error for no security gain. Drop it once and retry.
            _disable_attr_source()
            return _run_git(root, args, cwd=cwd, gitdir=gitdir)
        raise GitError("git could not read this repository", status=400)
    return bytes(out)


def _disable_attr_source() -> None:
    global _ATTR_SOURCE
    _ATTR_SOURCE = False


def reset_attr_source_for_test() -> None:
    global _ATTR_SOURCE
    _ATTR_SOURCE = True


# --------------------------------------------------------------------------- discovery


def _contained(path: str) -> bool:
    root = home_root()
    real = os.path.realpath(path)
    return real == root or real.startswith(root + os.sep)


def _resolve_gitdir_file(gitfile: str) -> str | None:
    """Parse a ``.git`` FILE and return the gitdir it names, or None if unparseable."""
    try:
        with open(gitfile, encoding="utf-8", errors="replace") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    for line in head.splitlines():
        if line.startswith("gitdir:"):
            target = line.split(":", 1)[1].strip()
            if not target:
                return None
            if os.path.isabs(target):
                return target
            return os.path.join(os.path.dirname(gitfile), target)
    return None


def _config_includes_are_contained(gitdir: str) -> bool:
    """Refuse a repo whose own config pulls in a file outside the root.

    A contained ``.git/config`` can carry ``include.path`` / ``includeIf.*.path`` pointing anywhere,
    and git reads and honours it — measured: an outside file set ``user.name`` and
    ``git config --get`` returned it from inside the contained repo. The distinction being drawn is
    deliberate and narrow: *repository*-controlled config must not reach outside the root, while the
    operator's own system/global config is trusted and still honoured.
    """
    seen: set[str] = set()
    todo = [os.path.join(gitdir, "config"), os.path.join(gitdir, "config.worktree")]
    while todo:
        cfg = todo.pop()
        real = os.path.realpath(cfg)
        if real in seen:
            continue
        seen.add(real)
        if len(seen) > 32:  # a pathological include chain is itself a refusal
            return False
        text = _read_capped(cfg, 256 * 1024)
        if text is None:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line.lower().startswith("path"):
                continue
            if "=" not in line:
                continue
            target = line.split("=", 1)[1].strip()
            if not target:
                continue
            target = os.path.expanduser(target)
            if not os.path.isabs(target):
                target = os.path.join(os.path.dirname(cfg), target)
            if not _contained(target):
                return False
            todo.append(target)
    return True


def _alternates_are_contained(gitdir: str) -> bool:
    """``objects/info/alternates`` reaches an object store directly — gitdir being in-root is not
    enough on its own."""
    alt = os.path.join(gitdir, "objects", "info", "alternates")
    text = _read_capped(alt, 64 * 1024)
    if text is None:
        return True  # absent is fine
    lines = text.splitlines()
    for line in lines:
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if not os.path.isabs(entry):
            entry = os.path.join(gitdir, "objects", entry)
        if not _contained(entry):
            return False
    return True


def _common_dir(gitdir: str) -> str:
    """Resolve `<gitdir>/commondir` — present only for a linked worktree."""
    target = (_read_capped(os.path.join(gitdir, "commondir"), 4096) or "").strip()
    if not target:
        return gitdir
    if not os.path.isabs(target):
        target = os.path.join(gitdir, target)
    return os.path.realpath(target)


@dataclass
class Repo:
    toplevel: str
    gitdir: str
    #: For a linked worktree, `<gitdir>/commondir` points at the shared store — objects and refs
    #: live there while HEAD and the index stay per-worktree.
    commondir: str = ""

    def common(self) -> str:
        return self.commondir or self.gitdir


def discover_repo(start: str) -> Repo | None:
    """Walk to the repository **server-side, before git is invoked at all.**

    Every step is validated here because git's own answers arrive too late: by the time
    ``--show-toplevel`` reports a contained worktree, git has already opened whatever metadata the
    ``.git`` file pointed at. Returns ``None`` for "not a repository", which is a state rather than
    an error; raises only when something is found and refused.
    """
    root = home_root()
    cur = os.path.realpath(start)
    while True:
        dot = os.path.join(cur, ".git")
        if os.path.isdir(dot):
            gitdir = dot
        elif os.path.isfile(dot):
            target = _resolve_gitdir_file(dot)
            if target is None:
                return None
            if not _contained(target):
                raise GitError(
                    "this repository's git directory is outside the browsable root", status=403
                )
            gitdir = os.path.realpath(target)
        else:
            if cur == root or not cur.startswith(root + os.sep):
                return None
            parent = os.path.dirname(cur)
            if parent == cur:
                return None
            cur = parent
            continue

        if not _contained(gitdir):
            raise GitError("the git directory is outside the browsable root", status=403)
        common = _common_dir(gitdir)
        if not _contained(common):
            raise GitError("the shared git directory is outside the browsable root", status=403)
        # Checked on BOTH: a linked worktree's own gitdir holds HEAD, the index and
        # `config.worktree`, while `config`, `objects` and `refs` live in the commondir. Checking
        # only the gitdir passed vacuously for every linked worktree — the files being validated
        # were not there to read.
        for d in {gitdir, common}:
            for child in ("objects", "refs", "reftable", "logs"):
                _verified_metadata_dir(d, child)  # raises 403 if it resolves outside
            if not _config_includes_are_contained(d):
                raise GitError(
                    "this repository's config includes a file outside the root", status=403
                )
            if not _alternates_are_contained(d):
                raise GitError("this repository uses an object store outside the root", status=403)
        return Repo(toplevel=cur, gitdir=gitdir, commondir=common)


# --------------------------------------------------------------------------- sanitized metadata

GIT_MAX_REFS = 20_000
GIT_MAX_REFS_BYTES = 8 * 1024 * 1024
GIT_MAX_INDEX_BYTES = 256 * 1024 * 1024
_REF_VALUE = re.compile(r"\A[A-Za-z0-9._/+-]{1,255}\Z")


def _open_nofollow(path: str) -> int | None:
    """Open exactly the named entry, never what a symlink at that name points to.

    Checking a path and then opening it are two different operations on two different objects: the
    name can be replaced in between. `O_NOFOLLOW` closes that gap by refusing at the syscall — the
    open fails outright if the final component is a symlink, so there is no window to lose. The
    `fstat` is on the descriptor already held, so it describes the file that was actually opened.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    return fd


def _read_capped(path: str, limit: int) -> str | None:
    fd = _open_nofollow(path)
    if fd is None:
        return None
    try:
        chunks: list[bytes] = []
        got = 0
        while got < limit:
            chunk = os.read(fd, min(_READ_CHUNK, limit - got))
            if not chunk:
                break
            chunks.append(chunk)
            got += len(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _copy_nofollow(src: str, dst: str, limit: int, *, keep_mtime: bool = False) -> bool:
    """Copy a metadata file through a verified descriptor. False ⇒ absent, a symlink, or too big.

    `shutil.copyfile` follows symlinks and stats the name rather than the handle, so a metadata
    file swapped for a link between the check and the copy would be read from wherever it pointed.

    ``keep_mtime`` carries the SOURCE's timestamps onto the copy. That is load-bearing for the
    index — see ``sanitized_gitdir`` — and harmless for everything else, so it is opt-in rather
    than the default.
    """
    fd = _open_nofollow(src)
    if fd is None:
        return False
    try:
        st = os.fstat(fd)
        if st.st_size > limit:
            return False
        with open(dst, "wb") as out:
            while True:
                chunk = os.read(fd, _READ_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
        if keep_mtime:
            os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    except OSError:
        return False
    finally:
        os.close(fd)
    return True


def _verified_metadata_dir(parent: str, name: str) -> str | None:
    """Resolve a metadata directory and refuse it if it lands outside the root.

    The gitdir being contained says nothing about its *children*: `.git/objects` can be a symlink
    to an external object store, and the snapshot would link straight through to it — measured, a
    repository whose objects lived in `/tmp` returned real blobs through the DIFF API. Same class
    of escape as `objects/info/alternates`, which was already refused; this is the same door with
    a different handle.

    The resolved path is what the caller uses, so what was verified is what gets consumed.
    """
    target = os.path.join(parent, name)
    if not os.path.exists(target):
        return None
    real = os.path.realpath(target)
    if not _contained(real):
        raise GitError(
            "this repository keeps its git metadata outside the browsable root", status=403
        )
    return real if os.path.isdir(real) else None


def _head_branch(gitdir: str) -> str | None:
    text = _read_capped(os.path.join(gitdir, "HEAD"), 4096) or ""
    line = text.strip()
    if line.startswith("ref: refs/heads/"):
        return line[len("ref: refs/heads/") :] or None
    return None


def _upstream_config(commondir: str, branch: str | None) -> str:
    """The two keys needed for ahead/behind, read WITHOUT git and WITHOUT following includes.

    These are ref *names*, never commands, and they are re-emitted into a config this module
    writes — so the repository contributes data to a file it does not control. Anything that does
    not look like a ref name is dropped rather than passed on.
    """
    if not branch:
        return ""
    text = _read_capped(os.path.join(commondir, "config"), 256 * 1024)
    if text is None:
        return ""
    want = f'[branch "{branch}"]'
    section = False
    remote = merge = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            section = line == want
            continue
        if not section or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().lower(), value.strip().strip('"')
        if key == "remote" and _REF_VALUE.match(value):
            remote = value
        elif key == "merge" and _REF_VALUE.match(value):
            merge = value
    if not (remote and merge):
        return ""
    return (
        f'[branch "{branch}"]\n\tremote = {remote}\n\tmerge = {merge}\n'
        f'[remote "{remote}"]\n\tfetch = +refs/heads/*:refs/remotes/{remote}/*\n'
    )


def _copy_refs(src: str, dst: str) -> bool:
    """Copy the ref tree, bounded. False ⇒ too big; the caller degrades to a detached HEAD."""
    count = 0
    total = 0
    for cur, _dirs, names in os.walk(src):
        # Directories count too. Bounding only files let a tree of a million empty directories
        # walk unbounded — the same shape of hole as the config preflight, one level down.
        count += 1
        if count > GIT_MAX_REFS:
            return False
        rel = os.path.relpath(cur, src)
        os.makedirs(os.path.join(dst, rel), exist_ok=True)
        for name in names:
            count += 1
            if count > GIT_MAX_REFS:
                return False
            srcf = os.path.join(cur, name)
            try:
                total += os.lstat(srcf).st_size
            except OSError:
                continue
            if total > GIT_MAX_REFS_BYTES:
                return False
            # A symlinked ref could point anywhere, so it is skipped rather than followed —
            # and skipping is decided by the OPEN, not by a preceding stat that a swap can outrun.
            _copy_nofollow(srcf, os.path.join(dst, rel, name), GIT_MAX_REFS_BYTES)
    return True


@contextmanager
def sanitized_gitdir(repo: Repo):
    """A private git directory holding only what reading requires — and no config the repo wrote.

    This replaces an earlier approach that enumerated the repository's conversion drivers and
    disabled each by name. That worked, but it had an enumerate-then-use gap: a concurrent write
    between the enumeration and the command could introduce a driver the generated flags did not
    cover, and (review's second point) the enumeration itself was unbounded — a config naming
    120_000 filters expanded into an argv that never finished building.

    Both problems are properties of *consulting* repository config at all. So none is consulted:
    git runs against a directory this module assembles, containing

    * ``objects`` — a symlink to the real store (read-only use; alternates already validated),
    * ``index`` and ``HEAD`` — copied, so a refresh writes to the copy and never the repository,
    * ``refs`` / ``packed-refs`` — copied within a bound,
    * ``config`` — **written here**, carrying only ``core.*`` plus the upstream ref names,
    * ``info/`` — empty, so ``info/attributes`` cannot bind a path to a driver.

    A driver the repository defines is therefore not disabled — it is never read, so there is
    nothing to race. It also makes "never writes to the repository" structural rather than a
    property of the flags passed.

    The copy is per call and bounded; on this repo the index is 57 KB.
    """
    tmp = tempfile.mkdtemp(prefix="agent-sessions-git-")
    try:
        common = repo.common()
        # The RESOLVED object store, verified contained — not the name, which can be a symlink out
        # of the root. Linking to the resolved path also means what was checked is what is used.
        objects = _verified_metadata_dir(common, "objects")
        if objects is None:
            raise GitError("this repository has no readable object store", status=400)
        os.symlink(objects, os.path.join(tmp, "objects"))
        os.makedirs(os.path.join(tmp, "info"), exist_ok=True)
        os.makedirs(os.path.join(tmp, "refs"), exist_ok=True)

        # A repo with no index yet is legal; status then reports everything as untracked. An index
        # that EXISTS but cannot be taken is a different thing and must not be silently skipped:
        # git would compare the worktree against an empty index and report a plausible, wrong set
        # of changes (measured: one path listed twice, as both a staged delete and an addition).
        index = os.path.join(repo.gitdir, "index")
        # `keep_mtime` is CORRECTNESS here, not tidiness (#797).
        #
        # git can usually decide a file is unchanged from `stat` alone, by comparing it against
        # the stat cached in the index. That shortcut is unsound for an edit made in the same
        # timestamp granule as the index write — same size, same mtime, different content — so
        # git guards it: any entry whose mtime is >= the INDEX FILE's own mtime is "racily
        # clean" and gets re-hashed instead of trusted.
        #
        # A fresh copy has a fresh mtime, which makes every entry look comfortably older than
        # the index and switches that guard off. The result is git reporting **no change for a
        # file that changed** — measured: a same-size edit made in the same second as the commit
        # is reported as modified when the panel runs within that second, and as clean once a
        # second has passed (6/6 reproducible). That is the panel's worst possible failure, and
        # it reached the Git tab as an intermittent "this file has no recorded change" 404.
        #
        # Carrying the source's timestamps over makes the snapshot's racy-clean arithmetic
        # identical to the real gitdir's, which is the whole intent of the copy.
        if os.path.lexists(index) and not _copy_nofollow(
            index, os.path.join(tmp, "index"), GIT_MAX_INDEX_BYTES, keep_mtime=True
        ):
            raise GitError("this repository's index could not be read safely", status=400)

        branch = _head_branch(repo.gitdir)
        refs_src = _verified_metadata_dir(common, "refs")
        refs_ok = refs_src is None or _copy_refs(refs_src, os.path.join(tmp, "refs"))
        reftable_src = _verified_metadata_dir(common, "reftable")
        if refs_ok and reftable_src is not None:
            # git 2.45+ can store refs in `reftable/` with no `refs/` tree at all; without this the
            # snapshot would have no refs to resolve HEAD against.
            refs_ok = _copy_refs(os.path.join(common, "reftable"), os.path.join(tmp, "reftable"))
        packed = os.path.join(common, "packed-refs")
        if refs_ok and os.path.lexists(packed):
            refs_ok = _copy_nofollow(packed, os.path.join(tmp, "packed-refs"), GIT_MAX_REFS_BYTES)

        head_src = _read_capped(os.path.join(repo.gitdir, "HEAD"), 4096) or ""
        if refs_ok and head_src.strip():
            head = head_src
        else:
            # Too many refs to snapshot: fall back to a detached HEAD at the resolved commit.
            # Everything still works except upstream divergence, which the UI already renders as
            # "NO DIVERGENCE DATA" rather than inventing a zero.
            head = _resolve_head_sha(repo, branch) or ""
            branch = branch if head else None
            refs_ok = False
        if not head.strip():
            # Better a named refusal than a directory git will reject as "not a git repository".
            raise GitError("git could not read this repository's HEAD", status=400)
        with open(os.path.join(tmp, "HEAD"), "w", encoding="utf-8") as fh:
            fh.write(head if head.endswith("\n") else head + "\n")

        cfg = "[core]\n\trepositoryformatversion = 0\n\tbare = false\n\tlogallrefupdates = false\n"
        if refs_ok:
            cfg += _upstream_config(common, branch)
        with open(os.path.join(tmp, "config"), "w", encoding="utf-8") as fh:
            fh.write(cfg)
        yield tmp, branch
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _resolve_head_sha(repo: Repo, branch: str | None) -> str | None:
    """Resolve HEAD to a raw sha by reading refs directly — no git, no ref enumeration."""
    common = repo.common()
    if branch:
        direct = (_read_capped(os.path.join(common, "refs", "heads", branch), 128) or "").strip()
        if direct:
            return direct
        packed = _read_capped(os.path.join(common, "packed-refs"), 8 * 1024 * 1024) or ""
        for line in packed.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == f"refs/heads/{branch}":
                return parts[0]
        return None
    head = (_read_capped(os.path.join(repo.gitdir, "HEAD"), 128) or "").strip()
    return head or None


def _verify_with_git(repo: Repo, gitdir: str) -> None:
    """Re-ask git where the WORK TREE is and re-contain the answer.

    Only the work tree is checked now. The git directory is this module's own temporary copy, so
    asking git about it would just confirm a path we wrote ourselves — and that path is outside
    the browsable root by construction, which the old check would have refused.
    """
    out = _run_git(
        home_root(),
        ["rev-parse", "--show-toplevel", "--path-format=absolute"],
        cwd=repo.toplevel,
        gitdir=gitdir,
    ).decode("utf-8", errors="replace")
    for line in out.splitlines():
        p = line.strip()
        if p and not _contained(p):
            raise GitError("this repository reaches outside the browsable root", status=403)


# --------------------------------------------------------------------------- status


def _parse_porcelain_v2(blob: bytes) -> dict:
    """NUL-delimited porcelain v2.

    ``-z`` matters: v1 and non-``-z`` output *quote-escape* paths containing spaces, quotes,
    newlines or non-ASCII bytes, so a parser built on that is wrong for real repositories. Records
    are length-delimited here instead, and a rename's two paths arrive as two NUL-separated fields.
    """
    branch: str | None = None
    upstream: str | None = None
    ahead: int | None = None
    behind: int | None = None
    entries: list[dict] = []
    truncated = False

    fields = blob.split(b"\x00")
    i = 0
    while i < len(fields):
        rec = fields[i]
        i += 1
        if not rec:
            continue
        text = rec.decode("utf-8", errors="replace")
        if text.startswith("# branch.head "):
            head = text[len("# branch.head ") :]
            branch = None if head == "(detached)" else head
            continue
        if text.startswith("# branch.upstream "):
            upstream = text[len("# branch.upstream ") :]
            continue
        if text.startswith("# branch.ab "):
            parts = text[len("# branch.ab ") :].split()
            for p in parts:
                if p.startswith("+"):
                    ahead = int(p[1:])
                elif p.startswith("-"):
                    behind = int(p[1:])
            continue
        if text.startswith("#"):
            continue
        if len(entries) >= GIT_MAX_ENTRIES:
            truncated = True
            continue

        kind = text[0]
        if kind == "?":
            entries.append(
                {"path": text[2:], "index": "?", "worktree": "?", "kind": "untracked", "oid": None}
            )
        elif kind == "1":
            # 1 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <path>
            parts = text.split(" ", 8)
            if len(parts) < 9:
                continue
            xy, oid_head, oid_index, path = parts[1], parts[6], parts[7], parts[8]
            entries.extend(_split_xy(path, xy, oid_head, oid_index))
        elif kind == "2":
            # 2 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <score> <path>\0<orig>
            parts = text.split(" ", 9)
            if len(parts) < 10:
                continue
            xy, oid_head, oid_index, path = parts[1], parts[6], parts[7], parts[9]
            orig = fields[i].decode("utf-8", errors="replace") if i < len(fields) else ""
            i += 1
            for e in _split_xy(path, xy, oid_head, oid_index):
                e["orig_path"] = orig
                entries.append(e)
        elif kind == "u":
            # u <XY> <sub> <m1> <m2> <m3> <mW> <h1> <h2> <h3> <path>
            parts = text.split(" ", 10)
            if len(parts) < 11:
                continue
            # h1/h2/h3 = base / ours / theirs. Dropping them left the conflict DIFF comparing
            # two empty blobs against the marker-laden worktree file, which is the one view of a
            # conflict that tells you nothing. Ours-vs-theirs is the comparison a conflict is about.
            entries.append(
                {
                    "path": parts[10],
                    "index": parts[1][0],
                    "worktree": parts[1][1],
                    "kind": "unmerged",
                    "oid": None,
                    "oid_base": parts[7],
                    "oid_ours": parts[8],
                    "oid_theirs": parts[9],
                }
            )
    return {
        "branch": branch,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "entries": entries,
        "truncated": truncated,
    }


def _split_xy(path: str, xy: str, oid_head: str, oid_index: str) -> list[dict]:
    """One porcelain record can mean two rows.

    ``MM`` is a staged edit *and* a later unstaged one. That is real git state, so it renders in
    both groups, and each row carries the oids its own diff needs rather than sharing one.
    """
    out: list[dict] = []
    x, y = xy[0], xy[1]
    # BOTH oids ride BOTH rows. Carrying only one made every staged diff compare HEAD with itself
    # — the index side was read from a key nothing ever wrote — so staged rows came back empty
    # while `git diff --cached` showed real changes.
    oids = {"oid_head": oid_head, "oid_index": oid_index}
    if x != ".":
        out.append(
            {"path": path, "index": x, "worktree": ".", "kind": "staged", "oid": oid_head, **oids}
        )
    if y != ".":
        out.append(
            {"path": path, "index": ".", "worktree": y, "kind": "changed", "oid": oid_index, **oids}
        )
    return out


# --------------------------------------------------------------------------- single flight


@dataclass
class _Flight:
    event: threading.Event = field(default_factory=threading.Event)
    value: dict | None = None
    error: BaseException | None = None
    at: float = 0.0


_flights_lock = threading.Lock()
_flights: dict[str, _Flight] = {}


def _single_flight(key: str, produce):
    """One run per repo at a time, with a 1s reuse window.

    A TTL cache alone does not coalesce simultaneous *cold* misses: N pollers arriving together
    each miss, each spawn git, and each then populate the cache. The leader/follower split is what
    actually makes "N pollers, one subprocess" true.
    """
    now = time.monotonic()
    with _flights_lock:
        fl = _flights.get(key)
        if fl is not None and fl.event.is_set() and now - fl.at < _STATUS_TTL_S:
            if fl.error:
                raise fl.error
            return fl.value
        if fl is not None and not fl.event.is_set():
            leader = False
        else:
            fl = _Flight()
            _flights[key] = fl
            leader = True
    if not leader:
        fl.event.wait(GIT_TIMEOUT_S + 2)
        if fl.error:
            raise fl.error
        return fl.value
    try:
        fl.value = produce()
    except BaseException as e:  # noqa: BLE001 - recorded and re-raised to every follower
        fl.error = e
        fl.at = time.monotonic()
        fl.event.set()
        raise
    fl.at = time.monotonic()
    fl.event.set()
    return fl.value


def reset_flights_for_test() -> None:
    with _flights_lock:
        _flights.clear()


def git_status(path: str | None) -> dict:
    """Repository state for ``path``. ``repo: None`` is a normal 200 — "not a repo" is a state."""
    base = contained_path(path or "")
    repo = discover_repo(base)
    if repo is None:
        return {
            "repo": None,
            "branch": None,
            "upstream": None,
            "ahead": None,
            "behind": None,
            "entries": [],
            "truncated": False,
        }

    def produce() -> dict:
        with sanitized_gitdir(repo) as (gitdir, branch):
            _verify_with_git(repo, gitdir)
            blob = _run_git(
                home_root(),
                ["status", "--porcelain=v2", "--branch", "--untracked-files=all", "-z"],
                cwd=repo.toplevel,
                gitdir=gitdir,
            )
            parsed = _parse_porcelain_v2(blob)
            # git reports `(detached)` when the ref snapshot was too large to copy, but the branch
            # name was read straight from HEAD and is still known.
            parsed["branch"] = parsed["branch"] or branch
            parsed["repo"] = repo.toplevel
            return parsed

    return _single_flight(repo.toplevel, produce)


def git_diff_kw(path: str, staged: bool) -> dict:
    """Positional adapter for the route dispatcher, which passes args positionally."""
    return git_diff(path, staged=staged)


__all__ = [
    "DIFF_MAX_BYTES",
    "DIFF_MAX_LINES",
    "DIFF_SOURCE_MAX_BYTES",
    "DIFF_SOURCE_MAX_LINES",
    "GIT_MAX_ENTRIES",
    "GitError",
    "Repo",
    "discover_repo",
    "executor",
    "git_bin",
    "git_diff",
    "git_diff_kw",
    "git_status",
    "reset_flights_for_test",
    "reset_git_bin_for_test",
]


# --------------------------------------------------------------------------- diff


def _cat_blob(repo: Repo, oid: str, gitdir: str) -> bytes:
    """Object bytes **as stored**. `cat-file` runs no conversion filters, which is the whole
    reason the diff is assembled here instead of asked of `git diff`."""
    if not oid or set(oid) == {"0"}:
        return b""  # the empty side of an add/delete
    return _run_git(home_root(), ["cat-file", "blob", oid], cwd=repo.toplevel, gitdir=gitdir)


def _decodable(data: bytes) -> list[str] | None:
    """Strict decode, deliberately.

    ``errors="replace"`` would silently produce a diff of mangled text, and a wrong diff is worse
    than an honest refusal. CONTENT keeps the lenient decode because it is a display surface rather
    than a comparison. A NUL in the sniff window means binary before decoding is even attempted.
    """
    if b"\x00" in data[:8192]:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # Lines keep their terminator. `split("\n")` loses it, and the loss is not cosmetic: `b"a\n"`
    # and `b"a"` both become `["a"]`-ish, so removing a file's final newline either compared equal
    # (no diff at all) or surfaced as a phantom blank line — measured `@@ -1,2 +1 @@` with a blank
    # deletion where git reports `-a` / `+a` / `\ No newline at end of file`.
    #
    # `str.splitlines(keepends=True)` is not usable here: it also breaks on \v, \f, \x1c and
    # U+2028, none of which git treats as a line ending, so a file containing one would diff
    # against itself. Split on "\n" only. \r is kept, so a CRLF file does not read as every line
    # changed.
    parts = text.split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])  # trailing fragment: the file does not end with a newline
    return lines


def git_diff(path: str, *, staged: bool) -> dict:
    """A unified diff assembled from blobs, never from ``git diff``.

    No flag combination makes ``git diff`` helper-proof — ``filter.*`` clean/process drivers run
    regardless of ``--no-textconv --no-ext-diff`` (measured) — so the two sides are fetched
    separately and compared here.
    """
    import difflib

    target = contained_path(path)
    repo = discover_repo(os.path.dirname(target))
    if repo is None:
        raise GitError("this file is not inside a git repository", status=404)

    status = git_status(repo.toplevel)
    rel = os.path.relpath(target, repo.toplevel)
    rows = [e for e in status["entries"] if e["path"] == rel]
    row = next((e for e in rows if (e["kind"] == "staged") == staged), rows[0] if rows else None)
    if row is None:
        raise GitError("this file has no recorded change", status=404)
    if row["kind"] == "untracked":
        return {
            "path": rel,
            "repo": repo.toplevel,
            "diff": "",
            "added": 0,
            "removed": 0,
            "truncated": False,
            "binary": False,
            "too_large": False,
            "conflict": False,
        }

    from . import files as _files

    conflict = row["kind"] == "unmerged"
    with sanitized_gitdir(repo) as (gitdir, _branch):
        if conflict:
            # A conflicted file's worktree copy is ours+theirs+markers interleaved; diffing it
            # against anything describes the markers, not the disagreement. Compare the two sides.
            old_bytes = _cat_blob(repo, row.get("oid_ours") or "", gitdir)
            new_bytes = _cat_blob(repo, row.get("oid_theirs") or "", gitdir)
        elif staged:
            # Staged means index vs HEAD: two blobs, no worktree involved.
            old_bytes = _cat_blob(repo, row.get("oid_head") or "", gitdir)
            new_bytes = _cat_blob(repo, row.get("oid_index") or "", gitdir)
        else:
            old_bytes = _cat_blob(repo, row.get("oid_index") or row.get("oid") or "", gitdir)
    if not conflict and not staged:
        try:
            # RAW bytes, not `read_file`'s display string: that decodes with `errors="replace"`,
            # and re-encoding the result yields valid UTF-8 whatever went in — so `_decodable`
            # below could never refuse a mangled file. Read one byte past the source budget so an
            # oversized file trips the `too_large` branch instead of being silently truncated.
            _v, new_bytes, _size, _trunc = _files.read_file_bytes(
                target, limit=DIFF_SOURCE_MAX_BYTES + 1
            )
        except FsError:
            new_bytes = b""  # deleted in the worktree

    # Source-side budgets, BEFORE comparison. A response cap applied after difflib has run bounds
    # nothing: large or adversarially repetitive inputs burn memory and CPU first.
    if len(old_bytes) > DIFF_SOURCE_MAX_BYTES or len(new_bytes) > DIFF_SOURCE_MAX_BYTES:
        return {
            "path": rel,
            "repo": repo.toplevel,
            "diff": "",
            "added": None,
            "removed": None,
            "truncated": True,
            "binary": False,
            "too_large": True,
            "conflict": conflict,
        }

    old_lines = _decodable(old_bytes)
    new_lines = _decodable(new_bytes)
    if old_lines is None or new_lines is None:
        return {
            "path": rel,
            "repo": repo.toplevel,
            "diff": "",
            "added": None,
            "removed": None,
            "truncated": False,
            "binary": True,
            "too_large": False,
            "conflict": conflict,
        }
    if len(old_lines) > DIFF_SOURCE_MAX_LINES or len(new_lines) > DIFF_SOURCE_MAX_LINES:
        return {
            "path": rel,
            "repo": repo.toplevel,
            "diff": "",
            "added": None,
            "removed": None,
            "truncated": True,
            "binary": False,
            "too_large": True,
            "conflict": conflict,
        }

    # Trim the common prefix/suffix first: O(n), and for a normal edit it leaves a handful of
    # lines, so the budget below is never reached in practice.
    pre = 0
    while pre < len(old_lines) and pre < len(new_lines) and old_lines[pre] == new_lines[pre]:
        pre += 1
    suf = 0
    while (
        suf < len(old_lines) - pre
        and suf < len(new_lines) - pre
        and old_lines[len(old_lines) - 1 - suf] == new_lines[len(new_lines) - 1 - suf]
    ):
        suf += 1
    a_mid = old_lines[pre : len(old_lines) - suf]
    b_mid = new_lines[pre : len(new_lines) - suf]

    out: list[str] = []
    added = removed = 0
    over = False
    coarse = False

    def _emit(entry: str) -> list[str]:
        """Strip the terminator for display, and say so when a side has none.

        The marker is git's own wording, and the frontend already renders it — what was missing
        was the server ever producing it.
        """
        if entry.endswith("\n"):
            return [entry[:-1]]
        return [entry, NO_NEWLINE_MARKER]

    if len(a_mid) * len(b_mid) > DIFF_MAX_CELLS:
        # Refused BEFORE the expensive matching starts, which is the only point at which it can
        # actually be refused. A coarse replacement is still useful and is labelled as such.
        coarse = True
        out.append(f"@@ -{pre + 1},{len(a_mid)} +{pre + 1},{len(b_mid)} @@")
        for line in a_mid:
            out.extend(_emit(f"-{line}"))
            removed += 1
            if len(out) >= DIFF_MAX_LINES:
                over = True
                break
        if not over:
            for line in b_mid:
                out.extend(_emit(f"+{line}"))
                added += 1
                if len(out) >= DIFF_MAX_LINES:
                    over = True
                    break
    else:
        for line in difflib.unified_diff(old_lines, new_lines, lineterm="", n=3):
            if len(out) >= DIFF_MAX_LINES:
                over = True
                break
            if line.startswith(("---", "+++", "@@")):
                out.append(line)
                continue
            out.extend(_emit(line))
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1
    text = "\n".join(out)
    if len(text) > DIFF_MAX_BYTES:
        # On a LINE boundary. A blind slice cuts a `+` line in half, and half a line of content
        # renders as if it were the whole line — a truncation the reader cannot see.
        text = text[:DIFF_MAX_BYTES].rsplit("\n", 1)[0]
        over = True
    return {
        "path": rel,
        "repo": repo.toplevel,
        "diff": text,
        # Counts from a truncated prefix are not totals, so they are withheld rather than shown as
        # if they were: "+18 -4" when the honest value is "+18 -4 so far" is a lie the UI would
        # have no way to detect.
        "added": None if over else added,
        "removed": None if over else removed,
        "truncated": over,
        "binary": False,
        "too_large": False,
        # Ours-vs-theirs, not worktree-vs-anything (see the `u` record parse), so the viewer can
        # label the two sides correctly instead of implying one of them is "the file".
        "conflict": conflict,
        # The pair exceeded the comparison budget, so this is a whole-block replacement rather
        # than a line-by-line diff. Said out loud instead of passed off as a real diff.
        "coarse": coarse,
    }
