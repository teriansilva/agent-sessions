"""Version-four variable identities and the retained upgrade snapshot (#1390).

IDs, the legacy identity ledger and values publish in ONE atomic document. Consumers
remain untouched; the ledger records scope/name/kind explicitly, including retired IDs.
The encryption identity remains the original scope/name, never the display label or ID.
No helper in this module decrypts a value or downgrades a store.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from uuid import uuid4

from .atomicjson import fsync_dir

ID_RE = re.compile(r"[0-9a-f]{32}")
IDENTITY_KEYS = frozenset({"id", "scope", "project_id", "name", "kind"})
DOCUMENT_KEYS = frozenset({"version", "variables", "identities", "collections"})
COLLECTIONS_MAX = 100
IDENTITIES_MAX = 32_000


class IdentityError(ValueError):
    """The identity ledger is incomplete or inconsistent; refuse use and publication."""


def new_id() -> str:
    return uuid4().hex


def identity(record: dict) -> dict:
    return {key: record[key] for key in IDENTITY_KEYS}


def validate_id(value: object) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise IdentityError("a variable ID must be 32 lowercase hexadecimal characters")
    return value


def validate(doc: dict, records: list[dict]) -> list[dict]:
    """Require every live record's immutable identity to match its durable ledger entry."""
    if set(doc) != DOCUMENT_KEYS or not isinstance(doc.get("identities"), list):
        raise IdentityError("the variable identity ledger is missing or has unknown metadata")
    ledger = doc["identities"]
    if len(ledger) > IDENTITIES_MAX:
        raise IdentityError("the variable identity ledger is full")
    by_id: dict[str, dict] = {}
    # Retired entries still validate completely; they are never silently repaired from a
    # same-name record. Imports are deferred to avoid the store/format import cycle.
    from . import template_vars as store

    for entry in ledger:
        if not isinstance(entry, dict) or set(entry) != IDENTITY_KEYS:
            raise IdentityError("an unreadable variable identity remains unresolved")
        vid = validate_id(entry["id"])
        if vid in by_id:
            raise IdentityError("a duplicate variable ID remains unresolved")
        try:
            store._name(entry["name"])
            if entry["kind"] not in store.KINDS:
                raise store.VariableError("invalid kind")
            store._coerce_scope(entry, 4)
        except store.VariableError as exc:
            raise IdentityError("an unreadable variable identity remains unresolved") from exc
        by_id[vid] = entry
    live_ids = set()
    for record in records:
        vid = validate_id(record.get("id"))
        if vid in live_ids or by_id.get(vid) != identity(record):
            raise IdentityError("a variable's immutable identity does not match its ledger")
        live_ids.add(vid)
    collections = doc.get("collections")
    if not isinstance(collections, list) or len(collections) > COLLECTIONS_MAX:
        raise IdentityError("the variable collections inventory is unreadable")
    collection_ids: set[str] = set()
    labels: set[str] = set()
    globals_ = {r["id"] for r in records if r["scope"] == "global"}
    for collection in collections:
        if not isinstance(collection, dict) or set(collection) != {
            "id",
            "name",
            "variable_ids",
            "created_at",
            "updated_at",
        }:
            raise IdentityError("a variable collection has unknown or missing metadata")
        cid = validate_id(collection["id"])
        try:
            label = store.tstore._text(collection, "name", max_len=80, required=True)
            store.tstore._number(collection["created_at"], "created_at")
            store.tstore._number(collection["updated_at"], "updated_at")
        except store.VariableError as exc:
            raise IdentityError("a variable collection is unreadable") from exc
        members = collection["variable_ids"]
        if not isinstance(members, list) or any(not isinstance(v, str) for v in members):
            raise IdentityError("collection membership must contain variable IDs")
        if len(set(members)) != len(members) or not set(members) <= globals_:
            raise IdentityError("a collection contains duplicate or missing variable IDs")
        if cid in collection_ids or label.casefold() in labels:
            raise IdentityError("a collection's ID and name must be unique")
        collection_ids.add(cid)
        labels.add(label.casefold())
    return ledger


def prepare(records: list[dict], original: dict) -> list[dict]:
    """Assign legacy IDs only inside the caller's authority fence and store lock."""
    if original.get("version", 0) >= 4:
        return list(validate(original, records))
    if set(original) - {"version", "variables"}:
        raise IdentityError("the legacy variables store has unknown metadata; nothing was written")
    for record in records:
        record["id"] = new_id()
        record["definition"] = {"value_type": "text"}
    return [identity(record) for record in records]


def publish_document(
    records: list[dict], ledger: list[dict], version: int, collections: list[dict]
) -> dict:
    """Append new identities; keep retired ones so deletion never makes an old ID reusable."""
    known = {entry["id"] for entry in ledger}
    out = list(ledger)
    for record in records:
        if record["id"] not in known:
            out.append(identity(record))
            known.add(record["id"])
    doc = {"version": version, "variables": records, "identities": out, "collections": collections}
    validate(doc, records)
    return doc


def retain_upgrade_backup(path: Path) -> Path:
    """Durably link the exact pre-upgrade bytes before publishing v4, never overwrite.

    A content-addressed name makes a retry after a crash idempotent. Symlinks and a
    mismatching pre-existing backup refuse the upgrade. A failed backup or directory
    flush propagates before the caller can replace the original store.
    """
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    target = path.with_name(f"{path.name}.pre-v4-{digest}")
    if not stat.S_ISREG(path.lstat().st_mode):
        raise IdentityError("the variable store must be a regular file before upgrade")
    # Existing stores are private; tightening a legacy mode preserves its data and also
    # applies to the retained hard link. Never create a readable secret-bearing backup.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fchmod(fd, 0o600)
        os.fsync(fd)
        try:
            os.link(path, target, follow_symlinks=False)
        except FileExistsError:
            if not stat.S_ISREG(target.lstat().st_mode) or target.read_bytes() != content:
                raise IdentityError(
                    "the retained variable backup does not match the store"
                ) from None
        backup_fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fchmod(backup_fd, 0o600)
            os.fsync(backup_fd)
        finally:
            os.close(backup_fd)
        fsync_dir(path.parent)
    finally:
        os.close(fd)
    return target
