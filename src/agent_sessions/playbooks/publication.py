"""Land a playbook deployment in its repository (#1196): create a forge remote, or publish an
update as a branch and a pull request. Resumable, never duplicating, never deleting.

Two operations, each a PLAN (a read that shows the target and returns a keyed digest) and an
EFFECT that accepts only that digest, recomputed at the call:

* **remote** — create `owner/name` on the project's `forge` connection, add it as a remote (its
  ssh address at the forge's own advertised endpoint, settled in the plan) and push the seeded
  branch. An existing name is refused. There is no delete path anywhere.
* **update** — commit exactly the applied paths, content-bound to the apply record, on a new
  branch `playbook/<id>-v<version>` created at the APPROVED BASE (the remote default branch head
  read at plan time; HEAD must be that commit, so no unpushed work can ride along), push that
  exact commit to the displayed destination through the existing push path, then open (or adopt)
  the pull request through the forge connection.

**The operation record** (`.publications/<project>/<operation>.json`, private, 0600) is written
before every external effect and after each one: whether this operation created the repository
(proved by a random marker it put in the description BEFORE asking, never by the name alone), the
commit, the branch, whether it was pushed, and the pull request. A same-id retry continues from
what is recorded, so a lost response never duplicates a repository, a commit or a pull request:
a push that landed but whose PR failed creates only the PR; an existing PR for the branch is
adopted; an existing branch with other content is refused and never force-pushed.

**Credentials.** The forge token is a secret variable binding, resolved from the scoped store at
the moment of each forge call and sent as a header (`forge_write`). It is never written into a
remote URL, a record or a response. A forge URL that came from a bundle default is refused: the
operator binds it.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import re
import secrets
import stat
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

from .. import atomicjson, forge_write, gitpanel, gitwrite, template_vars
from ..files import FsError
from . import deployment_state as state
from . import lifecycle, repo_git, review, store
from .repo_git import git_errors, only

DIRECTORY = ".publications"
_JOURNAL_MAX = 256 * 1024
_REPO_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
_PLAN_INPUTS = {"connection", "remote", "forge_kind"}
_REMOTE_INPUTS = {"connection", "owner", "name", "private", "remote", "forge_kind"}
_STAMP = "battlelab playbook publication"


# --- the operation record ---------------------------------------------------------------------


@contextlib.contextmanager
def _locked(pid: str) -> Iterator[int]:
    """The project's publication directory, exclusively locked for the whole operation."""
    created = store._open_root(create=True)
    if created is not None:
        os.close(created)
    with store.root_lock(exclusive=False) as root:
        if root is None:
            raise store.StoreError("the playbook store is unavailable", status=503)
        top = state._directory(root, DIRECTORY, True)
        try:
            fd = state._directory(top, pid, True)
        finally:
            os.close(top)
    try:
        with store._flock(fd, exclusive=True, wait=store.LOCK_WAIT_S):
            yield fd
    finally:
        os.close(fd)


def _read(fd: int, op: str) -> dict | None:
    try:
        source = os.open(
            f"{op}.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd
        )
    except FileNotFoundError:
        return None
    try:
        st = os.fstat(source)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_nlink != 1
            or st.st_uid != os.geteuid()
            or stat.S_IMODE(st.st_mode) != 0o600
            or st.st_size > _JOURNAL_MAX
        ):
            raise store.Conflict("the publication record is not a bounded private file")
        value = json.loads(os.read(source, _JOURNAL_MAX + 1))
    except (UnicodeError, ValueError):
        raise store.Conflict("the publication record is damaged") from None
    finally:
        os.close(source)
    if (
        not isinstance(value, dict)
        or value.get("id") != op
        or value.get("kind") not in ("update", "remote")
        or not isinstance(value.get("facts"), dict)
        or not isinstance(value.get("digest"), str)
    ):
        raise store.Conflict("the publication record is damaged")
    return value


def _write(fd: int, journal: dict) -> None:
    atomicjson.atomic_write_json(Path(f"/proc/self/fd/{fd}") / f"{journal['id']}.json", journal)


def _public(journal: dict) -> dict:
    """What a caller may see of a record. It never holds a secret, so this is a projection."""
    keys = (
        "id",
        "kind",
        "state",
        "step",
        "facts",
        "repository",
        "commit",
        "index",
        "pushed",
        "checkout",
    )
    out = {k: journal.get(k) for k in keys}
    out["pull_request"] = journal.get("pr")
    out["operation_id"] = out.pop("id")
    return out


def operation(pid: str, op: str) -> dict:
    """A publication operation's recorded progress (read-only)."""
    pid, op = template_vars.project_id(pid), lifecycle.operation_id(op)
    journal = None
    fd = None
    # NOT the publication lock: a progress read must answer while a publish holds it (it may
    # hold it through a push and a forge call). The record is replaced atomically
    # (`atomicjson`), so a lock-free read sees the old record or the new one, never a torn one.
    # Nothing is created on a read: a missing store, directory or record is "no such operation".
    with store.root_lock(exclusive=False) as root:
        if root is not None:
            top = state._directory(root, DIRECTORY, False)
            if top is not None:
                try:
                    fd = state._directory(top, pid, False)
                finally:
                    os.close(top)
    if fd is not None:
        try:
            journal = _read(fd, op)
        finally:
            os.close(fd)
    if journal is None:
        raise store.NotFound("no such publication operation")
    return _public(journal)


# --- the forge connection ---------------------------------------------------------------------


def _forge(record: dict, body: dict) -> dict:
    facts = record.get("review_facts")
    if not isinstance(facts, dict):
        raise store.Conflict("apply the deployment again so its connections are recorded")
    forges = [
        t
        for t in facts.get("targets", [])
        if t.get("kind") == "connection" and t.get("connection_kind") == "forge"
    ]
    names = sorted(t["id"].split(":", 1)[1] for t in forges)
    wanted = body.get("connection")
    if wanted is None:
        if len(forges) != 1:
            raise store.Conflict(
                "name the forge connection to use"
                if forges
                else "this playbook declares no forge connection, so nothing can be created or "
                "opened on a forge",
                connections=names,
            )
        target = forges[0]
    else:
        target = next((t for t in forges if t["id"] == f"connection:{wanted}"), None)
        if target is None:
            raise store.Conflict("no forge connection of that name", connections=names)
    if target.get("default_variables"):
        # A bundle default is not operator authority (#1096 §4), and this target receives a token.
        raise store.Conflict(
            "the forge connection's URL comes from the playbook's default; bind it for the "
            "project (or globally) first",
            variables=target["default_variables"],
        )
    if not target.get("credential"):
        raise store.Conflict("the forge connection declares no credential")
    kind = body.get("forge_kind", "forgejo")
    if kind not in forge_write.ForgeWriter.KINDS:
        raise store.StoreError(f"forge_kind must be one of {list(forge_write.ForgeWriter.KINDS)}")
    url = target["args"].get("url")
    try:
        forge_write.base_authority(url)
    except forge_write.ForgeWriteError as e:
        raise store.Conflict(str(e)) from None
    connection = target["id"].split(":", 1)[1]
    params = (record.get("connection_params") or {}).get(connection) or {}
    if not isinstance(params.get("url"), str):
        raise store.Conflict("apply the deployment again so its forge connection is recorded")
    return {
        "connection": connection,
        "url": url,
        # Which binding feeds the URL: re-resolved with the token at every forge call.
        "url_variable": params["url"],
        "kind": kind,
        "credential": target["credential"],
        "web_host": forge_write.web_host(kind, url),
    }


def _writer(pid: str, forge: dict) -> forge_write.ForgeWriter:
    """A writer holding the token resolved NOW from the project's binding; never stored.

    The URL is resolved in the SAME store read as the token and must still be the URL the plan
    (and the apply) showed: a forge URL rebound since then would otherwise receive the token at
    an address nobody reviewed for this operation.
    """
    try:
        resolved = template_vars.resolver(pid)
        variable = forge.get("url_variable")
        if not isinstance(variable, str):
            raise store.Conflict("plan the operation again so its forge URL binding is recorded")
        now = resolved.text(variable)["value"]
        if now != forge["url"]:
            raise store.Conflict(
                "the forge connection's URL is bound to a different address than the one "
                "applied and planned; apply the deployment again, then plan",
                variable=variable,
            )
        token = resolved.secret(forge["credential"])
    except template_vars.BindingMissing as e:
        raise store.Conflict(f"{e.name}: bind the forge connection first") from None
    except template_vars.BindingUnusable as e:
        raise store.Conflict(str(e)) from None
    except template_vars.ResolutionUnavailable:
        raise store.StoreError("the variable store could not be read", status=503) from None
    try:
        return forge_write.ForgeWriter(kind=forge["kind"], base_url=forge["url"], token=token)
    except forge_write.ForgeWriteError as e:
        raise store.Conflict(str(e)) from None


@contextlib.contextmanager
def _forge_errors(**extra: object):
    try:
        yield
    except forge_write.ForgeWriteError as e:
        raise store.StoreError(
            forge_write.why(e), status=409 if e.status in (409, 422) else 502, **extra
        ) from None
    except store.StoreError:
        raise
    except Exception as e:  # noqa: BLE001 - transport failures are named by kind only
        raise store.StoreError(forge_write.why(e), status=502, **extra) from None


def _remote_repository(url: str, web_host: str, confirm=None) -> tuple[str, str]:
    """`(owner, name)` of a git remote URL on the forge connection, or a refusal.

    The plain case: the forge's own web host, path exactly `owner/name[.git]`. Anything else (a
    separate ssh domain, an ssh port, a sub-path) is accepted only when the FORGE confirms it:
    `confirm(owner, name)` returns the addresses the forge advertises for that repository, and
    the URL must be one of them exactly. Without that, a pull request could be opened on a
    repository other than the one the branch was pushed to.
    """
    host = gitwrite.url_host(url)
    if host is None:
        raise store.Conflict(
            "the push remote is not on the forge connection's host, so its pull request cannot "
            "be opened there"
        )
    path = urlsplit(url).path if "://" in url else url.split(":", 1)[1]
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) < 2:
        raise store.Conflict("the push remote does not name one owner/repository")
    owner, name = parts[-2], parts[-1][:-4] if parts[-1].endswith(".git") else parts[-1]
    if not _REPO_NAME.fullmatch(owner) or not _REPO_NAME.fullmatch(name):
        raise store.Conflict("the push remote does not name one owner/repository")
    split = urlsplit(url) if "://" in url else None
    plain = (
        host.strip("[]").lower() == web_host
        and len(parts) == 2
        and (split is None or (split.scheme == "https" and split.port is None))
    )
    if not plain:
        advertised = confirm(owner, name) if confirm is not None else set()
        if url not in advertised:
            raise store.Conflict(
                "the push remote is not an address the forge connection gives for "
                f"{owner}/{name}, so its pull request cannot be opened there"
            )
    return owner, name


def _advertised(pid: str, forge: dict, owner: str, name: str) -> set[str]:
    """The git addresses the forge gives for `owner/name` (empty when it has no such repo)."""
    writer = _writer(pid, forge)
    with _forge_errors():
        found = writer.repo(owner, name)
    if not found:
        return set()
    return {found[k] for k in ("ssh_url", "clone_url") if isinstance(found.get(k), str)}


# --- git helpers ------------------------------------------------------------------------------


def _ls_remote(repo: gitpanel.Repo, url: str, remote: str, refs: list[str]) -> dict[str, str]:
    """`{ref: oid}` plus `"symref:HEAD"`, read through a scratch gitdir with the URL pinned."""
    gitwrite.refuse_insecure_tls(repo)
    gitwrite.refuse_url_rewrites(repo, url)
    dest = gitwrite.admit_destination(url, remote)
    with gitwrite._isolated_gitdir(repo) as gitdir:
        out = gitwrite.run_git_net(
            repo,
            gitdir,
            ["ls-remote", "--symref", "--upload-pack", "git-upload-pack", "--", url, *refs],
            extra_config=dest.pins,
            ssh_command=dest.ssh_command,
            allow_protocol=dest.allow_protocol,
        )
    found: dict[str, str] = {}
    for line in out.splitlines():
        left, _, ref = line.partition("\t")
        if left.startswith("ref: "):
            found[f"symref:{ref}"] = left[5:]
        elif ref:
            found[ref] = left
    return found


def _tip(repo: gitpanel.Repo, ref: str) -> str:
    try:
        return gitwrite.run_git_write(repo, ["rev-parse", "--verify", "--quiet", ref]).strip()
    except gitpanel.GitError:
        return ""


def _switch(repo: gitpanel.Repo, branch: str, base: str, from_branch: str, create: bool) -> None:
    """Point HEAD at `branch` (created at `base`) WITHOUT touching the worktree or the index.

    HEAD already resolves to `base`, so this is what `git switch -c` does for a same-commit
    switch, minus the checkout machinery: `update-ref` with an empty old value creates the
    branch only if it does not exist, and `symbolic-ref` moves HEAD. Both run under git's own
    `index.lock`, after HEAD is re-checked to be `from_branch` at `base`, so a `git commit` or
    `git switch` cannot land in between and leave the index describing another commit.
    """

    def run() -> None:
        held = gitwrite._acquire_index_lock(repo, gitwrite.INDEX_LOCK_WAIT_S)
        if isinstance(held, str):
            raise FsError(f"{held}; nothing was changed", status=409)
        try:
            if gitwrite._head_branch(repo) != from_branch or gitwrite._current_head(repo) != base:
                raise FsError(
                    "the checkout moved since the publication was planned; plan it again",
                    status=409,
                )
            blocked = gitwrite._unfinished_operation(repo)
            if blocked:
                raise FsError(f"{blocked}; finish it first", status=409)
            ref = f"refs/heads/{branch}"
            if create:
                gitwrite.run_git_write(repo, ["update-ref", "-m", _STAMP, ref, base, ""])
            gitwrite.run_git_write(repo, ["symbolic-ref", "-m", _STAMP, "HEAD", ref])
        finally:
            held.release()

    with git_errors():
        gitwrite._guarded(repo, run)


def _restore(repo: gitpanel.Repo, branch: str, base: str, original: str, journal: dict) -> bool:
    """Undo `_switch` after a failure BEFORE the commit landed; True when anything changed.

    Under git's own `index.lock`, and only while HEAD is still `branch` at `base` (a commit that
    did land, or an operator who moved on, is left exactly as it is). HEAD goes back to
    `original` with `symbolic-ref`, the worktree and index untouched, as `_switch` left them.
    The branch is deleted only when this operation created it, and only at `base`
    (`update-ref -d` with that old value), so it never takes a commit with it. Best-effort: the
    failure being reported is the one that matters, and a retry re-checks everything.
    """
    ref = f"refs/heads/{branch}"
    changed = []

    def run() -> None:
        held = gitwrite._acquire_index_lock(repo, gitwrite.INDEX_LOCK_WAIT_S)
        if isinstance(held, str):
            return
        try:
            if gitwrite._head_branch(repo) == branch and gitwrite._current_head(repo) == base:
                gitwrite.run_git_write(
                    repo, ["symbolic-ref", "-m", _STAMP, "HEAD", f"refs/heads/{original}"]
                )
                changed.append("head")
            if (
                journal.get("branch_created")
                and gitwrite._head_branch(repo) != branch
                and _tip(repo, ref) == base
            ):
                gitwrite.run_git_write(repo, ["update-ref", "-m", _STAMP, "-d", ref, base])
                journal["branch_created"] = False
                changed.append("branch")
        finally:
            held.release()

    with contextlib.suppress(Exception):
        gitwrite._guarded(repo, run)
    return bool(changed)


def _checkout(repo: gitpanel.Repo) -> dict:
    """Where the operator's checkout is now: the branch HEAD names and its commit."""
    try:
        return {"branch": gitwrite._head_branch(repo) or None, "head": _tip(repo, "HEAD") or None}
    except (gitpanel.GitError, FsError):
        return {"branch": None, "head": None}


def _is_ours(repo: gitpanel.Repo, tip: str, base: str, expected: dict) -> bool:
    """One commit on `base` that changes exactly the bound paths to exactly the bound blobs."""
    with git_errors():
        parents = gitwrite.run_git_write(repo, ["rev-list", "--parents", "-n", "1", tip]).split()
        diff = gitwrite.parse_raw_diff(
            gitwrite.run_git_bytes(
                repo, ["diff-tree", "-r", "-z", "--raw", "--no-renames", base, tip]
            )
        )
    if parents[1:] != [base] or set(diff) != set(expected):
        return False
    return all(repo_git.same_entry(diff[n][1], expected[n]) for n in expected)


def _push(
    repo: gitpanel.Repo, remote: str, url: str, branch: str, commit: str, extra: dict
) -> dict:
    """Push `commit` as `branch` to the displayed `url` through `gitwrite.git_push`, or adopt it.

    An absent remote branch is pushed (never forced); one already at `commit` is this
    publication's earlier push; anything else is somebody else's branch and is refused.
    """
    with git_errors():
        now = gitwrite._effective_url(repo, remote, push=True)
    if now != url:
        raise store.Conflict(
            "the remote's push destination changed since the plan; nothing was pushed", **extra
        )
    ref = f"refs/heads/{branch}"
    try:
        with git_errors():
            there = _ls_remote(repo, url, remote, [ref]).get(ref)
            if there == commit:
                return {"pushed": commit, "adopted": True}
            if there:
                raise store.Conflict(
                    f"the remote already has a `{branch}` branch with other content; it is never "
                    "force-pushed",
                    **extra,
                )
            if gitwrite._head_branch(repo) != branch:
                raise store.Conflict(f"switch back to `{branch}` to finish the push", **extra)
            expect = f"{remote}/{branch}@{gitwrite.destination_digest(url)}:{commit}"
            result = gitwrite.git_push(repo.toplevel, remote, expect)
    except store.StoreError as e:
        e.extra = {**extra, **e.extra}
        raise
    return {"pushed": result["pushed"], "adopted": False}


# --- update publication -----------------------------------------------------------------------


def _update_facts(pid: str, body: dict) -> dict:
    record = repo_git.deployment(pid)
    repo = repo_git.project_repo(record)
    assert repo is not None
    forge = _forge(record, body)
    with git_errors():
        checkout = gitwrite._head_branch(repo)
        if not checkout:
            raise store.Conflict("HEAD is detached; check out the default branch first")
        remote, _ = gitwrite.resolve_push(repo, checkout, body.get("remote"))
        url = gitwrite._effective_url(repo, remote, push=True)
        gitwrite.refuse_credential_in_url(url, remote)
        owner, name = _remote_repository(
            url, forge["web_host"], lambda o, n: _advertised(pid, forge, o, n)
        )
        heads = _ls_remote(repo, url, remote, ["HEAD"])
        default_ref = heads.get("symref:HEAD", "")
        base = heads.get("HEAD", "")
        if not default_ref.startswith("refs/heads/") or not base:
            raise store.Conflict("the remote does not say which branch is its default")
        head = gitwrite._current_head(repo)
    if head != base:
        raise store.Conflict(
            "the checkout is not at the remote default branch's head (it is behind, or has "
            "unpushed commits); sync it first",
            head=head or None,
            base=base,
        )
    expected = repo_git.bound_content(record, repo, base)
    names = sorted(expected)
    if not names:
        raise store.Conflict("the repository already holds everything the playbook applied")
    applied = record["applied_paths"]
    branch = f"playbook/{record['playbook_id']}-v{applied['version']}"
    with git_errors():
        gitwrite.validate_ref(branch)
        gitwrite.check_ref_format(repo, branch)
    title = f"chore(playbook): update {record['playbook_id']} to v{applied['version']}"
    return {
        "project_id": pid,
        "deployment_id": record["id"],
        "playbook_id": record["playbook_id"],
        "version": applied["version"],
        "revision": applied["revision"],
        "apply_operation": applied["operation_id"],
        "paths": {n: list(expected[n]) if expected[n] else None for n in names},
        "checkout_branch": checkout,
        "base": base,
        "default_branch": default_ref[len("refs/heads/") :],
        "remote": remote,
        "url": url,
        "repository": f"{owner}/{name}",
        "branch": branch,
        "title": title,
        "forge": forge,
    }


def plan_update(pid: str, body: object, *, key: str) -> dict:
    """What publishing would do: the approved base, the branch, the exact paths, the target."""
    pid = template_vars.project_id(pid)
    body = only(body, _PLAN_INPUTS, set(), "a publication plan")
    facts = _update_facts(pid, body)
    return {**facts, "digest": review._digest({"publication": facts}, key)}


def _pr_body(facts: dict) -> str:
    lines = [
        f"Playbook `{facts['playbook_id']}` v{facts['version']} (revision "
        f"`{facts['revision'][:12]}`), applied by BattleLab.",
        "",
        "Changed paths:",
        *[f"- `{p}`" for p in sorted(facts["paths"])],
    ]
    return "\n".join(lines)


def publish_update(pid: str, body: object, *, key: str) -> dict:
    """Commit, push and open the PR for exactly the planned update; a same-id retry resumes."""
    pid = template_vars.project_id(pid)
    body = only(
        body, _PLAN_INPUTS | {"digest", "operation_id"}, {"digest", "operation_id"}, "a publication"
    )
    op = lifecycle.operation_id(body["operation_id"])
    digest = store.revision(body["digest"])
    with _locked(pid) as fd:
        journal = _read(fd, op)
        if journal is None:
            facts = _update_facts(pid, {k: v for k, v in body.items() if k in _PLAN_INPUTS})
            if not hmac.compare_digest(review._digest({"publication": facts}, key), digest):
                raise store.Conflict("the publication changed since it was planned; plan it again")
            journal = {
                "id": op,
                "kind": "update",
                "digest": digest,
                "facts": facts,
                "state": "intent",
                "step": "commit",
                "branch_created": False,
                "commit": None,
                "index": None,
                "pushed": False,
                "pr": None,
            }
            _write(fd, journal)  # durable intent before any effect
        elif journal["kind"] != "update" or not hmac.compare_digest(journal["digest"], digest):
            raise store.Conflict("the operation id already names another publication")
        if journal["state"] != "complete":
            _run_update(pid, journal, lambda: _write(fd, journal))
        return _public(journal)


def _run_update(pid: str, journal: dict, write) -> None:
    facts = journal["facts"]
    extra = {"operation_id": journal["id"], "branch": facts["branch"]}
    record = repo_git.deployment(pid)
    repo = repo_git.project_repo(record)
    assert repo is not None
    expected = {n: tuple(v) if v else None for n, v in facts["paths"].items()}
    branch, base = facts["branch"], facts["base"]
    if journal["commit"] is None:
        journal["step"] = "commit"
        if record["applied_paths"]["operation_id"] != facts["apply_operation"]:
            raise store.Conflict("a newer apply landed since the plan; plan it again", **extra)
        tip = _tip(repo, f"refs/heads/{branch}")
        if tip and tip != base:
            if not _is_ours(repo, tip, base, expected):
                raise store.Conflict(
                    f"a local `{branch}` branch already exists with other content", **extra
                )
            journal["commit"] = tip  # an earlier attempt committed; its record did not land
        else:
            with git_errors():
                on_branch = gitwrite._head_branch(repo) == branch
            try:
                if not on_branch:
                    _switch(repo, branch, base, facts["checkout_branch"], create=not tip)
                    journal["branch_created"] = journal["branch_created"] or not tip
                    write()
                result = repo_git.commit_bound(
                    repo, record, sorted(expected), expected, base, facts["title"]
                )
            except BaseException as e:
                # Nothing landed (the branch is still at the base): put the checkout back where
                # the operator had it, and take away the branch only if this operation made it.
                restored = _restore(repo, branch, base, facts["checkout_branch"], journal)
                if restored:
                    write()
                if isinstance(e, store.StoreError):
                    e.extra = {**extra, "checkout": _checkout(repo), **e.extra}
                raise
            journal["commit"] = result["sha"]
            journal["index"] = {
                "state": result.get("index"),
                "left": result.get("index_left", []),
            }
        write()
    extra["commit"] = journal["commit"]
    if not journal["pushed"]:
        journal["step"] = "push"
        _push(repo, facts["remote"], facts["url"], branch, journal["commit"], extra)
        journal["pushed"] = True
        write()
    if journal["pr"] is None:
        journal["step"] = "pull_request"
        owner, name = facts["repository"].split("/", 1)
        writer = _writer(pid, facts["forge"])
        with _forge_errors(**extra):
            pr = writer.open_pull(owner, name, head=branch, base=facts["default_branch"])
            adopted = pr is not None
            if pr is None:
                pr = writer.create_pull(
                    owner,
                    name,
                    head=branch,
                    base=facts["default_branch"],
                    title=facts["title"],
                    body=_pr_body(facts),
                )
        journal["pr"] = {
            "number": pr.get("number"),
            "url": pr.get("html_url"),
            "adopted": adopted,
        }
    # Where the operator is left: ON the playbook branch at the commit (the worktree and index
    # already hold its content, so staying is what `git switch -c` + commit leaves too). The push
    # gave THAT branch its upstream, so a later panel push from it goes to the PR branch; the
    # operator's own branch is never touched and keeps its upstream.
    journal["checkout"] = {**_checkout(repo), "previous_branch": facts["checkout_branch"]}
    journal["state"] = "complete"
    journal["step"] = None
    write()


# --- remote creation --------------------------------------------------------------------------


def _remote_facts(pid: str, body: dict) -> dict:
    record = repo_git.deployment(pid)
    repo = repo_git.project_repo(record)
    assert repo is not None
    forge = _forge(record, body)
    owner, name = body.get("owner"), body.get("name")
    if not isinstance(owner, str) or not _REPO_NAME.fullmatch(owner):
        raise store.StoreError("owner must be a forge user or organisation name")
    if not isinstance(name, str) or not _REPO_NAME.fullmatch(name) or name.endswith(".git"):
        raise store.StoreError("name must be a repository name")
    private = body.get("private", True)
    if type(private) is not bool:
        raise store.StoreError("private must be true or false")
    with git_errors():
        remote = gitwrite.validate_ref(body.get("remote", "origin"), what="remote")
        if remote in gitwrite.list_remotes(repo):
            raise store.Conflict(f"the repository already has a remote named {remote!r}")
        branch = gitwrite._head_branch(repo)
        head = gitwrite._current_head(repo)
    if not branch or not head:
        raise store.Conflict("seed the repository (one commit on a branch) before creating it")
    writer = _writer(pid, forge)
    with _forge_errors():
        if writer.repo(owner, name) is not None:
            raise store.Conflict(f"{owner}/{name} already exists on the forge; pick another name")
        # SSH only, at the forge's OWN advertised endpoint (port, ssh domain, sub-path), settled
        # BEFORE anything is created. https is not offered: the push path clears credential
        # helpers and never hands git the forge token, so an https remote could not push a
        # private repository, and a guessed `git@<web host>:` address breaks on a forge whose
        # ssh runs on another port or domain, after the repository already exists.
        url = writer.ssh_endpoint() + f"{owner}/{name}.git"
    with git_errors():
        _check_ssh_url(url, owner, name, remote)
        gitwrite.admit_destination(url, remote)  # refused here, not after the create
    return {
        "project_id": pid,
        "deployment_id": record["id"],
        "forge": forge,
        "repository": f"{owner}/{name}",
        "private": private,
        "remote": remote,
        # The address the remote will be written with: never a credential in it.
        "url": url,
        "branch": branch,
        "head": head,
    }


def _check_ssh_url(url: str, owner: str, name: str, remote: str) -> None:
    """An ssh address (`ssh://` or scp-like) for exactly `owner/name`, with no credential."""
    gitwrite.refuse_credential_in_url(url, remote)
    host = gitwrite.url_host(url)
    scp = "://" not in url
    if host is None or not (url.startswith("ssh://") or scp) or any(c in url for c in "?#"):
        raise store.Conflict("the forge's ssh address for the repository is not an ssh URL")
    path = urlsplit(url).path if not scp else url.split(":", 1)[1]
    if not path.lower().endswith(f"{owner}/{name}.git".lower()):
        raise store.Conflict("the forge's ssh address does not name the planned repository")


def plan_remote(pid: str, body: object, *, key: str) -> dict:
    """The target, shown first: forge, owner/name, visibility, remote URL, what is pushed."""
    pid = template_vars.project_id(pid)
    body = only(body, _REMOTE_INPUTS, {"owner", "name"}, "a remote plan")
    facts = _remote_facts(pid, body)
    return {**facts, "digest": review._digest({"remote": facts}, key)}


def _check_remote_url(created: dict, facts: dict) -> str:
    """The forge's answer must be exactly the address the plan showed (and was admitted)."""
    url = created.get("ssh_url")
    if not isinstance(url, str):
        raise store.Conflict("the forge did not return the repository's ssh_url")
    with git_errors():
        gitwrite.refuse_credential_in_url(url, facts["remote"])
    if url.lower() != facts["url"].lower():
        raise store.Conflict(
            "the forge's address for the new repository is not the one the plan showed; the "
            "repository exists and was left alone, and it was not added as a remote",
            url=url,
            html_url=created.get("html_url"),
        )
    return facts["url"]


def create_remote(pid: str, body: object, *, key: str) -> dict:
    """Create the planned repository, add the remote, push the branch; a same-id retry resumes."""
    pid = template_vars.project_id(pid)
    body = only(
        body,
        _REMOTE_INPUTS | {"digest", "operation_id"},
        {"owner", "name", "digest", "operation_id"},
        "a remote creation",
    )
    op = lifecycle.operation_id(body["operation_id"])
    digest = store.revision(body["digest"])
    with _locked(pid) as fd:
        journal = _read(fd, op)
        if journal is None:
            facts = _remote_facts(pid, {k: v for k, v in body.items() if k in _REMOTE_INPUTS})
            if not hmac.compare_digest(review._digest({"remote": facts}, key), digest):
                raise store.Conflict("the remote changed since it was planned; plan it again")
            journal = {
                "id": op,
                "kind": "remote",
                "digest": digest,
                "facts": facts,
                "state": "intent",
                "step": "repository",
                # Put in the description BEFORE the create request is sent: the only proof a
                # retry has that an existing repository is the one this operation created.
                "marker": f"battlelab:{secrets.token_hex(16)}",
                "create_sent": False,
                "repository": None,
                "remote_added": False,
                "pushed": False,
            }
            _write(fd, journal)
        elif journal["kind"] != "remote" or not hmac.compare_digest(journal["digest"], digest):
            raise store.Conflict("the operation id already names another remote creation")
        if journal["state"] != "complete":
            _run_remote(pid, journal, lambda: _write(fd, journal))
        out = _public(journal)
        out.pop("index", None)
        out.pop("checkout", None)
        return out


def _run_remote(pid: str, journal: dict, write) -> None:
    facts = journal["facts"]
    extra = {"operation_id": journal["id"], "repository_name": facts["repository"]}
    owner, name = facts["repository"].split("/", 1)
    if journal["repository"] is None:
        journal["step"] = "repository"
        writer = _writer(pid, facts["forge"])
        with _forge_errors(**extra):
            existing = writer.repo(owner, name)
            if existing is None:
                journal["create_sent"] = True
                write()
                description = f"Seeded from a BattleLab playbook ({journal['marker']})"
                try:
                    existing = writer.create_repo(
                        owner, name, private=facts["private"], description=description
                    )
                except forge_write.ForgeWriteError as e:
                    if e.status not in (409, 422):
                        raise
                    existing = writer.repo(owner, name)  # created by a lost earlier response?
                    if existing is None:
                        raise
            if not journal["create_sent"] or journal["marker"] not in str(
                existing.get("description") or ""
            ):
                raise store.Conflict(
                    f"{facts['repository']} exists, and this operation did not create it; it "
                    "is left alone",
                    **extra,
                )
        url = _check_remote_url(existing, facts)
        journal["repository"] = {
            "full_name": facts["repository"],
            "url": url,
            "html_url": existing.get("html_url"),
            "created_by_operation": True,
        }
        write()
    record = repo_git.deployment(pid)
    repo = repo_git.project_repo(record)
    assert repo is not None
    url = journal["repository"]["url"]
    remote = facts["remote"]
    if not journal["remote_added"]:
        journal["step"] = "remote"
        with git_errors():
            if remote in gitwrite.list_remotes(repo):
                if gitwrite._effective_url(repo, remote, push=False) != url:
                    raise store.Conflict(f"a different remote {remote!r} appeared", **extra)
            else:
                gitwrite.admit_destination(url, remote)
                gitwrite.run_git_write(repo, ["remote", "add", "--", remote, url])
        journal["remote_added"] = True
        write()
    if not journal["pushed"]:
        journal["step"] = "push"
        _push(repo, remote, url, facts["branch"], facts["head"], extra)
        journal["pushed"] = True
    journal["state"] = "complete"
    journal["step"] = None
    write()
