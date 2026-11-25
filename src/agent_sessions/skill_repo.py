"""Read a skill out of a public git repository the Commander linked (#2145).

HQ's `inspect_skill_repo` fetches the repository at one commit, lists the folders that hold a
`SKILL.md`, and `propose_library` `skill.import` drafts the exact bytes of one of them. Nothing
here saves anything: the draft is a card the operator confirms (`ask_library`).

**The destination is admitted exactly as a playbook clone's** (`playbooks/repo_git.py`): one
`gitwrite.admit_destination` resolution (this machine refused, a credential in the URL refused,
the resolved addresses pinned with `http.curloptResolve`, url-specific `sslVerify=true`, the proxy
reset and `http.followRedirects=false`), narrowed further to **https only**, since ssh would sign in
with the operator's own keys. The URL must also be one the Commander TYPED in this turn
(`operator_url`): a complete URL token of their message, never a prefix, a substring, or a link
found only in tool content.

**No ambient credential is ever sent.** git reads no system or global config
(`GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`), `credential.helper` is reset, prompts are
off, and `HOME` is an EMPTY private folder, so neither `~/.git-credentials` nor the `~/.netrc` that
git's curl consults exists for it.

**The fetch is bounded while it runs, whatever the server supports.** It asks for
`--depth 1 --filter=blob:limit=128k` into a bare repository (no working tree, no checkout, no
hooks, no submodules, no smudge). A server may ignore the filter; nothing assumes it worked:

* a watchdog polls the private folder's total size every `POLL_S` and, past `MAX_REPO_BYTES`,
  kills git's whole process group (the overshoot is what arrives in one poll interval);
* `RLIMIT_FSIZE` caps every single file git writes (`fsize_exec`, an exec trampoline: no
  `preexec_fn` in this multithreaded app);
* `FETCH_TIMEOUT_S` caps the wall clock; a cancelled turn (Stop) kills it the same way.

Every exit (success, refusal, timeout, cancel) reaps the process group; a failed fetch removes its
folder at once and a fetched one is removed when the turn ends (`TurnRepos.close`).

**Reads come from git objects only** (`ls-tree`, `cat-file`) after the clone's remote is REMOVED,
so a blob the filter withheld is simply "missing": no lazy fetch can happen (also
`GIT_ALLOW_PROTOCOL=` empty and `GIT_NO_LAZY_FETCH=1`). Sizes are checked before content is
read, and the Library's own contract (`gallery/contract.py`) decides what a skill may hold.

Every subprocess is a literal argv; no shell, ever.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import re
import secrets
import select
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import fsize_exec, gitpanel, gitwrite

#: The watchdog's rejection threshold for everything the fetch writes (not a strict disk maximum:
#: what arrives within one poll interval may overshoot it before the kill).
MAX_REPO_BYTES = 32 * 1024 * 1024
#: `RLIMIT_FSIZE` on the fetch: no single file git writes may grow past this.
FSIZE_BYTES = 32 * 1024 * 1024
#: How often the watchdog measures the private folder.
POLL_S = 0.25
FETCH_TIMEOUT_S = 60.0
#: The filter asked of the server: no blob over the Library's own per-file cap.
BLOB_LIMIT = "128k"
#: Discovery bounds: tree entries walked, skills listed.
TREE_ENTRIES_MAX = 2_000
SKILLS_MAX = 50
#: Repositories one turn may hold, and fetches the whole app runs at once.
REPOS_PER_TURN = 3
CONCURRENT_FETCHES = 2
URL_MAX = 2048
REF_MAX = 200
#: A per-object-batch bound, so neither pipe can fill while the other is written (no deadlock).
_BATCH = 400
#: An orphaned folder (a crash mid-turn) older than this is removed by the next fetch.
ORPHAN_AGE_S = 3600
_REF = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._/-]{0,199}\Z")
_SHA = re.compile(r"\A[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
#: A URL in the Commander's words: from its scheme to the first space or delimiter.
_URL_IN_TEXT = re.compile(r"(?i)https?://[^\s<>\"'`()\[\]{}]+")
_TRAILING = ".,;:!?"
SCRIPT_EXTS = (".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".ps1")

_FETCHES = threading.BoundedSemaphore(CONCURRENT_FETCHES)


class Refused(Exception):
    """A refusal in the Commander's words, with a short code for the tool record."""

    def __init__(self, message: str, code: str = "refused"):
        super().__init__(message)
        self.code = code


class Cancelled(Exception):
    """The turn was stopped while the fetch ran: the fetch was killed and its folder removed."""


# ---- the link -----------------------------------------------------------------------------------


def normalize(url: str) -> str:
    """The comparison form: scheme and host lowercased, trailing ``/`` and ``.git`` dropped."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    authority, slash, path = rest.partition("/")
    out = f"{scheme.lower()}://{authority.lower()}{slash}{path}"
    while True:
        trimmed = out.rstrip("/")
        if trimmed.lower().endswith(".git"):
            trimmed = trimmed[:-4]
        if trimmed == out:
            return out
        out = trimmed


def typed_urls(text: str) -> set[str]:
    """Every complete URL token in the Commander's message, normalized."""
    found = set()
    for m in _URL_IN_TEXT.finditer(text or ""):
        token = m.group(0).rstrip(_TRAILING)
        if token:
            found.add(normalize(token))
    return found


def operator_url(url: object, question: str) -> str:
    """``url`` only if it IS a complete URL the Commander typed in this turn's message.

    Equality after the same normalization on both sides: a prefix, a substring, or a URL that
    appears only in tool content (a connector read, a fetched SKILL.md) is refused, so injected
    content can never choose a host for this fetch.
    """
    if not isinstance(url, str) or not url:
        raise Refused("url must be the repository link the Commander typed", "bad_arguments")
    if normalize(url.strip()) not in typed_urls(question):
        raise Refused(
            "refused: the url must be a link the Commander typed in this message, exactly as "
            "typed; ask them to paste the repository link",
            "not_typed",
        )
    return url.strip()


def check_url(url: str) -> str:
    """An anonymous https repository URL: no credential, query or fragment, no control chars."""
    if len(url) > URL_MAX or any(ord(c) < 0x21 or ord(c) == 0x7F for c in url):
        raise Refused("refused: that is not a repository link", "bad_url")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise Refused("refused: that is not a repository link", "bad_url") from None
    if parts.scheme.lower() != "https":
        raise Refused(
            "refused: only https repository links can be imported (no ssh, git:// or file "
            "links); a private repository goes through its connector",
            "bad_url",
        )
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise Refused(
            "refused: the link carries a sign-in; only public repositories can be imported",
            "bad_url",
        )
    if parts.query or parts.fragment or not parts.hostname or port == 0:
        raise Refused("refused: link a repository without ? or # parts", "bad_url")
    return url


#: Web-page links into a repository folder, by forge: the repository part, the ref, the folder.
_WEB_LINKS = (
    re.compile(
        r"\A(?P<repo>https://[^/]+/[^/]+/[^/]+?)/(?:tree|blob)/(?P<ref>[^/]+)(?:/(?P<path>.*))?\Z"
    ),
    re.compile(r"\A(?P<repo>https://[^/]+/.+?)/-/(?:tree|blob)/(?P<ref>[^/]+)(?:/(?P<path>.*))?\Z"),
    re.compile(
        r"\A(?P<repo>https://[^/]+/[^/]+/[^/]+?)/src/(?:branch|tag)/(?P<ref>[^/]+)(?:/(?P<path>.*))?\Z"
    ),
)


def split_web_link(url: str) -> tuple[str, str | None, str]:
    """``(repository URL, ref or None, folder)`` for a link to a folder on a forge's web page.

    The repository URL is the link's own prefix (same scheme and host), so nothing here reaches
    a destination the Commander did not type. A plain repository link returns it unchanged.
    """
    for pattern in _WEB_LINKS:
        m = pattern.match(url.rstrip("/"))
        if m:
            return m.group("repo"), m.group("ref"), (m.group("path") or "").strip("/")
    return url, None, ""


def check_ref(ref: object) -> str | None:
    if ref is None or ref == "":
        return None
    if (
        not isinstance(ref, str)
        or not _REF.fullmatch(ref)
        or ".." in ref
        or ref.endswith((".lock", "/"))
        or "//" in ref
    ):
        raise Refused("ref must be a branch or tag name", "bad_arguments")
    return ref


def display(url: str) -> str:
    """``host/owner/repo``: what the card's Source row shows."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.port and parts.port != 443:
        host = f"{host}:{parts.port}"
    return f"{host}{normalize(url).split('://', 1)[1][len(parts.netloc):]}"


# ---- the private folder -------------------------------------------------------------------------


def root() -> Path:
    home = os.environ.get("AGENT_SESSIONS_HOME") or "~/.local/share/agent-sessions"
    return Path(home).expanduser() / "skill-import"


def _new_workdir() -> Path:
    base = root()
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(base, 0o700)
    _sweep(base)
    work = base / f"fetch-{secrets.token_hex(8)}"
    work.mkdir(mode=0o700)  # FileExistsError on a collision: never reused
    (work / "home").mkdir(mode=0o700)
    return work


def _sweep(base: Path) -> None:
    """Remove folders a crashed process left behind: old enough that no live turn holds them."""
    now = time.time()
    with contextlib.suppress(OSError):
        for entry in os.scandir(base):
            if not entry.name.startswith("fetch-"):
                continue
            with contextlib.suppress(OSError):
                st = entry.stat(follow_symlinks=False)
                if now - st.st_mtime > ORPHAN_AGE_S and entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.path, ignore_errors=True)


def remove(work: Path) -> None:
    shutil.rmtree(work, ignore_errors=True)


def tree_bytes(path: Path) -> int:
    """Total size of every file under ``path`` (never following a link)."""
    total = 0
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError:
            continue
        with it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    else:
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    return total


# ---- git ----------------------------------------------------------------------------------------


def _env(work: Path, *, network: bool, allow_protocol: str = "") -> dict[str, str]:
    """The fetch's environment: gitwrite's allowlist env, isolated from every ambient config.

    ``HOME`` is the fetch's own EMPTY folder: no `~/.git-credentials`, no `~/.netrc` (which git's
    curl reads), no XDG config. Reads after the clone allow NO transport at all.
    """
    env = gitwrite._write_env(str(work))
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env.pop("SSH_AUTH_SOCK", None)
    env["HOME"] = str(work / "home")
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    env["GIT_NO_LAZY_FETCH"] = "1"
    env["GIT_ALLOW_PROTOCOL"] = allow_protocol if network else ""
    env["GIT_CEILING_DIRECTORIES"] = str(work)
    return env


def _base(exe: str) -> list[str]:
    """The playbook clone's own option prefix (hooks void, program keys reset)."""
    from .playbooks import repo_git

    return [*repo_git._base_argv(exe), "-c", "core.askPass="]


def _admit(url: str) -> gitwrite.Admission:
    """`admit_destination`'s refusals in the Commander's words; https was already required."""
    from .files import FsError

    try:
        admission = gitwrite.admit_destination(url, "skill repository")
    except (FsError, gitpanel.GitError) as e:
        text = gitwrite.redact(str(e))
        if "this machine" in text:
            raise Refused(
                "refused: that link points at this machine, which an import never reads",
                "local",
            ) from None
        if "could not be resolved" in text:
            raise Refused(
                "refused: that repository's host could not be found", "unresolved"
            ) from None
        raise Refused(f"refused: {text}", "refused") from None
    if admission.allow_protocol != "https":
        raise Refused("refused: only https repository links can be imported", "bad_url")
    return admission


def _too_large() -> Refused:
    mib = MAX_REPO_BYTES // (1024 * 1024)
    return Refused(
        f"refused: the repository is larger than {mib} MiB, so it was not read", "too_large"
    )


@dataclasses.dataclass
class Fetched:
    returncode: int
    stderr: str
    peak: int


def _watched(argv: list[str], env: dict, cwd: str, *, watch: Path, cancel) -> Fetched:
    """Run one fetch with its own process group, watched every `POLL_S`.

    Past `MAX_REPO_BYTES`, past `FETCH_TIMEOUT_S`, or when ``cancel`` is set, the WHOLE group is
    killed and reaped (git and every helper it started) before this returns or raises.
    """
    proc = subprocess.Popen(  # noqa: S603 - literal argv, no shell
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
        cwd=cwd,
        start_new_session=True,
    )
    err = bytearray()
    peak = 0
    deadline = time.monotonic() + FETCH_TIMEOUT_S
    stream = proc.stderr
    assert stream is not None
    os.set_blocking(stream.fileno(), False)
    eof = False
    try:
        while True:
            size = tree_bytes(watch)
            peak = max(peak, size)
            if size > MAX_REPO_BYTES:
                gitwrite._reap(proc)
                raise _too_large()
            if cancel is not None and cancel.is_set():
                gitwrite._reap(proc)
                raise Cancelled()
            if time.monotonic() > deadline:
                gitwrite._reap(proc)
                raise Refused(
                    f"refused: reading the repository took longer than {int(FETCH_TIMEOUT_S)} s",
                    "timeout",
                )
            if eof:
                time.sleep(POLL_S)  # stderr closed: select would return at once, every time
            else:
                ready, _, _ = select.select([stream], [], [], POLL_S)
                if ready:
                    with contextlib.suppress(BlockingIOError):
                        chunk = os.read(stream.fileno(), 4096)
                        eof = not chunk
                        if len(err) < 16384:
                            err += chunk
            if proc.poll() is not None:
                break
        # git exited: its group may still hold a helper it started, and nothing of it may outlive
        # the fetch (the reap also closes any stderr a helper still held).
        gitwrite._reap(proc)
        with contextlib.suppress(BlockingIOError, OSError):
            while len(err) < 16384 and (chunk := os.read(stream.fileno(), 4096)):
                err += chunk
    except BaseException:
        gitwrite._reap(proc)
        raise
    finally:
        stream.close()
    peak = max(peak, tree_bytes(watch))
    if peak > MAX_REPO_BYTES:
        # A fetch faster than one poll interval is judged by what it left: still refused.
        raise _too_large()
    return Fetched(proc.returncode, err.decode("utf-8", "replace"), peak)


def _fetch_refusal(stderr: str, returncode: int, ref: str | None) -> Refused:
    text = gitwrite.redact(stderr)
    low = text.lower()
    # SIGXFSZ on git itself, EFBIG reported, or index-pack (writing the pack) killed by the limit.
    if returncode in (-25, 153) or "file too large" in low or "invalid index-pack output" in low:
        return _too_large()
    if "redirect" in low or re.search(r"returned error: 30[12378]\b|\bhttp 30[12378]\b", low):
        return Refused(
            "refused: the server answered with a redirect to another address, which an import "
            "never follows; link the repository's final address",
            "redirect",
        )
    if "could not read username" in low or "authentication" in low or "403" in low:
        return Refused(
            "refused: that repository needs a sign-in; only public repositories can be "
            "imported (a private one goes through its connector)",
            "private",
        )
    if ref and ("remote branch" in low or "not found in upstream" in low):
        return Refused(f"refused: the repository has no branch or tag {ref!r}", "no_ref")
    if "not found" in low or "404" in low or "does not appear to be a git repository" in low:
        return Refused("refused: there is no git repository at that link", "not_found")
    if "transport" in low and "not allowed" in low:
        return Refused("refused: only https repository links can be imported", "bad_url")
    tail = " ".join(text.strip().splitlines()[-2:])[:300]
    return Refused(f"refused: the repository could not be read ({tail or 'git failed'})", "failed")


def _local(snap: Snapshot, args: list[str], *, stdin: bytes | None = None) -> bytes:
    """A read of the fetched repository's objects: no transport allowed, no working tree."""
    exe = gitpanel.git_bin()
    if not exe:
        raise Refused("git is not installed on this host", "not_available")
    argv = [*_base(exe), f"--git-dir={snap.gitdir}", *args]
    try:
        return gitwrite._run_argv(
            argv,
            _env(snap.work, network=False),
            str(snap.work),
            gitwrite.LOCAL_TIMEOUT_S,
            stdin=stdin,
        )
    except gitpanel.GitError as e:
        raise Refused(f"refused: the repository could not be read ({e})", "failed") from None


@dataclasses.dataclass
class Snapshot:
    """One repository, fetched at one commit, in a private folder this turn owns."""

    url: str
    commit: str
    work: Path
    peak: int
    filter_ignored: bool
    #: path → (mode, type, oid), at most `TREE_ENTRIES_MAX`.
    entries: dict[str, tuple[str, str, str]]
    truncated: bool
    _sizes: dict[str, int | None] = dataclasses.field(default_factory=dict)

    @property
    def gitdir(self) -> str:
        return str(self.work / "repo.git")

    def sizes(self, oids: list[str]) -> dict[str, int | None]:
        """Each blob's size, or None when the server withheld it (over the filter) or it is gone."""
        want = [o for o in dict.fromkeys(oids) if o not in self._sizes]
        for i in range(0, len(want), _BATCH):
            chunk = want[i : i + _BATCH]
            out = _local(
                self,
                ["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
                stdin=("\n".join(chunk) + "\n").encode(),
            )
            for line in out.decode("ascii", "replace").splitlines():
                parts = line.split()
                if len(parts) == 3 and parts[1] == "blob" and parts[2].isdigit():
                    self._sizes[parts[0]] = int(parts[2])
                elif parts:
                    self._sizes[parts[0]] = None
        return {o: self._sizes.get(o) for o in oids}

    def blob(self, oid: str) -> bytes:
        return _local(self, ["cat-file", "blob", oid])


def fetch(url: str, ref: str | None = None, *, cancel: threading.Event | None = None) -> Snapshot:
    """Fetch ``url`` (at ``ref``, else its default branch) into a private folder. Blocking.

    Raises :class:`Refused` (in words) or :class:`Cancelled`; either way nothing is left behind.
    """
    url = check_url(url)
    ref = check_ref(ref)
    admission = _admit(url)
    exe = gitpanel.git_bin()
    if not exe:
        raise Refused("git is not installed on this host", "not_available")
    if not _FETCHES.acquire(blocking=False):
        raise Refused("another repository is being read right now; try again in a moment", "busy")
    try:
        work = _new_workdir()
        try:
            return _fetch_into(work, exe, url, ref, admission, cancel)
        except BaseException:
            remove(work)
            raise
    finally:
        _FETCHES.release()


def _fetch_into(work, exe, url, ref, admission, cancel) -> Snapshot:
    clone = [
        *_base(exe),
        *admission.pins,
        "clone",
        "--bare",
        "--quiet",
        "--depth",
        "1",
        f"--filter=blob:limit={BLOB_LIMIT}",
        "--single-branch",
        "--no-tags",
        "--template=",
        "--upload-pack",
        "git-upload-pack",
        *(["--branch", ref] if ref else []),
        "--",
        url,
        str(work / "repo.git"),
    ]
    argv = fsize_exec.argv(sys.executable, FSIZE_BYTES, clone)
    env = _env(work, network=True, allow_protocol=admission.allow_protocol or "https")
    done = _watched(argv, env, str(work), watch=work, cancel=cancel)
    if done.returncode != 0:
        raise _fetch_refusal(done.stderr, done.returncode, ref)
    filter_ignored = "filtering not recognized" in done.stderr
    snap = Snapshot(url, "", work, done.peak, filter_ignored, {}, False)
    # STRUCTURAL: with no remote there is no promisor to lazily fetch a withheld blob from, so a
    # later read of one is "missing", never a second network request.
    _local(snap, ["remote", "remove", "origin"])
    commit = _local(snap, ["rev-parse", "--verify", "HEAD^{commit}"]).decode().strip()
    if not _SHA.fullmatch(commit):
        raise Refused("refused: the repository has no commit to read", "empty")
    snap.commit = commit
    out = _local(snap, ["ls-tree", "-r", "-z", "--full-tree", commit])
    records = out.split(b"\0")
    complete = records[:-1] if not out.endswith(b"\0") else records
    for raw in complete:
        if not raw:
            continue
        if len(snap.entries) >= TREE_ENTRIES_MAX:
            snap.truncated = True
            break
        meta, _, path = raw.partition(b"\t")
        fields = meta.decode("ascii", "replace").split()
        if len(fields) != 3:
            continue
        snap.entries[path.decode("utf-8", "surrogateescape")] = (fields[0], fields[1], fields[2])
    if len(out) >= gitwrite._MAX_STDOUT or not out.endswith(b"\0") and out:
        snap.truncated = True
    return snap


# ---- what the repository holds ------------------------------------------------------------------


def _front_matter(text: str) -> dict[str, str]:
    """``name`` and ``description`` from a SKILL.md front matter (simple `key: value` lines)."""
    out: dict[str, str] = {}
    if not text.startswith("---"):
        return out
    for line in text.split("\n")[1:60]:
        if line.strip() == "---":
            break
        k, sep, v = line.partition(":")
        if sep and k.strip() in ("name", "description") and v.strip():
            out[k.strip()] = v.strip().strip("\"'")
    return out


def _under(path: str, folder: str) -> str | None:
    if not folder:
        return path
    return path[len(folder) + 1 :] if path.startswith(folder + "/") else None


def skill_dirs(snap: Snapshot) -> list[str]:
    """Folders holding a `SKILL.md` blob, shallowest first (``""`` is the repository root)."""
    dirs = []
    for path, (mode, kind, _oid) in snap.entries.items():
        if (
            kind == "blob"
            and mode != "120000"
            and (path == "SKILL.md" or path.endswith("/SKILL.md"))
        ):
            dirs.append(path[: -len("SKILL.md")].rstrip("/"))
    return sorted(dirs, key=lambda d: (d.count("/") if d else -1, d))


def listing(snap: Snapshot, within: str = "") -> dict:
    """What the repository holds: each skill folder with its name, description, file count and
    size, at the pinned commit. Bounded (`SKILLS_MAX`); anything beyond is said to be cut."""
    dirs = [d for d in skill_dirs(snap) if not within or d == within or d.startswith(within + "/")]
    shown = dirs[:SKILLS_MAX]
    files_of = {
        d: [(p, e) for p, e in snap.entries.items() if _under(p, d) is not None] for d in shown
    }
    sizes = snap.sizes([e[2] for rows in files_of.values() for _p, e in rows if e[1] == "blob"])
    skills = []
    for d in shown:
        rows = files_of[d]
        md = snap.entries.get(f"{d}/SKILL.md" if d else "SKILL.md")
        meta: dict[str, str] = {}
        if (
            md is not None
            and (sizes.get(md[2]) or 0) <= 128 * 1024
            and sizes.get(md[2]) is not None
        ):
            meta = _front_matter(snap.blob(md[2]).decode("utf-8", "replace"))
        skills.append(
            {
                "path": d,
                "name": meta.get("name") or (d.rsplit("/", 1)[-1] if d else "(repository root)"),
                "description": meta.get("description", ""),
                "files": len(rows),
                "bytes": sum(sizes.get(e[2]) or 0 for _p, e in rows if e[1] == "blob"),
            }
        )
    top = sorted({p.split("/", 1)[0] + ("/" if "/" in p else "") for p in snap.entries})[:20]
    return {
        "skills": skills,
        "more_skills": max(0, len(dirs) - len(shown)),
        "tree_cut": snap.truncated,
        "top_level": top,
    }


@dataclasses.dataclass
class Read:
    """One skill folder read for import: its files when every one can be stored, the rows the card
    shows, and every reason it cannot be saved (empty when it can)."""

    files: dict[str, str]
    rows: list[dict]
    reasons: list[str]


def _size_said(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB"


def read_skill(snap: Snapshot, folder: str) -> Read:
    """The exact bytes of one skill folder, checked against the Library's contract BEFORE content
    is read: type and size from the object store first, then the text rules. Every reason is
    collected (binary, over the size cap, a path or link the Library can't store), so a refusal
    names them all."""
    from .gallery import contract

    if folder not in skill_dirs(snap):
        raise Refused(
            "refused: path must be a skill folder inspect_skill_repo listed for this source",
            "unknown_path",
        )
    if snap.truncated:
        raise Refused(
            "refused: the repository is too large to list completely, so a folder in it can't "
            "be imported whole",
            "tree_cut",
        )
    members = sorted(
        (rel, e) for p, e in snap.entries.items() if (rel := _under(p, folder)) is not None
    )
    sizes = snap.sizes([e[2] for _r, e in members if e[1] == "blob"])
    files: dict[str, str] = {}
    rows: list[dict] = []
    reasons: list[str] = []
    total = 0
    if len(members) > contract.MAX_FILES:
        reasons.append(
            f"it has {len(members)} files (the Library takes at most {contract.MAX_FILES})"
        )
    for rel, (mode, kind, oid) in members:
        row: dict = {"path": rel, "bytes": None, "lines": None, "flag": ""}
        rows.append(row)
        try:
            contract.path(rel)
        except contract.GalleryError:
            row["flag"] = "path"
            reasons.append(f"{rel} has a path the Library can't store (depth, .git, .. or :)")
            continue
        if kind != "blob":
            row["flag"] = "submodule"
            reasons.append(f"{rel} is a submodule, not a file")
            continue
        if mode == "120000":
            row["flag"] = "link"
            reasons.append(f"{rel} is a symbolic link")
            continue
        size = sizes.get(oid)
        row["bytes"] = size
        if size is None or size > contract.MAX_FILE_BYTES:
            row["flag"] = "oversize"
            said = "over 128 KB" if size is None else f"{_size_said(size)}"
            reasons.append(f"{rel} is {said} (limit 128 KB)")
            continue
        total += size
        if total > contract.MAX_CONTENT_BYTES:
            row["flag"] = "oversize"
            reasons.append(f"the skill is over {contract.MAX_CONTENT_BYTES // 1024} KB in total")
            continue
        data = snap.blob(oid)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is None or any((ord(c) < 32 and c not in "\t\n\r") or ord(c) == 127 for c in text):
            row["flag"] = "binary"
            reasons.append(f"{rel} is binary")
            continue
        row["lines"] = text.count("\n") + (0 if text.endswith("\n") else 1)
        if mode == "100755" or text.startswith("#!") or rel.lower().endswith(SCRIPT_EXTS):
            row["flag"] = "script"
        files[rel] = text
    if not reasons:
        try:
            from .gallery import files as gallery_files

            gallery_files.tree(files)
        except (contract.GalleryError, ValueError) as e:
            reasons.append(str(e).rstrip("."))
    seen: set[str] = set()
    reasons = [r for r in reasons if not (r in seen or seen.add(r))]
    return Read(files if not reasons else {}, rows, reasons)


# ---- a turn's repositories ----------------------------------------------------------------------


class TurnRepos:
    """The repositories THIS turn fetched, by the `skillsrc:` refs it minted: the only sources
    `skill.import` may name. Removed, folders and all, when the turn ends."""

    def __init__(self) -> None:
        self._repos: dict[str, Snapshot] = {}
        self._lock = threading.Lock()
        self.closed = False

    def full(self) -> bool:
        with self._lock:
            return len(self._repos) >= REPOS_PER_TURN

    def add(self, snap: Snapshot) -> str:
        with self._lock:
            if self.closed:
                remove(snap.work)
                raise Refused("refused: this turn has ended", "closed")
            ref = f"skillsrc:{len(self._repos) + 1}"
            self._repos[ref] = snap
            return ref

    def get(self, ref: object) -> Snapshot | None:
        with self._lock:
            return self._repos.get(ref) if isinstance(ref, str) else None

    def close(self) -> None:
        with self._lock:
            self.closed = True
            repos, self._repos = list(self._repos.values()), {}
        for snap in repos:
            remove(snap.work)


__all__ = [
    "Cancelled",
    "Read",
    "Refused",
    "Snapshot",
    "TurnRepos",
    "check_url",
    "display",
    "fetch",
    "listing",
    "normalize",
    "operator_url",
    "read_skill",
    "split_web_link",
]
