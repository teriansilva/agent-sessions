"""Apply a procedure to a mission (#1777 S1b, #1613 P4 slice 1: one project).

An **objective** is one project, one procedure from that project's playbook, and that procedure's
inputs (#1613 §1). This module builds the two things the operator applies from:

* **the listing** — the mission project's DEPLOYED playbook and its procedures, by index, with a
  digest of exactly that list (`listing`). The client names a procedure only by its index plus
  that digest; a list that changed in between is a 409, never a different procedure.
* **a mission-local procedure** — a format-5 flow the operator wrote for this mission, validated
  by the bundle loader's own flow validator (`playbooks.validate.flow_document`). Never a bypass.

**Where every probe target comes from** (#1777 D-P4-2, #1076's principle):

* a deployed procedure: ONLY the deployment's recorded, reviewed targets (`review_facts`, written
  at apply from the review the operator confirmed, including any individually confirmed format-2
  default). Current bindings are not re-read, so nothing the operator did not review can become a
  target. The playbook's current revision must BE the deployed revision; otherwise the listing
  says "update the deployment first" — nothing is silently remapped;
* a mission-local procedure: the project's current bindings (`binding.resolve`), and a target
  that would come from a bundle DEFAULT is refused.

Either way each concrete argument passes `missions.validate_probe_args` at the store. An argument
that waits for an upstream step's output (`{{steps.<id>.<slot>}}`) is not a target yet: the row is
written as a required `none` gate carrying its real probe in `pending` (D-P4-3), which no runner
probes and `gate_tally` counts as unmet.

Every function here is BLOCKING (store reads, then one missions transaction); routes run them
through `missions.run_admitted`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tomllib

from . import mission_plan_document as plan_doc
from . import missions, template_vars
from .playbooks import PlaybookFormatError, binding, schema, store, validate
from .playbooks import deployment_state as state

#: The ONE reason that means "there is no deployment" — every other `_deployment` reason is a
#: deployment that exists but can't be used or read, and must never pass for an absent one.
NO_DEPLOYMENT = "this project has no deployed playbook"


class ProcedureError(missions.MissionError):
    """A refusal the route answers with its status (409: stale or not applicable; 422: invalid)."""


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()[:32]


def _current_bundle(playbook_id: str) -> tuple[dict, str]:
    """The validated bundle at its current revision, or `StoreError`. The bundle also carries
    each flow AS AUTHORED (`_documents`), which is what a plan snapshot stores and the store
    re-validates (`mission_plan_document`); parsed from the same tree snapshot it was validated
    from, so the two cannot be different bytes."""
    store.playbook_id(playbook_id)
    with store.root_lock(exclusive=False) as root_fd:
        entry = store._find(store._all_entries(root_fd, strict=True), playbook_id)
    if entry.error or entry.pb is None or entry.revision is None or entry.tree is None:
        raise store.StoreError("the project's playbook is invalid", status=409)
    documents = {}
    for fid in entry.pb["flows"]:
        data = entry.tree.files.get(f"{schema.FLOWS_DIR}/{fid}.toml")
        if data is None:
            raise store.StoreError("the project's playbook is invalid", status=409)
        documents[fid] = tomllib.loads(data.decode("utf-8"))
    return {**entry.pb, "_documents": documents}, entry.revision


def _deployment(project_id: str) -> tuple[dict | None, dict | None, str | None]:
    """`(record, bundle, reason)`: the project's applied deployment and the bundle it deployed, or
    the reason there is none to apply from. Fail closed: a damaged, unsettled or out-of-date
    deployment is a reason, never an empty list that looks like "no playbook"."""
    try:
        record = state.active(project_id)
    except store.StoreError as e:
        return None, None, f"the project's deployment record cannot be read ({e})"
    if record is None:
        return None, None, NO_DEPLOYMENT
    if record["state"] != "applied" or state.pending(record):
        return record, None, "the project's deployment is not settled; finish or recover it first"
    facts = record.get("review_facts")
    deployed = (record.get("applied_paths") or {}).get("revision")
    if (
        not isinstance(facts, dict)
        or not isinstance(facts.get("targets"), list)
        or not isinstance(deployed, str)
        or (record.get("inputs") or {}).get("revision") != deployed
    ):
        return record, None, "the deployment has no recorded review; review and apply it again"
    try:
        bundle, current = _current_bundle(record["playbook_id"])
    except store.StoreError as e:
        return record, None, f"the project's playbook cannot be read ({e.detail})"
    if current != deployed:
        return (
            record,
            None,
            "the playbook changed since this project deployed it; update the deployment first",
        )
    return record, bundle, None


def _reviewed_items(flow_id: str, flow: dict, facts: dict) -> list[dict]:
    """The procedure's evidence with each target taken from the deployment's review, or the
    `ProcedureError` saying why this procedure cannot be applied from it."""
    targets = {t.get("id"): t for t in facts["targets"] if isinstance(t, dict)}
    items: list[dict] = []
    for step in flow["steps"]:
        for item in step["checklist"]:
            t = targets.get(f"flow:{flow_id}:{step['id']}:{item['key']}")
            if t is None or t.get("kind") != "probe" or t.get("probe") != item["probe"]:
                raise ProcedureError(
                    "the deployment's review does not cover this procedure; review and apply "
                    "the deployment again",
                    status=409,
                )
            args, pending = t.get("args"), t.get("pending")
            defaults = t.get("default_variables")
            if (
                not isinstance(args, dict)
                or not isinstance(pending, dict)
                or not isinstance(defaults, list)
            ):
                raise ProcedureError("the deployment's review is damaged", status=409)
            # FAIL CLOSED ON A BUNDLE DEFAULT (the independent review on #1790). The review
            # record says which targets a bundle default would choose, but NOT which of them the
            # operator individually confirmed: a deployment applies with such targets left
            # unarmed. So a target derived from a default is never a mission's — concrete or
            # waiting — until the deployment records the confirmation itself (a follow-up).
            if defaults:
                raise ProcedureError(
                    "a probe target in this procedure comes from a playbook default ("
                    + ", ".join(sorted(str(d) for d in defaults))
                    + "); bind a project value for it and apply the deployment again",
                    status=409,
                )
            items.append(_item(step, item, args, pending))
    return items


def _item(step: dict, item: dict, args: dict, pending: dict) -> dict:
    title = item["title"]
    if pending:
        # WAITING for an upstream step's output: a `none` gate until the bridge (S1c) turns it
        # into its real probe. It GATES even when the item is optional (Hermes on #1790): a row no
        # runner can probe must never let a mission settle around it. Its own `required` travels
        # in `pending.gate`, which the bridge restores. Unmet in `gate_tally`; probed by nothing.
        return {
            "step": step["id"],
            "key": item["key"],
            "title": title,
            "probe": "none",
            "probe_args": None,
            "pending": {
                "probe": item["probe"],
                "args": copy.deepcopy(args),
                "refs": copy.deepcopy(pending),
                "gate": bool(item["required"]),
            },
            "gate": True,
        }
    missions.validate_probe_args(item["probe"], args)
    return {
        "step": step["id"],
        "key": item["key"],
        "title": title,
        "probe": item["probe"],
        "probe_args": copy.deepcopy(args) or None,
        "pending": None,
        "gate": item["required"],
    }


def _ready_reason(flow_id: str, flow: dict, facts: dict) -> str | None:
    try:
        items = _reviewed_items(flow_id, flow, facts)
        for it in items:
            plan_doc.evidence_key("o99", it["key"])
    except missions.MissionError as e:
        return str(e).removeprefix("invalid plan: ")
    if len(items) > missions.OBJECTIVES_MAX:
        return f"this procedure has more evidence than a mission holds ({missions.OBJECTIVES_MAX})"
    return None


def _variables_used(flow: dict) -> list[str]:
    names: set[str] = set()
    for step in flow["steps"]:
        for item in step["checklist"]:
            for value in item["probe_args"].values():
                ref = validate._whole_var_ref(value)
                if ref is not None:
                    names.add(ref)
    return sorted(names)


def listing(mission_id: str) -> dict:
    """The procedures this mission can apply, by index, with the digest of exactly this list."""
    return _listing(mission_id)[0]


def _flow_targets(facts: dict, fid: str) -> list:
    """The reviewed targets of one flow, in a stable order: part of what a listing digest binds."""
    prefix = f"flow:{fid}:"
    rows = [t for t in facts.get("targets", []) if isinstance(t, dict)]
    return sorted(
        (t for t in rows if str(t.get("id", "")).startswith(prefix)),
        key=lambda t: str(t.get("id")),
    )


def _listing(mission_id: str) -> tuple[dict, dict | None, dict | None]:
    """`(listing, record, bundle)`: the listing AND the one deployment snapshot it was built from.

    The digest binds that snapshot — the deployment's identity and generation, the deployed
    revision, each flow's digest AND each flow's reviewed targets (Hermes on #1790) — and an apply
    builds its rows from this same snapshot, never from a second read. So the targets written are
    exactly the ones the operator's `{index, listing}` was chosen against, and a re-applied
    deployment with other targets is a different listing."""
    scope = missions.procedure_scope(mission_id)
    pid = scope["project_id"]
    out: dict = {
        "mission_id": mission_id,
        "project_id": pid,
        "playbook": None,
        "reason": None,
        "procedures": [],
    }
    basis: dict = {"project": pid, "playbook": None, "flows": []}
    record = bundle = None
    if not pid:
        out["reason"] = "the mission has no project; every objective names one (choose it first)"
    else:
        record, bundle, reason = _deployment(pid)
        out["reason"] = reason
        if bundle is not None and record is not None:
            revision = record["applied_paths"]["revision"]
            out["playbook"] = {
                "id": record["playbook_id"],
                "name": bundle["identity"]["name"],
                "revision": revision,
            }
            basis["playbook"] = [
                record["playbook_id"],
                revision,
                record.get("id"),
                record.get("generation"),
            ]
            for index, (fid, flow) in enumerate(sorted(bundle["flows"].items())):
                why = _ready_reason(fid, flow, record["review_facts"])
                out["procedures"].append(
                    {
                        "index": index,
                        "flow_id": fid,
                        "title": flow["title"],
                        "description": flow["description"],
                        "steps": len(flow["steps"]),
                        "default": fid == bundle["default_flow"],
                        "ready": why is None,
                        "reason": why,
                    }
                )
                basis["flows"].append(
                    [fid, plan_doc.flow_digest(flow), _flow_targets(record["review_facts"], fid)]
                )
    out["listing"] = _digest(basis)
    return out, record, bundle


def apply_listed(mission_id: str, index: object, listing_digest: object) -> dict:
    """Apply the procedure at `index` of the listing whose digest the client echoes."""
    if type(index) is not int or index < 0:
        raise ProcedureError("index must be a non-negative integer", status=422)
    if not isinstance(listing_digest, str) or not listing_digest:
        raise ProcedureError(
            "listing is required: the digest of the list you chose from", status=422
        )
    now, record, bundle = _listing(mission_id)
    if now["listing"] != listing_digest:
        raise ProcedureError("the procedure list changed; read it again", status=409)
    if now["reason"] is not None:
        raise ProcedureError(now["reason"], status=409)
    if index >= len(now["procedures"]):
        raise ProcedureError("no procedure has that index", status=422)
    row = now["procedures"][index]
    if not row["ready"]:
        raise ProcedureError(row["reason"], status=409)
    pid = now["project_id"]
    assert record is not None and bundle is not None  # a listing with procedures has both
    fid = row["flow_id"]
    flow = bundle["flows"][fid]
    revision = record["applied_paths"]["revision"]
    items = _reviewed_items(fid, flow, record["review_facts"])
    procedure = {
        "source": "playbook",
        "playbook_id": record["playbook_id"],
        "revision": revision,
        "flow_id": fid,
        "title": flow["title"],
        "flow": copy.deepcopy(flow),
        "digest": plan_doc.flow_digest(flow),
        "document": copy.deepcopy(bundle["_documents"][fid]),
        "variables": copy.deepcopy(bundle["variables"]),
        "manifest_format": bundle["format"],
    }
    inputs = [{"name": n, "source": "deployment"} for n in _variables_used(flow)]
    return missions.apply_procedure(
        mission_id,
        project_id=pid,
        procedure=procedure,
        inputs=inputs,
        items=items,
        reason=f"applied {record['playbook_id']}@{revision[:12]} · {fid}",
    )


def apply_local(mission_id: str, flow_id: object, doc: object) -> dict:
    """Apply a mission-local procedure the operator wrote: the bundle's own flow validator, and
    targets from the project's current bindings with bundle defaults refused."""
    scope = missions.procedure_scope(mission_id)
    pid = scope["project_id"]
    if not pid:
        raise ProcedureError(
            "the mission has no project; every objective names one (choose it first)", status=409
        )
    record, bundle, reason = _deployment(pid)
    if bundle is None and reason != NO_DEPLOYMENT:
        # The project HAS a playbook, and its declarations are what a mission-local procedure may
        # reference — but not while the deployment is unsettled or out of date.
        raise ProcedureError(reason or "the project's deployment is not usable", status=409)
    variables = list(bundle["variables"]) if bundle is not None else []
    if not isinstance(flow_id, str):
        raise ProcedureError("flow_id is required", status=422)
    try:
        flow = validate.flow_document(doc, flow_id, variables)
    except PlaybookFormatError as e:
        raise ProcedureError(f"the procedure is invalid: {e}", status=422) from None
    try:
        resolved = binding.resolve({"variables": variables}, pid, [])
        targets = binding.targets({"flows": {flow_id: flow}, "connections": []}, resolved)
    except (
        store.StoreError,
        missions.MissionError,
        template_vars.BindingUnusable,
        template_vars.ResolutionUnavailable,
        template_vars.VariableStoreUnsupported,
        template_vars.VariableError,
    ) as e:
        raise ProcedureError(f"the procedure's inputs cannot be bound: {e}", status=409) from None
    defaulted = sorted({v for t in targets for v in t["default_variables"]})
    if defaulted:
        raise ProcedureError(
            "a probe target would come from a playbook default; bind a project value for "
            + ", ".join(defaulted)
            + " first",
            status=409,
        )
    by_id = {t["id"]: t for t in targets}
    items = []
    for step in flow["steps"]:
        for item in step["checklist"]:
            t = by_id[f"flow:{flow_id}:{step['id']}:{item['key']}"]
            items.append(_item(step, item, t["args"], t["pending"]))
    if len(items) > missions.OBJECTIVES_MAX:
        raise ProcedureError("this procedure has more evidence than a mission holds", status=422)
    sources = {row["name"]: row.get("source") for row in resolved.public}
    procedure = {
        "source": "mission_local",
        "flow_id": flow_id,
        "title": flow["title"],
        "flow": copy.deepcopy(flow),
        "digest": plan_doc.flow_digest(flow),
        "document": copy.deepcopy(doc),
        "variables": copy.deepcopy(variables),
    }
    inputs = [
        {"name": n, "source": str(sources.get(n) or "missing")} for n in _variables_used(flow)
    ]
    return missions.apply_procedure(
        mission_id,
        project_id=pid,
        procedure=procedure,
        inputs=inputs,
        items=items,
        reason=f"mission-local procedure {flow_id} added by the operator",
    )


def plan(mission_id: str, revision: int | None = None) -> dict:
    """A plan revision plus each step's derived state (never stored), or `{"plan": None}`."""
    rev = missions.plan_revision(mission_id, revision)
    if rev is None:
        if revision is not None:
            raise ProcedureError("no such plan revision", status=404)
        return {"mission_id": mission_id, "plan": None}
    latest = rev if revision is None else missions.plan_revision(mission_id)
    rows = missions.objectives(mission_id)
    return {
        "mission_id": mission_id,
        "plan": rev,
        # Derived from TODAY's evidence rows, on whichever revision was asked for.
        "step_states": plan_doc.step_states(rev["document"], rows),
        "is_latest": latest is not None and latest["revision"] == rev["revision"],
    }


__all__ = ["ProcedureError", "apply_listed", "apply_local", "listing", "plan"]
