"""Durable pre-launch context and server-owned reconciliation for ``new-<uuid>`` placeholders.

An ``adopt``-mint engine (``opencode``, ``opencode2``, ``codex``, ``antigravity``, ``kimi``) opens
under a client-minted ``<engine>:new-<uuid>`` placeholder and writes its real conversation record
only when the operator submits a first prompt — which may be more than 5 minutes after launch,
after a WebSocket disconnect/reload, or after an app restart while the ``dtach`` master stays up.

This module owns:
1. **The private pre-launch context file**
   (``<runtime_dir>/placeholders/<engine>_<placeholder>.json``, mode ``0600``, read back through
   ``provenance.open_verified``): stores the validated launch ``cwd``, the pre-launch baseline
   ``snapshot`` (never re-taken on reconnect), ``launched_at``, and ``reconcile_stopped``. On
   ambiguity (≥2 new IDs in ``cwd``) or a permanent alias-publication refusal,
   ``reconcile_stopped`` is set to ``True`` while keeping ``cwd`` on disk for the master's
   lifetime so a later un-aliased ``ATTACH`` still authorizes ``cwd`` against current roots and
   exclusions without re-enabling adoption.
2. **One server-owned reconciliation task and viewer set per physical placeholder key**: WebSocket
   viewers subscribe on ``LAUNCH`` and ``ATTACH`` and unsubscribe on disconnect without cancelling
   the poller while the ``dtach`` master lives.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import privatedir, ptybridge
from .engines import base
from .plugins import provenance

log = logging.getLogger("agent_sessions.placeholder_launch")

MAX_CONTEXT_BYTES = 1024 * 1024
_ENGINE_RE = re.compile(r"\A[a-z][a-z0-9_-]{0,31}\Z")

# In-memory state per physical key (`<engine>:new-<uuid>`).
_TASKS: dict[str, asyncio.Task] = {}
_VIEWERS: dict[str, set] = {}
# Latched in-memory when reconciliation stops (ambiguity / permanent refusal / baseline failure),
# so even if a disk write fails on ENOSPC this process never re-enables adoption.
_STOPPED: set[str] = set()


@dataclass(frozen=True)
class Context:
    engine: str
    placeholder: str
    cwd: str
    snapshot: set[str] | None
    launched_at: float
    reconcile_stopped: bool = False

    @property
    def phys_key(self) -> str:
        return f"{self.engine}:{self.placeholder}"


def _valid_ids(engine: str, placeholder: str) -> bool:
    return bool(
        isinstance(engine, str)
        and _ENGINE_RE.fullmatch(engine)
        and isinstance(placeholder, str)
        and base._NEW_PLACEHOLDER_RE.fullmatch(placeholder)
    )


def _dir(create: bool = False) -> Path | None:
    root = ptybridge.runtime_dir() / "placeholders"
    if not create and not root.exists():
        return None
    try:
        return Path(privatedir.ensure_private_dir(root, mode=0o700))
    except OSError:
        return None


def context_path(engine: str, placeholder: str, *, create_dir: bool = False) -> Path | None:
    if not _valid_ids(engine, placeholder):
        return None
    d = _dir(create=create_dir)
    if d is None:
        return None
    return d / f"{engine}_{placeholder}.json"


def _encode(ctx: Context) -> bytes:
    payload = {
        "version": 1,
        "engine": ctx.engine,
        "placeholder": ctx.placeholder,
        "cwd": ctx.cwd,
        "snapshot": sorted(ctx.snapshot) if ctx.snapshot is not None else None,
        "launched_at": ctx.launched_at,
        "reconcile_stopped": bool(ctx.reconcile_stopped or ctx.snapshot is None),
    }
    return (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")


def _write_verified(path: Path, raw: bytes, *, allow_inplace_fallback: bool = False) -> bool:
    if len(raw) > MAX_CONTEXT_BYTES:
        return False
    parent = path.parent
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(parent))
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, path)
        tmp_path = None
        return True
    except OSError:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
        if not allow_inplace_fallback:
            return False
        # Fallback for `mark_stopped` when disk block allocation fails (e.g. ENOSPC): rewrite the
        # already-allocated verified file in place so a restart sees `reconcile_stopped=True`.
        try:
            ro_fd, st = provenance.open_verified(str(path), canonicalize=False)
            try:
                if (
                    not stat.S_ISREG(st.st_mode)
                    or st.st_uid != os.geteuid()
                    or (st.st_mode & 0o077) != 0
                    or st.st_nlink != 1
                ):
                    return False
                rw_fd = os.open(
                    str(path),
                    os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                )
                try:
                    rw_st = os.fstat(rw_fd)
                    if (rw_st.st_dev, rw_st.st_ino) != (st.st_dev, st.st_ino):
                        return False
                    os.lseek(rw_fd, 0, os.SEEK_SET)
                    written = os.write(rw_fd, raw)
                    os.ftruncate(rw_fd, written)
                    os.fsync(rw_fd)
                    return True
                finally:
                    os.close(rw_fd)
            finally:
                os.close(ro_fd)
        except (OSError, provenance.ProvenanceError):
            # Last resort if in-place rewrite also fails: unlink so a restart fails closed rather
            # than reading a stale `reconcile_stopped=False` snapshot.
            with contextlib.suppress(OSError):
                path.unlink()
            return False


def read_context(engine: str, placeholder: str) -> Context | None:
    """Read and validate the private launch context for ``engine:placeholder``, or ``None``."""
    path = context_path(engine, placeholder, create_dir=False)
    if path is None:
        return None
    try:
        fd, st = provenance.open_verified(str(path), canonicalize=False)
    except (FileNotFoundError, OSError, provenance.ProvenanceError):
        return None
    try:
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.geteuid()
            or (st.st_mode & 0o077) != 0
            or st.st_nlink != 1
            or st.st_size <= 0
            or st.st_size > MAX_CONTEXT_BYTES
        ):
            return None
        with os.fdopen(fd, "rb", closefd=False) as fh:
            raw = fh.read(MAX_CONTEXT_BYTES + 1)
    except OSError:
        return None
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
    if len(raw) > MAX_CONTEXT_BYTES:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != 1:
        return None
    if data.get("engine") != engine or data.get("placeholder") != placeholder:
        return None
    cwd = data.get("cwd")
    if not isinstance(cwd, str) or not cwd.startswith("/") or "\x00" in cwd:
        return None
    raw_snap = data.get("snapshot")
    snapshot: set[str] | None
    if raw_snap is None:
        snapshot = None
    elif isinstance(raw_snap, list) and all(isinstance(x, str) and x for x in raw_snap):
        snapshot = set(raw_snap)
    else:
        return None
    launched_at = data.get("launched_at")
    if not isinstance(launched_at, int | float):
        return None
    stopped = bool(data.get("reconcile_stopped")) or snapshot is None
    phys_key = f"{engine}:{placeholder}"
    if phys_key in _STOPPED:
        stopped = True
    return Context(
        engine=engine,
        placeholder=placeholder,
        cwd=cwd,
        snapshot=snapshot,
        launched_at=float(launched_at),
        reconcile_stopped=stopped,
    )


def record_launch(
    engine: str,
    placeholder: str,
    *,
    cwd: str,
    snapshot: set[str] | None,
    launched_at: float | None = None,
) -> Context | None:
    """Persist the pre-launch context for a ``new=1`` placeholder launch.

    Never overwrites an existing context for the same placeholder (the pre-launch baseline is
    captured once, before the master spawns, and a stopped flag is permanent).
    """
    if not _valid_ids(engine, placeholder) or not isinstance(cwd, str) or not cwd.startswith("/"):
        return None
    phys_key = f"{engine}:{placeholder}"
    existing = read_context(engine, placeholder)
    if existing is not None:
        return existing
    stopped = snapshot is None or phys_key in _STOPPED
    if stopped:
        _STOPPED.add(phys_key)
    ctx = Context(
        engine=engine,
        placeholder=placeholder,
        cwd=cwd,
        snapshot=set(snapshot) if snapshot is not None else None,
        launched_at=float(time.time() if launched_at is None else launched_at),
        reconcile_stopped=stopped,
    )
    path = context_path(engine, placeholder, create_dir=True)
    if path is None:
        return None
    if not _write_verified(path, _encode(ctx)):
        return None
    return ctx


def mark_stopped(engine: str, placeholder: str) -> None:
    """Permanently stop automatic adoption for ``engine:placeholder`` while retaining ``cwd``
    on disk so a running placeholder session remains attachable/reloadable (#2097)."""
    if not _valid_ids(engine, placeholder):
        return
    phys_key = f"{engine}:{placeholder}"
    _STOPPED.add(phys_key)
    ctx = read_context(engine, placeholder)
    if ctx is None:
        return
    updated = Context(
        engine=ctx.engine,
        placeholder=ctx.placeholder,
        cwd=ctx.cwd,
        snapshot=ctx.snapshot,
        launched_at=ctx.launched_at,
        reconcile_stopped=True,
    )
    path = context_path(engine, placeholder, create_dir=False)
    if path is not None:
        _write_verified(path, _encode(updated), allow_inplace_fallback=True)


def is_stopped(phys_key: str) -> bool:
    if phys_key in _STOPPED:
        return True
    engine, sep, placeholder = phys_key.partition(":")
    if not sep:
        return True
    ctx = read_context(engine, placeholder)
    return bool(ctx is not None and ctx.reconcile_stopped)


def remove_context(engine: str, placeholder: str) -> None:
    """Remove the placeholder launch context once adoption succeeded or the master is gone."""
    phys_key = f"{engine}:{placeholder}"
    _STOPPED.discard(phys_key)
    path = context_path(engine, placeholder, create_dir=False)
    if path is not None:
        with contextlib.suppress(OSError):
            path.unlink()


def subscribe(phys_key: str, ws) -> None:
    if ws is None:
        return
    _VIEWERS.setdefault(phys_key, set()).add(ws)


def unsubscribe(phys_key: str, ws) -> None:
    if ws is None:
        return
    viewers = _VIEWERS.get(phys_key)
    if viewers is not None:
        viewers.discard(ws)
        if not viewers:
            _VIEWERS.pop(phys_key, None)


def subscribers(phys_key: str, *, extra=None) -> list:
    out = list(_VIEWERS.get(phys_key, ()))
    if extra is not None and extra not in out:
        out.append(extra)
    return out


def ensure_task(phys_key: str, factory) -> asyncio.Task | None:
    """Return the single running reconciliation task for ``phys_key``, starting one via
    ``factory()`` if none is active and adoption is not stopped."""
    if phys_key in _STOPPED:
        return None
    existing = _TASKS.get(phys_key)
    if existing is not None and not existing.done():
        return existing
    coro = factory()
    task = asyncio.ensure_future(coro)
    _TASKS[phys_key] = task

    def _on_done(t: asyncio.Task) -> None:
        if _TASKS.get(phys_key) is t:
            _TASKS.pop(phys_key, None)
        if not t.cancelled():
            exc = t.exception()
            if exc is not None:
                log.warning("placeholder reconciliation for %s failed: %s", phys_key, exc)

    task.add_done_callback(_on_done)
    return task


async def stop_task(phys_key: str) -> None:
    """Cancel and await the in-memory reconciliation task for ``phys_key`` (if any)."""
    task = _TASKS.pop(phys_key, None)
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def shutdown() -> None:
    """App shutdown: cancel process-local tasks WITHOUT deleting context files on disk so a
    surviving ``dtach`` master recovers its context on the next start."""
    tasks = [t for t in _TASKS.values() if not t.done()]
    _TASKS.clear()
    _VIEWERS.clear()
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
