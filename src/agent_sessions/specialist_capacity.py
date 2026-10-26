"""Specialist capacity (#1693 S4): the pool's limit, and reconciling reservations with the sessions
that hold them.

* **The pool limit** is the ``pool_max_instances`` pref (1…32, default 6): how many specialist
  instances may run at once, across the whole pool. `specialists_store.reserve` checks it in the
  same transaction as the specialist's own capacity.
* **A reservation is released only when its session is proven dead**, asked per session kind:
  a terminal session through the single-writer ``flock`` its dtach master holds (handed over with
  ``pass_fds``, so it outlives an app restart): held → live, acquired by the probe → dead; an API
  session through `structured_runtime.probe`: containment ``gone`` → dead, ``live`` → live.
  Anything else — a probe that fails, an unrecognised key, a kind with no probe — is **unknown**,
  and an unknown reservation is HELD (with its reason) and asked again next pass: releasing it
  could double-book a running agent. `session_input.is_live` is never used: it sees only this
  process's terminal writers.
* **A `starting` reservation** belongs to a launch still under way (its session lock may not be
  taken yet) and is left alone for `specialists_store.STARTING_GRACE_S` since it was last touched,
  while the process that took it — this one or a peer — is alive. Once that process is gone the
  launch can't finish, so it is asked at once. One found live is marked live.
* **Every settlement applies to the state it observed** (a compare-and-set on the row's ``rev``,
  bumped by EVERY change — re-take, activate, live, hold, release; never a clock, which can
  repeat): a newer answer or launch about the same attempt always wins over an older one.
* A release takes the reservation's skill bundle with it (`release`), and every pass sweeps
  orphan bundles — each only when older than the grace by its own times AND a fresh lookup finds
  no active reservation for it (a launch may publish its bundle at any moment).
* **Capacity is enforced where a specialist launch reserves**; a later relaunch of the same
  session re-reserves at its own seam (S4b).

Env kill-switch ``AGENT_SESSIONS_SPECIALIST_LOOP=0`` keeps the loop from running (tests).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from . import prefs
from . import specialists_store as store

log = logging.getLogger("agent_sessions.specialist_capacity")

POOL_KEY = "pool_max_instances"
DEFAULT_MAX_INSTANCES = 6
MIN_MAX_INSTANCES, MAX_MAX_INSTANCES = 1, 32
INTERVAL_S = 60.0
_BACKOFF_MAX_MULT = 8

LIVE, DEAD, UNKNOWN = "live", "dead", "unknown"


# ---- the pool limit -----------------------------------------------------------------------------


def _valid(raw: object) -> bool:
    return (
        isinstance(raw, int)
        and not isinstance(raw, bool)
        and MIN_MAX_INSTANCES <= raw <= MAX_MAX_INSTANCES
    )


def max_instances() -> int:
    raw = prefs.read_block(POOL_KEY)
    return raw if _valid(raw) else DEFAULT_MAX_INSTANCES


def validate_max_instances(raw: object) -> str | None:
    if not _valid(raw):
        return f"{POOL_KEY} must be a whole number from {MIN_MAX_INSTANCES} to {MAX_MAX_INSTANCES}"
    return None


def set_max_instances(raw: int) -> int:
    """Lowering it stops nothing that runs: new reservations wait until the pool is under it."""
    err = validate_max_instances(raw)
    if err is not None:
        raise ValueError(err)
    return prefs.mutate_block(POOL_KEY, lambda _old: raw)


# ---- liveness, per session kind -----------------------------------------------------------------


async def liveness(session_key: str) -> tuple[str, str]:
    """``(LIVE | DEAD | UNKNOWN, reason)`` for the session a reservation holds."""
    from . import engines

    try:
        # A `new-<uuid>` placeholder is a real key here: codex, opencode and others run their
        # master, its lock and its socket under it for the session's whole life.
        prov, native = engines.parse_key(session_key, allow_new_placeholder=True)
    except Exception:  # noqa: BLE001 — an unrecognisable key proves nothing
        return UNKNOWN, "unrecognised session key"
    manifest = getattr(prov, "manifest", None)
    runtime = getattr(manifest, "runtime", None)
    if runtime == "api":
        from . import structured_runtime

        try:
            answer = await structured_runtime.probe(f"{prov.engine_id}:{native}")
        except Exception as exc:  # noqa: BLE001 — a probe that fails is unknown, never dead
            return UNKNOWN, f"the session's probe failed: {exc}"[:200]
        containment = answer.get("containment") if isinstance(answer, dict) else None
        if containment == "gone":
            return DEAD, "its worker is gone"
        if containment == "live":
            return LIVE, ""
        return UNKNOWN, f"the session's state is {containment or 'unreadable'}"
    if runtime == "pty":
        from . import sessionlock

        # The lock lives under the PHYSICAL key: a reconciled real id maps back to the placeholder
        # its master was launched under.
        lock_key = engines.physical_key(f"{prov.engine_id}:{native}")
        try:
            held = await asyncio.to_thread(sessionlock.is_locked, lock_key)
        except OSError as exc:
            return UNKNOWN, f"its session lock can't be read: {exc}"[:200]
        return (LIVE, "") if held else (DEAD, "its terminal is gone")
    return UNKNOWN, "no liveness probe for this kind of session"


# ---- release and reconcile ----------------------------------------------------------------------


def _drop_bundle(reservation_id: str) -> None:
    from . import skill_bundles

    try:
        skill_bundles.remove(reservation_id)
    except Exception:  # noqa: BLE001 — the slot is free either way; the sweep retries the bundle
        log.warning("specialist bundle %s could not be removed", reservation_id, exc_info=True)


def release(reservation_id: str, *, owner: str) -> bool:
    """A failed launch releases the slot it created, with its skill bundle. ``owner``: the token
    `reserve` gave it — an attempt that got an existing reservation has none, and releases
    nothing."""
    released = store.release(reservation_id, owner=owner)
    if released:
        _drop_bundle(reservation_id)
    return released


def release_held(reservation_id: str, *, seen_rev: int) -> bool:
    """The operator frees the HELD attempt they saw (reconcile could not prove it free), with its
    bundle — never a new launch that re-took the slot since."""
    released = store.release_held(reservation_id, seen_rev=seen_rev)
    if released:
        _drop_bundle(reservation_id)
    return released


def _sweep_bundles(now: float) -> int:
    """Remove skill bundles no reservation holds (a removal that failed earlier, or one a crash
    left). Returns how many went.

    No snapshot decides a deletion — a launch may reserve and publish its bundle at any moment:
    a bundle is swept only when (1) it is older than `STARTING_GRACE_S` by its own times (a bundle
    is built only AFTER its reservation row exists, so a fresh one always belongs to a launch
    under way), and (2) a fresh lookup right before deleting finds no active reservation for it.
    An old bundle's reservation id predates the bundle, so no new launch can claim it later."""
    from . import skill_bundles

    root = skill_bundles.root()
    if not root.is_dir():
        return 0
    gone = 0
    for path in root.iterdir():
        name = path.name
        if name.startswith("."):
            continue
        try:
            st = path.lstat()
        except OSError:
            continue
        if now - max(st.st_mtime, st.st_ctime) < store.STARTING_GRACE_S:
            continue  # built moments ago: its launch is under way
        try:
            held = store.get_reservation(name)["state"] != "released"
        except store.NotFound:
            held = False
        except Exception:  # noqa: BLE001 — unsure is not orphaned; asked again next pass
            log.warning("specialist bundle %s: its reservation can't be read", name, exc_info=True)
            continue
        if held:
            continue
        try:
            if skill_bundles.remove(name):
                gone += 1
        except Exception:  # noqa: BLE001 — retried next pass
            log.warning("orphan specialist bundle %s could not be removed", name, exc_info=True)
    return gone


async def _settle(r: dict, ts: float, counts: dict) -> None:
    touched = r["updated_at"]
    young = ts - touched < store.STARTING_GRACE_S
    # A launch under way is left alone unless the process that took the slot is PROVEN gone —
    # this one or a peer, alive or unreadable — since its session lock may not be taken yet.
    # Once that process is proven gone, its launch can't finish, so it is asked at once.
    if r["state"] == "starting" and young and store.process_state(r["process"]) != store.GONE:
        counts["skipped"] += 1
        return
    verdict, reason = await liveness(r["session_key"])
    rid = r["reservation_id"]
    # Every settlement applies only to the state this pass read (the row's `rev`, never a clock):
    # a re-take, a launch going live or a newer answer since each supersede this one.
    rev = r["rev"]
    if verdict == DEAD:
        if await asyncio.to_thread(store.reclaim, rid, seen_rev=rev):
            await asyncio.to_thread(_drop_bundle, rid)
            counts["released"] += 1
    elif verdict == LIVE:
        await asyncio.to_thread(store.mark_live, rid, seen_rev=rev)
        counts["live"] += 1
    else:
        await asyncio.to_thread(store.hold, rid, reason, seen_rev=rev)
        counts["held"] += 1


async def reconcile(*, now: float | None = None) -> dict:
    """One pass over every active reservation; each is settled on its own, so one that fails
    never stops the others."""
    ts = time.time() if now is None else now
    counts = {"released": 0, "held": 0, "live": 0, "skipped": 0, "failed": 0, "bundles": 0}
    rows = await asyncio.to_thread(store.active)
    for r in rows:
        try:
            await _settle(r, ts, counts)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — one reservation never stops the pass
            counts["failed"] += 1
            log.exception("specialist reservation %s could not be settled", r["reservation_id"])
    counts["bundles"] = await asyncio.to_thread(_sweep_bundles, ts)
    return counts


def loop_enabled() -> bool:
    return (os.environ.get("AGENT_SESSIONS_SPECIALIST_LOOP", "1") or "1") != "0"


async def run() -> None:
    """Reconcile at boot, then every `INTERVAL_S` (backing off after a crashed pass)."""
    if not loop_enabled():
        log.info("specialist capacity loop disabled (AGENT_SESSIONS_SPECIALIST_LOOP=0)")
        return
    failures = 0
    while True:
        try:
            counts = await reconcile()
            if counts["released"] or counts["held"] or counts["failed"]:
                log.info("specialist reservations: %s", counts)
            failures = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            failures += 1
            log.exception("specialist capacity pass crashed")
        await asyncio.sleep(INTERVAL_S * min(2**failures, _BACKOFF_MAX_MULT))
