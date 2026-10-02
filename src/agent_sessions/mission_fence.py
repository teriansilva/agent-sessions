"""The mission write fence: one protocol, every authority withdrawal (#900 review 7, finding 1).

A mission changes authority in several places — a terminal transition, a detach, an objective
edit, a stand-down, a question opening, a question being answered — and every one of them has the
same requirement: it must not commit between the write fence's final comparison and `os.write()`,
or the supervisor types into a session whose authority has just been withdrawn.

Three things make that hold, and **all three or none**:

1. **Enumerate FAIL CLOSED.** A fence that cannot read the sessions it is meant to lock does not
   become a fence by locking nothing. The earlier answer path mapped a store read failure to `[]`
   on the reasoning that a fence should not block the operator — which is the same wrong trade
   that was made and reverted in `_authority_fence`: a lock over an empty set is not a weaker
   guarantee, it is the absence of one.
2. **Lock the ROSTER as well as the sessions.** Locking the sessions we know about cannot order
   us against an ADOPTION, because the session being adopted is by definition not in that set.
   Both sides take a pseudo-key naming the roster, so adopt and every withdrawal serialize in one
   lock domain instead of checking in one and writing in another.
3. **Re-read INSIDE the lock and compare.** The set was enumerated before the lock, so a session
   adopted in between is one this transaction is not holding. Compared rather than re-locked:
   taking more locks from inside a held set is a lock-order problem, and a refusal here is
   retried by the caller with the new set.

The lock and the store write go into ONE callable that runs off the loop. Holding a
`threading.Lock` across an `await` on the event loop is the deadlock #888's review reproduced:
request A takes the lock, awaits, and its continuation is queued behind request B, which is
blocked on the same lock on the loop thread.
"""

from __future__ import annotations

import logging

from . import engines, missions, session_input

log = logging.getLogger(__name__)


def roster_key(mission_id: str) -> str:
    """The lock name for "this mission's set of sessions" (#900 review 6, finding 1).

    Not a session key and deliberately shaped so it can never collide with one: `engines.parse_key`
    would refuse it. `session_input`'s epoch registry is keyed by string and does not care, which
    is what lets a roster be a lockable thing without inventing a second lock domain for it.
    """
    return f"mission-roster:{mission_id}"


def physical_of(session_key: str, *, path=None) -> str:
    """The PHYSICAL key a session's runtime lives under, from the store's mapping first (#989).

    A session a mission adopted after binding a late id keeps its master under the placeholder, and
    the store records that mapping in the same transaction as the adoption. The alias in the
    metadata sidecar is published afterwards, so between the commit and the publication — or after
    a publication that failed — `engines.physical_key` would answer the logical key and a fence
    would lock a key no runtime lives under. Raises when the store cannot be read: a fence that
    guesses its key is not a fence.
    """
    stored = missions.physical_key_of(session_key, path=path)
    return stored or engines.physical_key(session_key)


def held_keys(mission_id: str, *, path=None) -> list[str]:
    """The mission's ACTIVE sessions as PHYSICAL keys. Raises rather than answering partially.

    Physical, not app-facing: the write fence is keyed on the pty, which is what `session_input`
    bumps and compares. The stored mapping wins over the alias for the reason `physical_of` gives.

    The roster is read through `missions.active_session_keys` — the one enumeration every fence
    fails closed on — and each key is then resolved on its own. A roster is a handful of sessions,
    so the per-key lookup costs nothing, and a failure in either read still raises.
    """
    return [
        physical_of(str(k), path=path) for k in missions.active_session_keys(mission_id, path=path)
    ]


async def fenced_write(mission_id: str, fn, *, path=None):
    """Run `fn` under the mission's write fence. See the module note for what that means.

    Raises `MissionError` — never a bare `AuthorityFenceBusy` — so every caller has one contract:

    * **503** when the roster cannot be enumerated, or when the shared fence is HELD. Both are
      ordinary contention on a busy install, both mean the write did NOT happen, and both are
      retryable. `AuthorityFenceBusy` escaping instead (#900 review 8, finding 2) reached routes
      that catch only `MissionError` and became a 500 — an internal failure reported for a safe
      refusal, on the one path where the operator's next move is simply "try again". `adopt` had
      already been given this translation route-side; putting it in the shared helper is what
      stops the next caller inheriting the old contract.
    * **409** when the roster moved between the enumeration and the lock.
    """
    try:
        keys = await missions.run_admitted(lambda: held_keys(mission_id, path=path))
    except Exception as e:  # noqa: BLE001
        raise missions.MissionError(
            "the mission's sessions could not be read, so this could not be ordered against "
            "them; try again",
            status=503,
        ) from e

    def _run():
        try:
            with session_input.sessions_transaction([roster_key(mission_id), *keys]):
                # RE-READ INSIDE THE LOCK, AND STILL FAIL CLOSED. A read that throws here is a
                # fence that cannot prove what it is holding, which is the same answer as one
                # that could not enumerate at all: refuse, retryably.
                try:
                    now = set(held_keys(mission_id, path=path))
                except Exception as e:  # noqa: BLE001
                    raise missions.MissionError(
                        "the mission's sessions could not be re-read inside the fence; "
                        "try again",
                        status=503,
                    ) from e
                if now != set(keys):
                    raise missions.MissionError(
                        "the mission's sessions changed while this was being written; "
                        "read it again",
                        status=409,
                    )
                return fn()
        except session_input.AuthorityFenceBusy as e:
            # HELD, not broken. The write did not happen and the next move is simply to try
            # again — which is a 503, and was an unhandled 500 until this translation existed.
            raise missions.MissionError("the authorization fence is busy; retry", status=503) from e

    return await missions.run_admitted(_run)


def adoption_keys(
    mission_id: str, session_key: str, *, physical_key: str | None = None
) -> list[str]:
    """The lock set a DURABLE ADOPTION takes: the roster, and the session joining or leaving it.

    One protocol, and it has to be one for the reason rule 2 above gives (#904 review 9, finding
    1). Locking only the physical session orders an adoption against a teardown of that session
    and against nothing else — a question enumerating the roster can take its own roster fence,
    decide what it is writing about, and have a dispatch add a session outside that lock. Locking
    only the roster orders it against the question and leaves the teardown race open. Every
    session-bearing settlement therefore takes both, exactly as `routes/missions.adopt` does.

    **`physical_key` names the lock for the FIRST adoption of a late-bound session** (#989). That
    settlement is the transaction that writes the store's mapping, and the alias is published only
    after it — so at the moment the lock is chosen neither exists, and resolving the real key would
    lock the real key while the runtime (and every teardown of it) lives under the placeholder. The
    caller passes the placeholder it recorded before the spawn. Every later adoption reads the
    committed mapping through `physical_of`.
    """
    return [roster_key(mission_id), physical_key or physical_of(session_key)]


async def fenced_teardown(key: str, *, spare_if=None) -> str:
    """Stop a session UNDER ITS OWN FENCE, and only while nobody owns it (#904 review 9, f. 2).

    The one door for "this session is an orphan; tear it down". It exists because the answer was
    got wrong three times in the same way, each time on a different call site:

    1. read the owner, then call `cleanup_runtime` — a snapshot, and an adoption commits after it;
    2. pass `spare_if` so the predicate is re-asked before each signal — much narrower, and still
       a window: the predicate runs, then the cgroup and the process tree are read from `/proc`,
       and only then does the signal go out. An adoption inside THAT kills the new owner's agent;
    3. hold the session's own fence across the check AND the teardown, which is the lock an
       adoption itself takes. Then the two cannot interleave at all.

    The recovery pass was fixed to (3) and the REQUEST-TIME paths were left on (2) — so the same
    race survived in `mission_dispatch._abandon_session` and `headless_dispatch._abandon`, where
    an exact-head probe reproduced `['guard', 'adopt', 'signal']`. There is one implementation now
    because three copies of a rule this subtle is how the third one gets missed.

    **Unreadable ownership counts as OWNED.** The cost of being wrong here is another mission's
    live agent, so a store that cannot answer must not license a kill.

    The lock and the work go onto ONE worker thread: holding a `threading.Lock` across an `await`
    on the event loop is #888, and `cleanup_runtime` is a coroutine, so the worker runs it on a
    loop of its own. Nothing it touches is bound to the caller's.

    Raises `AuthorityFenceBusy` when the fence is held — not being able to take it means not being
    able to prove the session is unowned, and that is the direction where being wrong kills
    somebody else's agent. Callers treat it as "not proved empty".
    """
    import asyncio

    from . import runtime_cleanup

    # THE RUNTIME PARSER, not the public one (#989). A late-id launch that failed before it bound
    # its real id leaves a master keyed on `<engine>:new-<uuid>`, and stopping it must not need an
    # id that never existed. `parse_runtime_key` is the one place that shape is accepted, and this
    # teardown is its only caller besides `runtime_cleanup`'s own resolution.
    prov, _native = engines.parse_runtime_key(key)
    # THE STORE'S MAPPING, not the alias: between an adoption's commit and the alias publication,
    # the real key resolves to itself and nothing lives there. Raises on an unreadable store,
    # which `abandon` reports as `leaked` — a teardown that cannot name its target proved nothing.
    phys = physical_of(key)
    _eng, _, phys_native = phys.partition(":")

    def _unowned() -> bool:
        try:
            if missions.holder_of(key) is not None:
                return False
        except Exception:  # noqa: BLE001 — unprovable ownership must not authorise a kill
            return False
        return True if spare_if is None else spare_if()

    def _fenced() -> str:
        with session_input.sessions_transaction([phys]):
            if not _unowned():
                return "spared"
            # `spare_if` stays as well — belt and braces inside a boundary that already excludes
            # the race, and the only thing that still speaks for a caller-specific reason to stop.
            # THE BINDING IS NOT RETIRED HERE, and a proved stop is not permission to (#994
            # review 5). `cleanup_runtime` deliberately leaves the socket alone when the physical
            # key's launch lock is HELD — a NEW generation owns that path — and still returns the
            # OLD master's result, so `stopped` can describe a boundary a replacement already took.
            # Readers that resolved this mapping before the teardown began sit outside this
            # transaction too, so no check made here can speak for them either.
            # The mapping is therefore append-only, exactly like the alias it projects
            # (`metadata.set_alias` has no deleter). Retiring one safely needs a generation-safe
            # protocol, which is #1017.
            return asyncio.run(
                runtime_cleanup.cleanup_runtime(prov.engine_id, phys_native, spare_if=_unowned)
            )

    return await asyncio.to_thread(_fenced)


async def abandon(key: str) -> str:
    """`fenced_teardown` that never raises, and SAYS WHICH of the three things happened (#904 r13).

    Two callers had their own copy of this wrapper and both collapsed the answer to a boolean —
    "was the boundary made safe" — which is true of `stopped` and of `spared` alike. It is not the
    same fact. `spared` means the session is STILL RUNNING and another mission has adopted it, so
    the obligation is discharged because somebody now answers for the agent, NOT because the agent
    is gone. Reported as a stop, it told the operator a live agent had been killed and hid the key
    they would need to go and look at it.

    So there is one wrapper, and it returns the outcome rather than a verdict about it:

    * ``stopped`` — the process boundary is provably empty.
    * ``spared``  — somebody owns it; it is running and accounted for.
    * ``leaked``  — it survived SIGKILL, or the attempt raised. Nothing was proved, so a durable
      record must SURVIVE: it is the only trace of an unattended agent nobody has stopped.

    Never raised: the mission has already been settled by somebody else, and an exception here
    would replace "a stray agent we tried to stop" with "a stray agent we tried to stop and a 500".
    """
    try:
        outcome = await fenced_teardown(key)
    except Exception:  # noqa: BLE001
        log.warning("could not stop the orphaned dispatch session %s", key, exc_info=True)
        return "leaked"
    if outcome == "spared":
        log.info("left %s alone: a mission owns it and is answering for it", key)
        return "spared"
    if outcome == "leaked":
        log.error(
            "the dispatch session %s survived SIGKILL; an unattended agent may still be running",
            key,
        )
        return "leaked"
    return "stopped"
