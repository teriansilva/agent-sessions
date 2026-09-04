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

from . import engines, missions, session_input


def roster_key(mission_id: str) -> str:
    """The lock name for "this mission's set of sessions" (#900 review 6, finding 1).

    Not a session key and deliberately shaped so it can never collide with one: `engines.parse_key`
    would refuse it. `session_input`'s epoch registry is keyed by string and does not care, which
    is what lets a roster be a lockable thing without inventing a second lock domain for it.
    """
    return f"mission-roster:{mission_id}"


def held_keys(mission_id: str, *, path=None) -> list[str]:
    """The mission's ACTIVE sessions as PHYSICAL keys. Raises rather than answering partially.

    Physical, not app-facing: the write fence is keyed on the pty, which is what `session_input`
    bumps and compares.
    """
    return [
        engines.physical_key(str(k)) for k in missions.active_session_keys(mission_id, path=path)
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
