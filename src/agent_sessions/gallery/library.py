"""Explicit imports and reviewed updates create new copies; originals stay untouched."""

from __future__ import annotations

import hashlib
import re

from ..playbooks import loader, store
from . import contract, origins, skills, storage, trust


def _kind(value):
    if value not in ("playbook", "skill"):
        raise contract.GalleryError("Choose a playbook or skill.")
    return value


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise contract.GalleryError("Expected a 32-character operation ID.")
    return value


def local(kind, local_id):
    return store.get_playbook(local_id) if _kind(kind) == "playbook" else skills.get(local_id)


def catalog():
    try:
        record = trust.current()
        if record is None:
            return dict(state="empty", entries=[], error=None)
        parsed = trust.checked(record, historical=True)
        try:
            trust.checked(record)
            fresh = True
        except ValueError:
            fresh = False
        return dict(
            state="ready" if fresh else "stale",
            sequence=parsed.sequence,
            digest=parsed.digest,
            issued_at=parsed.issued_at,
            expires_at=parsed.expires_at,
            entries=list(parsed.entries),
            error=None,
            source="https://relay.battlelab.superstatus.io/catalogs/gallery",
        )
    except (ValueError, OSError) as exc:
        return dict(state="unavailable", entries=[], error=str(exc))


def inspect(digest, local_id):
    record, entry = trust.selected(digest, fresh=False)
    eligible = True
    reason = None
    preview = None
    try:
        trust.selected(digest)
        store.local_id(entry["content"]["kind"], local_id)
        base = origins.rewritten(entry["content"], local_id)
        origins._revision(entry["content"]["kind"], base)
        if entry["content"]["kind"] == "playbook":
            playbook = store._validate(store.tree_from_files(base), local_id)
            preview = {
                "flows": loader.card(playbook)["flows"],
                "inputs": [
                    {key: item[key] for key in ("name", "label", "required", "type") if key in item}
                    for item in playbook["variables"]
                ],
            }
    except (ValueError, store.StoreError) as exc:
        eligible = False
        reason = str(exc)
    return dict(
        entry=entry,
        importable=eligible,
        reason=reason,
        preview=preview,
        cut_digest=record["digest"],
    )


def compare(kind, local_id, digest, *, admitted=None):
    current = local(kind, local_id)
    origin = current.get("gallery_origin")
    if not origin or origin.get("state") != "verified":
        raise contract.GalleryError("This copy has no usable gallery origin.")
    original = origins.evidence(origin["pointer"], kind, local_id)
    _, entry = trust.selected(digest) if admitted is None else admitted
    if (entry["content"]["kind"], entry["content"]["id"]) != (kind, original["content"]["id"]):
        raise contract.GalleryError("Choose an update of the same upstream content.")
    base = original["base_files"]
    incoming = origins.rewritten(entry["content"], local_id)
    mine = current["files"]
    proposal = {}
    conflicts = []
    changes = []
    for name in sorted(set(base) | set(mine) | set(incoming)):
        before, local_value, remote_value = base.get(name), mine.get(name), incoming.get(name)
        chosen = remote_value if local_value == before else local_value
        if local_value != before and remote_value != before and local_value != remote_value:
            conflicts.append(name)
        if chosen is not None:
            proposal[name] = chosen
        if before != local_value or before != remote_value:
            changes.append(name)
    binding = dict(
        kind=kind,
        id=local_id,
        revision=current["revision"],
        origin=origin["pointer"],
        incoming_digest=digest,
        base_revision=original["base_revision"],
    )
    digest_value = hashlib.sha256(contract.canonical(binding)).hexdigest()
    return {
        **binding,
        "comparison": digest_value,
        "base": base,
        "local": mine,
        "incoming": incoming,
        "proposed": proposal,
        "conflicts": conflicts,
        "changed_files": changes,
        "incoming_version": entry["content"]["version"],
        "base_version": original["content"]["version"],
    }


def operation(operation_id):
    _id(operation_id)
    # Recovery must not settle an operation while its publisher is still validating it.
    with storage.locked("publications"):
        return _operation(operation_id)


def _operation(operation_id):
    """Read/recover an outcome while the caller holds the publication lane."""
    with storage.locked("operation-" + operation_id) as path:
        record = storage.read(path)
        if record is None:
            raise store.NotFound("No gallery operation was recorded.")
        if record["state"] == "complete":
            return {"operation": operation_id, "state": "complete", "result": record["result"]}
    payload = record["payload"]
    if payload["action"] == "update":
        # Matching new bytes cannot prove that the displaced revision/origin was valid.
        # A rejected or interrupted update may intentionally leave those bytes live.
        return {
            "operation": operation_id,
            "state": "uncertain",
            "message": (
                "This interrupted update has no recorded completed outcome. Inspect live "
                "and retained copies; the update will not be repeated."
            ),
        }
    try:
        current = local(payload["kind"], payload["id"])
    except (ValueError, OSError, store.StoreError):
        current = None
    origin = current.get("gallery_origin") if current else None
    if (
        origin
        and origin.get("state") == "verified"
        and origin.get("pointer") == record["origin"]
        and current["revision"] == record["revision"]
    ):
        # A pointer can survive a rename whose candidate changed before verification.
        # Without a completed outcome, only the exact planned bytes prove publication.
        # A later local edit is preserved but leaves this interrupted operation uncertain.
        result = {
            "id": payload["id"],
            "kind": payload["kind"],
            "revision": current["revision"],
            "gallery_origin": origin,
            "recovered": True,
            "durable": False,
            "durable_reason": (
                "Publication was found after an interrupted response; "
                "durability before restart is unknown."
            ),
        }
        with storage.locked("operation-" + operation_id) as path:
            latest = storage.read(path)
            if latest is None or latest["payload"] != record["payload"]:
                raise contract.GalleryError("Operation evidence changed during recovery.")
            storage.write(path, {**latest, "state": "complete", "result": result})
        return {"operation": operation_id, "state": "complete", "result": result}
    return {
        "operation": operation_id,
        "state": "uncertain",
        "message": (
            "No exact matching publication was found. Inspect local copies "
            "and evidence; this operation will not create another copy."
        ),
    }


def _existing(operation_id, payload):
    with storage.locked("operation-" + operation_id) as path:
        record = storage.read(path)
        if record is not None and record["payload"] != payload:
            raise store.Conflict("This operation ID belongs to different content.")
    return _operation(operation_id) if record is not None else None


def _complete(operation_id, result):
    with storage.locked("operation-" + operation_id) as path:
        record = storage.read(path)
        if record is None:
            raise contract.GalleryError("Publication outcome has no durable operation record.")
        origin = result.get("gallery_origin")
        matches_origin = (
            origin is None
            if record["origin"] is None
            else origin is not None
            and origin.get("pointer") == record["origin"]
            and origin.get("state") == "verified"
        )
        if not matches_origin or result.get("revision") != record["revision"]:
            return {
                "operation": operation_id,
                "state": "uncertain",
                "message": (
                    "Published content and origin could not be verified against the operation; "
                    "inspect retained evidence."
                ),
            }
        compact = {
            key: result[key]
            for key in (
                "id",
                "revision",
                "gallery_origin",
                "durable",
                "durable_reason",
                "retained",
                "recovery_durable",
            )
            if key in result
        }
        compact["kind"] = record["payload"]["kind"]
        try:
            storage.write(path, {**record, "state": "complete", "result": compact})
        except (OSError, ValueError):
            compact.update(
                durable=False,
                durable_reason=(
                    "Content is published; operation completion could not be flushed. "
                    "Check this operation before retrying."
                ),
            )
        return {"operation": operation_id, "state": "complete", "result": compact}


def import_copy(kind, local_id, digest, operation_id, acknowledge):
    _kind(kind)
    store.local_id(kind, local_id)
    _id(operation_id)
    if acknowledge is not True:
        raise contract.GalleryError("Review and acknowledge the complete content before import.")
    payload = dict(action="import", kind=kind, id=local_id, digest=digest)
    # One publication lane serializes new/import/update attempts across processes.
    with storage.locked("publications"):
        prior = _existing(operation_id, payload)
        if prior is not None:
            return prior
        with trust.admitted(digest) as admitted:
            _, entry = admitted
            pointer = origins.create(kind, local_id, digest, operation_id, admitted=admitted)
            publication = origins.Publication(operation_id, payload, pointer, None)
            files = origins.rewritten(entry["content"], local_id)
            result = (
                store.create_playbook(files, _publication=publication)
                if kind == "playbook"
                else skills.save(local_id, files, publication=publication)
            )
            return _complete(operation_id, result)


def apply_update(kind, local_id, digest, comparison, files, operation_id, acknowledge, *, new_id):
    _kind(kind)
    store.local_id(kind, local_id)
    store.local_id(kind, new_id)
    if new_id == local_id:
        raise store.Conflict("Choose a new copy ID; the original stays unchanged.")
    _id(operation_id)
    if acknowledge is not True:
        raise contract.GalleryError("Review and acknowledge the results before creating a copy.")
    payload = dict(
        action="update-copy",
        kind=kind,
        id=new_id,
        source_id=local_id,
        digest=digest,
        comparison=comparison,
        files_digest=hashlib.sha256(contract.canonical(files)).hexdigest(),
    )
    with storage.locked("publications"):
        prior = _existing(operation_id, payload)
        if prior is not None:
            return prior
        with trust.admitted(digest) as admitted:
            view = compare(kind, local_id, digest, admitted=admitted)
            if view["comparison"] != comparison:
                raise store.Conflict(
                    "Local content or provenance changed. Your draft is retained; compare again."
                )
            pointer = origins.create(
                kind,
                new_id,
                digest,
                operation_id,
                previous=view["origin"],
                previous_id=local_id,
                admitted=admitted,
            )
            publication = origins.Publication(
                operation_id,
                payload,
                pointer,
                None,
                source_revision=view["revision"],
                source_origin=view["origin"],
            )
            if kind == "playbook":
                from ..playbooks import tomlw

                tree = store.tree_from_files(files)
                store._validate(tree, local_id)
                manifest = contract.tomllib.loads(tree.files["playbook.toml"].decode("utf-8"))
                manifest["identity"]["id"] = new_id
                files = {**files, "playbook.toml": tomlw.dumps(manifest)}
            result = (
                store.create_playbook(files, _publication=publication)
                if kind == "playbook"
                else skills.save(new_id, files, publication=publication)
            )
            return _complete(operation_id, result)


def edit_skill_copy(local_id, new_id, revision, origin, files, operation_id):
    """Save local edits under a new identity, preserving the source and its imported base."""
    store.skill_id(local_id)
    store.skill_id(new_id)
    store.revision(revision)
    _id(operation_id)
    if new_id == local_id:
        raise store.Conflict("Choose a new copy ID; the original stays unchanged.")
    payload = dict(
        action="edit-copy",
        kind="skill",
        id=new_id,
        source_id=local_id,
        revision=revision,
        source_origin=origin,
        files_digest=hashlib.sha256(contract.canonical(files)).hexdigest(),
    )
    with storage.locked("publications"):
        prior = _existing(operation_id, payload)
        if prior is not None:
            return prior
        current = skills.get(local_id)
        source = current.get("gallery_origin")
        if source and source.get("state") != "verified":
            raise store.Conflict("Restore skill provenance before copying edits.")
        pointer = source["pointer"] if source else None
        if current["revision"] != revision or pointer != origin:
            raise store.Conflict("This skill changed; compare again.")
        publication = origins.Publication(
            operation_id,
            payload,
            origins.copy_skill(pointer, local_id, new_id, operation_id),
            None,
            source_revision=revision,
            source_origin=pointer,
        )
        return _complete(operation_id, skills.save(new_id, files, publication=publication))


def create_skill(new_id, files):
    """A new local skill from scratch (#1611): no source, no origin. Existing ids are refused."""
    store.skill_id(new_id)
    return skills.save(new_id, files)
