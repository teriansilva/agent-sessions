"""Durable, per-proposal approval for API-agent edits (#1230).

Model output can only stage a proposal. A separate authenticated operator decision calls the
existing lease-bound file editor. Pending content/checkpoints live in private sidecars, never
the append-only transcript; a terminal decision clears them. A durable `deciding` marker precedes
the save. Interrupted decisions are refused, never replayed: the operator inspects the file.
Owned ancestry must be syncable before any checkpoint or turn is acknowledged. A private,
immutable record permits only a search-only boundary proven to predate this proposal tree.
"""

from __future__ import annotations

import contextlib
import difflib
import fcntl
import hashlib
import json
import os
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import chat_config, chat_store, chat_tools, fileedit, files
from .fsbrowse import FsError

MAX_BYTES = chat_tools.READ_MAX_BYTES
MAX_RECORD_BYTES = 4 * 1024 * 1024
SPEC = {
    "type": "function",
    "function": {
        "name": "propose_edit",
        "description": "Propose a whole-file replacement of an existing text file. Read the "
        "whole file first and echo its base_sha256. Use this as the only call in the round. "
        "Nothing is saved until the operator approves. LF newlines, no BOM, at most 64 KiB.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "base_sha256": {"type": "string"},
            },
            "required": ["path", "content", "base_sha256"],
            "additionalProperties": False,
        },
    },
}


@dataclass
class TurnFence:
    """Process-shared ownership of a decision/turn, retained until its task has settled."""

    fd: int | None
    handed_off: bool = False

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def turn_fence(root: Path, sid: str) -> TurnFence | None:
    path = _directory(root, sid) / ".turn.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.getuid()
            or st.st_mode & 0o077
            or st.st_nlink != 1
        ):
            raise FsError("turn lock is not private", status=503)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        return TurnFence(fd)
    except BaseException:
        os.close(fd)
        raise


def binding(engine_id: str) -> str:
    """Bind a paused turn to its endpoint configuration, without storing a key. For the API agent
    on Settings → AI this is the RESOLVED endpoint, so a rotation there invalidates it (#1305)."""
    return chat_config.resolved_binding(engine_id)


def _initial_boundary(root: Path) -> dict | None:
    """Observe a pre-existing search-only boundary BEFORE creating any directory entries."""
    path = root.absolute()
    child_existed = False
    while path != path.parent:
        try:
            st = path.lstat()
        except FileNotFoundError:
            child_existed = False
            path = path.parent
            continue
        if not stat.S_ISDIR(st.st_mode):
            raise FsError("proposal ancestry is not a directory", status=503)
        if st.st_uid != os.getuid():
            break
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except PermissionError:
            if child_existed and not st.st_mode & stat.S_IWUSR:
                return {"path": str(path), "device": st.st_dev, "inode": st.st_ino}
            raise FsError("proposal ancestry cannot be synced", status=503) from None
        else:
            os.close(fd)
        child_existed = True
        path = path.parent
    return None


def _ancestry_boundary(directory: Path, initial: dict | None, *, created: bool) -> dict | None:
    """Persist the only allowed traversal boundary without replacing another worker's record.

    An existing tree with no record has unknown history: every owned ancestor must be synced.
    That is also the crash-before-record recovery path. The record is installed complete, before
    the ancestor barriers, and never broadened after a failed barrier or a permission change.
    """
    marker = directory / ".ancestry"
    try:
        record = _read(marker)
    except FsError as exc:
        if exc.status != 404:
            raise
        if not created and initial is not None:
            # A first creator may still be publishing its evidence. Refuse this attempt at
            # the barrier without racing in a permanent denial of its legitimate boundary.
            return None
        temporary = directory / f".ancestry-{uuid.uuid4().hex}"
        try:
            data = json.dumps({"id": ".ancestry", "boundary": initial if created else None})
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data.encode())
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.link(temporary, marker, follow_symlinks=False)
            except FileExistsError:
                pass
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
        record = _read(marker)
    boundary = record.get("boundary")
    if set(record) != {"id", "boundary"} or (
        boundary is not None
        and (
            not isinstance(boundary, dict)
            or set(boundary) != {"path", "device", "inode"}
            or not isinstance(boundary["path"], str)
            or not Path(boundary["path"]).is_absolute()
            or type(boundary["device"]) is not int
            or type(boundary["inode"]) is not int
            or boundary["device"] < 0
            or boundary["inode"] <= 0
        )
    ):
        raise FsError("proposal ancestry record is invalid", status=503)
    return boundary


def _directory(root: Path, sid: str) -> Path:
    if not chat_store.valid_turn_id(sid):
        raise FsError("invalid conversation id", status=422)
    directory = root / "proposals" / sid
    initial = _initial_boundary(root)
    created = False
    try:
        directory.parent.mkdir(mode=0o700, parents=True)
        created = True
    except FileExistsError:
        pass
    directory.mkdir(mode=0o700, exist_ok=True)
    for path in (directory.parent, directory):
        st = path.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise FsError("proposal storage is not private", status=503)
    boundary = _ancestry_boundary(directory.parent, initial, created=created)
    # A first-use checkpoint is durable only if its newly created ancestry is reachable after
    # power loss too. Repeat on every use: an earlier failed sync can leave directories present
    # but not durable, so existence cannot stand in for a completed barrier.
    parents = []
    path = directory.parent.absolute()
    try:
        while path != path.parent:
            st = path.lstat()
            if not stat.S_ISDIR(st.st_mode):
                raise FsError("proposal ancestry is not a directory", status=503)
            if st.st_uid != os.getuid():
                break
            try:
                fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except PermissionError:
                # Current mode is not history: this directory may have acquired a new child
                # before a failed sync, then become search-only. Only the recorded boundary
                # predates creation; a changed/missing record or changed inode cannot bless it.
                if st.st_mode & stat.S_IWUSR or boundary != {
                    "path": str(path),
                    "device": st.st_dev,
                    "inode": st.st_ino,
                }:
                    raise FsError("proposal ancestry cannot be synced", status=503) from None
                break
            parents.append(fd)
            path = path.parent
        for fd in reversed(parents):
            os.fsync(fd)
    finally:
        for fd in parents:
            os.close(fd)
    return directory


def _path(root: Path, sid: str, pid: str) -> Path:
    if not chat_store.valid_turn_id(pid):
        raise FsError("invalid proposal id", status=422)
    return _directory(root, sid) / f"{pid}.json"


def _write(path: Path, record: dict) -> None:
    data = json.dumps(record, ensure_ascii=True).encode()
    if len(data) > MAX_RECORD_BYTES:
        raise FsError("proposal checkpoint is too large", status=422)
    tmp = path.with_name(f".{path.name}-{uuid.uuid4().hex}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def _read(path: Path) -> dict:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
                raise FsError("proposal storage is not private", status=503)
            data = fh.read(MAX_RECORD_BYTES + 1)
        if len(data) > MAX_RECORD_BYTES:
            raise ValueError
        rec = json.loads(data)
        if not isinstance(rec, dict) or rec.get("id") != path.stem:
            raise ValueError
        return rec
    except FileNotFoundError:
        raise FsError("no such proposal", status=404) from None
    except (ValueError, OSError):
        raise FsError("proposal cannot be read", status=503) from None


def admission(cwd: str, expected_root: str, target: str) -> None:
    """Live read policy on a descriptor-proven path, also used inside fileedit's save."""
    root = chat_tools.admit(cwd)
    rel = chat_tools._verified_rel(expected_root, target)
    if root != expected_root or rel is None or not chat_tools._in_boundary(target):
        raise FsError("the file is outside the conversation's current boundary", status=403)
    why = chat_tools.refused_name(chat_tools._parts(rel))
    if why:
        raise FsError(why, status=403)


def _file(cwd: str, raw_path: object) -> tuple[str, str, bytes]:
    root = chat_tools.admit(cwd)
    if root is None:
        raise FsError("the conversation's folder is not readable now", status=403)
    resolved = chat_tools._resolve(root, raw_path)
    if isinstance(resolved, str):
        raise FsError(resolved, status=403)
    candidate, relative = resolved
    why = chat_tools.refused_name(chat_tools._parts(relative))
    if why:
        raise FsError(why, status=403)
    verified, data, _size, cut = files.read_file_bytes(candidate, limit=MAX_BYTES, root=root)
    admission(cwd, root, verified)
    if cut:
        raise FsError("only complete files up to 64 KiB can be proposed", status=422)
    reason, _eol, _bom = fileedit._text_shape(data)
    if reason:
        raise FsError(f"this file {reason}", status=422)
    return root, verified, data


def stage(
    root: Path,
    sid: str,
    tid: str,
    engine_id: str,
    cwd: str,
    args: dict,
    *,
    call_id: str,
    checkpoint: dict,
    reads: dict[str, str],
    endpoint_binding: str,
) -> dict:
    """Validate and persist a proposal; never write the target file."""
    if not chat_config.edits_enabled(engine_id):
        raise FsError("the operator has not enabled proposed edits", status=403)
    if not isinstance(args, dict) or set(args) != {"path", "content", "base_sha256"}:
        raise FsError("expected path, content and base_sha256", status=422)
    expected = args["base_sha256"]
    folder, verified, data = _file(cwd, args["path"])
    current = hashlib.sha256(data).hexdigest()
    if not isinstance(expected, str) or reads.get(verified) != expected:
        raise FsError("read this file completely in this turn before proposing an edit", status=409)
    if expected != current:
        raise FsError("the file changed since it was read; read it again", status=409)
    _reason, eol, bom = fileedit._text_shape(data)
    new = fileedit._encode(args["content"], eol, bom)
    if len(new) > MAX_BYTES:
        raise FsError("the proposed file exceeds 64 KiB", status=422)
    if new == data:
        raise FsError("the proposed content is unchanged", status=422)
    if binding(engine_id) != endpoint_binding:
        raise FsError("the endpoint configuration changed during this turn", status=409)
    pid = str(uuid.uuid4())
    rec = {
        "id": pid,
        "turn_id": tid,
        "engine_id": engine_id,
        "cwd": cwd,
        "root": folder,
        "path": os.path.relpath(verified, folder),
        "target": verified,
        "base_sha256": current,
        "new_sha256": hashlib.sha256(new).hexdigest(),
        "size": len(new),
        "base": data.decode("utf-8-sig").replace("\r\n", "\n"),
        "content": args["content"],
        "status": "awaiting_approval",
        "created_at": time.time(),
        "call_id": call_id,
        "checkpoint": checkpoint,
        "binding": endpoint_binding,
    }
    _write(_path(root, sid, pid), rec)
    return rec


def summary(rec: dict) -> dict:
    return {
        k: rec.get(k)
        for k in (
            "id",
            "turn_id",
            "path",
            "base_sha256",
            "new_sha256",
            "size",
            "status",
            "created_at",
            "decided_at",
            "decided_by",
            "reason",
        )
    }


def views(root: Path, sid: str, tid: str) -> list[dict]:
    out = []
    for path in sorted(_directory(root, sid).glob("*.json")):
        rec = _read(path)
        if rec.get("turn_id") != tid:
            continue
        view = summary(rec)
        if rec["status"] == "awaiting_approval":
            try:
                folder, target, data = _file(rec["cwd"], rec["path"])
                if (
                    folder != rec["root"]
                    or target != rec["target"]
                    or hashlib.sha256(data).hexdigest() != rec["base_sha256"]
                ):
                    raise FsError("the file changed after the agent read it", status=409)
                if not chat_config.edits_enabled(rec["engine_id"]):
                    raise FsError("proposed edits are turned off", status=409)
                if binding(rec["engine_id"]) != rec["binding"]:
                    raise FsError("the endpoint configuration changed", status=409)
                view["can_approve"] = True
            except FsError as e:
                view["can_approve"], view["reason"] = False, str(e)
            lines = difflib.unified_diff(
                rec["base"].splitlines(keepends=True),
                rec["content"].splitlines(keepends=True),
                fromfile=rec["path"],
                tofile=rec["path"],
                n=3,
            )
            view["diff"] = "".join(
                line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                for line in lines
            )
        out.append(view)
    return out


def interrupt(root: Path, sid: str, pid: str) -> dict:
    """Retire an orphaned stage/decision without writing or inventing operator consent."""
    path = _path(root, sid, pid)
    fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        rec = _read(path)
        if rec["status"] in ("awaiting_approval", "deciding"):
            rec.update(
                status="interrupted",
                reason="the proposal was interrupted; inspect the "
                "file before requesting another edit",
            )
            for key in ("base", "content", "checkpoint"):
                rec.pop(key, None)
            _write(path, rec)
        return summary(rec)
    finally:
        os.close(fd)


def decide(
    root: Path,
    sid: str,
    tid: str,
    pid: str,
    engine_id: str,
    decision: str,
    user: str,
    *,
    provider,
) -> tuple[dict, dict | None]:
    """Return decision + one in-memory resume checkpoint. Duplicate calls never save/resume."""
    path = _path(root, sid, pid)
    fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        rec = _read(path)
        if rec.get("turn_id") != tid or rec.get("engine_id") != engine_id:
            raise FsError("no such proposal in this turn", status=404)
        if rec["status"] == "deciding":
            rec.update(
                status="interrupted",
                reason="the save was interrupted; inspect the file "
                "before requesting another edit",
            )
            rec.pop("base", None)
            rec.pop("content", None)
            rec.pop("checkpoint", None)
            _write(path, rec)
            return rec, None
        if rec["status"] != "awaiting_approval":
            return rec, None
        if decision not in ("approve", "reject"):
            raise FsError("decision must be approve or reject", status=422)
        checkpoint = rec["checkpoint"]
        rec.update(status="deciding", decided_at=time.time(), decided_by=user)
        _write(path, rec)  # admission to the mutation is durable, even across process failure
        if decision == "reject":
            rec.update(status="rejected", reason="the operator rejected this change")
        else:
            try:
                from .engines import registry
                from .plugins import admission as plugin_admission

                def admit_provider() -> None:
                    if provider.engine_id != engine_id or not registry.admits(provider):
                        raise FsError("the agent changed or was removed", status=409)

                admit_provider()
                if not chat_config.edits_enabled(engine_id) or binding(engine_id) != rec["binding"]:
                    raise FsError("the endpoint or its tool policy changed", status=409)
                folder, verified, data = _file(rec["cwd"], rec["path"])
                if folder != rec["root"] or verified != rec["target"]:
                    raise FsError("the file's location changed", status=409)
                if hashlib.sha256(data).hexdigest() != rec["base_sha256"]:
                    raise FsError("the file changed after the agent read it", status=409)

                def admit(target: str) -> None:
                    admit_provider()
                    admission(rec["cwd"], folder, target)
                    if (
                        not chat_config.edits_enabled(engine_id)
                        or binding(engine_id) != rec["binding"]
                    ):
                        raise FsError("the endpoint or its tool policy changed", status=409)

                # Order the actual mutation/settlement with managed disable/replacement.
                # Live checks alone would still leave a final check-to-settlement window.
                with plugin_admission.acquire(provider) as guard:
                    if guard.reason:
                        raise FsError(guard.reason, status=409)
                    saved = fileedit.save(
                        verified, rec["content"], rec["base_sha256"], root=folder, admit=admit
                    )
                rec.update(status="approved", new_sha256=saved["version"], reason=None)
            except FsError as e:
                rec.update(status="refused", reason=str(e))
            except Exception:
                rec.update(
                    status="interrupted",
                    reason="the save could not be confirmed; "
                    "inspect the file before requesting another edit",
                )
        # The checkpoint never retains previous read bodies. The proposed content occurs only
        # in the model's call arguments; omit it before resuming or persisting an audit.
        for msg in checkpoint["messages"]:
            for call in msg.get("tool_calls", []):
                if call.get("function", {}).get("name") == "propose_edit":
                    call["function"]["arguments"] = json.dumps(
                        {
                            "path": rec["path"],
                            "base_sha256": rec["base_sha256"],
                            "content": "[proposal content no longer retained]",
                        }
                    )
        checkpoint["messages"].append(
            {
                "role": "tool",
                "tool_call_id": rec["call_id"],
                "content": json.dumps(
                    {"path": rec["path"], "outcome": rec["status"], "reason": rec.get("reason")}
                ),
            }
        )
        checkpoint["binding"] = rec["binding"]
        for key in ("base", "content", "checkpoint"):
            rec.pop(key, None)
        _write(path, rec)
        return rec, checkpoint
    finally:
        os.close(fd)
