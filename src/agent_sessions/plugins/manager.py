"""Durable plugin candidates and activation, separate from artifact-owned files (#1259).

Only this module writes manager.json. A candidate is never a live installation: activation
commits its complete manifest, provenance and verification together. Request IDs are durable;
recovery marks unfinished work interrupted and never replays it. Generations are retained.
The history limit stops new work, but leaves one durable revocation per known enabled identity.
Ordinary writes cannot consume the byte reserve for revocation and bounded existing-work outcomes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from . import artifacts, budget, feed, npm_closure, plugins_home, provenance, storage
from .provider import PluginProvider

DOCUMENT = "manager.json"
REVISION_MARKER = "manager-revision.json"
_REVISION_REQUIRED = {"contract": 1, "revision_required": True}
# Also remember observed decisions in-process if the independent durable marker is lost.
# Across restarts the marker distinguishes lost manager state from a fresh installation.
_REVISIONED_STATE: set[Path] = set()
MAX_OPERATIONS = 2048
# A revocation adds a fixed outcome and, for a packaged identity, an empty row: <1 KiB.
# Reserve 4 MiB beyond the ordinary 8 MiB write ceiling, enough for every managed identity
# admitted under MAX_OPERATIONS plus the packaged roster and bounded settlement of pre-existing
# work. New work cannot spend it; the reader accepts the full reserve.
REVOCATION_RESERVE_BYTES = 2 * MAX_OPERATIONS * 1024
MAX_MANAGER_BYTES = storage.MAX_STATE_BYTES + REVOCATION_RESERVE_BYTES
MAX_OUTCOME_BYTES = 512
MAX_REVIEWS = 64
REVIEW_SECONDS = 900
MAX_TOTAL_DOWNLOAD = 512 * 1024 * 1024
MAX_TOTAL_EXPANDED = 1024 * 1024 * 1024
MAX_INSTALL_SECONDS = 600
_ID = re.compile(r"[a-z][a-z0-9-]{1,23}")
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_BUSY = frozenset({"planned", "running", "ready", "cleanup_pending"})
_OUTCOMES = frozenset({"complete", "failed", "interrupted", "cleanup_pending"})


class ManagerError(ValueError):
    pass


def _now() -> int:
    return int(time.time())


def _id(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ManagerError("invalid agent id")
    return value


def _operation_id(value: str) -> str:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise ManagerError("invalid operation id")
    return value


def _revision_required(path: Path) -> bool:
    marker = storage.read(path.with_name(REVISION_MARKER))
    if marker is not None and (
        marker != _REVISION_REQUIRED
        or type(marker.get("contract")) is not int
        or marker.get("revision_required") is not True
    ):
        raise ManagerError(
            "plugin manager revision marker is unreadable; restore its durable state"
        )
    return marker is not None or path in _REVISIONED_STATE


def _remember_revision(path: Path) -> None:
    # This independent, immutable sentinel is durable BEFORE the first roster decision.
    # Losing manager.json after a process restart must not look like first installation.
    with storage.locked(REVISION_MARKER) as marker:
        if not _revision_required(path):
            storage.write(marker, _REVISION_REQUIRED)
        elif storage.read(marker) is None:
            storage.write(marker, _REVISION_REQUIRED)
    _REVISIONED_STATE.add(path)


def _write_roster(path: Path, doc: dict, *, revocation: bool = False) -> None:
    options = {"max_bytes": MAX_MANAGER_BYTES} if revocation else {}
    if not _revision_required(path):
        # Establish a revisioned copy of the PREVIOUS roster before the sentinel. A crash
        # between sentinel and decision then retains the old roster rather than stranding a
        # valid first activation. Losing that copy still fails closed after restart.
        previous = storage.read(path, max_bytes=MAX_MANAGER_BYTES) or {
            "contract": 1,
            "plugins": {},
            "operations": {},
            "reviews": {},
        }
        if "roster_revision" not in previous:
            previous["roster_revision"] = str(uuid.uuid4())
            storage.write(path, previous, **options)
        doc.setdefault("roster_revision", previous["roster_revision"])
    _remember_revision(path)
    storage.write(path, doc, **options)


def _load(path: Path) -> dict:
    doc = storage.read(path, max_bytes=MAX_MANAGER_BYTES)
    if doc is None:
        if _revision_required(path):
            raise ManagerError("plugin manager revision is missing; restore its durable state")
        return {"contract": 1, "plugins": {}, "operations": {}, "reviews": {}}
    if (
        set(doc)
        not in (
            {"contract", "plugins", "operations", "reviews"},
            {"contract", "plugins", "operations", "reviews", "roster_revision"},
        )
        or type(doc["contract"]) is not int
        or doc["contract"] != 1
        or not all(isinstance(doc[k], dict) for k in ("plugins", "operations", "reviews"))
    ):
        raise ManagerError("plugin manager state is unreadable; it was left untouched")
    if "roster_revision" in doc:
        _operation_id(doc["roster_revision"])
        _remember_revision(path)
    elif _revision_required(path):
        raise ManagerError("plugin manager revision is missing; restore its durable state")
    return doc


def snapshot() -> dict:
    # Atomic replacement makes an unlocked read one complete durable snapshot. Do not take the
    # worker lock here: the browser must be able to observe an in-progress download/sign-in.
    return _load(storage.root() / DOCUMENT)


def _entry(review: dict, *, fresh: bool) -> feed.Entry:
    if review["source"] == "signed":
        if fresh:
            catalog = feed.current()
            found = (
                next((e for e in catalog.entries if e.digest == review["recipe_digest"]), None)
                if catalog is not None
                else None
            )
            if found is None:
                raise ManagerError("the catalog changed; review this installation again")
            entry = found
        else:
            entry = feed.entry(review["entry"], signed=True)
    elif review["source"] == "local":
        entry = feed.entry(review["entry"], signed=False)
    else:
        raise ManagerError("invalid installation source")
    if entry.digest != review["recipe_digest"] or _review_digest(review) != review["digest"]:
        raise ManagerError("installation review binding is invalid")
    return entry


def _review_digest(item: dict) -> str:
    return hashlib.sha256(
        feed.canonical(
            {
                "recipe": item["recipe_digest"],
                "source": item["source"],
                "adopted_path": item["adopted_path"],
                "adopted_sha256": item["adopted_sha256"],
            }
        )
    ).hexdigest()


def _adopted_file(path: str) -> tuple[str, str]:
    if not isinstance(path, str) or not os.path.isabs(path) or len(path) > 4096:
        raise ManagerError("the adopted executable needs an absolute path")
    real = os.path.realpath(path)
    from . import kinds

    if kinds.is_forbidden_entrypoint(Path(real).name):
        raise ManagerError("a shell, interpreter or privilege tool cannot be adopted")
    fd, _ = provenance.open_verified(real, executable=True, canonicalize=False)
    try:
        digest = provenance._sha256_fd(fd)
    finally:
        os.close(fd)
    return real, digest


def review(
    *, plugin_id: str | None = None, local: dict | None = None, adopted_path: str | None = None
) -> dict:
    """Create a short-lived, exact-byte review. Merely dropping a manifest grants nothing."""
    if (plugin_id is None) == (local is None):
        raise ManagerError("choose one catalog agent or local manifest")
    sequence = None
    if local is not None:
        entry = feed.entry(local, signed=False)
        raw = copy.deepcopy(local)
        source = "local"
    else:
        catalog = feed.current()
        entry = (
            next((e for e in catalog.entries if e.manifest.id == _id(plugin_id)), None)
            if catalog is not None
            else None
        )
        if entry is None:
            raise ManagerError("the catalog does not offer this agent")
        raw = {
            "manifest": feed.decode(entry.manifest_bytes),
            "recipe": {"artifacts": [asdict(a) for a in entry.artifacts]},
        }
        sequence, source = catalog.sequence, "signed"
    adopted, adopted_digest = (
        _adopted_file(adopted_path) if adopted_path is not None else (None, None)
    )
    if adopted and entry.manifest.runtime != "pty":
        raise ManagerError("an API agent has no executable to adopt")
    item = {
        "id": str(uuid.uuid4()),
        "plugin_id": entry.manifest.id,
        "source": source,
        "sequence": sequence,
        "entry": raw,
        "recipe_digest": entry.digest,
        "adopted_path": adopted,
        "adopted_sha256": adopted_digest,
        "created_at": _now(),
        "expires_at": _now() + REVIEW_SECONDS,
        "consumed_by": None,
    }
    item["digest"] = _review_digest(item)
    with storage.locked(DOCUMENT) as path:
        doc = _load(path)
        # Review challenges have no install authority after expiry. Operations retain their own
        # complete snapshot; dropping an expired challenge never removes an operation/generation.
        doc["reviews"] = {k: v for k, v in doc["reviews"].items() if v["expires_at"] > _now()}
        if len(doc["reviews"]) >= MAX_REVIEWS:
            raise ManagerError("too many open installation reviews")
        doc["reviews"][item["id"]] = item
        storage.write(path, doc)
    return copy.deepcopy(item)


def begin_install(
    request_id: str,
    review_id: str,
    digest: str,
    *,
    confirm_local: bool = False,
    confirm_adopted: bool = False,
) -> dict:
    request_id, review_id = _operation_id(request_id), _operation_id(review_id)
    if type(confirm_local) is not bool or type(confirm_adopted) is not bool:
        raise ManagerError("confirmation must be a boolean")
    payload = {
        "review_id": review_id,
        "digest": digest,
        "confirm_local": confirm_local,
        "confirm_adopted": confirm_adopted,
    }
    with storage.locked(DOCUMENT) as path:
        doc = _load(path)
        previous = doc["operations"].get(request_id)
        if previous is not None:
            if previous["kind"] != "install" or previous["request"] != payload:
                raise ManagerError("operation id was already used for a different request")
            return copy.deepcopy(previous)
        item = doc["reviews"].get(review_id)
        if item is None or item["expires_at"] <= _now() or item["consumed_by"] is not None:
            raise ManagerError("this review expired or was used; review the installation again")
        entry = _entry(item, fresh=True)
        if digest != item["digest"]:
            raise ManagerError("the installation does not match the reviewed bytes")
        if item["source"] == "local" and not confirm_local:
            raise ManagerError("this local installation needs fresh explicit confirmation")
        if item["adopted_path"] and not confirm_adopted:
            raise ManagerError("this adopted path and digest need explicit confirmation")
        if any(o["state"] in _BUSY for o in doc["operations"].values()):
            raise ManagerError("another plugin operation is busy")
        if len(doc["operations"]) >= MAX_OPERATIONS:
            raise ManagerError("plugin operation history is full; no history was removed")
        operation = {
            "id": request_id,
            "plugin_id": entry.manifest.id,
            "kind": "install",
            "request": payload,
            "review": copy.deepcopy(item),
            "state": "planned",
            "created_at": _now(),
            "updated_at": _now(),
            "error": None,
        }
        item["consumed_by"] = request_id
        doc["operations"][request_id] = operation
        storage.write(path, doc)
    return copy.deepcopy(operation)


def operation(request_id: str) -> dict:
    item = snapshot()["operations"].get(_operation_id(request_id))
    if item is None:
        raise ManagerError("plugin operation not found")
    return item


def _update_outcome(item: dict, state: str, error: str | None = None) -> None:
    """Bound new outcome metadata; retain the request, review and every other audit field."""
    if state not in _BUSY | _OUTCOMES:
        raise ManagerError("unknown plugin operation state")
    outcome = {"state": state, "error": error, "updated_at": _now()}
    if len(json.dumps(outcome, indent=2, sort_keys=True).encode("utf-8")) > MAX_OUTCOME_BYTES:
        outcome["error"] = "operation diagnostic exceeded the state limit"
    item.update(outcome)


def expire_ready_signins() -> None:
    """Reconcile abandoned reservations while the caller owns the worker fence.

    Only an unclaimed sign-in can expire here. Running work and uncertain teardown keep their
    reservation; their executor/recovery owns settlement. A socket claiming this same ready
    record needs the worker fence too, so it cannot race expiration into a new admission.
    """
    with storage.locked(DOCUMENT) as path:
        doc = _load(path)
        now = _now()
        changed = False
        for item in doc["operations"].values():
            if (
                item["kind"] == "signin"
                and item["state"] == "ready"
                and item["created_at"] + REVIEW_SECONDS <= now
            ):
                _update_outcome(item, "interrupted", "sign-in expired")
                changed = True
        if changed:
            # Expiry is required before revocation. It must not spend the last ordinary bytes
            # and strand that path; each existing outcome grows by less than 512 bytes.
            storage.write(path, doc, max_bytes=MAX_MANAGER_BYTES)


def _set_operation(
    request_id: str, state: str, *, error: str | None = None, if_states: frozenset | None = None
) -> dict:
    with storage.locked(DOCUMENT) as path:
        doc = _load(path)
        item = doc["operations"][request_id]
        if if_states is not None and item["state"] not in if_states:
            return copy.deepcopy(item)
        settling = item["state"] in _BUSY and state in _OUTCOMES
        _update_outcome(item, state, error)
        options = {"max_bytes": MAX_MANAGER_BYTES} if settling else {}
        storage.write(path, doc, **options)
        return copy.deepcopy(item)


def generation_root(plugin_id: str, generation: str) -> Path:
    return plugins_home().absolute() / _id(plugin_id) / "generations" / _operation_id(generation)


def _record(entry: feed.Entry, root: Path) -> provenance.Record:
    if entry.manifest.runtime in ("chat", "api"):
        # Nothing of its own to execute: the record pins the manifest. An `api` client's
        # executable is its source's, verified through the source's own record (#1311).
        return provenance.Record(manifest_sha256=entry.manifest.digest)
    relative = entry.manifest.install.entrypoint
    fd, st = provenance.open_verified(str(root / relative), canonicalize=False)
    try:
        if not st.st_mode & 0o111:
            raise ManagerError("the installed entrypoint is not executable")
        digest = provenance._sha256_fd(fd)
    finally:
        os.close(fd)
    record = provenance.Record(
        install_entrypoint=relative, install_sha256=digest, manifest_sha256=entry.manifest.digest
    )
    provenance.resolve(
        entry.manifest, trust=provenance.LOCAL, root=root, record=record, env={}, home=Path.home()
    )
    return record


def run_install(request_id: str) -> dict:
    """Worker entry. A repeated call observes terminal state and cannot execute it again."""
    with storage.locked("worker", wait=0):
        return _run_install(request_id)


def _check_platform(entry: feed.Entry) -> None:
    import platform

    wanted = entry.manifest.install.platform if entry.manifest.install else None
    if wanted is not None and not (
        wanted == "linux-x64"
        and platform.system() == "Linux"
        and platform.machine().lower() in ("x86_64", "amd64")
    ):
        raise ManagerError(f"this recipe requires {wanted}; no artifact was installed")


def _run_install(request_id: str) -> dict:
    """Only for a caller already holding the worker fence from planning through completion."""
    request_id = _operation_id(request_id)
    with budget.limited(MAX_INSTALL_SECONDS):
        item = operation(request_id)
        if item["state"] != "planned":
            return item
        _set_operation(request_id, "running")
        try:
            entry = _entry(item["review"], fresh=True)
            _check_platform(entry)
            root = generation_root(item["plugin_id"], request_id)
            with storage.directory(root.parent) as parent:
                os.mkdir(root.name, mode=0o700, dir_fd=parent)  # existing stage is never reused
                os.fsync(parent)
            total_download, total_expanded = 0, 0
            for artifact in () if item["review"]["adopted_path"] else entry.artifacts:
                budget.check()
                data = artifacts.fetch(artifact, entry.manifest.install.kind)
                budget.check()
                total_download += len(data)
                if total_download > MAX_TOTAL_DOWNLOAD:
                    raise ManagerError("installation exceeded its total download limit")
                total_expanded += artifacts.extract(
                    data,
                    artifact,
                    entry.manifest.install.kind,
                    root / artifact.destination,
                    max_expanded=MAX_TOTAL_EXPANDED - total_expanded,
                )
                budget.check()
                if total_expanded > MAX_TOTAL_EXPANDED:
                    raise ManagerError("installation exceeded its total extraction limit")
            if (
                not item["review"]["adopted_path"]
                and entry.manifest.install
                and entry.manifest.install.kind == "npm-prefix"
            ):
                npm_closure.validate(root, entry)
                budget.check()
            if item["review"]["adopted_path"]:
                adopted, digest = _adopted_file(item["review"]["adopted_path"])
                if (adopted, digest) != (
                    item["review"]["adopted_path"],
                    item["review"]["adopted_sha256"],
                ):
                    raise ManagerError(
                        "the adopted executable changed; confirm its current bytes again"
                    )
                record = provenance.Record(
                    confirmed_path=adopted,
                    confirmed_sha256=digest,
                    manifest_sha256=entry.manifest.digest,
                )
            else:
                record = _record(entry, root)
            budget.check()
            generation = {
                "id": request_id,
                "review": item["review"],
                "record": asdict(record),
                "installed_at": _now(),
                "verification": None,
            }
            with storage.locked(DOCUMENT) as path:
                doc = _load(path)
                row = doc["plugins"].setdefault(
                    item["plugin_id"],
                    {"enabled": None, "active": None, "candidate": None, "generations": {}},
                )
                _validate_row(item["plugin_id"], row)
                row["generations"][request_id] = generation
                row["candidate"] = request_id
                doc["operations"][request_id].update(state="installed", updated_at=_now())
                budget.check()
                storage.write(path, doc)
        except Exception as exc:
            # No process output, network URLs or arbitrary exception text enter persisted state.
            message = (
                str(exc)
                if isinstance(
                    exc,
                    ManagerError | feed.FeedError | artifacts.ArtifactError | budget.BudgetError,
                )
                else "installation could not complete; the previous agent is unchanged"
            )
            # replace may have committed before the directory fsync raised. Re-read under the
            # state lock and never overwrite a visible installed outcome as a definite failure.
            with storage.locked(DOCUMENT) as path:
                doc = _load(path)
                current = doc["operations"][request_id]
                if current["state"] in ("planned", "running"):
                    _update_outcome(current, "failed", message)
                    storage.write(path, doc, max_bytes=MAX_MANAGER_BYTES)
                else:
                    storage.write(path, doc)  # retry durability of an already published cut
        return operation(request_id)


def recover() -> None:
    """Startup recovery. A live worker owns the lock; no process declares its work interrupted."""
    with storage.locked("worker", wait=0), storage.locked(DOCUMENT) as path:
        doc = _load(path)
        changed = False
        for item in doc["operations"].values():
            if item["state"] in _BUSY:
                if item["kind"] in ("signin", "verify"):
                    from . import process

                    if not process.stop_operation(item["id"]):
                        raise ManagerError("temporary process cleanup is pending")
                _update_outcome(
                    item, "interrupted", "operation interrupted; start a new reviewed attempt"
                )
                changed = True
        if changed:
            storage.write(path, doc, max_bytes=MAX_MANAGER_BYTES)


def _validate_row(plugin_id: str, row: dict) -> None:
    _id(plugin_id)
    if not isinstance(row, dict) or set(row) != {"enabled", "active", "candidate", "generations"}:
        raise ManagerError("invalid plugin state row")
    if row["enabled"] is not None and type(row["enabled"]) is not bool:
        raise ManagerError("invalid plugin enabled state")
    if not isinstance(row["generations"], dict):
        raise ManagerError("invalid plugin generations")
    for key in ("active", "candidate"):
        value = row[key]
        if value is not None and (_operation_id(value) not in row["generations"]):
            raise ManagerError("plugin generation is missing")
    if row["enabled"] is True and row["active"] is None:
        raise ManagerError("enabled agent has no active generation")


def provider(plugin_id: str, generation: dict) -> PluginProvider:
    entry = _entry(generation["review"], fresh=False)
    _check_platform(entry)
    if entry.manifest.id != _id(plugin_id):
        raise ManagerError("generation belongs to a different agent")
    record = provenance.Record(**generation["record"])
    if record.manifest_sha256 != entry.manifest.digest:
        raise ManagerError("generation provenance is for another manifest")
    # A managed generation never follows the legacy env override. Other vendor configuration
    # remains available to its store readers; process runners separately construct a minimal env.
    env = dict(os.environ)
    if entry.manifest.binary and entry.manifest.binary.env_var:
        env.pop(entry.manifest.binary.env_var, None)
    prov = PluginProvider(
        entry.manifest,
        trust=provenance.LOCAL,
        root=generation_root(plugin_id, generation["id"]),
        env=env,
        record=record,
    )
    if entry.manifest.runtime == "chat":
        prov.endpoint_scope = endpoint_scope(plugin_id, generation["id"])
    prov.manifest_copy = ("plugin.json", entry.manifest_bytes)
    return prov


def required_checks(prov: PluginProvider) -> list[str]:
    m = prov.manifest
    if m.runtime == "chat":
        return ["endpoint"]
    if m.runtime == "api":
        # The adapter's own readiness against the live source (#1311): the source resolves to an
        # active console agent, its CLI meets the protocol floor, and containment is available.
        return ["source"]
    checks = {"binary", "version", *m.verify}
    if m.store:
        checks.add("store")
    for capability in ("new", "resume"):
        if m.can(capability):
            checks.add(capability)
    if m.transcript_kind != "none":
        checks.add("transcript")
    if m.usage and m.usage.source not in ("manual", "none"):
        checks.add("usage")
    return sorted(checks)


def _verification(prov: PluginProvider, gen: dict, results: list[dict]) -> dict:
    prov.entrypoint()
    required = required_checks(prov)
    if sorted(x["check"] for x in results) != required or any(
        type(x["passed"]) is not bool for x in results
    ):
        raise ManagerError("verification did not execute the required checks")
    result = {
        "digest": gen["review"]["digest"],
        "checked_at": _now(),
        "results": copy.deepcopy(results),
    }
    if prov.manifest.runtime == "chat":
        binding = _endpoint_binding(prov.engine_id, gen["id"])
        endpoint = next(r for r in results if r["check"] == "endpoint")
        if endpoint["passed"] and endpoint.get("binding") != binding:
            raise ManagerError("the endpoint changed during verification; run the check again")
        result["endpoint_binding"] = binding
    return result


def endpoint_scope(plugin_id: str, generation_id: str) -> str:
    """Private prefs/AAD identity, never a route-selectable engine ID."""
    return f"{_id(plugin_id)}:{_operation_id(generation_id)}"


def workspace(plugin_id: str, generation_id: str) -> Path:
    """The candidate's private workspace: vendor trust is explicit and survives a check retry."""
    path = storage.root() / "workspaces" / _id(plugin_id) / _operation_id(generation_id)
    with storage.directory(path):
        pass
    return path


def _was_activated(doc: dict, plugin_id: str, generation_id: str) -> bool:
    # Activation history is durable and never pruned. A replaced/disabled generation may
    # still belong to a captured chat turn, so losing the active pointer never unfreezes it.
    return doc["plugins"][plugin_id]["active"] == generation_id or any(
        item["kind"] == "activate"
        and item["state"] == "complete"
        and item["plugin_id"] == plugin_id
        and item["request"]["generation_id"] == generation_id
        for item in doc["operations"].values()
    )


def _require_inactive(row: dict, generation_id: str) -> None:
    if row["enabled"] is True and row["active"] == generation_id:
        raise ManagerError("disable this installation before sign-in or verification")


def set_endpoint(plugin_id: str, generation_id: str, patch: dict) -> dict:
    from .. import chat_config

    scope = endpoint_scope(plugin_id, generation_id)
    with storage.locked("worker", wait=0), storage.locked(DOCUMENT) as path:
        doc = _load(path)
        row = doc["plugins"].get(plugin_id)
        if row is None:
            raise ManagerError("installed agent not found")
        _validate_row(plugin_id, row)
        gen = row["generations"].get(generation_id)
        if gen is None or provider(plugin_id, gen).manifest.runtime != "chat":
            raise ManagerError("this candidate has no API endpoint")
        if _was_activated(doc, plugin_id, generation_id):
            raise ManagerError(
                "this installation was activated; prepare a new candidate to change its endpoint"
            )
        # Invalidate durably BEFORE prefs can change. A failed save/crash leaves a check pending,
        # never a stale passing verdict. The worker fence excludes verification and activation.
        gen["verification"] = None
        storage.write(path, doc)
        return chat_config.set_config(scope, patch)


def _endpoint_binding(plugin_id: str, generation_id: str) -> str:
    from .. import chat_config

    return hashlib.sha256(
        feed.canonical(chat_config.stored(endpoint_scope(plugin_id, generation_id)))
    ).hexdigest()


def record_verification(plugin_id: str, generation_id: str, results: list[dict]) -> None:
    """Private executor boundary; routes never accept check results from a client."""
    with storage.locked(DOCUMENT) as path:
        doc = _load(path)
        row = doc["plugins"][_id(plugin_id)]
        _validate_row(plugin_id, row)
        gen = row["generations"][_operation_id(generation_id)]
        gen["verification"] = _verification(provider(plugin_id, gen), gen, results)
        storage.write(path, doc)


def generation(plugin_id: str, generation_id: str) -> dict:
    doc = snapshot()
    row = doc["plugins"].get(_id(plugin_id))
    if row is None:
        raise ManagerError("installed agent not found")
    _validate_row(plugin_id, row)
    gen = row["generations"].get(_operation_id(generation_id))
    if gen is None:
        raise ManagerError("installed generation not found")
    provider(plugin_id, gen).entrypoint()
    return gen


def begin_action(
    request_id: str, plugin_id: str, generation_id: str, kind: str, *, confirm_effects: bool = False
) -> dict:
    """Plan a fixed sign-in or verification, under the caller's worker reservation.

    Sign-in remains ready until an authenticated socket claims it. A verify worker holds the
    reservation continuously from this write through all checks and their atomic result.
    """
    if kind not in ("signin", "verify"):
        raise ManagerError("unknown plugin action")
    if kind == "verify" and confirm_effects is not True:
        raise ManagerError("confirm verification's host actions, vendor history and quota effects")
    request_id, plugin_id, generation_id = (
        _operation_id(request_id),
        _id(plugin_id),
        _operation_id(generation_id),
    )
    payload = {"plugin_id": plugin_id, "generation_id": generation_id}
    if kind == "verify":
        payload["confirm_effects"] = True
    with storage.locked(DOCUMENT) as path:
        doc = _load(path)
        previous = doc["operations"].get(request_id)
        if previous is not None:
            if previous["kind"] != kind or previous["request"] != payload:
                raise ManagerError("operation id was already used for a different request")
            return copy.deepcopy(previous)
        if any(item["state"] in _BUSY for item in doc["operations"].values()):
            raise ManagerError("another plugin operation is busy")
        if len(doc["operations"]) >= MAX_OPERATIONS:
            raise ManagerError("plugin operation history is full; no history was removed")
        row = doc["plugins"].get(plugin_id)
        if row is None:
            raise ManagerError("installed agent not found")
        _validate_row(plugin_id, row)
        gen = row["generations"].get(generation_id)
        if gen is None:
            raise ManagerError("installed generation not found")
        _require_inactive(row, generation_id)
        prov = provider(plugin_id, gen)
        prov.entrypoint()
        if kind == "signin" and prov.manifest.signin_kind == "none":
            raise ManagerError("this agent has no sign-in command")
        if kind == "verify":
            gen["verification"] = None
        item = {
            "id": request_id,
            "plugin_id": plugin_id,
            "kind": kind,
            "request": payload,
            "state": "ready" if kind == "signin" else "planned",
            "created_at": _now(),
            "updated_at": _now(),
            "error": None,
            "review_digest": gen["review"]["digest"],
        }
        doc["operations"][request_id] = item
        storage.write(path, doc)
        return copy.deepcopy(item)


def claim_signin(request_id: str) -> dict:
    """Consume one ready sign-in while the caller owns the worker fence."""
    with storage.locked(DOCUMENT) as path:
        doc = _load(path)
        item = doc["operations"].get(_operation_id(request_id))
        if item is None or item["kind"] != "signin" or item["state"] != "ready":
            raise ManagerError("sign-in is not ready; start a new attempt")
        if item["created_at"] + REVIEW_SECONDS <= _now():
            _update_outcome(item, "interrupted", "sign-in expired")
            storage.write(path, doc, max_bytes=MAX_MANAGER_BYTES)
            raise ManagerError("sign-in expired; start a new attempt")
        row = doc["plugins"][item["plugin_id"]]
        _validate_row(item["plugin_id"], row)
        _require_inactive(row, item["request"]["generation_id"])
        gen = row["generations"][item["request"]["generation_id"]]
        if gen["review"]["digest"] != item["review_digest"]:
            raise ManagerError("the sign-in candidate changed")
        provider(item["plugin_id"], gen).entrypoint()
        # A new login may change vendor identity/permissions; previous checks no longer attest it.
        gen["verification"] = None
        item.update(state="running", updated_at=_now())
        storage.write(path, doc)
        return copy.deepcopy(item)


def cancel_ready(request_id: str) -> dict:
    with storage.locked("worker", wait=0):
        item = operation(request_id)
        if item["state"] == "ready":
            return _set_operation(request_id, "interrupted", error="sign-in cancelled")
        if item["state"] in _BUSY:
            raise ManagerError("close the sign-in terminal or recover the interrupted operation")
        return item


def finish_verification(request_id: str, results: list[dict]) -> dict:
    """Commit the check results and the operation outcome as one record."""
    with storage.locked(DOCUMENT) as path:
        doc = _load(path)
        item = doc["operations"][_operation_id(request_id)]
        if item["kind"] != "verify" or item["state"] != "running":
            raise ManagerError("verification no longer owns this operation")
        gen = doc["plugins"][item["plugin_id"]]["generations"][item["request"]["generation_id"]]
        if gen["review"]["digest"] != item["review_digest"]:
            raise ManagerError("the verification candidate changed")
        gen["verification"] = _verification(provider(item["plugin_id"], gen), gen, results)
        passed = all(r["passed"] for r in results)
        item.update(
            state="verified" if passed else "failed",
            updated_at=_now(),
            error=None if passed else "required verification did not pass",
        )
        storage.write(path, doc)
        return copy.deepcopy(item)


def activate(request_id: str, plugin_id: str, generation_id: str) -> dict:
    return _change(request_id, _id(plugin_id), "activate", _operation_id(generation_id))


_UNSPECIFIED = object()


def deactivate(
    request_id: str,
    plugin_id: str,
    *,
    remove: bool = False,
    expected_active=_UNSPECIFIED,
    expected_revision=_UNSPECIFIED,
) -> dict:
    if expected_active is not _UNSPECIFIED and expected_active is not None:
        expected_active = _operation_id(expected_active)
    if expected_revision is not _UNSPECIFIED and expected_revision is not None:
        expected_revision = _operation_id(expected_revision)
    return _change(
        request_id,
        _id(plugin_id),
        "remove" if remove else "disable",
        None,
        expected_active,
        expected_revision,
    )


def _change(
    request_id: str,
    plugin_id: str,
    kind: str,
    generation_id: str | None,
    expected_active=_UNSPECIFIED,
    expected_revision=_UNSPECIFIED,
) -> dict:
    from ..engines import registry

    with storage.locked("worker", wait=0), storage.locked("launch"):
        expire_ready_signins()
        result = _commit_change(
            request_id,
            plugin_id,
            kind,
            generation_id,
            expected_active,
            expected_revision,
            known_provider=plugin_id in registry.capture().by_id,
        )
        registry.sync_committed()  # also retried for an existing id after a lost response
        return result


def _commit_change(
    request_id: str,
    plugin_id: str,
    kind: str,
    generation_id: str | None,
    expected_active=_UNSPECIFIED,
    expected_revision=_UNSPECIFIED,
    *,
    known_provider: bool,
) -> dict:
    request_id = _operation_id(request_id)
    payload = {"plugin_id": plugin_id, "generation_id": generation_id}
    if expected_active is not _UNSPECIFIED:
        payload["expected_active"] = expected_active
    if expected_revision is not _UNSPECIFIED:
        payload["expected_revision"] = expected_revision
    with storage.locked(DOCUMENT) as path:
        doc = _load(path)
        old = doc["operations"].get(request_id)
        if old is not None:
            if old["kind"] != kind or old["request"] != payload:
                raise ManagerError("operation id was already used for a different request")
            return copy.deepcopy(old)
        if any(o["state"] in _BUSY for o in doc["operations"].values()):
            raise ManagerError("another plugin operation is busy")
        known = known_provider or plugin_id in doc["plugins"]
        row = doc["plugins"].setdefault(
            plugin_id, {"enabled": None, "active": None, "candidate": None, "generations": {}}
        )
        _validate_row(plugin_id, row)
        if expected_active is not _UNSPECIFIED and row["active"] != expected_active:
            raise ManagerError("the active installation changed; refresh and confirm again")
        if (
            expected_revision is not _UNSPECIFIED
            and doc.get("roster_revision") != expected_revision
        ):
            # Generation IDs can repeat across disable/re-enable. The durable revision is
            # fresh for every decision, including activation of the same generation.
            raise ManagerError("the agent roster changed; refresh and confirm again")
        revocation = kind in {"disable", "remove"} and known and row["enabled"] is not False
        if len(doc["operations"]) >= MAX_OPERATIONS:
            # Retain every audit/retry record while allowing revocation at the lifetime cap.
            # New installs and activations are capped too, so each known identity can consume
            # this lane only once: no fresh-ID no-ops, unknown IDs or disable/enable cycles.
            if not revocation:
                raise ManagerError("plugin operation history is full; no history was removed")
        if kind == "activate":
            gen = row["generations"].get(generation_id)
            if gen is None:
                raise ManagerError("installed generation not found")
            prov = provider(plugin_id, gen)
            prov.entrypoint()
            result = gen["verification"]
            if (
                not result
                or result["digest"] != gen["review"]["digest"]
                or sorted(x["check"] for x in result["results"]) != required_checks(prov)
                or not all(x["passed"] is True for x in result["results"])
                or (
                    prov.manifest.runtime == "chat"
                    and result.get("endpoint_binding")
                    != _endpoint_binding(plugin_id, generation_id)
                )
            ):
                raise ManagerError("required verification has not passed for this installation")
            row.update(active=generation_id, enabled=True)
        else:
            row["enabled"] = False
        item = {
            "id": request_id,
            "plugin_id": plugin_id,
            "kind": kind,
            "request": payload,
            "state": "complete",
            "created_at": _now(),
            "updated_at": _now(),
            "error": None,
        }
        doc["operations"][request_id] = item
        doc["roster_revision"] = request_id
        # Pointer, revision and durable response commit together, with byte headroom reserved
        # for actual revocation even when the count or ordinary byte limit was reached.
        _write_roster(path, doc, revocation=revocation)
        return copy.deepcopy(item)


def overlay(loaded):
    """Apply committed rows only. Corrupt manager state refuses new work for every identity."""
    try:
        doc = snapshot()
    except (OSError, ValueError, provenance.ProvenanceError):
        loaded.providers.clear()
        loaded.problems["manager"] = "plugin manager state is unreadable; new work is unavailable"
        return loaded
    for plugin_id, row in doc["plugins"].items():
        try:
            _validate_row(plugin_id, row)
            if row["enabled"] is None:
                continue  # staged candidate: preserve the unmodified legacy/in-tree provider
            loaded.providers.pop(plugin_id, None)
            if row["enabled"]:
                loaded.providers[plugin_id] = provider(plugin_id, row["generations"][row["active"]])
        except Exception:
            loaded.providers.pop(plugin_id, None)
            loaded.problems[plugin_id] = "plugin state is invalid; this agent is disabled"
    return loaded
