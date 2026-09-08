"""Resolve dispatches that outlived the process that started them (#904 review 2).

`dispatching` is a promise a running process makes, and a process can be killed. The plan row is
consumed by `claim_plan`, so without a durable record there is nothing on disk afterwards that
says a launch was ever attempted — leaving a mission `dispatching` for ever, possibly beside a
live unattended agent nobody owns. `mission_dispatches` is that record, written in the
claim's own transaction and stamped with the session key the instant it is minted and BEFORE the
master exists.

That ordering is what makes this pass possible, and it is deliberately one-sided: the record may
name a session that never came to be, and may never miss one that did.

Three crash points, three different answers:

* **before the key was minted** (`session_key IS NULL`) — nothing was spawned. The mission is
  failed with a reason that says so, and there is nothing to clean up.
* **after the key, and the engine's own store has a record for it** — a session exists. It is
  ADOPTED, so the operator can reach it, and the mission is failed rather than run: the third
  fact — that the brief was delivered and acked — is not recoverable from disk, and `running` on
  a session that may be sitting on a consent screen is exactly the false report #898 refused to
  make. `failed` with the session attached is the honest shape; `reopen` is one tap.
* **after the key, and the store has nothing** — the launch got as far as an id and no further,
  or produced a process with no agent behind it. Whatever runtime footprint exists is torn down
  and the mission is failed.

…and none of it happens at all for a dispatch whose OWNER is still running. The record carries a
lease (`pid:starttime`), and a pass acts only on one whose owner is provably gone: this runs as an
un-awaited startup task, so without it a request that was mid-launch in this instance — or in a
sibling over the same store — was snapshotted as crashed and torn down (#904 review 2, finding 3).

## A FOURTH kind of row, which is not a crash at all (#904 review 4, finding 1)

A row whose mission has ALREADY SETTLED is a **retained cleanup obligation**. An ordinary
settlement deletes the record, so a row sitting beside a `failed` or `abandoned` mission exists
only because a teardown could not prove the boundary empty — `cleanup_runtime` answered `leaked`,
or raised — and somebody asked to keep it (`settle_dispatch(..., keep_record=True)`).

Selecting only `dispatching` rows therefore skipped exactly the rows that matter most: every path
that retains a record moves the mission to `failed`/`abandoned` FIRST, so every later pass ignored
the only durable trace of a possibly-live unattended agent, for ever. The obligation outlives the
mission, so the selection must not be filtered on the mission's lifecycle.

Such a row needs no lifecycle decision and no owner-lease check — the dispatch is over and there
is nothing left to race. It needs one thing repeated until it succeeds: stop the session. The row
is dropped only when the teardown PROVES the boundary empty, which is what turns "we tried" into
"there is nothing there".

**A store that cannot be read is not an answer.** Every probe here is wrapped, and a mission whose
evidence could not be gathered is left alone for the next startup rather than settled on a guess:
`unknown` is not `absent`, which is the same rule the objective probes and the relay reconciler
follow.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging

from . import headless_dispatch, mission_dispatch, missions

log = logging.getLogger(__name__)


async def _has_session(key: str, cwd: str) -> bool | None:
    """Does the ENGINE'S OWN STORE know this session? `None` means we could not look.

    Through `store_record_state`, which is where the third answer actually survives (#904 review
    2, finding 4). The boolean helper collapses an unreadable store into `False`, so this
    function's careful `None` could never be produced by the code it called — only by the test
    that patched it. A transiently locked provider store therefore read as "that session never
    existed", and a possibly live agent was torn down on the strength of it.
    """
    try:
        from . import engines

        prov, native = engines.parse_key(key)
        state = await asyncio.to_thread(headless_dispatch.store_record_state, prov, native)
    except Exception:  # noqa: BLE001
        log.debug("could not read the engine store for %s", key, exc_info=True)
        return None
    if state == "unreadable":
        return None
    return state == "found"


async def _stop(key: str) -> str:
    """Tear the session down, and say WHICH of the three happened (#904 reviews 3 and 13).

    `cleanup_runtime` answers `leaked` when something in the session's process group survived
    SIGKILL. Discarding that — as this did — settled the mission and deleted the durable record
    of an unattended agent that is very possibly still running, leaving nothing for anyone to
    find. `leaked` keeps the record so a later pass can try again.

    `spared` is the third, and it is not `stopped`: the session is RUNNING and another mission
    has adopted it. It discharges this obligation, because somebody now answers for the agent —
    but nothing was stopped, and saying so was a false line in the operator's log.

    **"Nobody owns it" and the kill are MUTUALLY EXCLUSIVE with adoption** (#904 reviews 5 and 6,
    finding 1). Three answers were tried and only the third is a guarantee:

    1. read the owner before calling — a snapshot, and an adoption can commit after it;
    2. `spare_if`, re-asked before each signal — much narrower, and still a window: the predicate
       runs, then the cgroup and the process tree are read from `/proc`, and only then does the
       signal go out. An adoption inside THAT is enough to kill the new owner's agent;
    3. hold the session's OWN fence across the whole teardown, which is the lock adoption itself
       takes (`routes/missions.adopt`). Then there is no window, because the two cannot interleave
       at all.

    The lock and the work go onto one worker thread together — the idiom `_fenced_write` uses,
    and for the same reason: holding a `threading.Lock` across an `await` on the event loop is
    the #888 deadlock. `cleanup_runtime` is a coroutine, so the worker runs it on its own loop;
    nothing it touches is bound to the caller's.

    A fence that is BUSY leaves the obligation in place rather than proceeding: not being able to
    take the lock means not being able to prove the session is unowned, and this is the direction
    where being wrong kills somebody else's agent. `spare_if` stays as well — belt and braces
    inside a boundary that already excludes the race.
    """
    from . import mission_fence

    # ONE DOOR, AND ONE ANSWER (#904 reviews 9 and 13). This pass was the first to get the fence
    # right and the request-time teardowns were left on the older, narrower guard — so the same
    # race survived in two other files. It then kept its own copy of the wrapper, which collapsed
    # `spared` and `stopped` into one boolean and logged "stopped the retained dispatch session"
    # about an agent another mission had adopted and was still running. The rule, and the word for
    # what happened, both live in one place now.
    return await mission_fence.abandon(key)


async def recover_once(*, path=None) -> int:
    """Settle every `dispatching` mission this process did not start. Returns how many moved.

    Never raises: it runs at startup, and an unreadable store must not stop the app from coming
    up. A mission it cannot resolve stays `dispatching` and is looked at again next time, which is
    the honest answer for a dispatch whose fate is genuinely unknown.
    """
    try:
        rows = missions.unsettled_dispatches(path=path)
    except Exception:  # noqa: BLE001
        log.warning("dispatch recovery could not read the store", exc_info=True)
        return 0

    moved = 0
    for row in rows:
        mission_id = row["mission_id"]
        # THE ROW'S OWN GENERATION (#904 review 15). This pass acts on a snapshot, and between
        # reading it and writing, the mission can legally retreat to `planned`, be re-planned and
        # re-claimed — which overwrites this very row. Every write below therefore names the plan
        # it read, so a superseded pass discharges nothing and settles nothing.
        row_plan = str(row.get("plan_id") or "") or None
        if str(row.get("state") or "") != "dispatching":
            # A RETAINED CLEANUP OBLIGATION (#904 review 4, finding 1). An ordinary settlement
            # deletes the record, so a row beside an already-settled mission exists only because
            # a teardown could not prove the boundary empty. The mission needs nothing more said
            # about it; the SESSION does — it may still be an unattended agent nobody has
            # accounted for, and this row is the only thing that remembers it.
            #
            # …UNLESS SOMEBODY OWNS IT. `running` is not `dispatching` either, and there is a real
            # window where a dispatch has SUCCEEDED — `settle_dispatch(to="running")` committed,
            # `clear_dispatch()` has not run yet — in which a lifecycle-only test would call this
            # a retained obligation and stop the live agent the mission had just started. A crash
            # inside that window leaves the same row behind for good.
            #
            # The question is membership, not lifecycle: an orphan is a session NOBODY owns —
            # not one this mission has stopped owning (#904 review 5, finding 1). A failed
            # dispatch can retain its row without holding the session, and once a second mission
            # legitimately adopts that key, tearing it down trades an orphan for somebody else's
            # live agent.
            key = row.get("session_key")
            if row.get("held"):
                with contextlib.suppress(Exception):
                    if missions.clear_dispatch(mission_id, expect_plan=row_plan, path=path):
                        moved += 1
                        log.info(
                            "discharged the stale dispatch record of mission %s: %s has an owner",
                            mission_id,
                            key,
                        )
                continue
            if not key:
                # Nothing to stop, so nothing is owed. Drop it rather than carrying a row that
                # names no session for ever.
                #
                # AND ITS RESERVATION GOES IN THE SAME COMMIT (#894 review 6, finding 2).
                #
                # This was two calls: `discharge_unlaunched_spawn`, its exceptions suppressed, and
                # then `clear_dispatch`. A transient failure of the first with the second
                # succeeding — SQLite lock contention is enough — deleted the record while leaving
                # a `reserved` row nothing can reach: the reaper skips keyless rows by design, and
                # there was no dispatch left for a later pass to repair. Repeating it exhausted the
                # cap with nothing ever having launched.
                #
                # `clear_dispatch` now owns that reconciliation for every shape, so one call does
                # both or neither. `stopped=True` is correct and safe here precisely BECAUSE no key
                # was minted: a row that never named a session cannot have a process behind it.
                with contextlib.suppress(Exception):
                    missions.clear_dispatch(
                        mission_id, expect_plan=row_plan, stopped=True, path=path
                    )
                continue
            outcome = await _stop(str(key))
            if outcome == "leaked":
                # Still not provably empty. Leave the row for the next pass — an obligation that
                # is dropped on a failed attempt is an obligation nobody ever discharges.
                log.warning("the retained dispatch session %s still cannot be stopped", key)
                continue
            with contextlib.suppress(Exception):
                # PROVED STOPPED DISCHARGES THE SLOT (#894 review 4, finding 2). This branch is
                # the retained-cleanup path — a failed child whose parent went back to running —
                # and it cleared the dispatch without ever saying so, deleting the only recovery
                # record while the reservation stayed `launching` and unreachable.
                missions.clear_dispatch(
                    mission_id,
                    expect_plan=row_plan,
                    stopped=(outcome == "stopped"),
                    path=path,
                )
                # …and a SPARED one keeps its charge but becomes reapable — in the SAME commit,
                # since `clear_dispatch` now owns that transition (review 5).
                moved += 1
                # THE WORD FOR WHAT ACTUALLY HAPPENED (#904 review 13). `spared` discharges this
                # obligation because another mission now answers for the session — not because
                # anything was stopped, which is what this line used to claim of it.
                log.info(
                    "discharged the retained dispatch session %s: %s",
                    key,
                    "stopped" if outcome == "stopped" else "another mission owns it",
                )
            continue
        # WHOSE DISPATCH IS THIS? (#904 review 2, finding 3.)
        #
        # `dispatching` says a launch is somewhere between claimed and settled; it does not say
        # whether anyone is still doing it. This pass runs as an un-awaited startup task, so a
        # request that is actively launching — in this instance, or in a sibling over the same
        # store — was snapshotted as crashed and torn down. A lease makes the question
        # answerable, and only a provably dead owner is recovered: alive, or "cannot tell", both
        # mean leave it, because acting on a live dispatch is the harmful direction.
        if missions.owner_is_live(row.get("owner")) is not False:
            continue
        key = row["session_key"]
        clean = True
        # DEFAULT: NOT PROVED STOPPED (#894 review 7).
        #
        # This defaulted to True, which was harmless only while the moved-state early return did
        # not reconcile anything. Round six made that return reconcile — correctly — and the
        # default then became a claim: the `present` branch below schedules an ADOPTION and stops
        # nothing, so a mission moving out of `dispatching` mid-lookup took the early return,
        # refused the adoption, and discharged a live child's slot on the strength of a default
        # nobody had set. The child had no owner, no recovery record and no teardown.
        #
        # **Session presence is not proof of termination.** Only a branch that establishes a stop
        # may say so, and each one below now does it explicitly.
        resource_stopped = False
        if not key:
            # PRE-SPAWN. The record exists because the claim committed; the key does not, because
            # nothing was ever minted. Nothing ran — and this is the one branch where "stopped" is
            # true by construction rather than by observation: there is no process to stop.
            detail = "the app stopped before the agent was started"
            adopt = None
            resource_stopped = True
        else:
            present = await _has_session(str(key), str(row["cwd"]))
            if present is None:
                continue  # we could not look; that is not a verdict
            if present:
                detail = (
                    f"the app stopped while {key} was being dispatched; the session started but "
                    "there is no record that it received its brief"
                )
                adopt = str(key)
            else:
                # THE TEARDOWN'S OWN WORD, all the way to the operator's timeline (#904 review
                # 14). `_stop` reports three outcomes and this reduced them to two, then wrote
                # "which never started" over all of them. `spared` means another mission adopted
                # the session between this pass's store probe and the fence — so it EXISTS and is
                # RUNNING under that mission, which is the opposite of what the event said, and
                # the record was discharged with that false line as its only durable trace.
                outcome = await _stop(str(key))
                if outcome == "spared":
                    detail = (
                        f"the app stopped during the launch of {key}; another mission has since "
                        "adopted that session, which is running under it"
                    )
                elif outcome == "leaked":
                    detail = (
                        f"the app stopped during the launch of {key}, and it could not be "
                        "proved stopped — something may still be running"
                    )
                else:
                    detail = f"the app stopped during the launch of {key}, which never started"
                clean = outcome != "leaked"
                # THE DISPATCH RECORD AND THE RESOURCE SLOT ARE DIFFERENT OBLIGATIONS (#894
                # review 3, finding 3). `clean` says somebody answers for the agent, which
                # `spared` satisfies — another mission adopted it. It does NOT say the process
                # stopped, and `spared` means precisely that it did not: it is still on this host,
                # under a new owner. Discharging the record while freeing the slot let the
                # originating mission spawn again beside a child it no longer knew about.
                resource_stopped = outcome == "stopped"
                adopt = None
        try:
            # THROUGH THE ADOPTION FENCE, like every other settlement that adopts (#904 review 8,
            # finding 1). This one is a recovery pass, so it is the pass most likely to be running
            # beside a `_stop()` for the very same key — its own, one row later — and beside a
            # sibling instance's live dispatch. Going straight to the store here would leave the
            # fence covering the paths that don't adopt and skipping the two that do.
            #
            # On a worker thread: `_lock` held across an await on this loop is the #888 deadlock.
            verdict = await asyncio.to_thread(
                functools.partial(
                    mission_dispatch.fenced_settle,
                    mission_id,
                    to="failed",
                    detail=detail,
                    session_key=adopt,
                    # THE OBLIGATION OUTLIVES THE MISSION. A teardown that could not prove the
                    # boundary empty leaves the record in place, so the next pass looks again
                    # rather than the only trace of a possibly-live agent being deleted with it.
                    keep_record=not clean,
                    # SPARED DISCHARGES THE RECORD BUT NOT THE SLOT — the process is still
                    # running, under another mission (review 3, finding 3).
                    discharge_resource=resource_stopped,
                    # …AND IT SETTLES THE ATTEMPT IT READ, not whatever is dispatching now.
                    expect_plan=row_plan,
                    path=path,
                )
            )
        except Exception:  # noqa: BLE001
            log.warning("dispatch recovery could not settle %s", mission_id, exc_info=True)
            continue
        if verdict.get("settled"):
            moved += 1
            log.info("recovered mission %s from dispatching: %s", mission_id, detail)
            continue

        # THE SETTLEMENT REFUSED, AND A CHILD MAY BE RUNNING WITH NOBODY TO OWN IT
        # (#894 review 8).
        #
        # The mission moved out of `dispatching` between this pass's store lookup and its
        # settlement, so the early return refused the adoption and dropped the dispatch. The
        # ACCOUNTING for that child is correct — its reservation stays charged and reapable — but
        # a charge is not a cleanup obligation: the reaper only ever observes death, it never
        # stops an orphan. So a child nobody adopted kept running with its only recovery record
        # gone, and the next pass found nothing to repair.
        #
        # Only three things may end that obligation: a successful adoption (handled above),
        # another verified owner, or a PROVED stop. `spared` and `stopped` both discharge it —
        # somebody answers for the agent either way — and anything else leaves it for the next
        # pass rather than being forgotten here.
        if not adopt:
            continue
        # THE RECORD IS STILL THERE, AND THAT IS THE FIX (#894 review 9). The settlement used to
        # delete it on this exit, so the teardown below ran with its own retry record already
        # gone: a `leaked` result logged "leaving the obligation for the next pass" and left
        # nothing behind for the next pass to find. It now survives the refused adoption, and
        # only the three answers that discharge it may take it away.
        try:
            after = await _stop(str(adopt))
        except Exception:  # noqa: BLE001
            log.warning(
                "mission %s: recovery could not reconcile the unowned session %s; the dispatch "
                "record is kept so the next pass retries",
                mission_id,
                adopt,
                exc_info=True,
            )
            continue
        if after == "leaked":
            log.warning(
                "mission %s: %s was not adopted and could not be proved stopped; keeping the "
                "dispatch record so the next pass retries the teardown",
                mission_id,
                adopt,
            )
            continue
        # PROVED, so the obligation ends — and `stopped` and `spared` end DIFFERENT parts of it.
        # `spared` means another mission holds that agent: the record goes because somebody
        # answers for it, but the process is still on this host, so the slot stays charged and
        # becomes reapable instead of being handed straight back. One transaction, so a crash
        # cannot separate the deletion from the resource transition.
        #
        # `expect_plan` keeps a slow pass from clearing a NEWER attempt's record: by the time we
        # get here the mission has already moved once, and it may have moved into a new dispatch.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(
                functools.partial(
                    missions.clear_dispatch,
                    mission_id,
                    expect_plan=row_plan,
                    stopped=(after == "stopped"),
                    path=path,
                )
            )
        log.info(
            "mission %s: %s was not adopted after recovery and is now %s",
            mission_id,
            adopt,
            after,
        )
        moved += 1
    return moved
