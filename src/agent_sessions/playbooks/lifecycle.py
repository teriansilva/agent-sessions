"""Durable reviewed project binding, ahead of destination apply/remove (#1191).

The projects fence is outermost, then authoring/project locks, then the existing variable-store
mutation fence. A private intent records encrypted before/after binding records before the one
variable store changes. Retrying that exact operation reconciles those records without inventing
a second variable store or keeping plaintext secret inputs in the journal. No probe runs here.
"""

from __future__ import annotations

import copy
import hmac
import re
import time

from itsdangerous import BadData, URLSafeTimedSerializer

from .. import projects, template_vars
from . import apply, review, store, targets
from . import deployment_state as state

_OPERATION = re.compile(r"[a-z0-9][a-z0-9-]{7,63}")
_HISTORY_MAX = 10000


def operation_id(raw: object) -> str:
    if not isinstance(raw, str) or not _OPERATION.fullmatch(raw):
        raise store.StoreError("operation_id needs 8–64 lowercase letters, digits or hyphens")
    return raw


def _mine(records: list[dict], pid: str) -> dict[str, dict]:
    return {r["name"]: r for r in records if r["project_id"] == pid}


def _basis(record: dict) -> dict:
    return {**record, "state": "bound"}


def _replay(record: dict | None, pid: str, op: str, digest: str, key: str) -> dict | None:
    history = (record or {}).get("binding_history", {})
    if not isinstance(history, dict) or len(history) > _HISTORY_MAX:
        raise store.Conflict("the binding operation history is damaged")
    old = history.get(op)
    if old is None:
        if len(history) == _HISTORY_MAX:
            raise store.Conflict("the binding operation history is full")
        return None
    try:
        if set(old) != {"request_digest", "result"}:
            raise ValueError
        store.revision(old["request_digest"])
        if not hmac.compare_digest(old["request_digest"], digest):
            raise store.Conflict("the binding operation id already names another request")
        result = old["result"]
        if set(result) != {
            "project_id",
            "deployment_id",
            "operation_id",
            "state",
            "digest",
            "receipt",
        }:
            raise ValueError
        if (
            result["project_id"] != pid
            or result["operation_id"] != op
            or result["state"] != "bound"
        ):
            raise ValueError
        signed = URLSafeTimedSerializer(key, salt=review._SALT).loads(result["receipt"])
        if signed["digest"] != result["digest"]:
            raise ValueError
    except (BadData, KeyError, TypeError, ValueError):
        raise store.Conflict("the binding operation history is damaged") from None
    return copy.deepcopy(result)


def _journal(record: dict | None, pid: str, key: str) -> dict | None:
    if record is None or "binding_operation" not in record:
        return None
    try:
        journal = record["binding_operation"]
        if not isinstance(journal, dict) or set(journal) != {
            "id",
            "request_digest",
            "state",
            "before",
            "after",
            "apply_digest",
            "result",
        }:
            raise ValueError
        operation_id(journal["id"])
        store.revision(journal["request_digest"])
        store.revision(journal["apply_digest"])
        if journal["state"] not in {"intent", "complete"}:
            raise ValueError
        before, after = journal["before"], journal["after"]
        if not isinstance(before, dict) or not isinstance(after, dict) or set(before) != set(after):
            raise ValueError
        if len(after) > template_vars.PROJECT_VARIABLES_MAX:
            raise ValueError
        for snapshot in (before, after):
            for name, value in snapshot.items():
                if value is None and snapshot is before:
                    continue
                parsed = template_vars._coerce_record(value, template_vars.STORE_VERSION)
                if (
                    parsed != value
                    or parsed["scope"] != "project"
                    or parsed["project_id"] != pid
                    or parsed["name"] != name
                ):
                    raise ValueError
        result = journal["result"]
        if set(result) != {
            "project_id",
            "deployment_id",
            "operation_id",
            "state",
            "digest",
            "receipt",
        }:
            raise ValueError
        if (
            result["project_id"] != pid
            or result["deployment_id"] != record["id"]
            or result["operation_id"] != journal["id"]
            or result["state"] != "bound"
            or result["digest"] != journal["apply_digest"]
        ):
            raise ValueError
        signed = URLSafeTimedSerializer(key, salt=review._SALT).loads(result["receipt"])
        if signed["digest"] != result["digest"]:
            raise ValueError
        inputs = record["inputs"]
        if (
            inputs.get("bindings") != []
            or inputs.get("project_id") != pid
            or inputs.get("destination") != record["destination"]["path"]
        ):
            raise ValueError
    except (
        BadData,
        KeyError,
        TypeError,
        ValueError,
        template_vars.VariableError,
        store.StoreError,
    ):
        raise store.Conflict("the deployment binding journal is damaged") from None
    return journal


def _choices(plan: review.Plan, records: list[dict], pid: str) -> list[dict]:
    mine = _mine(records, pid)
    choices = copy.deepcopy(plan.fingerprint["inputs"]["bindings"])
    for row in plan.public["variables"]:
        name = row["name"]
        # Record every implicit global dependency, without claiming a preexisting project ref.
        if row.get("source") == "global" and name not in mine and name not in choices:
            choices[name] = {"name": name, "kind": row["kind"], "ref": "global"}
    return list(choices.values())


def _prepared(records: list[dict], pid: str, choices: list[dict]) -> tuple[list[dict], dict, dict]:
    after = copy.deepcopy(records)
    mine = _mine(records, pid)
    before, written = {}, {}
    now = time.time()
    for choice in choices:
        name = choice["name"]
        old = mine.get(name)
        before[name] = copy.deepcopy(old)
        created = old["created_at"] if old else now
        updated = template_vars._bumped(old["updated_at"]) if old else now
        if "ref" in choice:
            target = next(
                (r for r in records if r["project_id"] is None and r["name"] == name), None
            )
            if target is None or target["kind"] != choice["kind"]:
                raise store.Conflict(f"{name}: the reviewed global binding is no longer available")
            item = {
                **choice,
                "scope": "project",
                "project_id": pid,
                "created_at": created,
                "updated_at": updated,
            }
        else:
            item = template_vars._stored(choice, "project", pid, created, updated)
        if old:
            after.remove(old)
        after.append(item)
        written[name] = item
    after.sort(key=template_vars._key)
    if (
        len(after) > template_vars.RECORDS_MAX
        or len(_mine(after, pid)) > template_vars.PROJECT_VARIABLES_MAX
    ):
        raise store.StoreError("the variables store is full")
    return after, before, written


def _same_effects(before: review.Plan, after: review.Plan) -> None:
    # Binding changes provenance from unsaved input to project-owned records, and records an
    # explicit global dependency. It must not change a rendered byte, target or agent request.
    for name in (
        "playbook",
        "destination",
        "materials",
        "changes",
        "conflicts",
        "targets",
        "assignments",
        "capability_requests",
        "rituals",
        "deployment_id",
    ):
        if before.public[name] != after.public[name]:
            raise store.Conflict("binding changed the reviewed plan; review it again")

    def values(plan):
        return [
            {k: v for k, v in row.items() if k not in {"source", "revision", "explicit_global"}}
            for row in plan.public["variables"]
        ]

    if values(before) != values(after):
        raise store.Conflict("binding changed the reviewed values; review them again")


def _check_plan(record: dict, records: list[dict], key: str) -> None:
    journal = record["binding_operation"]
    live = review.build(
        record["playbook_id"], record["inputs"], key=key, _record=_basis(record), _variables=records
    )
    if not hmac.compare_digest(live.public["digest"], journal["apply_digest"]):
        raise store.Conflict("the pending binding's review changed; retain it for inspection")


def bind(pid: str, playbook_id: str, inputs: dict, receipt: str, op: str, *, key: str) -> dict:
    """Bind only what a signed review showed. An exact retry settles one durable operation."""
    pid, op = template_vars.project_id(pid), operation_id(op)
    store.playbook_id(playbook_id)
    if not isinstance(inputs, dict) or inputs.get("project_id") not in (None, pid):
        raise store.StoreError("bindings must name their destination project")
    request_digest = review._digest(
        {"project": pid, "playbook": playbook_id, "inputs": inputs, "receipt": receipt}, key
    )
    # This is the existing project update/archive/delete fence, not a separate owner index.
    with projects.locked_index() as index:
        project = index.get(pid)
        if project is None or project.archived:
            raise store.Conflict("the destination project is missing or archived")
        with state.locked(pid, create=True) as locked:
            assert locked is not None
            record = locked.read()
            if record is not None and record["state"] == "removed" and state.holds(record):
                raise store.Conflict(
                    "the removed deployment has an unsettled operation; recover it"
                )
            previous = _replay(record, pid, op, request_digest, key)
            if previous is not None:
                return previous
            journal = _journal(record, pid, key)
            if journal and journal["id"] == op:
                if not hmac.compare_digest(request_digest, journal["request_digest"]):
                    raise store.Conflict("the binding operation id already names another request")
                if journal["state"] == "complete":
                    return copy.deepcopy(journal["result"])
            elif record and record["state"] == "binding_intent":
                raise store.Conflict("retry the pending binding operation before starting another")
            elif apply.pending(record):
                raise store.Conflict("retry the pending apply operation before binding")
            else:
                journal = None

            def mutate(records: list[dict]) -> None:
                nonlocal record, journal
                # _mutate's general repair policy is too lenient for a deployment: fail closed
                # on any damaged store before recording intent, encrypting inputs or writing.
                if template_vars._read_strictly() != records:
                    raise store.Conflict("the variables store changed")
                if journal is None:
                    if inputs.get("create") is True:
                        # #1187 new-folder mode: adopt only the folder this review's CREATE made,
                        # still empty of every touched path, with identical effects.
                        original, effective = targets.adopt(
                            playbook_id, inputs, receipt, key=key, record=record, records=records
                        )
                    else:
                        effective = inputs
                        original = review.build(
                            playbook_id, inputs, key=key, _record=record, _variables=records
                        )
                    if original.public["destination"]["path"] not in project.folders:
                        raise store.Conflict("the project must own the reviewed destination")
                    if original.public["conflicts"]:
                        raise store.Conflict("resolve material conflicts before binding")
                    if effective is inputs:
                        review.accept(original, receipt, key=key)
                    # A pre-project review cannot authorize replacing bindings which it never
                    # saw on an already-existing project.
                    if inputs.get("project_id") is None and _mine(records, pid):
                        raise store.Conflict("the project has bindings; review them before binding")
                    after, before, written = _prepared(
                        records, pid, _choices(original, records, pid)
                    )
                    active = record if record and record["state"] != "removed" else None
                    pending = (
                        copy.deepcopy(active)
                        if active
                        else {
                            "version": state.VERSION,
                            "id": original.public["deployment_id"],
                            "project_id": pid,
                            "playbook_id": playbook_id,
                            "destination": original.public["destination"],
                            "files": {},
                            "generation": 0,
                            "prior_bindings": {},
                            "owned_bindings": {},
                            "binding_history": copy.deepcopy(
                                (record or {}).get("binding_history", {})
                            ),
                        }
                    )
                    pending["state"] = "bound"
                    pending["inputs"] = {
                        **effective,
                        "project_id": pid,
                        "bindings": [],
                        "destination": original.public["destination"]["path"],
                    }
                    expected = review.build(
                        playbook_id, pending["inputs"], key=key, _record=pending, _variables=after
                    )
                    _same_effects(original, expected)
                    # The original receipt has already passed timed signature and exact-plan
                    # checks. Re-sign the equivalent stored-input plan for the apply boundary.
                    confirmed = URLSafeTimedSerializer(key, salt=review._SALT).loads(
                        receipt, max_age=review.REVIEW_TTL
                    )
                    accepted = review.confirm(
                        expected, expected.public["digest"], confirmed["targets"], key=key
                    )
                    result = {
                        "project_id": pid,
                        "deployment_id": pending["id"],
                        "operation_id": op,
                        "state": "bound",
                        "digest": accepted["digest"],
                        "receipt": accepted["receipt"],
                    }
                    journal = {
                        "id": op,
                        "request_digest": request_digest,
                        "state": "intent",
                        "before": before,
                        "after": written,
                        "apply_digest": accepted["digest"],
                        "result": result,
                    }
                    pending["binding_operation"] = journal
                    pending["state"] = "binding_intent"
                    record = pending
                    locked.write(record)  # durable intent BEFORE the one variable store changes
                assert record is not None and journal is not None
                current = _mine(records, pid)
                names = set(journal["after"])
                observed = {name: current.get(name) for name in names}
                if observed == journal["after"]:
                    _check_plan(record, records, key)
                    return
                if observed != journal["before"]:
                    raise store.Conflict(
                        "a project binding changed during recovery; nothing was overwritten"
                    )
                intended = [
                    r for r in records if not (r["project_id"] == pid and r["name"] in names)
                ]
                intended.extend(copy.deepcopy(list(journal["after"].values())))
                intended.sort(key=template_vars._key)
                _check_plan(record, intended, key)
                for name in names:
                    if current.get(name):
                        template_vars._retire(current[name])
                records[:] = intended

            template_vars._mutate(mutate)
            assert record is not None and journal is not None
            for name, prior in journal["before"].items():
                record["prior_bindings"].setdefault(name, prior)
            record["owned_bindings"].update(journal["after"])
            journal["state"] = "complete"
            record["state"] = "bound"
            record.setdefault("binding_history", {})[op] = {
                "request_digest": request_digest,
                "result": copy.deepcopy(journal["result"]),
            }
            locked.write(record)
            return copy.deepcopy(journal["result"])
