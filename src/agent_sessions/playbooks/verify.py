"""Verify an applied deployment against what it recorded (#1191), read-only.

Runs the checks the bundle declared in `verify` (`schema.VERIFY_CHECKS`) AS APPLIED, from the
deployment's secret-free `verify_facts` snapshot, never the editable source, which may have
changed since apply. Nothing else runs:

* `variables`: every variable the deployment recorded still resolves for the project. A secret is
  checked with `secret_state` only (ok, or needs re-entry); its value is never read here.
* `materials`: every owned path still passes the planner's own ownership rules (`_owned`, with
  digest and inode, or `_region`), and every secret reference file is still the inode apply
  wrote (its content is never read); drift is listed by path.
* `binaries`: the bundle's required binaries on `PATH` (`loader.requires_status`; nothing runs).
* `instructions`: present engines whose instruction files are not verified on disk.
* `connections`: listed with their resolved targets and **never probed**. A connection probe is an
  outbound request and stays a separate, operator-initiated action (#863 §7a).
* `capabilities`: listed as requested. Per-project grants do not exist yet, so none is granted.

Verify writes nothing, probes nothing and schedules nothing.
"""

from __future__ import annotations

from .. import template_vars
from ..fsbrowse import FsError
from . import (
    apply,
    destination,
    instructions,
    loader,
    materials,
    mutation_plan,
    secret_files,
    store,
)
from . import deployment_state as state


def _bundle(playbook_id: str) -> tuple[dict, str]:
    with store.root_lock(exclusive=False) as root_fd:
        entry = store._find(store._all_entries(root_fd, strict=True), playbook_id)
        if entry.error or entry.pb is None:
            raise store.StoreError("the playbook is invalid", status=409)
        return entry.pb, entry.revision


def _variables(pid: str, declared: dict, names: list[str]) -> dict:
    resolver = template_vars.resolver(pid)
    problems = {}
    for name in names:
        var = declared.get(name)
        try:
            if var is not None and var["kind"] == "secret":
                if resolver.secret_state(name)["state"] != "ok":
                    problems[name] = "needs re-entry"
            else:
                default = var.get("default") if var is not None else None
                resolver.text(name, None if default is None else str(default))
        except template_vars.BindingMissing:
            problems[name] = "missing"
        except template_vars.BindingUnusable as e:
            problems[name] = str(e)
    return {"ok": not problems, "problems": problems}


def _secret_drift(record: dict) -> list[str]:
    drift = []
    for name, inode in sorted(record.get("secret_files", {}).items()):
        try:
            if secret_files.present(record["secret_root"], record["project_id"], name, inode):
                continue
        except (OSError, KeyError, store.StoreError):
            pass
        drift.append(secret_files.path(record["project_id"], name, record.get("secret_root")))
    return drift


def _materials(record: dict) -> dict:
    out = _project_materials(record)
    secrets = _secret_drift(record)
    if secrets:
        out = {**out, "ok": False, "drift": out["drift"] + secrets}
    return out


def _project_materials(record: dict) -> dict:
    files = record.get("files", {})
    checked = sorted(
        p for p, o in files.items() if o.get("disposition") == "managed" and o.get("kind")
    )
    if not checked:
        return {"ok": True, "drift": []}
    try:
        live = destination.snapshot(destination.Folder(**record["destination"]), checked)
    except (FsError, OSError):
        return {"ok": False, "drift": checked, "error": "the destination could not be read"}
    drift = []
    for path in checked:
        owned = files[path]
        try:
            if owned["kind"] == "region":
                if live[path].kind != "file":
                    raise materials.MaterialError("not a regular file")
                mutation_plan._region(path, record["id"], live[path].data, owned, None)
            else:
                mutation_plan._owned(live[path], owned)
        except (materials.MaterialError, UnicodeDecodeError):
            drift.append(path)
    return {"ok": not drift, "drift": drift}


def verify(pid: str) -> dict:
    """Run the bundle's declared checks against the applied deployment; nothing is changed."""
    pid = template_vars.project_id(pid)
    with state.locked(pid) as locked:
        record = locked.read() if locked is not None else None
    if record is None or record.get("state") != "applied":
        raise store.Conflict("there is no applied deployment to verify")
    try:
        bundle, current = _bundle(record["playbook_id"])
    except store.StoreError:
        # The editable source may be invalid or gone; the APPLIED checks do not depend on it.
        bundle, current = None, None
    deployed = record.get("inputs", {}).get("revision")
    applied = record.get("verify_facts")
    if applied is None:
        # Applied before the snapshot existed: the source may only stand in while unchanged.
        if bundle is None or deployed != current:
            raise store.Conflict(
                "the playbook changed since this deployment was applied and its applied checks "
                "were not recorded; re-apply it to verify"
            )
        applied = apply.verify_facts(bundle)
    facts = record.get("review_facts") or {}
    checks: dict[str, dict] = {}
    for check in applied["checks"]:
        if check == "variables":
            checks[check] = _variables(pid, applied["variables"], facts.get("variables", []))
        elif check == "materials":
            checks[check] = _materials(record)
        elif check == "binaries":
            found = loader.requires_status({"requires": {"binaries": applied["binaries"]}})[
                "binaries"
            ]
            checks[check] = {
                "ok": all(found.values()),
                "missing": sorted(n for n, ok in found.items() if not ok),
            }
        elif check == "instructions":
            missing = instructions.missing(record, instructions.present())
            checks[check] = {"ok": not missing, "missing": missing}
        elif check == "connections":
            checks[check] = {
                "probed": False,
                "targets": [
                    t for t in facts.get("targets", []) if t["id"].startswith("connection:")
                ],
            }
        elif check == "capabilities":
            checks[check] = {
                "requested": facts.get("capability_requests", []),
                "granted": [],
            }
    return {
        "project_id": pid,
        "deployment_id": record["id"],
        "playbook_id": record["playbook_id"],
        # None: the current source cannot be read, so whether an update exists is unknown.
        "update_available": None if current is None else deployed != current,
        "ok": all(c.get("ok", True) for c in checks.values()),
        "checks": checks,
    }
