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

from .. import template_vars
from . import apply, review, store
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
