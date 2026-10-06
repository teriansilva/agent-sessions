"""#1187 new-folder mode: CREATE an absent-target review's folder, and let BIND adopt only it.

1. REVIEW (`review.build` with `create: true`) binds the held parent's identity and an ABSENT
   target, with every touched path absent.
2. CREATE (`create`) verifies that signed review and makes the folder with
   `destination.create_target` (exclusive, beneath the held parent, fails if the name exists).
   Its durable checkpoint writes a private ledger entry, keyed by the review digest, holding the
   new folder's device and inode and a digest of the plan's effects. Only a recorded folder can be
   adopted: one created by someone else, or whose record never landed, is not.
3. BIND (`adopt`) requires that ledger entry, a live folder that is exactly that inode, and a fresh
   review of the now-existing folder in which every touched path is still absent and the effects
   are identical. Bind then proceeds as an ordinary existing-folder bind, which pins the folder's
   identity, so APPLY refuses a replaced target with no special case.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import stat
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

from itsdangerous import BadData, URLSafeTimedSerializer

from .. import atomicjson
from . import deployment_state as state
from . import destination, review, store

LEDGER_DIR = ".targets"
_ENTRY_MAX = 64 * 1024
_EFFECTS = (
    "playbook",
    "variables",
    "materials",
    "changes",
    "conflicts",
    "targets",
    "assignments",
    "capability_requests",
    "rituals",
)


def effects(public: dict, key: str) -> str:
    """What the plan does, without where: the digest a created folder's adoption must match."""
    return review._digest({name: public[name] for name in _EFFECTS}, key)


@contextlib.contextmanager
def _ledger() -> Iterator[int]:
    created = store._open_root(create=True)
    if created is not None:
        os.close(created)
    with store.root_lock(exclusive=False) as root:
        if root is None:
            raise store.StoreError("the playbook store is unavailable", status=503)
        fd = state._directory(root, LEDGER_DIR, True)
    try:
        with store._flock(fd, exclusive=True, wait=store.LOCK_WAIT_S):
            yield fd
    finally:
        os.close(fd)


def _read(fd: int, digest: str) -> dict | None:
    try:
        source = os.open(
            f"{digest}.json",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=fd,
        )
    except FileNotFoundError:
        return None
    try:
        st = os.fstat(source)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_nlink != 1
            or st.st_uid != os.geteuid()
            or stat.S_IMODE(st.st_mode) != 0o600
            or st.st_size > _ENTRY_MAX
        ):
            raise store.Conflict("the created-folder record is not a bounded private file")
        value = json.loads(os.read(source, _ENTRY_MAX + 1))
        if not isinstance(value, dict) or set(value) != {
            "playbook_id",
            "path",
            "device",
            "inode",
            "effects",
        }:
            raise ValueError
        if not all(type(value[k]) is int for k in ("device", "inode")):
            raise ValueError
        store.revision(value["effects"])
    except (UnicodeError, ValueError, store.StoreError):
        raise store.Conflict("the created-folder record is damaged") from None
    finally:
        os.close(source)
    return value


def _signed_digest(receipt: object, key: str) -> str:
    try:
        payload = URLSafeTimedSerializer(key, salt=review._SALT).loads(
            receipt, max_age=review.REVIEW_TTL
        )
        return store.revision(payload["digest"])
    except (BadData, KeyError, TypeError, store.StoreError):
        raise store.Conflict("the review confirmation is invalid or expired") from None


def create(playbook_id: str, inputs: dict, receipt: str, *, key: str) -> dict:
    """CREATE: make the reviewed absent target exclusively and durably record its identity."""
    if not isinstance(inputs, dict) or inputs.get("create") is not True:
        raise store.StoreError("only a new-folder review creates a folder")
    digest = _signed_digest(receipt, key)
    # The shared authoring lock is held from planning through publication and checkpoint, so a
    # playbook deletion (exclusive) cannot commit in between: CREATE never succeeds for a source
    # that is gone. A retry re-checks that the reviewed source revision still exists.
    with store.root_lock(exclusive=False) as root_fd, _ledger() as fd:
        if root_fd is None:
            raise store.StoreError("the playbook store is unavailable", status=503)
        existing = _read(fd, digest)
        if existing is not None:
            try:
                entry = store._find(store._all_entries(root_fd, strict=True), playbook_id)
            except store.NotFound:
                entry = None
            if entry is None or entry.error or entry.revision != inputs.get("revision"):
                raise store.Conflict("the reviewed playbook changed or is gone; review it again")
            # A retry of this review's CREATE: answer only for the very folder it recorded.
            live = destination.review_folder(existing["path"])
            if (live.device, live.inode) != (existing["device"], existing["inode"]):
                raise store.Conflict("the created folder was replaced; review it again")
            return {**asdict(live), "review_digest": digest}
        plan = review.build(playbook_id, inputs, key=key)
        review.accept(plan, receipt, key=key)
        if plan.public["conflicts"]:
            raise store.Conflict("resolve the review's conflicts before creating the folder")
        dest = plan.public["destination"]
        parent = destination.Folder(**dest["parent"])
        name = os.path.basename(dest["path"])
        fx = effects(plan.public, key)

        def checkpoint(folder: destination.Folder) -> None:
            entry = {
                "playbook_id": playbook_id,
                "path": folder.path,
                "device": folder.device,
                "inode": folder.inode,
                "effects": fx,
            }
            atomicjson.atomic_write_json(Path(f"/proc/self/fd/{fd}") / f"{digest}.json", entry)

        try:
            folder = destination.create_target(parent, name, record=checkpoint)
        except OSError:
            raise store.StoreError(
                "the new folder could not be created safely", status=409
            ) from None
        except destination.FsError as e:
            raise store.StoreError(str(e), status=e.status) from None
        return {**asdict(folder), "review_digest": digest}


def adopt(
    playbook_id: str, inputs: dict, receipt: str, *, key: str, record, records
) -> tuple[review.Plan, dict]:
    """BIND's half: the existing-folder plan for the folder CREATE made, or a refusal."""
    digest = _signed_digest(receipt, key)
    with _ledger() as fd:
        entry = _read(fd, digest)
    if entry is None or entry["playbook_id"] != playbook_id:
        raise store.Conflict("this review created no folder; create it before binding")
    existing_inputs = {k: v for k, v in inputs.items() if k != "create"}
    try:
        live = destination.review_folder(existing_inputs.get("destination"))
    except destination.FsError:
        raise store.Conflict("the created folder is gone; review it again") from None
    if (live.path, live.device, live.inode) != (entry["path"], entry["device"], entry["inode"]):
        raise store.Conflict("the created folder was replaced; review it again")
    plan = review.build(playbook_id, existing_inputs, key=key, _record=record, _variables=records)
    # Every touched path AND every parent directory was reviewed as absent; any one present now
    # (even an empty intermediate directory) is content the review did not see.
    if any(node.kind != "absent" for node in plan.nodes.values()):
        raise store.Conflict("the created folder has content the review did not see")
    if not hmac.compare_digest(effects(plan.public, key), entry["effects"]):
        raise store.Conflict("the plan changed since the new-folder review; review it again")
    return plan, existing_inputs
