"""Run a claimed plan, and resolve the mission to a TRUE state whatever happens (#893).

The mission is already `dispatching` when this runs — `claim_plan` moved it there, in the same
transaction that consumed the plan and wrote the durable `mission_dispatches` record, so there is
exactly one winner and there is something on disk that says a launch was attempted. What remains
is to launch and then to say honestly what happened, because `dispatching` is a transient state
and a mission stuck in it is the crash outcome #840 names explicitly.

**Alive is not started, and this is where that rule is enforced at the mission boundary.** #898
already keeps `launched` / `started` / `briefed` apart at the launch boundary, and `Dispatch.ok`
is the conjunction of the last two — the engine's own store knows the session exists AND the brief
reached it. This module refuses to move the mission to `running` on anything less. A dispatched
agent sitting on a trust dialog is alive by every process-level measure and has received nothing;
reporting that as `running` is the worst outcome available here, because the supervisor then
follows through on work that is not happening.

**The adoption and the final state are ONE settlement** (#904 review 3). They were two, and the
gap between them was long enough for the operator to abandon the mission: the terminal transition
releases the roster, the late adopt re-attaches a live unattended agent to a mission that
is already closed, and the `dispatching -> running` CAS then fails while this module reported
`running` anyway. `missions.settle_dispatch` puts both halves under one state predicate, and when
it says the mission is no longer ours, the session it just started is TORN DOWN rather than left
running with nobody owning it.

**Every path ends in a state the operator can act on.** A refused launch, a failed one, a launch
that produced a process and no store record: all of them land in `failed` with the launcher's own
reason on the timeline. The only way to stay `dispatching` is a crash, and the startup recovery
pass in `mission_dispatch_recover` owns that case.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
from typing import NamedTuple

from . import headless_dispatch, missions, session_input

log = logging.getLogger(__name__)


async def _abandon_session(key: str) -> str:
    """Stop a session this mission turned out not to own — and say WHICH of the three happened.

    **The outcome is the point, and a boolean is not it** (#904 reviews 3 and 13). `cleanup_runtime`
    answers `leaked` when something in the session's process group survived SIGKILL, and the fence
    answers `spared` when another mission has adopted the session — so it is running, owned, and
    accounted for. Both of those used to arrive here as `True`/`False` and then be read as
    "stopped"/"not stopped", which made the console tell the operator that a live agent another
    mission is running had been killed, and hid the key they would need to look at it.

    **And it cannot interleave with an ADOPTION at all** (#904 reviews 5 and 9, finding 1/2).
    `spare_if` re-asked before every signal narrowed that window and did not close it — the
    predicate runs, then `/proc` is read, and only then does the signal go out; an exact-head
    probe landed an adoption inside exactly that gap. The recovery pass was moved onto the
    session's own fence and this REQUEST-TIME path was left behind, so the race survived here.

    One door for both: `mission_fence.abandon` holds the fence across the ownership question and
    the teardown together, on a worker thread, and never raises.
    """
    from . import mission_fence

    return await mission_fence.abandon(key)


def fenced_settle(
    mission_id: str,
    *,
    to: str,
    detail: str,
    session_key: str | None = None,
    keep_record: bool = False,
    expect_plan: str | None = None,
    path=None,
) -> dict:
    """Settle a dispatch UNDER THE ADOPTION FENCE when it adopts. Raises what the store raises.

    A settlement that carries a `session_key` IS a durable adoption — the mission takes ownership
    of the session it has just started — and a recovery pass tearing that session down as an
    orphan takes the same lock (#904 reviews 7 and 8, finding 1). Without it the two commits can
    straddle each other and the agent this settlement has just claimed is killed.

    The SUCCESSFUL path is the one that matters and the one that was missed: it called
    `settle_dispatch` directly, so the fence covered every path except the one that actually
    adopts. One function now, and no caller reaches `settle_dispatch` with a key without it.
    """

    def _commit() -> dict:
        return missions.settle_dispatch(
            mission_id,
            to=to,
            detail=detail,
            session_key=session_key,
            keep_record=keep_record,
            expect_plan=expect_plan,
            path=path,
        )

    if not session_key:
        return _commit()
    from . import mission_fence, session_input

    # THE SAME LOCK SET `routes/missions.adopt` TAKES (#904 review 9, finding 1). The session's
    # own key orders this against a teardown; the ROSTER's pseudo-key orders it against a
    # question or a withdrawal that enumerated the mission's sessions a moment ago — and a
    # settlement that adopts is exactly the thing that makes such an enumeration stale. Taking
    # one of the two is not most of the guarantee; it is a different, smaller one.
    with session_input.sessions_transaction(mission_fence.adoption_keys(mission_id, session_key)):
        return _commit()


def fenced_adopt(mission_id: str, key: str, *, role: str = "primary") -> dict:
    """Adopt a session UNDER THE ADOPTION FENCE. The one door for a durable adoption (#904 rev 7).

    A recovery pass deciding this session is an orphan and tearing it down takes the same lock, so
    "nobody owns it" and the kill cannot straddle this commit. Without it, every ownership read on
    the teardown side is a snapshot: it answers "nobody" and an adoption commits before the signal
    lands, killing the agent the new owner has just taken responsibility for.

    **And the ROSTER's pseudo-key with it** (#900 review 6, finding 1). A question locks the
    sessions it knows about, which cannot order it against an adoption — the session being adopted
    is by definition not in that set. Both sides take the roster key, so "the roster is changing"
    and "a question is being committed against it" cannot interleave either. The two fences came
    from different phases and guard the same act, so they are taken together rather than left as
    two doors a later caller has to remember to use both of.

    Synchronous, and callers run it off the loop — holding `_lock` across an await is the #888
    deadlock, and the attach path calls `bump_epoch()` from the loop.
    """
    from . import mission_fence, session_input

    with session_input.sessions_transaction(mission_fence.adoption_keys(mission_id, key)):
        return missions.adopt(mission_id, key, role=role)


async def settle_offloop(
    mission_id: str,
    *,
    to: str,
    detail: str,
    session_key: str | None = None,
    keep_record: bool = False,
    expect_plan: str | None = None,
    path=None,
) -> dict:
    """`fenced_settle` on a WORKER THREAD. The only shape a coroutine may settle in (#904 rev 9).

    `fenced_settle` takes `session_input`'s lock and then writes to the store, and both are
    blocking. Calling it from `run()` — a coroutine on the app's event loop — stalls every other
    request for as long as somebody else holds that lock: measured at 2.0s against a held fence,
    with a 50ms ticker not firing until 2.051s, and unbounded if the holder is.

    The same rule `mission_fence` states for every other fenced write, applied to the one caller
    that was still synchronous.
    """
    return await asyncio.to_thread(
        functools.partial(
            fenced_settle,
            mission_id,
            to=to,
            detail=detail,
            session_key=session_key,
            keep_record=keep_record,
            expect_plan=expect_plan,
            path=path,
        )
    )


async def _settle(
    mission_id: str,
    *,
    to: str,
    detail: str,
    session_key: str | None = None,
    keep_record: bool = False,
    expect_plan: str | None = None,
) -> dict:
    """Move the mission out of `dispatching`, and never leave it there.

    Returns `settle_dispatch`'s verdict. Suppressed on a store failure rather than raised: the
    launch has already happened (or not), and an exception here would replace a wrong state with
    the same wrong state plus a 500. The startup recovery pass is what covers a `dispatching` that
    outlives its request.

    Off the loop, like every other fenced write (#904 review 9, finding 4).
    """

    try:
        return await settle_offloop(
            mission_id,
            to=to,
            detail=detail,
            session_key=session_key,
            keep_record=keep_record,
            expect_plan=expect_plan,
        )
    except missions.MissionError as e:
        log.warning("mission %s could not leave dispatching: %s", mission_id, e)
        return {"settled": False, "state": "dispatching", "adopted": False, "error": str(e)}
    except Exception:  # noqa: BLE001
        log.warning("mission %s could not leave dispatching", mission_id, exc_info=True)
        return {"settled": False, "state": "dispatching", "adopted": False}


class _Reconciled(NamedTuple):
    """What the one reconciliation actually achieved — all three facts, none of them assumed.

    Every caller composes a sentence for the operator out of these, and each of the three was got
    wrong by a different branch before it existed: `state` was reported as the transition the
    caller *attempted* over a mission the store never moved (review 12, finding 1; review 13,
    finding 1), and the teardown was asserted in advance by a sentence written before it ran
    (review 13, finding 2). `record_kept` is the durable consequence of the other two.

    **`outcome` is the teardown's own word, not a verdict about it** (review 13, second round).
    It was a boolean, and `spared` — the session is running and another mission has adopted it —
    collapsed into the same `True` as `stopped`. Discharging the obligation is the ONE thing those
    two share: somebody answers for the agent either way. Everything the operator is TOLD about it
    differs, so the three stay three all the way to the sentence.
    """

    state: str
    outcome: str
    record_kept: bool

    @property
    def stopped(self) -> bool:
        """PROVED EMPTY, and nothing else. `spared` is not a stop (#904 review 13, finding 1)."""
        return self.outcome == "stopped"

    @property
    def held_elsewhere(self) -> bool:
        """Running, and another mission is answering for it."""
        return self.outcome == "spared"


async def _orphaned_after_launch(
    mission_id: str,
    out,
    why: str,
    *,
    cancelled: bool,
    verdict: dict | None = None,
    expect_plan: str | None = None,
) -> _Reconciled:
    """The ONE reconciliation for every post-launch path that does not end in ownership (#904).

    There were five of these and they disagreed, which is how four of them ended up wrong. Reviews
    11 and 12 folded three together; review 13 found the same two mistakes surviving in the two
    siblings that had not been folded in, which is the argument for there being one function:

    * a `MissionError` — a refused adoption — was handled fully;
    * **any other exception** (a locked store, a filesystem error) escaped the frame entirely,
      leaving the mission `dispatching` with THIS process's owner lease on the durable row. That
      is exactly the row startup recovery is right to skip, so a live unattended agent stayed
      unattended for the life of the process;
    * **a cancellation whose worker returned a verdict that did NOT settle** — the mission moved
      out from under it — was read as ownership, because only the absence of an exception was
      checked. Nobody owned the session and nobody tore it down;
    * **an ATTEMPTED launch that failed** — a live process with nothing in the engine's store —
      discharged the durable row on the teardown's answer alone. When the terminal write had also
      failed the mission stayed `dispatching` for ever, with its only recovery row deleted;
    * **a mission that moved while the settlement was in flight** told the operator the session
      "was stopped" whatever the teardown answered, so a `leaked` cleanup was reported as a clean
      one.

    All five are the same fact: an agent is running and this mission does not own it. So the answer
    is the one the refused-adoption path already gave — terminalize, KEEP the record, tear down,
    and discharge the record only when the mission is terminal AND the boundary is PROVED empty.

    **`verdict` is for the caller that already has the store's answer.** The mission-moved sibling
    was told, by `settle_dispatch` itself, what the mission became; asking again would be a second
    write that can fail on a busy fence and turn a known `cancelled` back into an assumed
    `dispatching`. A caller that knows passes it; everybody else leaves it `None` and this settles.

    `cancelled` picks the store call, not the policy. Unwinding a `CancelledError` is the one
    place an `await` cannot be relied on to resume, so the transition goes through the synchronous
    store there and through the ordinary async path everywhere else.
    """
    if verdict is None:
        # THE REASON WRITTEN FIRST CLAIMS NOTHING ABOUT THE TEARDOWN (review 12, finding 2). It
        # used to say the session "was stopped" and only then try to stop it, so a `leaked`
        # cleanup left the timeline telling the operator an unattended agent was gone. The durable
        # event states the INTENT; the one at the bottom states what actually happened.
        pending = f"{why}; the session {out.key} it had started is being stopped"
        if cancelled:
            verdict = {}
            with contextlib.suppress(Exception):
                verdict = (
                    missions.settle_dispatch(
                        mission_id,
                        to="failed",
                        detail=pending,
                        keep_record=True,
                        expect_plan=expect_plan,
                    )
                    or {}
                )
        else:
            verdict = await _settle(
                mission_id,
                to="failed",
                detail=pending,
                keep_record=True,
                expect_plan=expect_plan,
            )
    state = str(verdict.get("state") or "")
    # SETTLED BY US, or already terminal because somebody else got there first. Both mean the
    # mission is no longer `dispatching`; only a failed WRITE leaves it there.
    terminal = bool(verdict.get("settled")) or (state != "" and state != "dispatching")

    # `none` when there was nothing to stop; otherwise `leaked` until a teardown says otherwise,
    # because an attempt that never returned has proved nothing.
    outcome = "none" if not out.launched else "leaked"
    if out.launched:
        with contextlib.suppress(Exception):
            # SHIELDED: on the cancellation path this is the teardown that must not be skipped.
            outcome = await asyncio.shield(asyncio.ensure_future(_abandon_session(out.key)))
    # DISCHARGED means somebody answers for the agent — which `spared` satisfies without the agent
    # being gone. Only `leaked` leaves an obligation, because only `leaked` proved nothing.
    discharged = outcome != "leaked"
    if discharged and terminal:
        with contextlib.suppress(Exception):
            missions.clear_dispatch(mission_id, expect_plan=expect_plan)
    elif not terminal:
        log.error(
            "mission %s could not be moved out of dispatching; keeping the dispatch record so "
            "recovery can still find it",
            mission_id,
        )
    # THE TRUTH ABOUT THE TEARDOWN, once it is known — never asserted in advance, and never
    # rounded to the nearest reassuring one.
    said = {
        "stopped": f"the session {out.key} was stopped",
        "spared": (
            f"the session {out.key} was NOT stopped: another mission adopted it while it was "
            "being torn down, and is running it"
        ),
        "leaked": f"the session {out.key} could NOT be proved stopped; it may still be running",
        "none": "no session had been started, so there was nothing to stop",
    }[outcome]
    with contextlib.suppress(Exception):
        missions.append_event(
            mission_id,
            "session",
            session_key=out.key,
            text=said,
            meta={
                "outcome": outcome,
                "stopped": outcome == "stopped",
                "record_kept": not (discharged and terminal),
            },
        )
    return _Reconciled(state or "dispatching", outcome, not (discharged and terminal))


async def run(
    mission_id: str,
    plan: dict,
    *,
    registry,
    policy_epoch: str | None = None,
    verify_cwd=None,
) -> dict:
    """Launch the plan and settle the mission. Returns what the console renders.

    Never raises for an ordinary failure: a dispatch that could not be attempted and one that was
    attempted and failed are both outcomes the operator needs to see, and an exception here would
    turn either into a 500 with the mission still `dispatching`.

    `policy_epoch` is what the ROUTE observed when it read the master switch. It is compared
    inside the launch fence, immediately before the spawn — see `session_input.launch_fence`.
    """
    engine = str(plan.get("engine") or "")
    cwd = str(plan.get("cwd") or "")
    brief = str(plan.get("brief") or "")
    # THIS ATTEMPT'S IDENTITY, carried into every write it makes (#904 review 15). `mission_id`
    # names the MISSION; it does not name the dispatch, and `dispatching` is a state the mission
    # can re-enter. `plan_id` is minted per plan and is stamped on the claim row, so it is the
    # generation: a write that finds another plan's row belongs to a superseded attempt and must
    # not land. Empty only if a caller hands us a plan without one, in which case every CAS below
    # degrades to the old mission-keyed behaviour rather than refusing a legitimate dispatch.
    plan_id = str(plan.get("plan_id") or "") or None
    epoch = session_input.policy_fingerprint() if policy_epoch is None else policy_epoch

    def authorize(observed: str | None) -> str | None:
        # Under the fence, immediately before the spawn. The comparand is a digest of the POLICY
        # ITSELF (#904 review 3, finding 3), so it means the same thing to every instance and
        # there is no second record to fail open. `None` is "could not read", and an unreadable
        # authority record is the absence of a check rather than permission.
        if observed is None or epoch is None or observed != epoch:
            return (
                "orchestration policy changed while this launch was being authorised, "
                "so nothing was started"
            )
        # …AND THE DIRECTORY (#904 review 2, finding 6). The route resolved the project and the
        # operator confirmed that path; between then and here it can move again, and an agent
        # starting somewhere the operator never approved is the whole hazard. Reading the project
        # store here is a different file from prefs and takes no lock this fence holds, so the
        # one-way lock order the module note describes is unchanged.
        if verify_cwd is not None:
            try:
                now = verify_cwd()
            except Exception:  # noqa: BLE001 — an unresolvable project is a refusal, not a crash
                now = None
            if now != cwd:
                return (
                    "that project moved while this launch was being authorised, "
                    "so nothing was started"
                )
        # …AND THIS ATTEMPT IS STILL THE ONE THE OPERATOR APPROVED (#904 review 16).
        #
        # The CAS on every WRITE closes the window after the spawn; this is the window before it.
        # `on_key` runs BEFORE the launch fence — deliberately, because a record of an intention
        # is the only thing a crashed dispatch can be reconciled against — so an attempt can stamp
        # while it is current and then wait here for the fence. The mission may legally retreat to
        # `planned` in that wait, a new plan be proposed, approved and claimed, and this attempt
        # would still spawn: the later settlement would catch it, but only after an agent had
        # started and been handed a brief nobody approved. Policy and cwd were rechecked here for
        # exactly this reason; the attempt's own identity was not.
        #
        # FAIL CLOSED on a missing row as well as a mismatched one: no row means this dispatch is
        # already over, and "we could not tell whose this is" must never authorise a spawn.
        if plan_id is not None:
            try:
                row = missions.get_dispatch(mission_id)
            except Exception:  # noqa: BLE001 — an unreadable record is not permission
                row = None
            if row is None or str(row.get("plan_id") or "") != plan_id:
                return (
                    "this dispatch was superseded while the launch was being authorised, "
                    "so nothing was started"
                )
        return None

    # WHAT WAS MINTED, if anything (#904 review 18, finding 1). The launcher can raise AFTER the
    # key is stamped and after a process may exist — `proc.wait()` failing, a full disk on a log
    # write — and the handler below cannot tell that from a refusal before anything ran. Keeping
    # the key is what lets it tell the truth in the one direction that matters.
    minted: str | None = None

    def on_key(key: str) -> None:
        # The record must be ahead of the master, never behind it. `False` means the dispatch has
        # already been settled by somebody else, and starting an agent for it would produce
        # exactly the orphan this whole path exists to prevent.
        nonlocal minted
        if not missions.note_dispatch_session(mission_id, key, expect_plan=plan_id):
            raise missions.MissionError("this dispatch is no longer current", status=409)
        minted = key

    try:
        out = await headless_dispatch.dispatch(
            engine=engine,
            cwd=cwd,
            brief=brief,
            registry=registry,
            # UNATTENDED BYPASS IS NOT INHERITED. #898 defaults it to False precisely so the
            # decision belongs to a layer that knows whether an operator authorised it, and the
            # answer here is no: an operator approving a PLAN approved the work, not the removal
            # of every tool prompt from an agent nobody is watching. It stays off until it is a
            # grant somebody explicitly makes.
            bypass=False,
            on_key=on_key,
            authorize=authorize,
        )
    except headless_dispatch.DispatchError as e:
        # Could not be ATTEMPTED — an ineligible engine, a brief the sanitiser refused. Nothing
        # was spawned, so this is not a mission that failed; it is a dispatch that did not happen,
        # and the operator must be able to fix the proposal and press again (#904 review 2,
        # finding 7).
        await _settle(mission_id, to="planned", detail=str(e), expect_plan=plan_id)
        return {"state": "planned", "reason": str(e), "session_key": None}
    except asyncio.CancelledError:
        # THE REQUEST WENT AWAY, and the mission must not go with it (#904 review 3, finding 2).
        #
        # `CancelledError` is a `BaseException`, so it walked straight past the handler below and
        # out of this function — leaving the mission `dispatching` with a lease whose process is
        # still very much alive, which is exactly the row startup recovery is right to refuse.
        # Nothing then moved it for the life of the process.
        #
        # Settled as `failed` rather than `planned`: a cancellation can land after the spawn, and
        # "nothing was started" is not something this path can claim. The dispatch record is KEPT
        # for the same reason — there may be an agent out there and this is its only trace.
        await asyncio.shield(
            asyncio.ensure_future(
                _settle(
                    mission_id,
                    to="failed",
                    detail="the request was cancelled while the launch was in flight",
                    keep_record=True,
                    expect_plan=plan_id,
                )
            )
        )
        raise
    except Exception as e:  # noqa: BLE001
        # An exception from INSIDE the launcher is not knowably pre-spawn, so this one keeps
        # `failed`: the honest answer for "something happened and we cannot say what".
        #
        # …AND IF A KEY WAS MINTED, THE RECORD SURVIVES AND NAMES IT (review 18, finding 1). This
        # settled with the default `keep_record=False` and reported `session_key=None`, so the
        # durable row was DELETED — while the very comment on `on_key` says the case it exists for
        # is a session that DID come to be. An `OSError` after the spawn therefore left an agent
        # running with nothing on disk naming it, and recovery reconciles only what it can still
        # find. "We cannot say what happened" is exactly the state in which the record must be
        # kept: the startup pass probes the engine's own store and settles it from evidence, which
        # is the one thing this frame cannot do.
        started = minted is not None
        await _settle(
            mission_id,
            to="failed",
            detail=f"the launch failed ({type(e).__name__})",
            keep_record=started,
            expect_plan=plan_id,
        )
        return {
            "state": "failed",
            "reason": f"the launch failed ({type(e).__name__})",
            "session_key": minted,
        }

    if not out.ok and not out.launched:
        # REFUSED BEFORE ANYTHING EXISTED — a withdrawn policy, a lock held elsewhere, a dispatch
        # this process no longer owns. `launched` is #898's first fact and the one that decides
        # this: with no master there is nothing to clean up and nothing to report as a failure.
        reason = out.reason or "the launch was refused"
        await _settle(mission_id, to="planned", detail=reason, expect_plan=plan_id)
        return {"state": "planned", "reason": reason, "session_key": None}

    if not out.ok:
        # ATTEMPTED AND FAILED, which includes the case this feature exists to catch: a live
        # process with nothing in the engine's store. The launcher's own reason is carried
        # verbatim — it names which of the three facts was missing, and a friendlier sentence
        # would be a less useful one.
        reason = out.reason or "the session never started"
        # KEPT FIRST, DROPPED ONLY ON PROOF (#904 review 3, finding 1) — and only when the
        # MISSION ALSO MOVED (review 13, finding 1). This branch had its own copy of the
        # reconciliation, and the copy discharged the durable row on the teardown's answer alone.
        # `_settle` turns a store failure into `{settled: false, state: "dispatching"}` rather
        # than raising, so a second write failure deleted the only row recovery could have found
        # while the mission stayed `dispatching` for ever — and this returned `out.state`, the
        # launcher's furthest-point diagnostic, so the operator was told `failed` about it.
        #
        # One reconciliation now. The launcher's own reason still rides back verbatim, because it
        # names which of the three facts was missing; the STATE is the mission's real one.
        landed = await _orphaned_after_launch(
            mission_id, out, reason, cancelled=False, expect_plan=plan_id
        )
        return {"state": landed.state, "reason": reason, "session_key": out.key}

    # STARTED AND BRIEFED. The adoption and the transition are one act: a `running` mission with
    # no session is a board that promises follow-through it cannot perform (#896), and a session
    # adopted into a mission that has since been closed is an agent nobody owns (#904).
    try:
        # THROUGH THE FENCE (#904 review 8, finding 1). This is the path that actually adopts, and
        # it was the one calling the store directly — so recovery could hold the session's fence,
        # decide it was an orphan, and kill the agent this line has just recorded ownership of.
        #
        # …AND THE WORKER IS RECLAIMED IF THE REQUEST GOES AWAY (#904 review 10, finding 2). The
        # launcher's `CancelledError` handler covers the spawn and stops there, so a cancellation
        # landing HERE escaped without terminalizing anything — while `asyncio.to_thread` kept its
        # worker running. If that worker then raised (a busy fence, a refused adoption) the
        # mission stayed `dispatching` with a LIVE owner lease, which is exactly the row recovery
        # is right to skip: stranded for the life of the process, beside a running agent.
        #
        # Shielded and then awaited to completion, like the spawn: the settlement is bounded, so
        # waiting for it is bounded too, and every path out of this frame leaves either a mission
        # that owns the session or a terminal one with a durable teardown obligation.
        settling = asyncio.ensure_future(
            settle_offloop(
                mission_id,
                to="running",
                detail=f"dispatched {engine} in {cwd}",
                session_key=out.key,
                expect_plan=plan_id,
                # Preserved through a REFUSED settlement; the branches below drop it once the
                # session has been proved stopped. A successful settlement deletes it, which is
                # right — nothing is owed when the mission owns the session.
                keep_record=True,
            )
        )
        try:
            verdict = await asyncio.shield(settling)
        except asyncio.CancelledError:
            # THE SETTLEMENT IS STILL OURS. Finish it, then decide — reporting "cancelled" over a
            # mission that has since been recorded `running` would be the false report, and
            # leaving one `dispatching` is the stranded row.
            with contextlib.suppress(Exception):
                await asyncio.shield(settling)
            landed = None
            if not settling.cancelled() and settling.exception() is None:
                landed = settling.result()
            # THE VERDICT, NOT MERELY THE ABSENCE OF AN EXCEPTION (#904 review 11, finding 2). A
            # settlement can return normally and still not settle — the operator abandoned the
            # mission while the worker waited — and reading that as ownership left the agent
            # running with nobody answering for it and nobody tearing it down.
            if landed is not None and landed.get("settled"):
                raise  # it landed and adopted: the mission owns the session, nothing is owed
            await _orphaned_after_launch(
                mission_id,
                out,
                "the request was cancelled while this dispatch was being recorded",
                cancelled=True,
                expect_plan=plan_id,
            )
            raise
    except Exception as e:  # noqa: BLE001 — every failure to record is the same orphan
        # A REFUSED ADOPTION, OR ANY OTHER FAILURE TO RECORD (#904 review 11, finding 1). This
        # caught only `MissionError`, so a locked store or a filesystem error propagated with the
        # mission still `dispatching` and its owner lease live — the row recovery deliberately
        # skips. The distinction never mattered: whatever the reason, the agent is running and
        # this mission does not own it.
        landed = await _orphaned_after_launch(
            mission_id,
            out,
            f"the session could not be adopted ({e})",
            cancelled=False,
            expect_plan=plan_id,
        )
        # THE MISSION'S REAL STATE, not the one this path tried for (#904 review 12, finding 1).
        # Reporting `failed` over a mission the store never moved is the false report — and the
        # operator's next decision would be made on it.
        return {"state": landed.state, "reason": str(e), "session_key": out.key}
    if not verdict.get("settled"):
        # THE MISSION MOVED UNDER US. Reporting `running` here is the false report — the store
        # says otherwise and the operator's next decision would be made on it. The agent we just
        # started belongs to nobody, so it is stopped — and the record only goes once that is
        # PROVED, because otherwise it is the sole trace of an unattended agent (finding 1).
        #
        # …AND THE SENTENCE SAYS WHAT THE TEARDOWN ANSWERED (review 13, finding 2). This was the
        # second copy of the reconciliation and it asserted the stop unconditionally: a `leaked`
        # cleanup logged "may still be running" while this line told the operator, in the response
        # their next decision is made on, that the agent was gone. Through the one helper now, and
        # carrying the store's OWN verdict rather than asking a second time — the mission has
        # already moved, so a re-settle is a write that can fail on a busy fence and turn a known
        # `cancelled` back into an assumed `dispatching`.
        moved = str(verdict.get("state") or "failed")
        landed = await _orphaned_after_launch(
            mission_id,
            out,
            f"the mission became {moved} while it was being dispatched",
            cancelled=False,
            verdict=verdict,
            expect_plan=plan_id,
        )
        became = f"the mission became {moved} while it was being dispatched"
        if landed.stopped:
            reason = f"{became}, so the session that had started was stopped"
        elif landed.held_elsewhere:
            # RUNNING, AND OWNED (#904 review 13, second round). This was reported as a stop,
            # because `spared` and `stopped` arrived here as the same boolean.
            reason = (
                f"{became}; the session {out.key} it had started was NOT stopped — another "
                "mission adopted it and is running it"
            )
        else:
            reason = (
                f"{became}; the session {out.key} that had started could NOT be proved stopped "
                "and may still be running"
            )
        return {
            "state": landed.state,
            "reason": reason,
            # NAMED WHENEVER IT IS STILL RUNNING. `None` is right only when the boundary was
            # proved empty; a leaked agent — or one another mission now holds — is something the
            # operator has to be able to go and look at.
            "session_key": None if landed.stopped else out.key,
        }
    # SETTLED AND OWNED. Nothing is owed — the mission holds the session and its state says so —
    # so the in-flight record is discharged here rather than by the settlement, which had to keep
    # it in case the settlement was the thing that refused.
    missions.clear_dispatch(mission_id, expect_plan=plan_id)
    return {"state": "running", "reason": "", "session_key": out.key}
