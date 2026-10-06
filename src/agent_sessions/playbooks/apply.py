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
    lifecycle,
    material_write,
    materials,
    mutation_plan,
    replay_plan,
    review,
    store,
)

#: Beneath the editor's recovery store, so the same-filesystem rule and its upload refusal apply.
#: A leading dot keeps the editor's own resolver from treating these as its records.
ENTRIES = ".playbooks"
_HISTORY_MAX = 10000
_RESULT = {"project_id", "deployment_id", "operation_id", "state", "digest"}


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
        if not isinstance(journal, dict) or set(journal) != {
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


def status(pid: str) -> dict:
    """The deployment's state and, for an unsettled apply, each path's last known effect."""
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
    journal = _journal(record)
    if journal is not None and journal["state"] != "complete":
        out["state"] = "interrupted"
        out["operation_id"] = journal["id"]
        out["files"] = [
            {
                "path": path,
                "action": effect["action"],
                "state": {"done": "written", "pending": "not written"}.get(
                    effect["phase"], "unknown"
                ),
            }
            for path, effect in sorted(journal["effects"].items())
            if effect["action"] != "keep"
        ]
    return out
