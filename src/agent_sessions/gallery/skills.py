"""App-owned inert skill copies: every write publishes a new name with NOREPLACE."""

from __future__ import annotations

import contextlib
import os
import secrets

from ..playbooks import store
from . import contract, files, origins, storage


@contextlib.contextmanager
def locked(*, exclusive):
    with storage.private.directory(storage.skills_root()) as fd, store._flock(fd, exclusive, 5):
        if exclusive:
            for name in os.listdir(fd):
                match = store.STAGING_RE.fullmatch(name)
                if match:
                    store._retain(fd, name, match.group(1))
        yield fd


def _read(root_fd, local_id):
    store.skill_id(local_id)
    try:
        fd = os.open(local_id, files.DIR, dir_fd=root_fd)
    except FileNotFoundError:
        raise store.NotFound("Unknown local skill.") from None
    try:
        tree = files.read(fd)
        try:
            origin = origins.read(fd, "skill", local_id)
            status = origins.view(origin)
        except (OSError, ValueError) as exc:
            status = {"state": "unavailable", "error": str(exc)}
        return {
            "id": local_id,
            "revision": tree.digest(),
            "files": {k: v.decode("utf-8") for k, v in tree.files.items()},
            "gallery_origin": status,
            "editable": True,
        }
    finally:
        os.close(fd)


def get(local_id):
    with locked(exclusive=False) as fd:
        return _read(fd, local_id)


def listing():
    if not storage.skills_root().exists():
        return {"skills": []}
    with locked(exclusive=False) as fd:
        out = []
        names = sorted(n for n in os.listdir(fd) if not n.startswith("."))
        for name in names[:128]:
            try:
                out.append(_read(fd, name))
            except (OSError, ValueError, store.StoreError) as exc:
                out.append({"id": name, "error": str(exc), "editable": False})
        return {"skills": out, "limit_reached": len(names) > 128}


def _stage(fd, tree, local_id, pointer):
    name = f".staging-{local_id}-{secrets.token_hex(8)}"
    os.mkdir(name, 0o700, dir_fd=fd)
    descriptors = {}
    try:
        descriptors[""] = os.open(name, files.DIR, dir_fd=fd)
        for directory in sorted(tree.dirs, key=lambda p: (p.count("/"), p)):
            parent, _, leaf = directory.rpartition("/")
            os.mkdir(leaf, 0o700, dir_fd=descriptors[parent])
            descriptors[directory] = os.open(leaf, files.DIR, dir_fd=descriptors[parent])
        for path, data in tree.files.items():
            parent, _, leaf = path.rpartition("/")
            child = os.open(
                leaf,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=descriptors[parent],
            )
            try:
                store._write_all(child, data)
                os.fsync(child)
            finally:
                os.close(child)
        for child in descriptors.values():
            os.fsync(child)
        origins.bind(descriptors[""], pointer)
        if files.read(descriptors[""]).digest() != tree.digest():
            raise store.Conflict("Skill changed while staging.")
        return name
    except BaseException:
        store._retain(fd, name, local_id)
        raise
    finally:
        for child in descriptors.values():
            os.close(child)


def save(local_id, contents, *, publication=None):
    """Create a new skill only. Existing identities are never replaced."""
    store.skill_id(local_id)
    tree = files.tree(contents)
    with locked(exclusive=True) as fd:
        if store._exists_locally(fd, local_id):
            raise store.Conflict("That local skill ID already exists.")
        if len([n for n in os.listdir(fd) if not n.startswith(".")]) >= 128:
            raise contract.GalleryError("Local skill library is full.")
        pointer = publication.origin if publication else None
        staging = _stage(fd, tree, local_id, pointer)
        if publication:
            publication.prepare(fd, staging, local_id, tree)
        not_durable = store._publish_new(fd, staging, local_id)
        try:
            result = _read(fd, local_id)
            origin = result["gallery_origin"]
            valid = result["revision"] == tree.digest() and (
                origin is None
                and pointer is None
                or origin is not None
                and origin["state"] == "verified"
                and origin["pointer"] == pointer
            )
        except (OSError, ValueError, store.StoreError):
            valid = False
        if not valid:
            store._publication_conflict(fd, local_id)
        if not_durable:
            result.update(durable=False, durable_reason=not_durable)
        return result
