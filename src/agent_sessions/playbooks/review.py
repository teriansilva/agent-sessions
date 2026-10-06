"""Read-only deployment review and exact-plan target confirmations (#1191, formats 2 and 3).

Reviews never save bindings, write materials, contact endpoints or launch agents. A confirmation
receipt is a short-lived, signed statement about a server-recomputed plan, not a bearer command.
Every future effect must reconstruct the plan under its own mutation fences and call `accept`;
neither client-supplied plan JSON nor a transport's completed turn grants deployment authority.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import asdict, dataclass, field

from itsdangerous import BadData, URLSafeTimedSerializer

from .. import engines, missions, model_choice, prefs, project_dirs, projects, template_vars
from ..fsbrowse import FsError
from . import (
    binding,
    deployment_state,
    destination,
    flow_document,
    instructions,
    materials,
    mutation_plan,
    schema,
    secret_files,
    store,
)
from .errors import PlaybookFormatError
from .tree import check_segment

REVIEW_TTL = 3600
_SALT = "agent-sessions:playbook-review:v2"
_FIELDS = {"revision", "destination", "project_id", "bindings", "assignments", "create"}
_UNREAD = object()


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value: object, key: str) -> str:
    # Unsaved secrets are included privately. A public, unkeyed hash would expose a guessing oracle.
    return hmac.new(key.encode(), _SALT.encode() + _json(value), hashlib.sha256).hexdigest()


@dataclass
class Plan:
    public: dict
    fingerprint: dict = field(repr=False)
    rendered: list = field(default_factory=list, repr=False)
    nodes: dict = field(default_factory=dict, repr=False)
    bundle: dict = field(default_factory=dict, repr=False)
    resolved: binding.Resolved | None = field(default=None, repr=False)


def _assignments(bundle: dict, raw: object) -> tuple[list[dict], dict]:
    if not isinstance(raw, dict) or len(raw) > schema.MAX_FLOWS * schema.MAX_STEPS:
        raise store.StoreError("assignments must be a bounded object")
    expected = {
        f"{fid}:{s['id']}": s
        for fid, flow in bundle["flows"].items()
        for s in flow["steps"]
        if s["actor"]["kind"] == schema.ACTOR_AGENT
    }
    if set(raw) - set(expected):
        raise store.StoreError("assignments may only name agent steps")
    rows = []
    roster = engines.registry.current()
    facts = {"generation": roster.generation, "engines": {}}
    for step_id, step in expected.items():
        proposed = raw.get(step_id, step["actor"])
        if step_id in raw and proposed is not None:
            if not isinstance(proposed, dict) or set(proposed) != {"engine", "model"}:
                raise store.StoreError("an assignment needs only engine and model")
        if proposed is None:
            rows.append(
                {"step": step_id, "requested": None, "assignment": None, "reason": "unassigned"}
            )
            continue
        engine, model = proposed.get("engine"), proposed.get("model")
        if not isinstance(engine, str) or not schema.ENGINE_REF_RE.fullmatch(engine):
            raise store.StoreError("invalid assignment engine")
        if not isinstance(model, str) or not schema.MODEL_REF_RE.fullmatch(model):
            raise store.StoreError("invalid assignment model")
        requested = {"engine": engine, "model": model}
        provider = roster.by_id.get(engine)
        available = bool(
            provider and engines.is_agent(provider) and engines.registry.can_start(provider)
        )
        facts["engines"][engine] = {
            "present": available,
            "manifest": provider.manifest.digest if provider is not None else None,
            "operator_models": model_choice.operator_ids(engine),
        }
        assignment, reason = None, "agent unavailable"
        if available:
            try:
                selection = model_choice.select(provider, model)
                assignment = {"engine": engine, "model": selection.model or model_choice.DEFAULT}
                reason = None
            except model_choice.ModelRefused as e:
                reason = e.detail
        rows.append(
            {"step": step_id, "requested": requested, "assignment": assignment, "reason": reason}
        )
    return rows, facts


def _file_row(material: materials.Material, node: destination.Node) -> dict:
    row = {
        "path": material.path,
        "disposition": material.disposition,
        "before": {
            "kind": node.kind,
            "digest": materials.digest(node.data) if node.data is not None else None,
            "target": node.target,
        },
    }
    if material.target is not None:
        row["after"] = {"kind": "symlink", "target": material.target}
    else:
        data = material.data
        assert data is not None
        try:
            content = {"text": data.decode("utf-8")}
        except UnicodeDecodeError:
            content = {"base64": base64.b64encode(data).decode("ascii")}
        row["after"] = {"kind": "file", "digest": materials.digest(data), **content}
    # This describes the rendered material, not a mutation plan: update/remove still need the
    # deployment's ownership record and managed-region transform before they can write bytes.
    return row


def build(
    playbook_id: str,
    raw: object,
    *,
    key: str,
    _record: object = _UNREAD,
    _variables: list[dict] | None = None,
    _prestate: dict[str, destination.Node] | None = None,
) -> Plan:
    """Recompute everything the operator reviews; no client-supplied derived fact is trusted.

    Only accepted-operation recovery supplies `_prestate`, then verifies the original keyed
    digest. Its writer still checks every live inode. Public routes always read live pre-state.
    """
    store.playbook_id(playbook_id)
    if not isinstance(raw, dict) or set(raw) - _FIELDS:
        raise store.StoreError(
            "review takes revision, destination, project_id, bindings and assignments"
        )
    revision = store.revision(raw.get("revision"))
    pid = raw.get("project_id")
    try:
        if pid is not None:
            template_vars.project_id(pid)
            if _record is _UNREAD:
                with deployment_state.locked(pid) as locked:
                    return build(
                        playbook_id,
                        raw,
                        key=key,
                        _record=locked.read() if locked else None,
                        _variables=_variables,
                        _prestate=_prestate,
                    )
        record = None if _record is _UNREAD else _record
        if record is not None and (
            not isinstance(record, dict)
            or record.get("project_id") != pid
            or record.get("state") not in {"bound", "applied", "removed"}
        ):
            raise store.Conflict("the deployment needs recovery before a new review")
        if record is not None and record["state"] == "removed" and deployment_state.holds(record):
            # A removal is final only once it settled and dropped its journals.
            raise store.Conflict("the removed deployment has an unsettled operation; recover it")
        active = record if record and record["state"] != "removed" else None
        secret_files.check_root(active)
        if active and active["playbook_id"] != playbook_id:
            raise store.Conflict("remove the project's existing playbook before replacing it")
        create = raw.get("create", False)
        if create not in (True, False):
            raise store.StoreError("create must be true or false")
        if create:
            # #1187 new-folder mode: an ABSENT target beneath a held, reviewed parent. Only a
            # pre-project review may do this; CREATE makes it exclusively, BIND adopts only it.
            if pid is not None or record is not None:
                raise store.Conflict("a new folder is reviewed before its project exists")
            target = raw.get("destination")
            if not isinstance(target, str) or not os.path.isabs(target) or target.endswith("/"):
                raise store.StoreError("a new folder needs an absolute path")
            name = os.path.basename(target)
            check_segment(name, "destination")
            parent = destination.review_folder(os.path.dirname(target))
            target = os.path.join(parent.path, name)
            if os.path.lexists(target):
                raise store.Conflict(
                    "the new folder already exists; choose it as an existing folder or pick "
                    "another name"
                )
            folder = None
            dest_public = {
                "path": target,
                "device": None,
                "inode": None,
                "create": True,
                "parent": asdict(parent),
            }
        else:
            folder = destination.review_folder(raw.get("destination"))
            dest_public = asdict(folder)
        if active and active.get("destination") != dest_public:
            raise store.Conflict("the deployment destination changed")
        project = None
        if pid is not None:
            project = projects.load().get(pid)
            if project is None or project.archived:
                raise store.StoreError("the project is missing or archived", status=409)
            if dest_public["path"] not in project.folders:
                raise store.StoreError("the project must own the reviewed destination", status=409)
        with store.root_lock(exclusive=False) as root_fd:
            entry = store._find(store._all_entries(root_fd, strict=True), playbook_id)
            if entry.error or entry.pb is None or entry.tree is None:
                raise store.StoreError("the playbook is invalid", status=409)
            if entry.revision != revision:
                raise store.Conflict("the playbook changed; review it again")
            bundle, tree = entry.pb, entry.tree
            resolved = binding.resolve(bundle, pid, raw.get("bindings", []), _records=_variables)
            secret_names = materials.secret_names(bundle, tree)
            if secret_names and pid is None:
                # The file lives under the project's id, which a pre-project review lacks; a later
                # adoption could never match this review's rendered paths. Refused, reversibly.
                raise store.Conflict(
                    "this playbook writes secret reference files, which belong to a project: "
                    "create the project first, then review the playbook for it"
                )
            states = {row["name"]: row["state"] for row in resolved.public}
            for name in secret_names:
                if states.get(name) != "ok":
                    raise store.StoreError(
                        f"{name}: a secret file needs a bound secret", status=409
                    )
            if secret_names:
                secret_files.check_outside(
                    [dest_public["path"]]
                    + [f for p in projects.load().values() for f in p.folders],
                    pid,
                )
            secret_paths = {name: secret_files.path(pid, name) for name in secret_names}
            rendered = materials.render(bundle, tree, resolved.text, secret_paths)
            assignments, roster = _assignments(bundle, raw.get("assignments", {}))
            targets = binding.targets(bundle, resolved)
            rendered = flow_document.include(
                rendered, flow_document.render(bundle, resolved.text, assignments, targets)
            )
            # Every present engine reads the playbook's instructions (§11), derived per roster.
            engines_present = instructions.present()
            rendered = instructions.include(rendered, bundle, engines_present)
            owned = mutation_plan.ownership((active or {}).get("files", {}))
            deployment_id = (active or {}).get("id") or "d-" + _digest(
                {"playbook": playbook_id, "destination": dest_public, "project": pid}, key
            )[:40]
            # Validates a stored identity before it can become a marker or filesystem name.
            materials._markers(deployment_id)
            touched = sorted({m.path for m in rendered} | set(owned))
            if len(touched) > schema.MAX_MATERIALS:
                # One hard limit over the full union, never worked around: an update that would
                # touch more paths than one checked snapshot admits is refused, reversibly.
                raise materials.MaterialError(
                    f"this deployment would touch {len(touched)} paths (the playbook's materials, "
                    "its instruction aliases for the present engines and the paths it already "
                    f"owns), over the {schema.MAX_MATERIALS}-material limit; remove a material "
                    "from the playbook, or remove the deployment and apply it again"
                )
            if _prestate is not None:
                nodes = _prestate
            elif folder is None:
                # Nothing exists beneath an absent target: every touched path and parent is absent.
                nodes = {
                    "/".join(path.split("/")[: i + 1]): destination.Node("absent")
                    for path in touched
                    for i in range(path.count("/") + 1)
                }
            else:
                nodes = destination.snapshot(folder, touched)
            changes, conflicts = mutation_plan.build(rendered, nodes, owned, deployment_id)
            public = {
                "playbook": {
                    "id": playbook_id,
                    "format": bundle["format"],
                    "source": entry.source,
                    "version": bundle["identity"]["version"],
                    "revision": revision,
                },
                "project_id": pid,
                "deployment_id": deployment_id,
                "destination": dest_public,
                "variables": resolved.public,
                "materials": [_file_row(m, nodes[m.path]) for m in rendered],
                "changes": [mutation_plan.public(c) for c in changes],
                "conflicts": conflicts,
                "targets": targets,
                "assignments": assignments,
                "capability_requests": sorted(k for k, v in bundle["capabilities"].items() if v),
                "rituals": [
                    {**r, "state": "declared", "scheduled": False} for r in bundle["rituals"]
                ],
                # Paths only: apply writes each bound value there at 0600, remove deletes it.
                "secret_files": [{"name": n, "path": p} for n, p in secret_paths.items()],
            }
            fingerprint = {
                "review": public,
                "inputs": resolved.fingerprint,
                # Pending-operation bookkeeping must not invalidate its own planned settlement.
                # These are the deployed facts from which the mutation plan is derived.
                "deployment": (
                    {
                        k: active.get(k)
                        for k in (
                            "id",
                            "playbook_id",
                            "project_id",
                            "destination",
                            "files",
                            "generation",
                        )
                    }
                    if active
                    else None
                ),
                "project": project.as_dict() if project is not None else None,
                "prestate": {
                    path: {
                        "kind": n.kind,
                        "identity": n.identity,
                        "target": n.target,
                        "digest": materials.digest(n.data) if n.data is not None else None,
                    }
                    for path, n in nodes.items()
                },
                "roster": roster,
                "instructions": engines_present,
                "policy": {
                    "roots": project_dirs.effective_roots(),
                    "exclusions": prefs.get_folder_exclusions(),
                },
            }
            digest = _digest(fingerprint, key)
            return Plan(
                {**public, "digest": digest}, fingerprint, rendered, nodes, bundle, resolved
            )
    except FsError as e:
        raise store.StoreError(str(e), status=e.status) from None
    except (template_vars.BindingUnusable, template_vars.BindingMissing) as e:
        raise store.StoreError(str(e), status=409) from None
    except template_vars.ResolutionUnavailable:
        raise store.StoreError(
            "the variables store could not be read in full", status=503
        ) from None
    except (
        template_vars.VariableError,
        PlaybookFormatError,
        materials.MaterialError,
        missions.MissionError,
    ) as e:
        raise store.StoreError(str(e)) from None
    except OSError:
        raise store.StoreError(
            "the deployment destination could not be read safely", status=409
        ) from None


#: The largest receipt `confirm` can sign: every flow checklist target plus every connection, each
#: id at its schema maximum, as compact JSON, base64'd by the signer, plus the digest and signature.
_TARGET_ID_MAX = max(
    len("flow:") + 48 + 1 + 32 + 1 + schema.ITEM_KEY_MAX,
    len("connection:") + schema.NAME_MAX,
)
_TARGETS_MAX = (
    schema.MAX_FLOWS * schema.MAX_STEPS * schema.MAX_ITEMS_PER_STEP + schema.MAX_CONNECTIONS
)
RECEIPT_MAX = (_TARGETS_MAX * (_TARGET_ID_MAX + 3) + 256) * 4 // 3 + 256


def confirm(plan: Plan, expected: object, targets: object, *, key: str) -> dict:
    """Confirm exact named targets from this recomputed review. Never fetch or execute one."""
    digest = store.revision(expected)
    if not hmac.compare_digest(digest, plan.public["digest"]):
        raise store.Conflict("the deployment review changed; review it again")
    known = {t["id"] for t in plan.public["targets"]}
    if (
        not isinstance(targets, list)
        or len(targets) > len(known)
        or not all(isinstance(t, str) for t in targets)
        or len(set(targets)) != len(targets)
        or set(targets) - known
    ):
        raise store.StoreError("confirmations must name each reviewed target at most once")
    chosen = sorted(targets)
    receipt = URLSafeTimedSerializer(key, salt=_SALT).dumps({"digest": digest, "targets": chosen})
    assert len(receipt) <= RECEIPT_MAX, "RECEIPT_MAX must bound every receipt confirm can sign"
    return {
        "digest": digest,
        "confirmed_targets": chosen,
        "receipt": receipt,
        "expires_in": REVIEW_TTL,
    }


def accept(plan: Plan, receipt: object, *, key: str) -> list[dict]:
    """Verify a receipt against live recomputation before an effect; return only armed targets.

    The caller must hold its effect's authority/episode/project/store fences. This read-only
    module cannot grant a capability or turn a pending observed output into evidence.
    """
    if not isinstance(receipt, str) or len(receipt) > 256 * 1024:
        raise store.StoreError("a deployment review receipt is required")
    try:
        record = URLSafeTimedSerializer(key, salt=_SALT).loads(receipt, max_age=REVIEW_TTL)
    except BadData:
        raise store.Conflict(
            "the deployment review expired or is invalid; review it again"
        ) from None
    if (
        not isinstance(record, dict)
        or set(record) != {"digest", "targets"}
        or record["digest"] != plan.public["digest"]
    ):
        raise store.Conflict("the deployment review changed; review it again")
    known = {t["id"] for t in plan.public["targets"]}
    chosen = record["targets"]
    if not isinstance(chosen, list) or not all(isinstance(t, str) and t in known for t in chosen):
        raise store.Conflict("the deployment review receipt is invalid")
    return [
        {**t, "armed": True}
        for t in plan.public["targets"]
        if not t["pending"] and (not t["requires_confirmation"] or t["id"] in chosen)
    ]
