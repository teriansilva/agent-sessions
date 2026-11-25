"""Cross-instance session ownership, anchored in the runtime dir (#293).

Single-active-viewer take-over (#293) needs an owner record that stays correct
even when **two app processes attach the SAME dtach masters**. On a shared host
prod and staging share ``AGENT_SESSIONS_RUNTIME_DIR`` (same ``…/pty`` dir, so
the same ``.sock`` files), which means an in-process owner table (the #184
``Claim``) can't arbitrate between them: each process would believe it holds the
session and drive the agent's pty to a different width → the cross-instance
garble. So the authoritative owner lives in a file next to the session's dtach
socket — ``<runtime>/<engine>-<sid>.owner`` (JSON) — and read-modify-write is
serialised across processes by an ``flock`` on a sibling ``.owner.lock``.

Ownership model (matches the #184 lease semantics, now cross-process):
- A connection is identified by ``(fp, tab_id)`` (device/tab) plus a unique
  per-connection ``conn_id``.
- The active viewer **heartbeats** ``last_seen``; a holder whose lease has
  expired is treated as *gone* — its ghost never blocks a new viewer.
- A **live** holder is never auto-stolen: a *different* device that attaches
  while the holder is fresh lands ``passive`` and must explicitly take over
  (``force=True`` — the "Take over" button). This is the same-device-reconnect
  vs. new-device distinction the #293 gate is built on.
- ``release`` and ``heartbeat`` are ``conn_id``-guarded, so a forced take-over
  that already replaced the owner is never clobbered by the displaced holder's
  clean-up (Hermes: "release clears only the matching active connection").

All disk work is synchronous and tiny (a few hundred bytes under a briefly-held
flock); the async wrappers run it in the default executor so a contended flock
never blocks the event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import time
import uuid
from pathlib import Path

from . import ptybridge

# An owner is "stale" (its device is presumed gone) if it hasn't heartbeat
# within this window. Mirrors session_stream._CLAIM_LEASE_S — the budget a live
# viewer owes for keeping its lease warm. A fresh holder is never auto-stolen.
LEASE_S = 5.0

# Identifies which app process wrote a record (prod vs staging). Display/debug
# only — arbitration is by conn_id, not instance.
INSTANCE = os.environ.get("AGENT_SESSIONS_INSTANCE") or "default"


def takeover_enabled() -> bool:
    """Gate for the single-active-viewer + take-over model (#293). Default OFF, so
    merging Phase 1 to prod is a behavioural no-op — sessions attach exactly as the
    #184 path does today. Flipped on for staging to evaluate the take-over flow.
    Read live (not cached) so a deploy that sets it takes effect on the next attach.
    """
    return os.environ.get("AGENT_SESSIONS_TAKEOVER", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def new_conn_id() -> str:
    """A fresh per-connection id. One WS attach == one conn_id."""
    return uuid.uuid4().hex


def _paths(engine: str, sid: str) -> tuple[Path, Path]:
    """``(owner_file, lock_file)`` next to the session's dtach socket.

    Derived from ``ptybridge.socket_path`` (which validates + sanitises the ids
    and ensures the runtime dir exists) by replacing the ``.sock`` tail — NOT via
    ``Path.with_suffix`` (a session id may legitimately contain a dot, which
    ``with_suffix`` would eat).
    """
    sock = ptybridge.socket_path(engine, sid)
    stem = sock.name[: -len(".sock")]
    return sock.parent / f"{stem}.owner", sock.parent / f"{stem}.owner.lock"


@contextlib.contextmanager
def _flock(lock_path: Path):
    """Hold an exclusive flock for the read-modify-write critical section. The
    lock file is created 0600 and never unlinked (unlinking races the flock)."""
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read(owner_path: Path) -> dict | None:
    """Parse the owner record, or ``None`` if absent/corrupt. Best-effort: a
    truncated/garbage file reads as "no owner" rather than raising."""
    try:
        with open(owner_path, encoding="utf-8") as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) else None
    except (OSError, ValueError):
        return None


def _write_atomic(owner_path: Path, rec: dict) -> None:
    """Write the record via a temp + ``os.replace`` so a reader never sees a
    half-written file. The temp is per-pid to avoid two processes colliding on
    the same temp name (they're already serialised by the flock, but be safe)."""
    tmp = owner_path.with_name(f"{owner_path.name}.tmp.{os.getpid()}")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rec, f)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    os.replace(tmp, owner_path)


def _is_stale(rec: dict, now: float) -> bool:
    return (now - float(rec.get("last_seen", 0.0))) > LEASE_S


# ---- synchronous core (run under the flock) ---------------------------------


def _claim_sync(
    engine: str,
    sid: str,
    *,
    conn_id: str,
    fp: str,
    tab_id: str,
    label: str,
    force: bool,
) -> tuple[str, dict | None]:
    """Compare-and-set the owner record. Returns one of:

    - ``("owner", displaced_or_None)`` — the caller now holds the session. If a
      *different* live holder was forcibly displaced, its record is returned so
      the caller can fire a same-instance demotion signal (cross-instance
      demotion is detected by the holder's own ``owns`` poll).
    - ``("passive", holder)`` — a *different* live holder owns it and ``force``
      was not set; ``holder`` is its record (for the gate display).
    """
    owner_p, lock_p = _paths(engine, sid)
    now = time.time()
    with _flock(lock_p):
        cur = _read(owner_p)
        same_device = cur is not None and cur.get("fp") == fp and cur.get("tab_id") == tab_id
        live_other = cur is not None and not same_device and not _is_stale(cur, now)
        if live_other and not force:
            return ("passive", cur)
        displaced = cur if (cur is not None and not same_device) else None
        rec = {
            "instance": INSTANCE,
            "conn_id": conn_id,
            "fp": fp,
            "tab_id": tab_id,
            "label": label,
            # Preserve the original "since" across a same-device reconnect so the
            # gate's "active since …" reflects continuous possession.
            "since": float(cur["since"]) if same_device and cur and "since" in cur else now,
            "last_seen": now,
        }
        _write_atomic(owner_p, rec)
        return ("owner", displaced)


def _heartbeat_sync(engine: str, sid: str, conn_id: str) -> bool:
    """Bump ``last_seen`` iff we still own the record. Returns ``False`` if we've
    been taken over (the caller should transition itself to the gate)."""
    owner_p, lock_p = _paths(engine, sid)
    now = time.time()
    with _flock(lock_p):
        cur = _read(owner_p)
        if cur is None or cur.get("conn_id") != conn_id:
            return False
        cur["last_seen"] = now
        _write_atomic(owner_p, cur)
        return True


def _release_sync(engine: str, sid: str, conn_id: str) -> bool:
    """Clear the record iff it still names ``conn_id``. A forced take-over has
    already replaced it, so the displaced holder's release is a no-op."""
    owner_p, lock_p = _paths(engine, sid)
    with _flock(lock_p):
        cur = _read(owner_p)
        if cur is not None and cur.get("conn_id") == conn_id:
            with contextlib.suppress(OSError):
                owner_p.unlink()
            return True
        return False


def clear_owner(engine: str, sid: str) -> bool:
    """Force-remove the owner record for a session, regardless of which ``conn_id`` holds it.

    Used by the manual restart path (#331): once the dtach master has been killed the old owner
    lease is meaningless, so it is cleared unconditionally so the resumed session's first viewer
    claims cleanly (rather than landing ``passive`` behind a ghost holder whose lease has not yet
    aged out). Unlike ``_release_sync`` this is NOT ``conn_id``-guarded — restart is an explicit,
    privileged teardown. The sibling ``.owner.lock`` is left in place (never unlinked — that races
    the flock; see ``_flock``). Returns ``True`` iff a record was present and removed.
    """
    owner_p, lock_p = _paths(engine, sid)
    with _flock(lock_p):
        try:
            owner_p.unlink()
            return True
        except OSError:
            return False


# ---- read paths (no lock — a stale read is acceptable for display/poll) -----


def read_owner(engine: str, sid: str) -> dict | None:
    """The current owner record (or ``None``). Unlocked: callers use it for the
    gate display and the demotion poll, where a one-tick-stale read is fine."""
    owner_p, _ = _paths(engine, sid)
    return _read(owner_p)


def owns(engine: str, sid: str, conn_id: str) -> bool:
    """True iff the on-disk record still names ``conn_id`` — the cross-process
    "am I still the active viewer?" check the WS bridge polls."""
    cur = read_owner(engine, sid)
    return cur is not None and cur.get("conn_id") == conn_id


# ---- async wrappers ---------------------------------------------------------


async def claim(
    engine: str,
    sid: str,
    *,
    conn_id: str,
    fp: str,
    tab_id: str,
    label: str = "",
    force: bool = False,
) -> tuple[str, dict | None]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: _claim_sync(
            engine, sid, conn_id=conn_id, fp=fp, tab_id=tab_id, label=label, force=force
        ),
    )


async def heartbeat(engine: str, sid: str, conn_id: str) -> bool:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: _heartbeat_sync(engine, sid, conn_id))


async def release(engine: str, sid: str, conn_id: str) -> bool:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: _release_sync(engine, sid, conn_id))
