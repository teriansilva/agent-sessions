"""Fleet listing and the combined update review (#1191), read-only; the update itself follows.

A fleet update may batch a project only when its update needs no operator decision of its own:
no material conflict, no new variable (a new required one would need input; any new one is
treated conservatively), no new or changed resolved probe target and no target awaiting
confirmation, no new capability request, and no changed agent assignment. The comparison is
against the review facts the deployment recorded at apply. A deployment without them, one not
cleanly `applied`, or one whose update review refuses, needs its own per-project review.

Each project's row carries the changes its update makes (path, action, before/after and diff), so
the combined review shows exactly what a batched update would write. The keyed digest covers the
playbook revision and every batchable project's own plan digest, so the later update can freeze
exactly that set and re-check each project.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path

from .. import atomicjson, template_vars
from . import apply, lifecycle, review, store
from . import deployment_state as state


def _current_revision(playbook_id: str) -> str:
    with store.root_lock(exclusive=False) as root_fd:
        entry = store._find(store._all_entries(root_fd, strict=True), playbook_id)
        if entry.error or entry.revision is None:
            raise store.StoreError("the playbook is invalid", status=409)
        return entry.revision


def _deployments(playbook_id: str) -> list[tuple[str, dict]]:
    out = []
    for pid in state.project_ids():
        with state.locked(pid) as locked:
            record = locked.read() if locked is not None else None
        if record is not None and record["playbook_id"] == playbook_id and state.holds(record):
            out.append((pid, record))
    return out


def projects(playbook_id: str) -> dict:
    """Every project running this playbook, its deployed revision and whether one is newer."""
    store.playbook_id(playbook_id)
    current = _current_revision(playbook_id)
    rows = []
    for pid, record in _deployments(playbook_id):
        deployed = record.get("inputs", {}).get("revision")
        rows.append(
            {
                "project_id": pid,
                "deployment_id": record["id"],
                "state": apply.status(pid)["state"],
                "revision": deployed,
                "update_available": deployed != current,
            }
        )
    return {"playbook_id": playbook_id, "revision": current, "projects": rows}


def _reasons(baseline: dict | None, public: dict) -> list[str]:
    if baseline is None:
        return ["the deployment has no recorded review baseline; review it on its own"]
    reasons = []
    if public["conflicts"]:
        reasons.append("the update has material conflicts")
    if any(t["requires_confirmation"] for t in public["targets"]):
        reasons.append("a probe target needs individual confirmation")
    if apply.review_facts(public)["targets"] != baseline["targets"]:
        reasons.append("a probe target is new or changed")
    if set(public["capability_requests"]) - set(baseline["capability_requests"]):
        reasons.append("the update requests a new capability")
    if public["assignments"] != baseline["assignments"]:
        reasons.append("an agent assignment changed")
    if {v["name"] for v in public["variables"]} - set(baseline["variables"]):
        reasons.append("the update adds a variable")
    return reasons


def plan(playbook_id: str, *, key: str) -> dict:
    """The combined update review: per project, batchable or why not, and one keyed digest."""
    listing = projects(playbook_id)
    current = listing["revision"]
    rows, batched = [], []
    for row in listing["projects"]:
        if not row["update_available"]:
            continue
        pid = row["project_id"]
        out = {"project_id": pid, "deployment_id": row["deployment_id"]}
        if row["state"] != "applied":
            rows.append(
                {**out, "batchable": False, "reasons": [f"the deployment is {row['state']}"]}
            )
            continue
        with state.locked(pid) as locked:
            record = locked.read() if locked is not None else None
        if record is None:
            rows.append({**out, "batchable": False, "reasons": ["the deployment disappeared"]})
            continue
        inputs = {**record["inputs"], "revision": current}
        try:
            update = review.build(playbook_id, inputs, key=key)
        except (store.StoreError, template_vars.VariableError) as e:
            rows.append({**out, "batchable": False, "reasons": [f"the update review refuses: {e}"]})
            continue
        reasons = _reasons(record.get("review_facts"), update.public)
        row_out = {
            **out,
            "batchable": not reasons,
            "reasons": reasons,
            "digest": update.public["digest"],
            # What the operator approves: every path the update changes, with its exact before
            # and after content and diff (the single-project review's own change shape).
            "changes": [c for c in update.public["changes"] if c["action"] != "keep"],
        }
        rows.append(row_out)
        if not reasons:
            batched.append([pid, update.public["digest"]])
    digest = review._digest({"fleet": playbook_id, "revision": current, "projects": batched}, key)
    return {"playbook_id": playbook_id, "revision": current, "projects": rows, "digest": digest}


# --- the batched update -------------------------------------------------------------------------

FLEET_DIR = ".fleet"
_JOURNAL_MAX = 4 * 1024 * 1024
_OUTCOMES = frozenset({"not-attempted", "applied", "stale", "failed"})


@contextlib.contextmanager
def _locked(playbook_id: str) -> Iterator[int]:
    """The playbook's fleet directory, exclusively locked for the whole operation.

    Opened under the shared authoring lock, which is released before any project is touched:
    each project's bind/apply takes the projects index and its own project lock itself.
    """
    created = store._open_root(create=True)
    if created is not None:
        os.close(created)
    with store.root_lock(exclusive=False) as root:
        if root is None:
            raise store.StoreError("the playbook store is unavailable", status=503)
        top = state._directory(root, FLEET_DIR, True)
        try:
            fd = state._directory(top, playbook_id, True)
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
            f"{op}.json",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=fd,
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
            raise store.Conflict("the fleet operation journal is not a bounded private file")
        value = json.loads(os.read(source, _JOURNAL_MAX + 1))
    except (UnicodeError, ValueError):
        raise store.Conflict("the fleet operation journal is damaged") from None
    finally:
        os.close(source)
    try:
        if not isinstance(value, dict) or set(value) != {
            "id",
            "request_digest",
            "playbook_id",
            "revision",
            "digest",
            "projects",
        }:
            raise ValueError
        for pid, row in value["projects"].items():
            template_vars.project_id(pid)
            if set(row) != {"plan_digest", "outcome", "detail", "bind"}:
                raise ValueError
            if row["bind"] is not None and (
                not isinstance(row["bind"], dict)
                or set(row["bind"]) != {"inputs", "receipt"}
                or not isinstance(row["bind"]["receipt"], str)
                or len(row["bind"]["receipt"]) > review.RECEIPT_MAX
            ):
                raise ValueError
            if row["outcome"] not in _OUTCOMES:
                raise ValueError
            store.revision(row["plan_digest"])
    except (KeyError, TypeError, ValueError, AttributeError, template_vars.VariableError):
        raise store.Conflict("the fleet operation journal is damaged") from None
    return value


def _write(fd: int, journal: dict) -> None:
    atomicjson.atomic_write_json(Path(f"/proc/self/fd/{fd}") / f"{journal['id']}.json", journal)


def _derived(op: str, pid: str, kind: str, key: str) -> str:
    """The deterministic per-project operation id, so a same-id fleet retry reconciles."""
    return f"fleet-{kind}-" + review._digest({"fleet": op, "project": pid, "kind": kind}, key)[:40]


def _update_one(
    playbook_id: str, pid: str, revision: str, row: dict, op: str, key: str, write
) -> tuple[str, str | None]:
    """Bind and apply one project's frozen update; the outcome it reached.

    The exact bind request (inputs and receipt) is journaled BEFORE bind runs, so a crash at any
    point of bind (including after its durable intent) is recovered by replaying that request
    through bind's own same-id recovery, never by a fresh review (which refuses a pending bind).
    """
    bind_op, apply_op = _derived(op, pid, "b", key), _derived(op, pid, "a", key)
    with state.locked(pid) as locked:
        record = locked.read() if locked is not None else None
    if record is None:
        return "stale", "the deployment disappeared"
    if apply_op in record.get("apply_history", {}):
        return "applied", None  # an earlier attempt finished; its outcome was not yet recorded
    bound = record.get("binding_history", {}).get(bind_op)
    if bound is not None:
        result = bound["result"]
    else:
        if row["bind"] is None:
            inputs = {**record["inputs"], "revision": revision}
            update = review.build(playbook_id, inputs, key=key)
            if update.public["digest"] != row["plan_digest"]:
                return "stale", "the project changed since the fleet review; review it on its own"
            receipt = review.confirm(update, row["plan_digest"], [], key=key)["receipt"]
            row["bind"] = {"inputs": inputs, "receipt": receipt}
            write()  # the exact request, before bind can record any intent
        request = row["bind"]
        try:
            result = lifecycle.bind(
                pid, playbook_id, request["inputs"], request["receipt"], bind_op, key=key
            )
        except store.StoreError:
            after = _record(pid) or {}
            pending = (after.get("binding_operation") or {}).get("id") == bind_op
            if not pending:
                row["bind"] = None  # refused before any intent: the next retry reviews afresh
                write()
            raise
    apply.apply(pid, apply_op, result["receipt"], key=key)
    return "applied", None


def _record(pid: str) -> dict | None:
    with state.locked(pid) as locked:
        return locked.read() if locked is not None else None


def update(playbook_id: str, op: str, digest: str, *, key: str) -> dict:
    """Apply the combined review's batch: frozen set, per-project re-check, same-id retry.

    Only the projects the reviewed digest batched are ever enrolled. Each is re-checked under its
    own lifecycle fences; a changed project is `stale`, a refused one `failed`. A retry with the
    same id attempts only `failed` and `not-attempted` projects, with the same frozen plans.
    """
    store.playbook_id(playbook_id)
    op = lifecycle.operation_id(op)
    store.revision(digest)
    request_digest = review._digest({"fleet": playbook_id, "update": digest}, key)
    with _locked(playbook_id) as fd:
        journal = _read(fd, op)
        if journal is not None:
            if not hmac.compare_digest(journal["request_digest"], request_digest):
                raise store.Conflict("the fleet operation id already names another request")
        else:
            reviewed = plan(playbook_id, key=key)
            if not hmac.compare_digest(reviewed["digest"], digest):
                raise store.Conflict("the fleet changed since its review; review it again")
            journal = {
                "id": op,
                "request_digest": request_digest,
                "playbook_id": playbook_id,
                "revision": reviewed["revision"],
                "digest": digest,
                "projects": {
                    row["project_id"]: {
                        "plan_digest": row["digest"],
                        "outcome": "not-attempted",
                        "detail": None,
                        "bind": None,
                    }
                    for row in reviewed["projects"]
                    if row["batchable"]
                },
            }
            _write(fd, journal)  # the frozen set and plans, BEFORE any project is touched
        for pid in sorted(journal["projects"]):
            row = journal["projects"][pid]
            if row["outcome"] not in ("not-attempted", "failed"):
                continue
            try:
                outcome, detail = _update_one(
                    playbook_id, pid, journal["revision"], row, op, key, lambda: _write(fd, journal)
                )
            except (store.StoreError, template_vars.VariableError) as e:
                outcome, detail = "failed", str(e)
            row.update(outcome=outcome, detail=detail)
            _write(fd, journal)
        return _public(journal)


def _public(journal: dict) -> dict:
    return {
        "operation_id": journal["id"],
        "playbook_id": journal["playbook_id"],
        "revision": journal["revision"],
        "projects": [
            {"project_id": pid, "outcome": row["outcome"], "detail": row["detail"]}
            for pid, row in sorted(journal["projects"].items())
        ],
    }


def operation(playbook_id: str, op: str) -> dict:
    """A fleet operation's per-project outcomes (read-only)."""
    store.playbook_id(playbook_id)
    op = lifecycle.operation_id(op)
    with _locked(playbook_id) as fd:
        journal = _read(fd, op)
    if journal is None:
        raise store.StoreError("no such fleet operation", status=404)
    return _public(journal)
