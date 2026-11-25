"""Playbooks and git (#1196): clone into a new folder, seed a new repository, bind content.

Nothing here invents git hardening; it reuses `gitwrite.py`'s (#806, #863 §5):

* the `GIT_ALLOW_PROTOCOL` allowlist (https and ssh; `file` and `git` are absent), so a remote
  that is a path on this machine is refused by git itself;
* `core.hooksPath` pointed at the verified empty hooks-void directory, and every repo-config key
  that names a program reset on the command line;
* the URL resolved ONCE, refused if it names this machine (`admit_destination`, which also
  refuses a credential in the URL and pins the resolved addresses), and that exact string handed
  to git;
* `--no-recurse-submodules`, so a `.gitmodules` URL (a local path, a loopback ssh URL) is never
  fetched by a clone;
* `redact()` on everything git prints (`gitwrite._run_argv` applies it);
* a FRESH, EMPTY destination under the configured project roots, and no system or global Git
  config during clone or checkout: a URL rewrite cannot bypass admission, and no operator-level
  filter driver exists for repository content to select. SSH config and its agent socket remain
  available independently, with the admitted SSH command and address pins unchanged.

**A clone never runs a smudge filter.** The repository's `.gitattributes` choose which configured
driver runs on which file, so a checkout with the operator's global drivers active lets REPOSITORY
CONTENT drive them: measured, a hostile `* filter=lfs` plus a pointer file and an `.lfsconfig`
URL made git-lfs POST to a loopback listener during checkout, past the admission pins, the
protocol allowlist and the redirect policy (and `required=true` then left a non-empty folder).
So the clone is `--no-checkout`, and the checkout reads NO system or global config
(`GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`): no operator-level driver exists for it,
and a fresh clone's own config defines none. As defence in depth, any driver that isolated config
still lists is voided on the command line (a truncated listing refuses), and
`GIT_LFS_SKIP_SMUDGE=1` is set. The worktree holds the committed bytes (an LFS file stays its
pointer).

**A clone never writes into the destination.** It clones and checks out into a private 0700
staging sibling, then `rename(2)`s that onto the destination, which lands only on an absent name
or an empty directory; anything written there meanwhile makes the clone refuse and stays put. A
failure removes the staging folder, never anything inside a folder this clone did not create.

What is NOT claimed: the url-specific `http.<url>.sslVerify` residual stays #842's (see
`docs/invariants/playbook-git.md`).

Every subprocess call is a literal argv list through `gitwrite._run_argv`; no shell, ever.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import re
import secrets
import shutil
import stat

from .. import gitpanel, gitwrite, template_vars
from ..files import FsError
from ..fsbrowse import home_root
from . import apply, destination, store
from . import deployment_state as state

#: A clone talks to a remote and writes a whole worktree: longer than one fetch, still bounded.
CLONE_TIMEOUT_S = 300.0
URL_MAX = 2048
_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
SYMLINK = "120000"
REGULAR = "100644"


def only(body: object, allowed: set[str], required: set[str], what: str) -> dict:
    if not isinstance(body, dict):
        raise store.StoreError(f"{what} must be a JSON object")
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise store.StoreError(f"{what} does not take {', '.join(unknown)}")
    missing = sorted(required - set(body))
    if missing:
        raise store.StoreError(f"{what} needs {', '.join(missing)}")
    return body


@contextlib.contextmanager
def git_errors():
    """`FsError` / `GitError` → the playbook routes' one refusal type, message redacted."""
    try:
        yield
    except store.StoreError:
        raise
    except (FsError, gitpanel.GitError) as e:
        status = getattr(e, "status", None) or 409
        raise store.StoreError(gitwrite.redact(str(e)), status=status) from None


def _base_argv(exe: str) -> list[str]:
    """`gitwrite._base_argv` without a repository: there is none yet for clone and init."""
    return [
        exe,
        "-c",
        f"core.hooksPath={gitwrite.hooks_void()}",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.sshCommand=ssh",
        "-c",
        "credential.helper=",
        "-c",
        "core.gitProxy=",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "gpg.program=false",
        "-c",
        "http.sslVerify=true",
    ]


def _run_unbound(
    args: list[str],
    cwd: str,
    *,
    timeout: float,
    admission: gitwrite.Admission | None = None,
    isolated_config: bool = False,
    require_complete: bool = False,
) -> str:
    exe = gitpanel.git_bin()
    if not exe:
        raise store.StoreError("git is not installed on this host", status=501)
    env = gitwrite._write_env(home_root())
    # No repository yet: an empty GIT_DIR / GIT_WORK_TREE must not be read as one.
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    # Belt for the voided smudge drivers (`_filter_voids`): git-lfs itself skips the download.
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    if isolated_config:
        # STRUCTURAL: no system or global (incl. XDG) Git config is read, so a clone cannot
        # rewrite its admitted URL and a checkout cannot select operator-level filter drivers.
        # Admission pins ride on `-c` and the environment. SSH still reads its own config and
        # uses SSH_AUTH_SOCK; neither depends on Git config.
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
    pins: list[str] = []
    if admission is not None:
        pins = list(admission.pins)
        if admission.ssh_command:
            env["GIT_SSH_COMMAND"] = admission.ssh_command
        if admission.allow_protocol:
            env["GIT_ALLOW_PROTOCOL"] = admission.allow_protocol
    argv = [*_base_argv(exe), *pins, *args]
    try:
        out = gitwrite._run_argv(argv, env, cwd, timeout, require_complete=require_complete)
        return out.decode("utf-8", "replace")
    except gitpanel.GitError as e:
        raise gitwrite._refused_transport(e) from None


def _check_url(raw: object) -> str:
    if not isinstance(raw, str) or not raw or len(raw) > URL_MAX:
        raise store.StoreError("url must be a git remote URL")
    if raw.startswith("-") or any(ord(c) < 0x21 or ord(c) == 0x7F for c in raw):
        raise store.StoreError("url must be a git remote URL without spaces or control characters")
    return raw


def _parent_folder(raw: object) -> str:
    if not isinstance(raw, str) or not os.path.isabs(raw) or "\x00" in raw:
        raise store.StoreError("parent must be an absolute folder")
    real = os.path.realpath(raw)
    if real != os.path.normpath(raw):
        raise store.StoreError("parent may not pass through a symbolic link", status=409)
    with git_errors():
        destination._scope(real)
    if not os.path.isdir(real):
        raise store.StoreError("parent is not a folder", status=409)
    return real


def _folder_name(raw: object) -> str:
    if not isinstance(raw, str) or not _NAME.fullmatch(raw) or raw.lower() == ".git":
        raise store.StoreError("name must be one folder name (letters, digits, . _ -)")
    return raw


def _empty_destination(parent: str, name: str) -> str:
    """`parent/name` must be absent, or an existing EMPTY real directory of ours.

    Checked only to refuse early with a clear message; nothing is created or written there. The
    clone happens in a private staging folder, and the final `rename(2)` is what decides: it
    lands only on an absent name or an empty directory, atomically.
    """
    pfd = os.open(parent, _DIR_FLAGS)
    try:
        try:
            fd = os.open(name, _DIR_FLAGS, dir_fd=pfd)
        except FileNotFoundError:
            return os.path.join(parent, name)
        except OSError:
            raise store.Conflict("the destination exists and is not a folder") from None
        try:
            if os.fstat(fd).st_uid != os.geteuid():
                raise store.Conflict("the destination folder is not yours")
            if os.listdir(fd):
                raise store.Conflict(
                    "the destination folder is not empty; a clone needs a fresh, empty folder"
                )
            return os.path.join(parent, name)
        finally:
            os.close(fd)
    finally:
        os.close(pfd)


def _staging(parent: str) -> tuple[str, tuple[int, int]]:
    """A private (0700), uniquely named sibling folder this clone alone writes into."""
    pfd = os.open(parent, _DIR_FLAGS)
    try:
        name = f".battlelab-clone-{secrets.token_hex(8)}"
        os.mkdir(name, 0o700, dir_fd=pfd)  # FileExistsError on a collision: never reused
        st = os.stat(name, dir_fd=pfd, follow_symlinks=False)
        return name, (st.st_dev, st.st_ino)
    finally:
        os.close(pfd)


def _remove_staging(parent: str, name: str, identity: tuple[int, int]) -> None:
    """Remove the staging folder, and only it: never anything inside the destination.

    Only while the name is still the directory this clone created (same inode); removal is
    descriptor-relative and never follows a symlink (`shutil.rmtree(dir_fd=...)`).
    """
    with contextlib.suppress(OSError):
        pfd = os.open(parent, _DIR_FLAGS)
        try:
            st = os.stat(name, dir_fd=pfd, follow_symlinks=False)
            if (st.st_dev, st.st_ino) == identity and stat.S_ISDIR(st.st_mode):
                shutil.rmtree(name, dir_fd=pfd)
        finally:
            os.close(pfd)


def _filter_voids(target: str) -> list[str]:
    """`-c` arguments that void every filter driver the isolated checkout could still see.

    Defence in depth under the structural control (the checkout reads no system or global config,
    `isolated_config`): enumerated from `git config --list` under that SAME isolation, so it
    covers the fresh clone's own config and nothing is assumed. A truncated listing refuses
    (`require_complete`) rather than leaving a driver it did not see armed. An empty
    `smudge`/`process` makes git skip the driver and `required=false` stops that skip being an
    error. A driver name `-c` cannot express is refused rather than left armed.
    """
    listed = _run_unbound(
        ["config", "--null", "--list"],
        target,
        timeout=gitwrite.LOCAL_TIMEOUT_S,
        isolated_config=True,
        require_complete=True,
    )
    drivers: set[str] = set()
    for entry in listed.split("\0"):
        key = entry.split("\n", 1)[0]
        if not key[:7].lower() == "filter.":
            continue
        driver, dot, _var = key[7:].rpartition(".")
        if not dot or not driver:
            continue  # `filter.<var>` with no driver name binds nothing
        if "=" in driver or any(ord(c) < 0x20 or ord(c) == 0x7F for c in driver):
            raise store.Conflict(
                "a configured git filter driver has a name a clone cannot switch off; nothing "
                "was checked out"
            )
        drivers.add(driver)
    out: list[str] = []
    for driver in sorted(drivers):
        for var, value in (("smudge", ""), ("process", ""), ("required", "false")):
            out += ["-c", f"filter.{driver}.{var}={value}"]
    return out


def clone(body: object) -> dict:
    """`{url, parent, name}` → clone into a fresh empty `parent/name` under the project roots.

    The whole clone and checkout happen in a private staging sibling; only a complete clone is
    `rename(2)`d onto the destination, which succeeds only while the destination is absent or an
    empty directory. Anything that appeared there meanwhile makes the rename fail and the clone
    refuse, with the destination untouched. A failure removes the staging folder and nothing else.
    """
    body = only(body, {"url", "parent", "name"}, {"url", "parent", "name"}, "a clone")
    url = _check_url(body["url"])
    parent = _parent_folder(body["parent"])
    name = _folder_name(body["name"])
    if gitwrite.url_host(url) is None and "file" not in gitwrite.GIT_ALLOW_PROTOCOL.split(":"):
        # A path on this machine. git's allowlist refuses it too; saying so first means nothing
        # on the filesystem is touched, and a missing path is not reported as "does not exist".
        raise store.StoreError(
            "a clone needs a network remote (https or ssh), not a folder on this machine",
            status=403,
        )
    with git_errors():
        destination._scope(os.path.join(parent, name))
        # Resolved ONCE: refused when it names this machine or carries a credential, and the
        # addresses checked are the addresses git is pinned to.
        admission = gitwrite.admit_destination(url, "clone")
        target = _empty_destination(parent, name)
        staged, identity = _staging(parent)
    try:
        with git_errors():
            out = _clone_into(url, parent, staged, identity, admission)
            _publish_clone(parent, staged, identity, name)
    except BaseException:
        _remove_staging(parent, staged, identity)
        raise
    return {"path": target, **out}


def _clone_into(
    url: str, parent: str, staged: str, identity: tuple[int, int], admission: gitwrite.Admission
) -> dict:
    work = os.path.join(parent, staged)
    _run_unbound(
        [
            "clone",
            "--no-recurse-submodules",
            # No worktree yet: the checkout below runs with every smudge driver unreachable.
            "--no-checkout",
            # No template directory: nothing from a system template lands in the new gitdir.
            "--template=",
            # On the command line, for the same measured reason fetch pins it.
            "--upload-pack",
            "git-upload-pack",
            "--",
            url,
            work,
        ],
        parent,
        timeout=max(CLONE_TIMEOUT_S, gitwrite.NET_TIMEOUT_S),
        admission=admission,
        isolated_config=True,
    )
    _same_dir(work, identity)
    repo = gitwrite.resolve_repo(work)
    if os.path.realpath(repo.toplevel) != work:
        raise store.StoreError("the clone did not produce its own repository", status=409)
    head = gitwrite._current_head(repo)
    if head:
        _run_unbound(
            [*_filter_voids(work), "checkout", "--quiet", "--force"],
            work,
            timeout=CLONE_TIMEOUT_S,
            isolated_config=True,
        )
    _same_dir(work, identity)
    return {"branch": gitwrite._head_branch(repo), "head": head or None}


def _same_dir(path: str, identity: tuple[int, int]) -> None:
    st = os.stat(path, follow_symlinks=False)
    if (st.st_dev, st.st_ino) != identity:
        raise store.StoreError("the clone's staging folder was replaced while cloning", status=409)


def _publish_clone(parent: str, staged: str, identity: tuple[int, int], name: str) -> None:
    """Give the clone its name: one `rename(2)`, refused when the destination is not empty."""
    pfd = os.open(parent, _DIR_FLAGS)
    try:
        fd = os.open(staged, _DIR_FLAGS, dir_fd=pfd)
        try:
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) != identity:
                raise store.StoreError(
                    "the clone's staging folder was replaced while cloning", status=409
                )
            os.fchmod(fd, 0o755)  # private while it was being written, ordinary once it is done
        finally:
            os.close(fd)
        try:
            os.rename(staged, name, src_dir_fd=pfd, dst_dir_fd=pfd)
        except OSError as e:
            if e.errno in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOTDIR, errno.EISDIR):
                raise store.Conflict(
                    "something was written to the destination while cloning; it was left "
                    "untouched and nothing was cloned into it"
                ) from None
            raise
    finally:
        os.close(pfd)


# --- content binding --------------------------------------------------------------------------


def blob_oid(data: bytes, fmt: str) -> str:
    """The git blob id of exactly these bytes (no filters): what a clean tree must carry."""
    h = hashlib.new(fmt)
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def object_format(repo: gitpanel.Repo) -> str:
    fmt = gitwrite.run_git_write(repo, ["rev-parse", "--show-object-format"]).strip()
    if fmt not in ("sha1", "sha256"):
        raise store.StoreError(f"unknown git object format {fmt!r}", status=409)
    return fmt


def deployment(pid: str) -> dict:
    """The applied deployment record with its applied-path map, or a refusal."""
    with state.locked(pid) as locked:
        record = locked.read() if locked is not None else None
    if record is None or record.get("state") != "applied":
        raise store.Conflict("the project has no applied playbook deployment")
    if apply.pending(record):
        raise store.Conflict("retry the pending apply operation first")
    applied = record.get("applied_paths")
    if not isinstance(applied, dict) or not isinstance(applied.get("paths"), dict):
        raise store.Conflict(
            "this deployment was applied before BattleLab recorded what it wrote; apply it "
            "again, then publish"
        )
    return record


def project_repo(record: dict, *, required: bool = True) -> gitpanel.Repo | None:
    folder = record["destination"]["path"]
    with git_errors():
        repo = gitpanel.discover_repo(gitwrite.contained_path(folder))
    if repo is None:
        if required:
            raise store.Conflict("the project folder is not a git repository yet; seed it first")
        return None
    if os.path.realpath(repo.toplevel) != os.path.realpath(folder):
        raise store.Conflict(
            "the project folder is inside another git repository, not the root of its own"
        )
    return repo


def _seed(outcome: object) -> bool:
    return isinstance(outcome, dict) and outcome.get("kind") == "seed"


def _live_entry(node, fmt: str) -> tuple[str, str] | None | bool:
    """`(mode, blob)` of a live node, None when absent, False for anything git cannot hold."""
    if node.kind == "absent":
        return None
    if node.kind == "symlink":
        return (SYMLINK, blob_oid(node.target.encode("utf-8"), fmt))
    if node.kind == "file" and node.data is not None:
        return (REGULAR, blob_oid(node.data, fmt))
    return False


def _matches(node, outcome: dict | None) -> bool:
    """Do the live bytes still equal what the apply record says the playbook wrote?"""
    if outcome is None:
        return node.kind == "absent"
    if outcome.get("kind") == "symlink":
        return node.kind == "symlink" and node.target == outcome.get("target")
    return (
        node.kind == "file"
        and node.data is not None
        and hashlib.sha256(node.data).hexdigest() == outcome.get("digest")
    )


def _recorded_at(repo: gitpanel.Repo, outcome: dict | None, entry: tuple[str, str] | None) -> bool:
    """Does `base` already carry exactly what the apply record says the playbook wrote?"""
    if outcome is None or entry is None:
        return outcome is None and entry is None
    if outcome.get("kind") == "symlink":
        if entry[0] != SYMLINK:
            return False
        want = outcome.get("target", "").encode("utf-8")
    else:
        if entry[0] == SYMLINK or entry[0] == "160000":
            return False
        want = None
    with git_errors():
        data = gitwrite.run_git_bytes(repo, ["cat-file", "blob", entry[1]], require_complete=True)
    if want is not None:
        return data == want
    return hashlib.sha256(data).hexdigest() == outcome.get("digest")


def bound_content(
    record: dict, repo: gitpanel.Repo, base: str, *, seeds: bool = False
) -> dict[str, tuple[str, str] | None]:
    """`{path: (mode, blob) | None}` for every applied path whose content `base` lacks.

    Read through `destination.snapshot` (descriptor-relative, no symlink followed, bounded, the
    project roots re-checked). Only a path that is actually TO BE COMMITTED is bound: its live
    bytes must still hash to the apply record's digest, or the publication refuses (409, with
    `paths`) and asks for a new review, never silently committing the operator's newer bytes. A
    path whose recorded content `base` already carries is not committed, so an edit there (say,
    outside a managed region) blocks nothing. `base` empty means an unborn repository.

    Seeds (`{"kind": "seed"}`) are the project's: with `seeds` (the first seed commit only) they
    are taken as they are now, when present; otherwise they are never read or bound.
    """
    applied = record["applied_paths"]["paths"]
    names = sorted(p for p, o in applied.items() if seeds or not _seed(o))
    folder = destination.Folder(**record["destination"])
    with git_errors():
        live = destination.snapshot(folder, names)
        fmt = object_format(repo)
        at_base = gitwrite._head_entries(repo, names, rev=base) if base else dict.fromkeys(names)
    out: dict[str, tuple[str, str] | None] = {}
    drift: list[str] = []
    for path in names:
        outcome, node = applied[path], live[path]
        entry = _live_entry(node, fmt)
        if _seed(outcome):
            if entry and not same_entry(entry, at_base[path]):
                out[path] = entry
            continue
        if _matches(node, outcome) and entry is not False:
            if not same_entry(entry, at_base[path]):
                out[path] = entry
        elif not _recorded_at(repo, outcome, at_base[path]):
            drift.append(path)
    if drift:
        raise store.Conflict(
            f"{len(drift)} path(s) changed after the playbook applied them, so nothing is "
            "committed; review the update again",
            paths=drift,
        )
    return out


def same_entry(a: tuple[str, str] | None, b: tuple[str, str] | None) -> bool:
    if a is None or b is None:
        return a is b
    return a[1] == b[1] and (a[0] == SYMLINK) == (b[0] == SYMLINK)


def commit_bound(
    repo: gitpanel.Repo,
    record: dict,
    names: list[str],
    expected: dict[str, tuple[str, str] | None],
    head: str,
    message: str,
    *,
    seeds: bool = False,
) -> dict:
    """Commit exactly `names` through the path-commit machinery, content-bound.

    The row fingerprints are read fresh, then the content is re-bound AFTER that read (so the
    fingerprinted bytes are the bound bytes), and `git_commit_paths` is handed both: its private
    index, compare-and-swap publication under `index.lock`, ref fences and `index: "pending"`
    outcome apply unchanged, and its snapshot must carry exactly the bound blob ids.
    """
    with git_errors():
        rows = {e.get("path"): e.get("fp") for e in gitwrite._fresh_status(repo)["entries"]}
    missing = [n for n in names if not rows.get(n)]
    if missing:
        raise store.Conflict(
            f"git does not report {missing[0]!r} as changed, so it cannot be committed by path",
            paths=missing,
        )
    again = bound_content(record, repo, head, seeds=seeds)
    if any(n not in again or not same_entry(again[n], expected[n]) for n in names):
        raise store.Conflict("an applied path changed while publishing; review it again")
    with git_errors():
        return gitwrite.git_commit_paths(
            repo.toplevel,
            message,
            names,
            expect={n: rows[n] for n in names},
            head=head,
            blobs={n: expected[n] for n in names},
        )


# --- seed -------------------------------------------------------------------------------------


def seed(pid: str, body: object) -> dict:
    """`git init` the applied project folder and commit exactly the playbook's paths.

    Retry-safe: an already-initialised folder with no commit continues to the first commit, and
    one whose only commit is exactly the seed answers with it; anything else is refused.
    """
    body = only(body, {"branch", "message"}, set(), "a seed")
    record = deployment(template_vars.project_id(pid))
    branch = gitwrite.validate_ref(body.get("branch", "main"))
    playbook = record["playbook_id"]
    version = record["applied_paths"]["version"]
    message = body.get("message", f"chore: seed from playbook {playbook} v{version}")
    folder = record["destination"]["path"]
    with git_errors(), destination.open_folder(destination.Folder(**record["destination"])):
        pass  # the deployed folder is still the reviewed inode, inside the project roots
    repo = project_repo(record, required=False)
    if repo is None:
        with git_errors():
            st = os.lstat(os.path.join(folder, ".git")) if _exists(folder, ".git") else None
            if st is not None:
                raise store.Conflict("the project folder has a .git entry git does not accept")
            _run_unbound(
                ["init", "--quiet", "--template=", f"--initial-branch={branch}", "--", folder],
                folder,
                timeout=gitwrite.LOCAL_TIMEOUT_S,
            )
        repo = project_repo(record)
        assert repo is not None
    with git_errors():
        head = gitwrite._current_head(repo)
    if head:
        return _adopt_seed(repo, record, head)
    expected = bound_content(record, repo, "", seeds=True)
    names = sorted(n for n, e in expected.items() if e is not None)
    if not names:
        raise store.Conflict("the deployment wrote nothing to commit")
    result = commit_bound(repo, record, names, expected, "", message, seeds=True)
    return {"seeded": True, **result}


def _exists(folder: str, name: str) -> bool:
    try:
        os.lstat(os.path.join(folder, name))
        return True
    except FileNotFoundError:
        return False


def _adopt_seed(repo: gitpanel.Repo, record: dict, head: str) -> dict:
    """A repository that already has commits is the seed only if its one commit is exactly it.

    Exactly: one root commit whose tree is the playbook's recorded content (a seed may be
    present or not, with any bytes, since it is the project's).
    """
    with git_errors():
        parents = gitwrite.run_git_write(repo, ["rev-list", "--parents", "-n", "1", head]).split()
        listed = gitwrite.run_git_write(repo, ["ls-tree", "-r", "-z", "--full-tree", head])
    tree = set()
    for item in listed.split("\0"):
        if item:
            tree.add(item.partition("\t")[2])
    applied = record["applied_paths"]["paths"]
    managed = {p for p, o in applied.items() if o is not None and not _seed(o)}
    seeds = {p for p, o in applied.items() if _seed(o)}
    if len(parents) != 1 or not managed <= tree or tree - managed - seeds:
        raise store.Conflict("the project folder is already a repository with other history")
    if bound_content(record, repo, head):
        raise store.Conflict("the project folder is already a repository with other content")
    return {"seeded": False, "sha": head, "commit": head[:7], "paths": sorted(tree)}
