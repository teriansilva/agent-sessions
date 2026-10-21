"""Directory-bound provenance: immutable signed bases outside editable content.

The staged directory carries an xattr pointer through its publication rename. A private
inode binding detects a removed/changed pointer. Ordinary playbook saves preserve the pointer;
skill edit copies preserve the imported base through a new origin record. Unrelated creates
and duplicates do not adopt provenance by local ID. Retained directories
keep their binding and evidence. Nothing here executes content or deletes history.
"""

from __future__ import annotations

import errno
import os
import re
import uuid
from dataclasses import dataclass

from . import contract, storage, trust

XATTR = "user.battlelab.gallery_origin"
_UUID = re.compile(r"[0-9a-f]{32}")


def rewritten(content: dict, local_id: str) -> dict:
    from ..playbooks import store, tomlw

    files = dict(content["files"])
    if content["kind"] == "playbook":
        store.playbook_id(local_id)
        doc = contract.tomllib.loads(files["playbook.toml"])
        doc["identity"]["id"] = local_id
        files["playbook.toml"] = tomlw.dumps(doc)
        tree = store.tree_from_files(files)
        store._validate(tree, local_id)
    return files


def _revision(kind, files):
    from ..playbooks import store
    from . import files as skill_files

    return (
        store.tree_from_files(files) if kind == "playbook" else skill_files.tree(files)
    ).digest()


def _evidence(pointer: str, kind: str, local_id: str | None) -> dict:
    if not isinstance(pointer, str) or not _UUID.fullmatch(pointer):
        raise contract.GalleryError("Invalid gallery origin pointer.")
    record = storage.read(storage.root() / f"origin-{pointer}.json")
    required = {
        "format",
        "kind",
        "id",
        "entry_digest",
        "cut_digest",
        "base_files",
        "base_revision",
        "original",
        "previous",
        "operation",
    }
    if (
        not record
        or set(record) != required
        or record["format"] != 1
        or record["kind"] != kind
        or (local_id is not None and record["id"] != local_id)
    ):
        raise contract.GalleryError("Gallery origin evidence is missing or inconsistent.")
    cut = record["cut_digest"]
    if not isinstance(cut, str) or not re.fullmatch(r"[0-9a-f]{64}", cut):
        raise contract.GalleryError("Invalid gallery evidence cut.")
    signed = storage.read(storage.root() / f"cut-{cut}.json")
    if signed is None:
        raise contract.GalleryError("The original signed gallery evidence is missing.")
    parsed = trust.checked(signed, historical=True)
    if parsed.digest != cut:
        raise contract.GalleryError("Gallery evidence cut does not match its digest.")
    entry = next((e for e in parsed.entries if e["digest"] == record["entry_digest"]), None)
    if entry is None or entry["content"]["kind"] != kind:
        raise contract.GalleryError("Original content is absent from its signed gallery evidence.")
    from ..playbooks import store

    store.playbook_id(record["id"])
    base = rewritten(entry["content"], record["id"])
    if record["base_files"] != base or record["base_revision"] != _revision(kind, base):
        raise contract.GalleryError("The imported gallery base is inconsistent.")
    if (
        not _UUID.fullmatch(str(record["original"]))
        or (record["previous"] is not None and not _UUID.fullmatch(str(record["previous"])))
        or not _UUID.fullmatch(str(record["operation"]))
    ):
        raise contract.GalleryError("Invalid gallery history pointer.")
    return {**record, "content": entry["content"], "pointer": pointer}


def evidence(pointer: str, kind: str, local_id: str) -> dict:
    current = _evidence(pointer, kind, local_id)
    checked = current
    seen = {pointer}
    while checked["previous"] is not None:
        prior = checked["previous"]
        if prior in seen or len(seen) >= 128:
            raise contract.GalleryError("Invalid gallery origin history.")
        seen.add(prior)
        checked = _evidence(prior, kind, None)
        if (
            checked["original"] != current["original"]
            or checked["content"]["id"] != current["content"]["id"]
        ):
            raise contract.GalleryError("Gallery origin history changed.")
    if checked["pointer"] != current["original"]:
        raise contract.GalleryError("The first imported gallery base is missing.")
    return current


def binding_path(fd):
    st = os.fstat(fd)
    return storage.root() / f"inode-{st.st_dev}-{st.st_ino}.json"


def pointer(fd) -> str | None:
    try:
        raw = os.getxattr(fd, XATTR)
    except OSError as exc:
        if exc.errno not in (errno.ENODATA, errno.ENOTSUP):
            raise contract.GalleryError("Gallery origin pointer could not be read.") from None
        raw = None
    try:
        value = None if raw is None else raw.decode("ascii")
    except UnicodeError:
        raise contract.GalleryError("Gallery origin pointer is damaged.") from None
    if value is not None and not _UUID.fullmatch(value):
        raise contract.GalleryError("Gallery origin pointer is damaged.")
    # No gallery use means no extra state is created for existing local libraries.
    binding = storage.read(binding_path(fd)) if storage.root().exists() else None
    if binding is not None and binding != {"format": 1, "pointer": value}:
        raise contract.GalleryError(
            "Gallery origin pointer changed or was removed; restore its evidence."
        )
    if value is not None and binding is None:
        raise contract.GalleryError("Gallery origin has no matching directory binding.")
    return value


def read(fd, kind, local_id):
    value = pointer(fd)
    return None if value is None else evidence(value, kind, local_id)


def bind(fd, value: str | None):
    if value is not None:
        if not _UUID.fullmatch(value):
            raise contract.GalleryError("Invalid gallery origin pointer.")
        try:
            os.setxattr(fd, XATTR, value.encode("ascii"), flags=os.XATTR_CREATE)
        except OSError:
            raise contract.GalleryError(
                "This filesystem cannot preserve gallery provenance; nothing was imported."
            ) from None
        os.fsync(fd)
    if value is not None or storage.root().exists():
        with storage.locked("inode-bindings"):
            storage.write(binding_path(fd), {"format": 1, "pointer": value})
    if pointer(fd) != value:
        raise contract.GalleryError("Gallery origin changed while staging.")


def create(kind, local_id, digest, operation, *, previous=None, previous_id=None, admitted=None):
    record, entry = trust.selected(digest) if admitted is None else admitted
    if entry["content"]["kind"] != kind:
        raise contract.GalleryError("Content kind does not match the target library.")
    base = rewritten(entry["content"], local_id)
    old = evidence(previous, kind, previous_id or local_id) if previous else None
    if old and (old["content"]["kind"], old["content"]["id"]) != (kind, entry["content"]["id"]):
        raise contract.GalleryError("An update must belong to the same upstream content.")
    value = uuid.uuid4().hex
    payload = dict(
        format=1,
        kind=kind,
        id=local_id,
        entry_digest=digest,
        cut_digest=record["digest"],
        base_files=base,
        base_revision=_revision(kind, base),
        original=old["original"] if old else value,
        previous=previous,
        operation=operation,
    )
    with storage.locked("origin-records"):
        path = storage.root() / f"origin-{value}.json"
        if path.exists():
            raise contract.GalleryError("Origin identity already exists.")
        storage.write(path, payload)
    return value


def copy_skill(pointer, source_id, new_id, operation):
    """Carry the same authenticated imported base to a local edit; no fresh feed is needed."""
    if pointer is None:
        return None
    old = evidence(pointer, "skill", source_id)
    value = uuid.uuid4().hex
    record = {k: v for k, v in old.items() if k not in {"content", "pointer"}}
    record.update(id=new_id, previous=pointer, operation=operation)
    with storage.locked("origin-records"):
        path = storage.root() / f"origin-{value}.json"
        if path.exists():
            raise contract.GalleryError("Origin identity already exists.")
        storage.write(path, record)
    return value


@dataclass
class Publication:
    operation: str
    payload: dict
    origin: str | None
    previous: str | None
    source_revision: str | None = None
    source_origin: str | None = None

    def prepare(self, root_fd, staging, pid, tree):
        if self.payload["action"] in {"update-copy", "edit-copy"}:
            from ..playbooks import store
            from . import skills

            source_id = self.payload["source_id"]
            # The caller holds this library's exclusive lock. Do not reopen its pathname
            # or recursively lock it. A later disk writer still owns the untouched source.
            if self.payload["kind"] == "playbook":
                entry = store._entry_at(root_fd, source_id, store.SOURCE_LOCAL)
                current_revision = entry.revision
                current_origin = entry.origin["pointer"] if entry.origin else None
            else:
                current = skills._read(root_fd, source_id)
                source = current.get("gallery_origin")
                if source and source.get("state") != "verified":
                    raise store.Conflict("Original provenance changed; compare again.")
                current_revision = current["revision"]
                current_origin = (current.get("gallery_origin") or {}).get("pointer")
            if current_revision != self.source_revision or current_origin != self.source_origin:
                raise store.Conflict("Original content or provenance changed; compare again.")
        with storage.locked("operation-" + self.operation) as path:
            if storage.read(path) is not None:
                raise contract.GalleryError(
                    "This operation already has a recorded outcome; check its status."
                )
            fd = os.open(
                staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root_fd
            )
            try:
                st = os.fstat(fd)
                if pointer(fd) != self.origin:
                    raise contract.GalleryError("Staged origin changed.")
                root = os.fstat(root_fd)
                storage.write(
                    path,
                    dict(
                        format=1,
                        payload=self.payload,
                        origin=self.origin,
                        staging=staging,
                        device=st.st_dev,
                        inode=st.st_ino,
                        root_device=root.st_dev,
                        root_inode=root.st_ino,
                        revision=tree.digest(),
                        state="planned",
                        result=None,
                    ),
                )
            finally:
                os.close(fd)


def view(record):
    if record is None:
        return None
    c = record["content"]
    return {
        "state": "verified",
        "pointer": record["pointer"],
        "original": record["original"],
        "previous": record["previous"],
        "kind": c["kind"],
        "id": c["id"],
        "version": c["version"],
        "title": c["title"],
        "publisher": c["publisher"],
        "source": c["source"],
        "entry_digest": record["entry_digest"],
        "base_revision": record["base_revision"],
    }
