"""Remove a playbook deployment as the exact inverse of what it can PROVE it owns (#1191).

The same rule as apply: ownership is an inode proof (or, for regions, recorded content). The
removal set is the record's committed ownership plus anything an interrupted apply journal can
prove it made (`apply.proven`). Whole files and aliases move into retention (never deleted);
regions are stripped through the identity-bound guarded save; seeds and references stay.
Directories are never deleted: the ones this deployment created are left in place and reported.
Bindings return to their exact prior records.

`plan` is the dry run: its keyed digest covers the record facts, the proven ownership and the
live pre-state. `remove` accepts only that digest, journals the frozen pre-state before the first
effect, and a same-id retry reconciles. Settlement drops every operation journal, so a removed
record stops holding its project and source playbook. No probe, agent or shell runs here.
"""

from __future__ import annotations

import base64
import copy
import hmac

from .. import projects, template_vars
from ..fsbrowse import FsError
from . import (
    apply,
    binding_inverse,
    destination,
    lifecycle,
    materials,
    mutation_plan,
    replay_plan,
    review,
    secret_files,
    store,
)
from . import deployment_state as state

_HISTORY_MAX = 10000
_JOURNAL = {
    "id",
    "request_digest",
    "state",
    "digest",
    "prestate",
    "owned",
    "remove_directories",
    "directories",
    "effects",
    "attempt",
}
#: Absent from journals written before secret reference files (#1191).
_SECRETS = {"secret_files"}
_OPERATIONS = ("binding_operation", "apply_operation", "removal_operation")


def _removable(record: dict | None) -> dict:
    if record is None or record.get("state") not in {"bound", "applied"}:
        raise store.Conflict("there is no active deployment to remove")
    binding = record.get("binding_operation")
    if binding is not None and binding.get("state") != "complete":
        raise store.Conflict("retry the pending binding operation before removing")
    return record


def _owned(record: dict, key: str) -> tuple[dict, dict]:
    files = copy.deepcopy(mutation_plan.ownership(record.get("files", {})))
    directories = copy.deepcopy(record.get("directories", {}))
    try:
        proven_files, proven_directories = apply.proven(record, key=key)
    except store.StoreError:
        raise store.Conflict(
            "the interrupted apply no longer reproduces, so what it made cannot be proven; "
            "nothing was removed"
        ) from None
    for path, owned in proven_files.items():
        files[path] = owned  # the interrupted apply WROTE this path: its proof supersedes
    for rel, identity in proven_directories.items():
        directories.setdefault(rel, identity)
    return files, directories


def _secrets(record: dict) -> dict[str, dict]:
    """Per reference file: the names to reap and the inodes proven ours there (`proven_secrets`)."""
    try:
        return apply.proven_secrets(record)
    except (ValueError, TypeError, store.StoreError):
        raise store.Conflict("the deployment's secret files record is damaged") from None


def _validate_secrets(owned: object) -> None:
    if not isinstance(owned, dict):
        raise ValueError
    for name, row in owned.items():
        secret_files._name(name)
        if not isinstance(row, dict) or set(row) != {"inodes", "names", "root", "directory"}:
            raise ValueError
        if row["directory"] is not None:
            secret_files.validate_owned({name: row["directory"]})
        secret_files.validate_root(row["root"])
        if not isinstance(row["inodes"], list) or not isinstance(row["names"], list):
            raise ValueError
        for inode in row["inodes"]:
            secret_files.validate_owned({name: inode})
        for staging in row["names"][1:]:
            secret_files.staging_name(staging)
        if row["names"][:1] != [name]:
            raise ValueError


def _snapshot(record: dict, files: dict) -> dict[str, destination.Node]:
    folder = destination.Folder(**record["destination"])
    return destination.snapshot(folder, sorted(files)) if files else {}


def _digest(record: dict, files: dict, directories: dict, nodes: dict, key: str) -> str:
    return review._digest(
        {
            "secret_files": _secrets(record),
            "project": record["project_id"],
            "deployment": record["id"],
            "destination": record["destination"],
            "files": files,
            "directories": directories,
            "bindings": {
                "owned": record.get("owned_bindings", {}),
                "prior": record.get("prior_bindings", {}),
            },
            "prestate": {
                path: {
                    "kind": n.kind,
                    "identity": list(n.identity),
                    "target": n.target,
                    "digest": materials.digest(n.data) if n.data is not None else None,
                }
                for path, n in nodes.items()
            },
        },
        key,
    )


def _freeze(nodes: dict[str, destination.Node], digest: str) -> dict:
    return {
        "version": 1,
        "digest": digest,
        "prestate": {
            path: {
                "kind": n.kind,
                "identity": list(n.identity),
                "data": base64.b64encode(n.data).decode() if n.data is not None else None,
                "target": n.target,
            }
            for path, n in nodes.items()
        },
    }


def plan(pid: str, *, key: str) -> dict:
    """The dry run: exactly what removal would do, and the digest that authorizes it."""
    pid = template_vars.project_id(pid)
    with state.locked(pid) as locked:
        record = _removable(locked.read() if locked is not None else None)
        files, directories = _owned(record, key)
        try:
            nodes = _snapshot(record, files)
        except FsError as e:
            raise store.StoreError(str(e), status=e.status) from None
        changes, conflicts = mutation_plan.build([], nodes, files, record["id"])
        binding_refusal = None
        try:
            binding_inverse.prepare(record, template_vars._read_strictly())
        except store.Conflict as e:
            binding_refusal = str(e)
        return {
            "project_id": pid,
            "deployment_id": record["id"],
            "playbook_id": record["playbook_id"],
            "changes": [mutation_plan.public(c) for c in changes],
            "conflicts": conflicts,
            "bindings": {
                "remove": sorted(record.get("owned_bindings", {})),
                "restore": sorted(
                    n for n, v in record.get("prior_bindings", {}).items() if v is not None
                ),
                "refusal": binding_refusal,
            },
            "directories": sorted(directories),
            # Deleted while each is still the file this deployment wrote; otherwise kept.
            "secret_files": [
                {"name": name, "path": secret_files.path(pid, name, row["root"])}
                for name, row in sorted(_secrets(record).items())
            ],
            "digest": _digest(record, files, directories, nodes, key),
        }


def _journal(record: dict) -> dict | None:
    journal = record.get("removal_operation")
    if journal is None:
        return None
    try:
        if not isinstance(journal, dict) or set(journal) - _SECRETS != _JOURNAL:
            raise ValueError
        _validate_secrets(journal.get("secret_files", {}))
        lifecycle.operation_id(journal["id"])
        store.revision(journal["request_digest"])
        store.revision(journal["digest"])
        if journal["state"] != "intent" or journal["directories"] != {}:
            raise ValueError
        if type(journal["attempt"]) is not int or not 0 <= journal["attempt"] < 1_000_000:
            raise ValueError
        mutation_plan.ownership(journal["owned"])
        apply._journal({"apply_operation": {**_shape(journal)}})
    except (TypeError, ValueError, KeyError, store.StoreError):
        raise store.Conflict("the deployment removal journal is damaged") from None
    return journal


def _shape(journal: dict) -> dict:
    """Validate effects and directory identities with apply's journal validator."""
    return {
        "id": journal["id"],
        "request_digest": journal["request_digest"],
        "state": "intent",
        "basis_state": "applied",
        "plan": {},
        "effects": journal["effects"],
        "directories": journal["remove_directories"],
        "attempt": journal["attempt"],
    }


def remove(pid: str, op: str, digest: str, *, key: str) -> dict:
    """Remove only what the confirmed dry run showed; a same-id retry settles one operation."""
    pid, op = template_vars.project_id(pid), lifecycle.operation_id(op)
    store.revision(digest)
    request_digest = review._digest({"project": pid, "remove": digest}, key)
    with projects.locked_index():
        with state.locked(pid) as locked:
            record = locked.read() if locked is not None else None
            if record is None:
                raise store.Conflict("there is no deployment to remove")
            assert locked is not None
            history = record.get("removal_history", {})
            if not isinstance(history, dict) or len(history) > _HISTORY_MAX:
                raise store.Conflict("the removal operation history is damaged")
            old = history.get(op)
            if old is not None:
                if not hmac.compare_digest(str(old.get("request_digest")), request_digest):
                    raise store.Conflict("the removal operation id already names another request")
                return copy.deepcopy(old["result"])
            journal = _journal(record)
            if journal is not None:
                if journal["id"] != op:
                    raise store.Conflict("retry the pending removal operation before another")
                if not hmac.compare_digest(journal["request_digest"], request_digest):
                    raise store.Conflict("the removal operation id already names another request")
            else:
                _removable(record)
                files, directories = _owned(record, key)
                try:
                    nodes = _snapshot(record, files)
                except FsError as e:
                    raise store.StoreError(str(e), status=e.status) from None
                changes, conflicts = mutation_plan.build([], nodes, files, record["id"])
                if conflicts:
                    raise store.Conflict("resolve the removal conflicts first; nothing was removed")
                if not hmac.compare_digest(_digest(record, files, directories, nodes, key), digest):
                    raise store.Conflict("the removal plan changed; review it again")
                binding_inverse.prepare(record, template_vars._read_strictly())
                owned_secrets = _secrets(record)
                journal = {
                    "id": op,
                    "request_digest": request_digest,
                    "state": "intent",
                    "digest": digest,
                    "prestate": _freeze(nodes, digest),
                    "owned": files,
                    "remove_directories": directories,
                    "directories": {},
                    "effects": {
                        c.path: {
                            "action": c.action,
                            "phase": "pending",
                            "entry": None,
                            "inode": None,
                        }
                        for c in changes
                    },
                    "attempt": 0,
                    "secret_files": owned_secrets,
                }
                record["removal_operation"] = journal
                locked.write(record)  # durable intent BEFORE any destination or store effect

            def write() -> None:
                locked.write(record)

            _, nodes = replay_plan._nodes(journal["prestate"])
            changes, conflicts = mutation_plan.build([], nodes, journal["owned"], record["id"])
            if conflicts or {c.path: c.action for c in changes} != {
                p: e["action"] for p, e in journal["effects"].items()
            }:
                raise store.Conflict("the accepted removal plan no longer reproduces")
            folder = destination.Folder(**record["destination"])
            try:
                for change in changes:
                    apply._effect(folder, change, nodes, journal, write, locked.verify)
                apply._settle(folder, changes, journal["effects"], {})
                # Idempotent, so a same-id retry simply runs them again: a file already deleted
                # reads `absent`, and one that is no longer the recorded inode is kept.
                secrets_outcome = {
                    name: secret_files.reap(
                        row["root"], pid, row["names"], row["inodes"], row["directory"]
                    )[name]
                    for name, row in sorted(journal.get("secret_files", {}).items())
                }
            except FsError as e:
                raise store.StoreError(str(e), status=e.status) from None
            except OSError:
                raise store.StoreError(
                    "the deployment destination could not be changed safely", status=409
                ) from None
            # Directories are never deleted: removing an empty folder races concurrent writers and
            # buys nothing, while a leftover empty folder is harmless and reversible. Report them.
            kept = sorted(journal["remove_directories"])
            prior = record.get("prior_bindings", {})

            def restore(records: list[dict]) -> None:
                current = {r["name"]: r for r in records if r["project_id"] == pid}
                if {name: current.get(name) for name in prior} == prior:
                    return  # already restored by an earlier attempt of this operation
                result, owned, _ = binding_inverse.prepare(record, records)
                for name, item in owned.items():
                    if item != prior.get(name):
                        template_vars._retire(item)
                records[:] = result

            template_vars._mutate(restore)
            result = {
                "project_id": pid,
                "deployment_id": record["id"],
                "operation_id": op,
                "state": "removed",
                "kept_directories": kept,
                "kept_secret_files": [
                    secret_files.path(pid, n, journal["secret_files"][n]["root"])
                    for n, o in secrets_outcome.items()
                    if o == "kept"
                ],
                # Only ever declared (#1201 Phase 4 not landed): nothing was created to delete.
                "rituals_retired": [r["identity"] for r in record.get("rituals", [])],
            }
            record.update(
                state="removed",
                files={},
                secret_files={},
                directories={},
                rituals=[],
                owned_bindings={},
                prior_bindings={},
            )
            record.pop("secret_root", None)
            record.pop("secret_dir", None)
            for name in _OPERATIONS:
                record.pop(name, None)  # a settled removal holds nothing (deployment_state)
            record.setdefault("removal_history", {})[op] = {
                "request_digest": request_digest,
                "result": result,
            }
            write()
            return copy.deepcopy(result)
