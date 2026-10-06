"""Apply one bound, reviewed deployment plan to its destination, recoverably (#1191).

Fences, outermost first: the projects index, then the authoring/project locks. The accepted plan
is frozen into the record (`replay_plan`) with one intent per effect BEFORE any destination
write. Every effect goes through `material_write` (create/remove) or `fileedit.save_bytes`
(replace), each of which retains displaced bytes and never overwrites a claimant.

Recovery is a retry with the same operation id. Each path is reconciled against its accepted
before/after content: already at `after` is done, still at `before` is performed, anything else
refuses and is reported as `unknown`. Ownership is recorded only once every effect settled, so a
half-applied plan never becomes the basis of a later review. No probe, agent or shell runs here.
"""

from __future__ import annotations

import contextlib
import copy
import hmac
import os
import secrets
import stat
from collections.abc import Iterator

from .. import fileedit, projects, renameat
from ..fsbrowse import FsError
from . import deployment_state as state
from . import (
    destination,
    instructions,
    lifecycle,
    material_write,
    materials,
    mutation_plan,
    replay_plan,
    review,
    secret_files,
    store,
)

#: Beneath the editor's recovery store, so the same-filesystem rule and its upload refusal apply.
#: A leading dot keeps the editor's own resolver from treating these as its records.
ENTRIES = ".playbooks"
_HISTORY_MAX = 10000
_RESULT = {"project_id", "deployment_id", "operation_id", "state", "digest"}
_SECRET_KEYS = {"secrets", "secret_root", "secret_dir"}


def _same(a: destination.Node, b: destination.Node) -> bool:
    return (a.kind, a.data, a.target) == (b.kind, b.data, b.target)


@contextlib.contextmanager
def _entries() -> Iterator[int]:
    store_fd = fileedit._open_store()
    try:
        with contextlib.suppress(FileExistsError):
            os.mkdir(ENTRIES, 0o700, dir_fd=store_fd)
        os.fsync(store_fd)
        fd = os.open(
            ENTRIES, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=store_fd
        )
    finally:
        os.close(store_fd)
    try:
        st = os.fstat(fd)
        if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700:
            raise store.Conflict("the playbook recovery store is not private")
        yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def _effect_entry(name: str) -> Iterator[int]:
    """A fresh private directory per attempt; an earlier attempt's entry is never reused."""
    with _entries() as parent:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            raise store.Conflict("a recovery entry for this effect already exists") from None
        os.fsync(parent)
        fd = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent
        )
        try:
            yield fd
        finally:
            os.close(fd)


def _replay(record: dict, pid: str, op: str, digest: str) -> dict | None:
    history = record.get("apply_history", {})
    if not isinstance(history, dict) or len(history) > _HISTORY_MAX:
        raise store.Conflict("the apply operation history is damaged")
    old = history.get(op)
    if old is None:
        if len(history) == _HISTORY_MAX:
            raise store.Conflict("the apply operation history is full")
        return None
    if (
        not isinstance(old, dict)
        or set(old) != {"request_digest", "result"}
        or not isinstance(old["result"], dict)
        or set(old["result"]) != _RESULT
        or old["result"]["project_id"] != pid
        or old["result"]["operation_id"] != op
    ):
        raise store.Conflict("the apply operation history is damaged")
    if not hmac.compare_digest(str(old["request_digest"]), digest):
        raise store.Conflict("the apply operation id already names another request")
    return copy.deepcopy(old["result"])


def _journal(record: dict) -> dict | None:
    journal = record.get("apply_operation")
    if journal is None:
        return None
    try:
        # The secret reference file keys (#1191) are absent from journals written before them.
        if not isinstance(journal, dict) or set(journal) - _SECRET_KEYS != {
            "id",
            "request_digest",
            "state",
            "basis_state",
            "plan",
            "effects",
            "directories",
            "attempt",
        }:
            raise ValueError
        secret_files.validate_effects(journal.get("secrets", {}))
        if journal.get("secrets"):
            secret_files.validate_root(journal.get("secret_root"))
        secret_files._identity(journal.get("secret_dir"))
        lifecycle.operation_id(journal["id"])
        store.revision(journal["request_digest"])
        if journal["state"] not in {"intent", "complete"}:
            raise ValueError
        if journal["basis_state"] not in {"bound", "applied"}:
            raise ValueError
        if type(journal["attempt"]) is not int or not 0 <= journal["attempt"] < 1_000_000:
            raise ValueError
        effects = journal["effects"]
        if not isinstance(effects, dict):
            raise ValueError
        for path, effect in effects.items():
            if (
                not isinstance(effect, dict)
                or set(effect) != {"action", "phase", "entry", "inode"}
                or effect["action"] not in {"create", "replace", "remove", "keep"}
                or effect["phase"] not in {"pending", "intent", "staged", "done"}
            ):
                raise ValueError
            entry, inode = effect["entry"], effect["inode"]
            if entry is not None and (
                not isinstance(entry, str)
                or not entry.startswith(journal["id"] + "-")
                or not entry[len(journal["id"]) + 1 :].isdigit()
            ):
                raise ValueError
            if inode is not None and (
                not isinstance(inode, list)
                or len(inode) != 2
                or not all(type(i) is int for i in inode)
            ):
                raise ValueError
            mutation_plan.ownership({path: {"kind": "seed", "disposition": "seed"}})
        directories = journal["directories"]
        if not isinstance(directories, dict):
            raise ValueError
        for path, identity in directories.items():
            mutation_plan.ownership({path: {"kind": "seed", "disposition": "seed"}})
            if identity is not None and (
                not isinstance(identity, list)
                or len(identity) != 2
                or not all(type(i) is int for i in identity)
            ):
                raise ValueError
    except (TypeError, ValueError, KeyError, store.StoreError):
        raise store.Conflict("the deployment apply journal is damaged") from None
    return journal


def pending(record: dict | None) -> bool:
    """An unsettled apply blocks every other lifecycle operation on the project."""
    if record is None:
        return False
    journal = _journal(record)
    return journal is not None and journal["state"] != "complete"


def _basis(record: dict, journal: dict) -> dict:
    return {**record, "state": journal["basis_state"]}


def _parents(
    path: str,
    live: dict[str, destination.Node],
    frozen: dict[str, destination.Node],
    created: dict,
) -> dict[str, destination.Node]:
    """Each live parent must be the reviewed directory or one this operation recorded."""
    parents = {}
    parts = path.split("/")[:-1]
    for i in range(1, len(parts) + 1):
        rel = "/".join(parts[:i])
        node = live[rel]
        if node.kind != "directory":
            raise FsError("a material parent is missing or not a directory", status=409)
        reviewed = frozen.get(rel)
        if reviewed is not None and reviewed.kind == "directory":
            if node.identity[:2] != reviewed.identity[:2]:
                raise FsError("a material parent was replaced", status=409)
        elif created.get(rel) is not None and list(node.identity[:2]) != created[rel]:
            raise FsError("a created material parent was replaced", status=409)
        parents[rel] = node
    return parents


def _make_parents(
    folder: destination.Folder,
    path: str,
    journal: dict,
    write,
) -> None:
    """Create absent parents exclusively; record intent first and identity after.

    A directory that exists without a recorded identity (an interrupted mkdir, or an operator's)
    is used as a parent but never claimed, so removal can never delete it.
    """
    parts = path.split("/")[:-1]
    for i in range(1, len(parts) + 1):
        rel = "/".join(parts[:i])
        # Parents are read as the material path's intermediate entries; a leaf is never a dir.
        node = destination.snapshot(folder, [path])[rel]
        if node.kind == "directory":
            continue
        if node.kind != "absent":
            raise FsError("a material parent is not a directory", status=409)
        if rel not in journal["directories"]:
            journal["directories"][rel] = None
            write()
        with destination.open_folder(folder) as root, contextlib.ExitStack() as stack:
            held = [(root, folder.path)]
            fd = root
            for k, segment in enumerate(parts[: i - 1], 1):
                fd = stack.enter_context(
                    destination._open(segment, destination._DIR_FLAGS, dir_fd=fd)
                )
                held.append((fd, os.path.join(folder.path, *parts[:k])))

            def guard(held: list = held, upto: list = parts[:i]) -> None:
                # Bind every held parent to its reviewed pathname AT the write boundary.
                for parent_fd, absolute in held:
                    destination._verify(parent_fd, absolute, folder)
                destination._guard(folder, upto)

            guard()
            # Made under a random staging name, pinned by descriptor, THEN published: every
            # failure path knows exactly what to withdraw, without trusting a fallible stat.
            staging = ".battlelab-dir-" + secrets.token_hex(16)
            os.mkdir(staging, 0o755, dir_fd=fd)
            pinned = None
            published = False
            try:
                with destination._open(staging, destination._DIR_FLAGS, dir_fd=fd) as child:
                    pinned = os.fstat(child)
                guard()
                renameat.renameat2(fd, staging, fd, parts[i - 1], renameat.RENAME_NOREPLACE)
                published = True
                os.fsync(fd)
                st = os.stat(parts[i - 1], dir_fd=fd, follow_symlinks=False)
                if (st.st_dev, st.st_ino) != (pinned.st_dev, pinned.st_ino):
                    raise FsError("a created material parent was replaced", status=409)
                guard()
            except BaseException:
                # Withdraw only a name that still points to the PINNED inode. Without a pin
                # there is no proof which directory is ours: leave it and refuse.
                if pinned is not None:
                    with contextlib.suppress(OSError):
                        name = parts[i - 1] if published else staging
                        named = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        if (named.st_dev, named.st_ino) == (pinned.st_dev, pinned.st_ino):
                            os.rmdir(name, dir_fd=fd)
                            os.fsync(fd)
                raise
            journal["directories"][rel] = [st.st_dev, st.st_ino]
        write()


def _ours(change: mutation_plan.Change, effect: dict, current: destination.Node) -> bool:
    """Matching bytes alone never prove this operation wrote them (a claimant can race us).

    A create counts as done only when the live leaf is the candidate inode this operation pinned
    and journaled; a replace only once this operation started it. Anything else refuses.
    """
    if change.action == "create":
        return effect["inode"] is not None and list(current.identity[:2]) == effect["inode"]
    if change.action == "replace":
        # A replace journals its installed inode with `done`. Short of that, `intent` cannot
        # tell our interrupted save from another writer's identical bytes: fail closed. Putting
        # the reviewed `before` back (the save retains it) lets the same-id retry proceed.
        return False
    return True


def _release_pin(effect: dict) -> None:
    """Drop an interrupted create's own candidate pin, and only that exact recorded inode.

    A crash after the exclusive link but before the pin was dropped leaves the published file
    with two links, which every later snapshot refuses. The pin lives in this operation's private
    entry; a different inode under that name (or none) is left exactly as it is.
    """
    if effect["action"] != "create" or effect["entry"] is None or effect["inode"] is None:
        return
    with _entries() as parent:
        try:
            entry = os.open(
                effect["entry"],
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent,
            )
        except FileNotFoundError:
            return
        try:
            try:
                pin = os.stat("candidate", dir_fd=entry, follow_symlinks=False)
            except FileNotFoundError:
                return
            if [pin.st_dev, pin.st_ino] == effect["inode"]:
                os.unlink("candidate", dir_fd=entry)
                os.fsync(entry)
        finally:
            os.close(entry)


def _effect(
    folder: destination.Folder,
    change: mutation_plan.Change,
    frozen: dict[str, destination.Node],
    journal: dict,
    write,
    admit,
) -> None:
    effect = journal["effects"][change.path]
    if effect["phase"] == "done":
        return
    live = destination.snapshot(folder, [change.path])
    current = live[change.path]
    if _same(current, change.after) and _ours(change, effect, current):
        # Includes `keep` (and record-only reference adoption): `_settle` rechecks it last.
        effect["phase"] = "done"
        write()
        return
    if not _same(current, change.before) or (
        change.before.kind != "absent" and current.identity[:2] != change.before.identity[:2]
    ):
        # Equal bytes at another inode are a foreign file, not the reviewed one.
        raise store.Conflict(
            f"{change.path}: the destination is neither the reviewed nor the applied content"
        )
    if change.action == "replace":
        effect["phase"] = "intent"
        write()
        parent = "/".join(change.path.split("/")[:-1])
        reviewed_parent = (
            [folder.device, folder.inode] if not parent else list(frozen[parent].identity[:2])
        )
        saved = fileedit.save_bytes(
            os.path.join(folder.path, change.path),
            change.after.data,
            materials.digest(change.before.data),
            root=folder.path,
            admit=lambda _path: admit(),
            identity=(list(change.before.identity[:2]), reviewed_parent),
        )
        if not isinstance(saved.get("inode"), list):
            raise store.Conflict(f"{change.path}: the replacement installed no provable inode")
        effect["inode"] = saved["inode"]
    else:
        if change.action == "create":
            _make_parents(folder, change.path, journal, write)
            live = destination.snapshot(folder, [change.path])
        parents = _parents(change.path, live, frozen, journal["directories"])
        journal["attempt"] += 1
        name = f"{journal['id']}-{journal['attempt']}"
        effect.update(entry=name, inode=None)
        write()

        def progress(update: dict) -> None:
            effect["phase"] = update["phase"]
            if "inode" in update:
                effect["inode"] = list(update["inode"])
            write()

        with _effect_entry(name) as entry:
            operation = (
                material_write.create if change.action == "create" else material_write.remove
            )
            operation(folder, change, parents, entry, progress=progress, admit=admit)
    effect["phase"] = "done"
    write()


def _ownership(changes: list[mutation_plan.Change], effects: dict, previous: dict) -> dict:
    """Whole-file and alias ownership carry the inode this apply proved it installed.

    Settlement and the record write cannot be one atomic step, so the proof travels INTO the
    record: every later update/remove compares the live inode, and a same-byte file swapped in
    after settlement is ownership loss there, never a managed file.
    """
    files = {}
    for change in changes:
        if change.ownership is None:
            continue
        owned = dict(change.ownership)
        proof = _proof(change, effects, previous)
        if proof is not None:
            owned["inode"] = proof
        files[change.path] = owned
    return files


def _proof(change: mutation_plan.Change, effects: dict, previous: dict) -> list | None:
    """The inode a managed whole-file/alias outcome must be, or None for unpinned kinds.

    Written paths: the inode this operation installed. Kept paths (including reference
    adoption and legacy unpinned records): the recorded inode, else the REVIEWED one.
    """
    owned = change.ownership
    if owned is None or owned["kind"] not in ("file", "symlink"):
        return None
    if owned["disposition"] != "managed":
        return None
    if change.action in ("create", "replace"):
        return effects[change.path]["inode"]
    recorded = previous.get(change.path, {}).get("inode")
    if recorded is not None:
        return list(recorded)
    if len(change.before.identity) < 2:
        raise store.Conflict(f"{change.path}: the kept material has no reviewed identity")
    return list(change.before.identity[:2])


def _settle(
    folder: destination.Folder,
    changes: list[mutation_plan.Change],
    effects: dict,
    previous: dict,
) -> None:
    """Every path must read as its accepted outcome, and every pinned one as its proven inode."""
    live = destination.snapshot(folder, sorted(c.path for c in changes))
    for change in changes:
        node = live[change.path]
        if not _same(node, change.after):
            raise store.Conflict(f"{change.path}: changed before the apply record settled")
        if change.action in ("create", "replace"):
            inode = effects[change.path]["inode"]
            if inode is None or list(node.identity[:2]) != inode:
                raise store.Conflict(f"{change.path}: replaced before the apply record settled")
        else:
            proof = _proof(change, effects, previous)
            if proof is not None and list(node.identity[:2]) != proof:
                raise store.Conflict(f"{change.path}: replaced before the apply record settled")


def apply(pid: str, op: str, receipt: str, *, key: str) -> dict:
    """Apply only what a signed review of the BOUND inputs showed; a retry settles one operation."""
    pid, op = lifecycle.template_vars.project_id(pid), lifecycle.operation_id(op)
    request_digest = review._digest({"project": pid, "apply": receipt}, key)
    with projects.locked_index() as index:
        project = index.get(pid)
        if project is None or project.archived:
            raise store.Conflict("the destination project is missing or archived")
        with state.locked(pid) as locked:
            record = locked.read() if locked is not None else None
            if record is None or record.get("state") not in {"bound", "applied"}:
                raise store.Conflict("bind the reviewed playbook before applying it")
            assert locked is not None
            previous = _replay(record, pid, op, request_digest)
            if previous is not None:
                return previous
            if record.get("binding_operation", {}).get("state") not in (None, "complete"):
                raise store.Conflict("retry the pending binding operation before applying")
            journal = _journal(record)
            if journal is not None and journal["state"] != "complete":
                if journal["id"] != op:
                    raise store.Conflict("retry the pending apply operation before another")
                if not hmac.compare_digest(journal["request_digest"], request_digest):
                    raise store.Conflict("the apply operation id already names another request")
            else:
                secret_files.check_root(record)
                live = review.build(
                    record["playbook_id"], record["inputs"], key=key, _record=record
                )
                if live.public["destination"]["path"] not in project.folders:
                    raise store.Conflict("the project must own the reviewed destination")
                if live.public["conflicts"]:
                    raise store.Conflict("resolve material conflicts before applying")
                review.accept(live, receipt, key=key)
                changes, _ = mutation_plan.build(
                    live.rendered,
                    live.nodes,
                    mutation_plan.ownership(record.get("files", {})),
                    live.public["deployment_id"],
                )
                journal = {
                    "id": op,
                    "request_digest": request_digest,
                    "state": "intent",
                    "basis_state": record["state"],
                    "plan": replay_plan.freeze(live),
                    "effects": {
                        c.path: {
                            "action": c.action,
                            "phase": "pending",
                            "entry": None,
                            "inode": None,
                        }
                        for c in changes
                    },
                    "directories": {},
                    "attempt": 0,
                    "secrets": _secret_effects(live.public, record),
                    "secret_root": secret_files.root(),
                }
                record["apply_operation"] = journal
                locked.write(record)  # durable intent BEFORE any destination effect

            def write() -> None:
                locked.write(record)

            # Recompute the accepted plan from its frozen pre-state: this operation's own writes
            # cannot invalidate it, while changed source, bindings, project or policy refuse.
            plan = replay_plan.resume(
                record["playbook_id"],
                record["inputs"],
                _basis(record, journal),
                journal["plan"],
                key=key,
            )
            changes, conflicts = mutation_plan.build(
                plan.rendered,
                plan.nodes,
                mutation_plan.ownership(record.get("files", {})),
                plan.public["deployment_id"],
            )
            if conflicts or {c.path: c.action for c in changes} != {
                p: e["action"] for p, e in journal["effects"].items()
            }:
                raise store.Conflict("the accepted apply plan no longer reproduces")
            folder = destination.Folder(**record["destination"])
            try:
                for change in changes:
                    _release_pin(journal["effects"][change.path])
                for change in changes:
                    _effect(folder, change, plan.nodes, journal, write, locked.verify)
                _settle(folder, changes, journal["effects"], record.get("files", {}))
                written = _sync_secrets(
                    pid,
                    plan.public,
                    record,
                    journal,
                    write,
                    [folder.path] + [f for p in index.values() for f in p.folders],
                )
            except FsError as e:
                raise store.StoreError(str(e), status=e.status) from None
            except OSError:
                raise store.StoreError(
                    "the deployment destination could not be written safely", status=409
                ) from None
            created = record.setdefault("directories", {})
            for rel, identity in journal["directories"].items():
                if identity is not None:
                    created.setdefault(rel, identity)
            record["files"] = _ownership(changes, journal["effects"], record.get("files", {}))
            record["secret_files"] = written
            if written:
                record["secret_root"] = journal["secret_root"]
                record["secret_dir"] = journal["secret_dir"]
            else:
                record.pop("secret_root", None)
                record.pop("secret_dir", None)
            record["rituals"] = rituals(plan.public, record["id"])
            record["review_facts"] = review_facts(plan.public)
            record["verify_facts"] = verify_facts(plan.bundle)
            record["generation"] = record.get("generation", 0) + 1
            record["state"] = "applied"
            journal["state"] = "complete"
            result = {
                "project_id": pid,
                "deployment_id": record["id"],
                "operation_id": op,
                "state": "applied",
                "digest": plan.public["digest"],
            }
            record.setdefault("apply_history", {})[op] = {
                "request_digest": request_digest,
                "result": result,
            }
            write()
            return copy.deepcopy(result)


def _secret_effects(public: dict, record: dict) -> dict:
    """One journaled effect per reference file: write each one the plan names, delete each one
    the record owns that it no longer names."""
    wanted = {row["name"] for row in public["secret_files"]}
    owned = secret_files.validate_owned(record.get("secret_files", {}))
    return {
        name: {
            "action": "write" if name in wanted else "delete",
            "phase": "pending",
            "staging": None,
            "inode": None,
        }
        for name in sorted(wanted | set(owned))
    }


def _sync_secrets(
    pid: str, public: dict, record: dict, journal: dict, write, folders: list[str]
) -> dict:
    """Write the bound value to each reference file the plan names, and delete the ones it
    dropped, under the same project lock and journal, after the materials settled. Returns the
    written names' inodes, each re-read by path (`secret_files.settle`)."""
    effects = journal.get("secrets", {})
    if {n for n, e in effects.items() if e["action"] == "write"} != {
        row["name"] for row in public["secret_files"]
    }:
        raise store.Conflict("the accepted apply plan no longer reproduces")
    if not effects:
        return {}
    top = journal["secret_root"]
    owned = secret_files.validate_owned(record.get("secret_files", {}))
    if owned and record.get("secret_root") != top:
        raise store.Conflict("the deployment's secret files moved roots; remove and deploy again")
    secret_files.check_outside(folders, pid)
    # The project directory is journaled before any write: removal must find THIS directory.
    if journal.get("secret_dir") is None:
        if owned and record.get("secret_dir") is not None:
            # Its files' directory, re-proven: a replacement directory is refused, never adopted.
            secret_files.reap(top, pid, [], [], record["secret_dir"])
            journal["secret_dir"] = record["secret_dir"]
        else:
            journal["secret_dir"] = secret_files.directory(top, pid)
        write()
    expected = journal["secret_dir"]
    # The revision each secret had in the ACCEPTED review: a value rotated since is not written.
    accepted = {row["name"]: row.get("revision") for row in public["variables"]}
    resolver = None
    for name, effect in sorted(effects.items()):
        if effect["phase"] == "done":
            continue
        if effect["action"] == "delete":
            # `kept`: the name no longer holds the recorded file, so it is not ours to delete.
            secret_files.reap(top, pid, [name], [owned[name]], expected)
            effect["phase"] = "done"
            write()
            continue
        if resolver is None:
            try:
                resolver = lifecycle.template_vars.resolver(pid)  # one strict snapshot
            except lifecycle.template_vars.ResolutionUnavailable:
                raise store.StoreError(
                    "the variables store could not be read in full", status=503
                ) from None
        try:
            revision = resolver.secret_state(name)["revision"]
            value = resolver.secret(name)
        except (
            lifecycle.template_vars.BindingMissing,
            lifecycle.template_vars.BindingUnusable,
        ) as e:
            raise store.StoreError(str(e), status=409) from None
        if revision is None or revision != accepted.get(name):
            raise store.Conflict(f"{name}: the secret changed since the review; review it again")
        secret_files.write(top, pid, name, value, effect, owned.get(name), write, expected, folders)
    written = {n: e["inode"] for n, e in sorted(effects.items()) if e["action"] == "write"}
    secret_files.settle(top, pid, written, folders)
    return written


def proven_secrets(record: dict) -> dict[str, dict]:
    """Every reference-file name an apply may have left, with the inodes proven ours there.

    Per name: the inode the record owns, plus (while an apply is unsettled) the inode its journal
    created, with the staging name that inode was given. Removal reaps the final and staging
    names against exactly those inodes, so a publication interrupted at any point is found.
    """
    owned = secret_files.validate_owned(record.get("secret_files", {}))
    out = {
        name: {
            "inodes": [list(inode)],
            "names": [name],
            "root": record.get("secret_root"),
            "directory": record.get("secret_dir"),
        }
        for name, inode in owned.items()
    }
    journal = _journal(record)
    if journal is None or journal["state"] == "complete":
        return out
    for name, effect in journal.get("secrets", {}).items():
        row = out.setdefault(
            name,
            {
                "inodes": [],
                "names": [name],
                "root": journal.get("secret_root"),
                "directory": journal.get("secret_dir"),
            },
        )
        if effect["inode"] is not None:
            row["inodes"].append(list(effect["inode"]))
            row["names"].append(effect["staging"])
    return out


def verify_facts(bundle: dict) -> dict:
    """What `verify` checks, as APPLIED: the declared checks, each variable's kind and text default,
    and the required binaries. Secret-free: a secret variable has no default by the format's own
    rule. Verify must not read the editable source, which may have changed since apply."""
    return {
        "checks": list(bundle["verify"]),
        "variables": {
            v["name"]: {"kind": v["kind"], "default": v.get("default")} for v in bundle["variables"]
        },
        "binaries": list(bundle["requires"]["binaries"]),
    }


def review_facts(public: dict) -> dict:
    """The baseline a fleet update is compared against (#1191): the deployed revision's resolved
    probe targets, assignments, capability requests and variable names. The store keeps only a
    playbook's current revision, so the deployed facts are recorded here. No secret value enters:
    a secret is never a probe argument, and only variable NAMES are kept."""
    return {
        "targets": sorted(
            (
                {k: v for k, v in t.items() if k != "requires_confirmation"}
                for t in public["targets"]
            ),
            key=lambda t: t["id"],
        ),
        "assignments": public["assignments"],
        "capability_requests": sorted(public["capability_requests"]),
        "variables": sorted(v["name"] for v in public["variables"]),
        "secret_files": sorted(row["name"] for row in public["secret_files"]),
    }


def rituals(public: dict, deployment: str) -> list[dict]:
    """Each deployed ritual under its stable identity (deployment, ritual, playbook version).

    Scheduling belongs to Automations (#1201 Phase 4). Until it lands a ritual is recorded as
    declared and never scheduled, and nothing is created for it, so remove has nothing to delete.
    The identity is what a later proposal will be keyed by, so a retry or replay never duplicates.
    """
    version = public["playbook"]["version"]
    return [
        {
            "name": r["name"],
            "runbook": r["runbook"],
            "schedule": r["schedule"],
            "identity": {"deployment": deployment, "ritual": r["name"], "version": version},
            "state": "declared",
            "scheduled": False,
        }
        for r in public["rituals"]
    ]


def proven(record: dict, *, key: str) -> tuple[dict, dict]:
    """What an UNSETTLED apply provably made: ownership by journaled proof, and its directories.

    Only effects marked `done` count: a whole file or alias while its live leaf is still the
    journaled inode (whatever its bytes now, so an edit is a conflict, never a release), a region
    while the path is still a regular file (its markers and interior decide, as everywhere).
    Anything else the interrupted apply touched is not proven ours and is left alone. The accepted
    plan must still reproduce; if not, nothing is claimed.
    """
    journal = _journal(record)
    if journal is None or journal["state"] == "complete":
        return {}, {}
    plan = replay_plan.resume(
        record["playbook_id"], record["inputs"], _basis(record, journal), journal["plan"], key=key
    )
    changes, _ = mutation_plan.build(
        plan.rendered,
        plan.nodes,
        mutation_plan.ownership(record.get("files", {})),
        plan.public["deployment_id"],
    )
    folder = destination.Folder(**record["destination"])
    written = [
        c
        for c in changes
        if c.action in ("create", "replace") and journal["effects"][c.path]["phase"] == "done"
    ]
    live = destination.snapshot(folder, sorted(c.path for c in written)) if written else {}
    files = {}
    for change in written:
        if change.ownership is None:
            continue
        node = live[change.path]
        inode = journal["effects"][change.path]["inode"]
        owned = dict(change.ownership)
        if owned["kind"] in ("file", "symlink") and owned["disposition"] == "managed":
            # Proven by inode, not bytes: an in-place edit of OUR inode stays ours and the
            # planner reports it as a conflict; a different inode is foreign and is left alone.
            if inode is None or list(node.identity[:2]) != inode:
                continue
            owned["inode"] = inode
        elif owned["kind"] == "region" and node.kind != "file":
            continue
        # A region is keyed by its markers and interior, as everywhere: the planner strips it,
        # or reports a conflict for a damaged one. It is never silently released.
        files[change.path] = owned
    directories = {k: v for k, v in journal["directories"].items() if v is not None}
    return files, directories


def _effects(journal: dict, done: str, not_done: str) -> list[dict]:
    return [
        {
            "path": path,
            "action": effect["action"],
            "state": {"done": done, "pending": not_done}.get(effect["phase"], "unknown"),
        }
        for path, effect in sorted(journal["effects"].items())
        if effect["action"] != "keep"
    ]


def status(pid: str) -> dict:
    """The deployment's state. Any unsettled operation, whether bind, apply or remove, is
    `interrupted` with its `operation`, the `operation_id` a retry must reuse, and per-path
    effects where it has them. Internal record states are never exposed."""
    pid = lifecycle.template_vars.project_id(pid)
    with state.locked(pid) as locked:
        record = locked.read() if locked is not None else None
    if record is None:
        return {"project_id": pid, "state": "none"}
    out = {
        "project_id": pid,
        "deployment_id": record["id"],
        "playbook_id": record["playbook_id"],
        "state": record["state"],
        "generation": record.get("generation", 0),
    }
    removal = record.get("removal_operation")
    applying = _journal(record)
    binding = record.get("binding_operation")
    if isinstance(removal, dict):
        # Present only while unsettled: a settled removal drops every journal.
        out.update(state="interrupted", operation="remove", operation_id=removal.get("id"))
        out["files"] = _effects(removal, "removed", "not removed")
    elif applying is not None and applying["state"] != "complete":
        out.update(state="interrupted", operation="apply", operation_id=applying["id"])
        out["files"] = _effects(applying, "written", "not written")
    elif record["state"] == "binding_intent" or (
        isinstance(binding, dict) and binding.get("state") != "complete"
    ):
        out.update(
            state="interrupted",
            operation="bind",
            operation_id=binding.get("id") if isinstance(binding, dict) else None,
        )
    elif record["state"] not in {"bound", "applied", "removed"}:
        out["state"] = "interrupted"  # never expose an internal state
    if out["state"] == "applied":
        out["rituals"] = record.get("rituals", [])
        # An engine installed after apply reads none of this playbook's instructions yet (§11).
        out["instructions_missing"] = instructions.missing(record, instructions.present())
    return out
