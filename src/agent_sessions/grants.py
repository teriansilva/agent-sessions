"""Equipment grants: what one agent process was issued, and its revocation (#1951 slice B, D11/D15).

A **grant set** is one row per agent process (an API session's generation) started from a
specialist, plus one row per item it was issued: the project's equipment as the variables library
offers it (`mission_approval.equipment_of`: secrets and secret files with agent access on, and the
project's memory scope), never a value. It lives in the reservation's own store
(`specialists_store`), so a grant can never exist without the reservation that settles it.

* **Issued after the reservation** (`issue`, the order at launch: re-check → reservation → skill
  bundle → **grants** → the session starts). A reservation refused at capacity leaves zero grant
  rows; a set is written at exactly the reservation ``rev`` the launch took, so a slot a sweep or
  a re-take changed since issues nothing and the launch is refused. Every item lands or none does.
* **Narrowest grant.** Only the equipment of the project the session resolves to (the broker's
  own resolution): never another project's, never a global item the project does not bind. A step
  run bound to a plan approval gets at most what that approval pinned for its project: an item
  added since is not issued (the launch boundary already refuses an equipment change; this is the
  belt to that brace).
* **Revoked as a whole, with the reservation** (D-P6-2). The reservation's release (`release`,
  `reclaim`, `release_held`), a re-take and a renewed generation revoke its sets IN THE SAME
  TRANSACTION (`specialists_store`). Then the copies BattleLab materialized for it are deleted
  (`after_release` → `secrets_broker.revoke_grant_sets`); the broker asks the set on every call, so
  it refuses the session the moment the set is marked, before any copy is gone. The archive
  teardown revokes a session's sets before its footprint is reaped (`revoke_session`).
* **The boot sweep** (`sweep`, D-P6-3) completes before mission, step-run and dispatch recovery
  and before the automation scheduler fires (`ensure_swept`; an operator's "Run now" is a new
  reservation and a new set, never a pre-boot one). It revokes every open set whose reservation
  is not proven live, under reconcile's own fence: the reservation's ``rev`` it read (a
  compare-and-set), and the `STARTING_GRACE_S` rule. **Unknown liveness is revoked, not held**
  (least privilege). A session that may still run without its access raises ONE decision
  (`lost_access`).
* **`audit`** is the one invariant check, used per path by the tests and as their tripwire.

Workflow §0: revocation deletes only copies BattleLab itself materialized for that session.
Read `docs/invariants/mission-approval.md` (Grants) before changing this module.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time

from . import specialists_store as store

log = logging.getLogger(__name__)

#: How long the boot sweep waits for one session's liveness answer before calling it unknown.
PROBE_TIMEOUT_S = 10.0
#: How many sets the boot sweep probes at once, and its whole budget: a probe still unanswered
#: past the deadline is unknown (and so revoked), so a boot never waits on a host that does not
#: answer, however many sets are open (independent review of #2020).
SWEEP_CONCURRENCY = 8
SWEEP_DEADLINE_S = 30.0
#: A sweep settles a `starting` reservation of a live process only past this (reconcile's rule).
STARTING_GRACE_S = store.STARTING_GRACE_S


class GrantRefused(Exception):
    """No equipment could be issued, so the launch is refused (nothing runs)."""


# ---- issuing ------------------------------------------------------------------------------------


def _project_of(session_key: str, cwd: str) -> str | None:
    from . import secrets_broker

    return secrets_broker._project_for(session_key, cwd)


def _approved_pin(session_key: str, project_id: str) -> tuple[str | None, list[dict] | None]:
    """``(approval_id, pinned items)`` when ``session_key`` is a mission step run bound to a plan
    approval; ``(None, None)`` otherwise. An approval no longer the one in force pins nothing
    (fail closed: an empty pin)."""
    from . import missions

    try:
        run = missions.step_run_of_session(session_key)
    except Exception:  # noqa: BLE001 — unreadable: treat as bound, with nothing pinned
        return None, []
    if run is None or run.get("authority") == "operator" or not run.get("approval_id"):
        return None, None
    approval_id = run["approval_id"]
    try:
        approval = missions.approval(run["mission_id"])
    except Exception:  # noqa: BLE001
        return approval_id, []
    if approval is None or approval.get("id") != approval_id or approval.get("revoked_at"):
        return approval_id, []
    return approval_id, list((approval.get("pins") or {}).get("equipment", {}).get(project_id, []))


def _key(item: dict) -> tuple:
    return (item.get("kind"), item.get("id"), item.get("name"), item.get("scope"))


def items_for(session_key: str, cwd: str) -> tuple[str | None, str | None, list[dict]]:
    """``(project_id, approval_id, items)`` a new agent process of ``session_key`` may be issued:
    its project's equipment now, narrowed to an approval's pin when it is bound to one. Nothing
    outside a project; an unreadable library issues nothing (fail closed). Blocking."""
    from . import mission_approval

    pid = _project_of(session_key, cwd)
    if pid is None:
        return None, None, []
    now = mission_approval.equipment_of(pid)
    if now is None:
        log.warning("grants: %s's equipment could not be read; nothing is issued", pid)
        now = []
    approval_id, pinned = _approved_pin(session_key, pid)
    if pinned is not None:
        allowed = {_key(i) for i in pinned}
        now = [i for i in now if _key(i) in allowed]
    return pid, approval_id, now


def issue(claim, session_key: str, cwd: str) -> dict:
    """Issue the grant set for one agent process holding ``claim`` (a `specialist_launch.Claim`),
    after its reservation. Returns the set (`specialists_store.grant_set`). Raises
    `GrantRefused`: the caller refuses the launch, and its `abandon` releases the slot (and with it
    anything issued). Blocking: call it where the launch already runs off the loop."""
    if claim.rev is None:
        raise GrantRefused("this launch's specialist slot has no revision to issue under")
    try:
        pid, approval_id, items = items_for(session_key, cwd)
        set_id = store.issue_grants(
            claim.reservation_id,
            rev=claim.rev,
            session_key=claim.session_key,
            project_id=pid,
            approval_id=approval_id,
            items=items,
        )
    except store.GrantRefused as exc:
        raise GrantRefused(str(exc)) from None
    except store.StoreError as exc:
        raise GrantRefused(f"equipment could not be issued: {exc}") from None
    try:
        out = store.grant_set(set_id)
    except Exception:  # noqa: BLE001 — handled below, like a set that is not there
        out = None
    if out is None:
        # Written, but the launch can't carry it: the launch is refused, and the set ends here
        # (a re-take's slot release could not reach it).
        revoke_set(set_id, "released")
        raise GrantRefused("equipment could not be issued: the grant set can't be read back")
    return out


def project_of(session_key: str, cwd: str) -> str | None:
    """The project ``session_key`` resolves to NOW (the broker's and memory's resolution)."""
    return _project_of(session_key, cwd)


def granted_memory(grant: dict | None, project_now: str | None) -> bool:
    """Was the memory scope of ``project_now`` (the project the session resolves to now, which
    memory binds) issued in ``grant``? A session moved to another project since its set was issued
    gets none. A launch with no set: not asked."""
    if grant is None:
        return True
    if project_now is None:
        return False
    return any(i["kind"] == "memory" and i["id"] == project_now for i in grant["items"])


# ---- revoking -----------------------------------------------------------------------------------


def _delete_copies(set_ids: list[str]) -> None:
    from . import secrets_broker

    if set_ids:
        secrets_broker.revoke_grant_sets(list(set_ids))  # one fence, one connection


def after_release(reservation_id: str) -> None:
    """The reservation was released and its sets revoked in that transaction: delete the copies
    BattleLab materialized for them. Idempotent; the broker's pass finishes what a failure left.
    Blocking."""
    try:
        sets = store.revoked_set_ids(reservation_id)  # ids only; the broker touches what is left
    except Exception:  # noqa: BLE001 — the broker's pass revokes what the ledger revoked
        log.warning("grants: %s's sets could not be read", reservation_id, exc_info=True)
        return
    _delete_copies(sets)


def revoke_set(set_id: str, reason: str) -> None:
    """One set this launch was issued ends now (its launch failed before any spawn), with its
    copies. Never raises (the sweep and the broker's pass finish what a failure left).
    Blocking."""
    try:
        store.revoke_grant_set(set_id, reason)
    except Exception:  # noqa: BLE001
        log.warning("grants: revoking %s failed", set_id, exc_info=True)
    _delete_copies([set_id])


def revoke_session(session_keys: list[str], reason: str = "archived") -> list[str]:
    """A session's runtime is being torn down (archive, delete): mark every open set of it revoked,
    then delete its copies. Never raises (a teardown never blocks on it; the sweep retries).
    Blocking."""
    try:
        ids = store.revoke_session_grants(session_keys, reason)
    except Exception:  # noqa: BLE001
        log.warning("grants: revoking %s failed", session_keys, exc_info=True)
        return []
    _delete_copies(ids)
    return ids


# ---- the boot sweep -----------------------------------------------------------------------------

_swept = False
_sweep_lock: asyncio.Lock | None = None


def _budget(deadline: float) -> float:
    return min(PROBE_TIMEOUT_S, deadline - time.monotonic())


async def _liveness(session_key: str, deadline: float) -> str:
    from . import specialist_capacity

    left = _budget(deadline)
    if left <= 0:
        return specialist_capacity.UNKNOWN  # the sweep's budget is spent: unknown, revoked
    try:
        verdict, _why = await asyncio.wait_for(specialist_capacity.liveness(session_key), left)
    except Exception:  # noqa: BLE001 — a probe that fails or times out is unknown
        return specialist_capacity.UNKNOWN
    return verdict


async def _proved_ended(session_key: str, deadline: float) -> bool:
    """Is an API session's newest worker PROVED ended (its own unit and cgroup evidence,
    `structured_runtime.release_ended`)? A worker that idled out or crashed reads `unknown` to the
    probe, which certifies `gone` only for a closed session: this tells it from one that may still
    run, so it is revoked without a decision. Read-only (its release callback does nothing)."""
    from . import structured_runtime

    left = _budget(deadline)
    if left <= 0:
        return False
    try:
        worker = await asyncio.wait_for(
            structured_runtime.release_ended(session_key, lambda _worker: None), left
        )
    except Exception:  # noqa: BLE001 — not a native session, or unreadable: not proved
        return False
    return worker is not None


class _Undecided(Exception):
    """The sweep's compare-and-set lost to a change that left the set open: decide it again."""


#: Beyond the probes' own deadline, what the sweep's store work may add before the sets still
#: being settled are left to the retry (each store call can wait out its busy timeout).
SWEEP_STORE_GRACE_S = 15.0


async def _sweep_set(
    g: dict, ts: float, deadline: float, counts: dict, own: str | None, owed: list
) -> None:
    """One open set (see `sweep`). Raises on a store error: the caller retries this set. A set
    revoked on unknown liveness goes to ``owed``: its decision is raised after every probe, never
    in a probe slot or on the probes' budget."""
    from . import specialist_capacity

    try:
        r = await asyncio.to_thread(store.get_reservation, g["reservation_id"])
    except store.NotFound:
        r = None
    if r is None or r["state"] == "released":
        if await asyncio.to_thread(store.sweep_revoke, g["set_id"], seen_rev=-1, reason="released"):
            counts["revoked"] += 1
            await asyncio.to_thread(_delete_copies, [g["set_id"]])
        return
    if own is not None and r["process"] == own:
        # Taken or renewed by THIS process, so issued since it booted: a launch of this boot,
        # never the boot sweep's (a retried sweep at runtime must not probe a running session).
        counts["skipped"] += 1
        return
    young = ts - r["updated_at"] < STARTING_GRACE_S
    gone = await asyncio.to_thread(store.process_state, r["process"]) == store.GONE
    if r["state"] == "starting" and young and not gone:
        # Left to its own launch for now, but not settled: the retry decides it again, and once
        # its grace is past it is probed like any other (a process in another PID namespace
        # never reads gone, so "skipped" would leave it open for good).
        raise _Undecided(g["set_id"])
    verdict = await _liveness(r["session_key"], deadline)
    if verdict == specialist_capacity.UNKNOWN and await _proved_ended(r["session_key"], deadline):
        verdict = specialist_capacity.DEAD  # idled out or crashed: nothing still runs
    if verdict == specialist_capacity.LIVE:
        counts["kept"] += 1
        return
    reason = "boot_dead" if verdict == specialist_capacity.DEAD else "boot_unknown"
    if not await asyncio.to_thread(
        store.sweep_revoke, g["set_id"], seen_rev=r["rev"], reason=reason
    ):
        # Its reservation moved on since. A renew or a re-take superseded the set (the newer state
        # won, nothing left to do); anything else (going live, a hold) left it open, so it is
        # decided again at its new `rev` by the retry, never assumed kept.
        still = await asyncio.to_thread(store.grant_set, g["set_id"])
        if still is not None and still["revoked_at"] is None:
            raise _Undecided(g["set_id"])
        counts["skipped"] += 1
        return
    counts["revoked"] += 1
    await asyncio.to_thread(_delete_copies, [g["set_id"]])
    if reason == "boot_unknown":
        owed.append(g)


async def _announce(g: dict, counts: dict) -> None:
    """Raise one set's lost-access decision (`_announce_lost_access`) through the missions store's
    admission. A failure leaves it unannounced for a later pass and is NOT a sweep failure: the
    revocation, the security property, is already committed, and a notice that can't be written
    (a mission mid-archive, a busy store) must not hold every recovery hostage."""
    from . import missions

    try:
        if await missions.run_admitted(functools.partial(_announce_lost_access, g)):
            counts["announced"] += 1
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — retried by a later pass, gating nothing
        counts["unannounced"] += 1
        first = g["set_id"] not in _owed_logged
        _owed_logged.add(g["set_id"])
        (log.warning if first else log.debug)(
            "grants: %s's lost-access decision is not raised yet", g["set_id"], exc_info=first
        )


#: Sets whose owed decision was already logged with its traceback (once each, not every pass).
_owed_logged: set[str] = set()


async def sweep(
    *, now: float | None = None, only: set[str] | None = None, spare_own: bool = False
) -> dict:
    """Revoke every open grant set whose reservation is not proven live (#1951 D-P6-3).

    Reconcile's own fence: a set whose reservation is released or missing is revoked; a
    `starting` reservation touched within `STARTING_GRACE_S` by a process not proven gone is left
    to its own launch; any other is probed (`specialist_capacity.liveness`, `SWEEP_CONCURRENCY` at
    a time within `SWEEP_DEADLINE_S`) and revoked unless it answers LIVE, by a compare-and-set on
    the reservation's ``rev`` the sweep read, so a re-take, a renewed generation or a launch going
    live since always wins. A set revoked while its session may still run raises one decision;
    one whose decision could not be raised yet is raised now.

    ``only``: consider just these set ids (a retry of what failed). ``spare_own``: leave every
    set whose reservation THIS process holds (issued since it booted). Returns the counts, with
    ``retry``: the set ids that failed, or None when the ledger itself could not be read. Never
    raises."""
    ts = time.time() if now is None else now
    counts: dict = {
        "kept": 0,
        "revoked": 0,
        "skipped": 0,
        "announced": 0,
        "unannounced": 0,
        "failed": 0,
    }
    try:
        # `only` empty: owed decisions alone (no open set is read, nothing is probed).
        sets = [] if only == set() else await asyncio.to_thread(store.grant_sets, open_only=True)
        unannounced = await asyncio.to_thread(store.grant_sets, unannounced_reason="boot_unknown")
    except store.StoreUnsupported:
        # A NEWER schema than this build (a rollback without `specialists-schema rollback`): this
        # build can't read a grant from it, so the broker serves none (`store_unavailable`) and no
        # slot can be reserved. Nothing pre-boot can be served: vacuously settled, said loudly.
        log.error("grants: the specialists store is newer than this build; no grant is served")
        counts["retry"] = []
        return counts
    except Exception:  # noqa: BLE001
        log.warning("grants: the boot sweep could not read the ledger", exc_info=True)
        counts["failed"] += 1
        counts["retry"] = None
        return counts
    if only is not None:
        sets = [g for g in sets if g["set_id"] in only]  # decisions still owed: every one
    own = store._process() if spare_own else None
    deadline = time.monotonic() + SWEEP_DEADLINE_S
    fresh_owed: list[dict] = []
    gate = asyncio.Semaphore(SWEEP_CONCURRENCY)
    retry: list[str] = []

    done: set[str] = set()

    def failed(g: dict) -> None:
        counts["failed"] += 1
        retry.append(g["set_id"])
        log.warning("grants: sweeping %s failed", g.get("set_id"), exc_info=True)

    async def one(g: dict) -> None:
        async with gate:
            try:
                await _sweep_set(g, ts, deadline, counts, own, fresh_owed)
            except asyncio.CancelledError:
                raise
            except _Undecided:
                counts["undecided"] += 1
                retry.append(g["set_id"])
            except Exception:  # noqa: BLE001 — one set never stops the sweep; retried
                failed(g)
            done.add(g["set_id"])

    counts["undecided"] = 0
    try:
        await asyncio.wait_for(
            asyncio.gather(*(one(g) for g in sets)), SWEEP_DEADLINE_S + SWEEP_STORE_GRACE_S
        )
    except TimeoutError:
        # Store work outlasted the budget: whatever is still unsettled is the retry's.
        late = [g for g in sets if g["set_id"] not in done]
        log.warning("grants: the boot sweep ran out of time with %d sets unsettled", len(late))
        for g in late:
            counts["failed"] += 1
            retry.append(g["set_id"])
    # The decisions this pass owes (revoked above on unknown liveness) and those an earlier pass
    # could not raise (no probe: the sets are revoked), within the same budget; what is left over
    # is owed to a later pass.
    budget_end = deadline + SWEEP_STORE_GRACE_S
    seen_owed = {g["set_id"] for g in fresh_owed}
    for g in fresh_owed + [u for u in unannounced if u["set_id"] not in seen_owed]:
        left = budget_end - time.monotonic()
        if left <= 0:
            counts["unannounced"] += 1
            continue
        try:
            # Each write is bounded by what is left of the budget, not only started within it: a
            # writer blocked on the mission store must not hold the readiness path. Cancelling
            # the await is safe: `run_admitted` leaves the slot to a worker that already began,
            # and the write is idempotent (a late commit is found and marked next time).
            await asyncio.wait_for(_announce(g, counts), left)
        except TimeoutError:
            counts["unannounced"] += 1  # owed: a later pass raises it
    counts["retry"] = retry
    return counts


#: What the next `ensure_swept` sweeps: None, every set issued before this process (the ledger was
#: never read); else the ids an earlier pass failed on. Never a fresh full sweep at runtime: a set
#: this boot issued is not the boot sweep's, and a transient unknown would revoke a running one.
_retry: set[str] | None = None
#: When the last pass that did not complete ended (monotonic), and how many have not.
_last_pass: float | None = None
_failed_passes = 0
#: Lost-access decisions a pass could not raise yet (retried by later calls; never a gate).
_announce_owed = False
#: Between two passes (`ensure_swept` callers share one; `wait_until_swept` sleeps this long).
SWEEP_RETRY_S = 15.0
#: After this many passes that did not complete, the log says so as an error, once.
SWEEP_PASSES_BEFORE_ERROR = 4


def _still_owed(counts: dict) -> bool:
    """Are lost-access decisions still owed after a pass? Some could not be raised, or the ledger
    could not be read at all (then nothing is known to be settled, so keep asking)."""
    return counts.get("unannounced", 0) > 0 or counts.get("retry", []) is None


async def ensure_swept(*, wait_for_running: bool = True) -> bool:
    """Run the boot sweep (once it completes, never again in this process) and say whether it
    COMPLETED: every set issued before this process is settled. A caller that recovers, resumes,
    relaunches or fires does so only on True (D-P6-3: completion before the first recovery): a
    periodic caller skips its pass, a boot caller waits (`wait_until_swept`). A pass that failed
    is retried by the next call, for what failed only: the sets it could not settle, or (the
    ledger unreadable) the sets issued before this process, never one this process issued since.
    Each pass is bounded (`SWEEP_DEADLINE_S` + `SWEEP_STORE_GRACE_S`)."""
    global _swept, _sweep_lock, _retry, _last_pass, _failed_passes, _announce_owed
    if _swept and not _announce_owed:
        return True
    if _sweep_lock is None:
        _sweep_lock = asyncio.Lock()
    if _swept and _sweep_lock.locked():
        return True  # complete; another caller is raising the owed decisions, never wait on it
    if not wait_for_running and _sweep_lock.locked():
        return False  # a pass is running and the caller would rather defer than wait for it
    async with _sweep_lock:
        if _swept:
            # Complete; only decisions are still owed. Raised again at most once per
            # SWEEP_RETRY_S, and never a reason to say "not complete".
            if _announce_owed and (
                _last_pass is None or time.monotonic() - _last_pass >= SWEEP_RETRY_S
            ):
                counts = await sweep(only=set(), spare_own=True)
                _last_pass = time.monotonic()
                _announce_owed = _still_owed(counts)
            return True
        if _last_pass is not None and time.monotonic() - _last_pass < SWEEP_RETRY_S:
            return False  # a pass just failed: callers share it rather than re-probe at once
        counts = await sweep(only=_retry, spare_own=True)
        _last_pass = time.monotonic()
        _announce_owed = _still_owed(counts)
        retry = counts.pop("retry", None)
        if counts["revoked"] or counts["failed"] or retry:
            log.info("grants: boot sweep %s", counts)
        if not counts["failed"] and not retry:
            _swept = True
            _retry = None
        elif retry:
            _retry = set(retry)
        if not _swept:
            _failed_passes += 1
            if _failed_passes == SWEEP_PASSES_BEFORE_ERROR:
                log.error(
                    "grants: the equipment boot sweep has not completed after %d passes; the "
                    "mission supervisor, automations, approved steps and dispatch recovery wait "
                    "for it (retry: %s)",
                    _failed_passes,
                    sorted(_retry) if _retry is not None else "the whole ledger",
                )
            elif _failed_passes % SWEEP_PASSES_BEFORE_ERROR == 0:  # not every pass, forever
                log.warning("grants: the boot sweep is not complete yet; recovery waits for it")
        return _swept


async def wait_until_swept(*, attempts: int | None = None) -> bool:
    """Wait, attempt after attempt (`SWEEP_RETRY_S` apart, never a spin), until the boot sweep
    completed; at most ``attempts`` times when given (then False: the caller does not proceed).
    For callers that run once at boot and must not start before it (the supervisor, the
    automation scheduler, dispatch recovery). Cancellable."""
    n = 0
    while not await ensure_swept():
        n += 1
        if attempts is not None and n >= attempts:
            return False
        await asyncio.sleep(SWEEP_RETRY_S)
    return True


def swept() -> bool:
    return _swept


def reset_for_test() -> None:
    global _swept, _sweep_lock, _retry, _last_pass, _failed_passes, _announce_owed
    _swept = False
    _sweep_lock = None
    _retry = None
    _last_pass = None
    _failed_passes = 0
    _announce_owed = False
    _owed_logged.clear()


# ---- the one decision ---------------------------------------------------------------------------


def _item_words(items: list[dict]) -> str:
    words = []
    for i in items:
        if i["kind"] == "memory":
            words.append("project memory")
        else:
            words.append(f"{'secret file' if i['kind'] == 'secret_file' else 'secret'} {i['name']}")
    return ", ".join(words) if words else "no equipment"


def lost_access_text(g: dict, step: str | None = None) -> str:
    """The decision's server text (frame 9). Never model text."""
    who = f"step {step}'s session" if step else f"session {g['session_key']}"
    return (
        f"BattleLab restarted and could not prove {who} was still running, so its access was "
        f"revoked: {_item_words(g['items'])}. The session was not stopped. Stop it, or stop it and "
        "start the step again: a new start gets new access after a new reservation, and no turn "
        "is replayed."
    )


def _announce_lost_access(g: dict) -> bool:
    """Raise the ONE decision for a set revoked while its session may still run: a deterministic
    row on its mission's thread (`kind = session`, ``meta.access = "revoked"``), written
    idempotently by grant set in the mission store (`missions.ensure_lost_access_event`), then
    marked raised (`announced_at`). A failure in between leaves the set unannounced, and the next
    sweep finds the row already there and only marks it: exactly one row, never lost. A session no
    mission holds is logged only. Raises on a store error (the sweep retries). Returns True when
    it wrote the row now. Blocking."""
    from . import missions

    if g.get("announced_at") is not None:
        return False
    run = missions.step_run_of_session(g["session_key"])
    wrote = False
    if run is not None:
        try:
            wrote = missions.ensure_lost_access_event(
                run["mission_id"],
                session_key=g["session_key"],
                grant_set=g["set_id"],
                text=lost_access_text(g, run["step_id"]),
                meta={
                    "reason": "boot_unknown",
                    "step": run["step_id"],
                    "episode": int(run["episode"]),
                    "items": [{"kind": i["kind"], "name": i["name"]} for i in g["items"]],
                },
            )
        except missions.MissionError as exc:
            if not isinstance(exc, missions.MissionNotFound) and exc.status != 410:
                raise  # mid-archive / mid-unarchive: transient, the sweep retries it
            # Its mission is gone or archived: nobody to tell, and a retry would never settle.
            log.warning("grants: %s's mission takes no decision (%s)", g["session_key"], exc)
            run = None
    if run is None:
        log.warning("grants: %s lost its access after a restart", g["session_key"])
    store.mark_grant_announced(g["set_id"])
    return wrote


def lost_access(session_keys: list[str]) -> list[dict]:
    """The sessions among ``session_keys`` whose newest grant set the boot sweep revoked while
    they may still run (frame 9's state): ``[{session_key, set_id, revoked_at, items, text}]``.
    A newer set (a relaunch) clears it. Blocking."""
    out = []
    newest: dict[str, dict] = {}
    for g in store.grant_sets(session_keys=session_keys):
        newest[g["session_key"]] = g
    for key, g in newest.items():
        if g["revoked_reason"] == "boot_unknown":
            out.append(
                {
                    "session_key": key,
                    "set_id": g["set_id"],
                    "revoked_at": g["revoked_at"],
                    "items": [{"kind": i["kind"], "name": i["name"]} for i in g["items"]],
                    "text": lost_access_text(g),
                }
            )
    return out


# ---- the audit ----------------------------------------------------------------------------------


def audit() -> list[str]:
    """The one invariant, checked (#1951 Tests: "no grant outlives its session"). Returns every
    violation, as text; empty means it holds:

    * every grant set is revoked unless its reservation is active (``starting`` or ``live``);
    * no capability the broker minted under a revoked set is still unrevoked, and no copy of one
      remains on disk or in the broker's ledger, so the broker refuses that session.

    Shared by every per-path test and the tripwire after the mission suite, so the two can't
    drift. Blocking."""
    from . import secrets_broker

    bad: list[str] = []
    sets = store.grant_sets()
    for g in sets:
        if g["revoked_at"] is not None:
            continue
        try:
            r = store.get_reservation(g["reservation_id"])
        except store.NotFound:
            r = None
        if r is None or r["state"] not in ("starting", "live"):
            bad.append(f"grant set {g['set_id']} is open but its reservation is not active")
    revoked = {g["set_id"] for g in sets if g["revoked_at"] is not None}
    if not revoked or not secrets_broker.db_path().exists():
        return bad
    conn = secrets_broker._connect()
    try:
        for cap in conn.execute("SELECT * FROM caps WHERE grant_set IS NOT NULL").fetchall():
            if cap["grant_set"] not in revoked:
                continue
            if cap["revoked_at"] is None:
                bad.append(f"capability {cap['id']} outlives its revoked grant set")
            if conn.execute("SELECT 1 FROM copies WHERE cap_id = ?", (cap["id"],)).fetchone():
                bad.append(f"capability {cap['id']} still has a copy in the ledger")
            if secrets_broker._launch_dir(cap["id"]).exists():
                bad.append(f"capability {cap['id']} still has its launch directory")
    finally:
        conn.close()
    return bad


__all__ = [
    "GrantRefused",
    "after_release",
    "audit",
    "ensure_swept",
    "wait_until_swept",
    "granted_memory",
    "issue",
    "items_for",
    "lost_access",
    "lost_access_text",
    "revoke_session",
    "sweep",
]
