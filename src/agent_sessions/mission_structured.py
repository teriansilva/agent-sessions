"""The ONE door between mission code and the structured facade (#1273 P3, #1898).

Missions dispatch to API clients through `structured_runtime`: a structured create, then the brief
as one turn. Everything mission code sends into an API session goes through this module and
nothing else, because the fence on those turns is caller-supplied (#1897): a guarded turn is
checked at the worker's claim, an unguarded one is not. So this module never submits unguarded,
and `tests/test_mission_structured_dispatch.py` fails if any mission, dispatch, actuator,
orchestrator or automation module calls `structured_runtime.create_session` / `submit_turn`
itself.

* `admits(prov)`: may this client hold a mission role? An API client needs its kind's
  `kinds.API_MISSION_READY` capability bit AND `registry.can_start`. A console (terminal) client
  never may (#1902, #1613 D7/D17): there is no terminal fallback. `console_refusal(prov)` says
  so in the operator's words and names the API client to use, or says none exists yet.
* `ids(plan_id)`: the session and brief operation ids, derived from the dispatch ATTEMPT
  (`plan_id`), never the mission. A re-planned dispatch gets a new session; a recovered or
  retried one replays the same create and the same brief, and never makes a second of either.
* `guard(mission_id, key, check)`: the #1897 guard whose domain check runs inside the write fence.
* `dispatch(...)`: the structured launch, returning the same `headless_dispatch.Dispatch` facts
  the console launcher returns, so `mission_dispatch._conclude` settles both the same way.
* Automations (#1901) use the same door: `automation_start` (a create and one guarded first
  turn, sharing `_create_and_brief` with `dispatch`), `automation_target` (the native record's
  cwd and state, read before a send) and `automation_send` (one guarded turn). Their ids come
  from the automation's RUN (`automation_ids`) and their binding is `automation:<id>:<run>`.

No permission bypass, ever, on this path (#1613 D8): creates pass `bypass=False`, and #1897 also
refuses guarded work in a session that skips permissions.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable

from . import engines, handoff, headless_dispatch, missions, native_ipc, session_input
from . import structured_runtime as runtime
from .engines import registry
from .structured_types import ExecutionGuard

log = logging.getLogger(__name__)

#: Fixed forever: every derived id depends on it, and recovery re-derives them after a restart.
MISSION_NAMESPACE = uuid.UUID("3f1c9a52-1898-5e2b-9a41-6d0f2c7b1e83")

#: The mission states in which a dispatch (or, from a running mission, a spawn) may still act.
LIVE_STATES = frozenset({"dispatching", "running"})

#: What the picker says about an API client that can't hold a mission role yet (#1577 words).
NOT_MISSION_READY = "this agent's API client can't run missions yet"
#: What every mission path says about a console (terminal) client (#1902). The same sentence
#: `mission_team` gives a pool member's console client.
CONSOLE_REFUSED = "only a mission-ready API client can hold a mission role"


def ids(plan_id: str) -> tuple[str, str]:
    """``(session_id, brief_turn_id)`` for one dispatch attempt."""
    if not isinstance(plan_id, str) or not plan_id:
        raise ValueError("a structured dispatch needs its plan id")
    return (
        str(uuid.uuid5(MISSION_NAMESPACE, f"{plan_id}:session")),
        str(uuid.uuid5(MISSION_NAMESPACE, f"{plan_id}:brief")),
    )


def is_api(prov) -> bool:
    return getattr(getattr(prov, "manifest", None), "runtime", None) == "api"


def admits(prov) -> tuple[bool, str | None]:
    """May ``prov`` hold a mission role right now? ``(True, None)`` or ``(False, reason)``.

    May probe a client's readiness: call it off the event loop."""
    if is_api(prov):
        try:
            described = runtime.describe(prov.engine_id, check_ready=False)
        except Exception:  # noqa: BLE001 — a client that can't answer is not admitted
            return False, NOT_MISSION_READY
        if not described.mission_ready:
            return False, NOT_MISSION_READY
        if not registry.can_start(prov):
            return False, registry.api_unavailable_reason(prov) or "not ready"
        return True, None
    # A CONSOLE CLIENT NEVER HOLDS A MISSION ROLE (#1902). Not "unless it can be seeded": every
    # plan, spawn, dispatch and adopt path asks this one question, so a terminal fallback has no
    # door left. Cheap on purpose (the picker asks it of every agent); the words naming the API
    # client to use come from `console_refusal`, asked only where a refusal is shown.
    return False, CONSOLE_REFUSED


def console_refusal(prov) -> str:
    """Why ``prov`` (a console client) can't hold a mission role, naming the API client of its
    family that can (the `unattended` default, #1728), or saying none can yet. Never raises; may
    probe readiness, so call it off the loop."""
    from .engines import family

    eid = getattr(prov, "engine_id", "") or "this agent"
    try:
        fam = family.family_of(prov)
        alt, _why = family.default_client(fam, "unattended") if fam else (None, "")
    except Exception:  # noqa: BLE001 — an unreadable roster names no alternative
        alt = None
    if alt:
        return f"{eid} runs in a terminal, and missions run on API clients: use {alt}"
    return f"{eid} runs in a terminal, and none of its API clients can hold a mission role yet"


@contextlib.asynccontextmanager
async def _in_process_only():
    # A native turn never takes the in-process half of a guard (#1897); reaching here is a bug.
    raise RuntimeError("a mission guard has no in-process admission")
    yield  # pragma: no cover


def guard(mission_id: str, key: str, check: Callable[[], tuple[bool, str]]) -> ExecutionGuard:
    """The #1897 guard for a mission's turn into ``key``. ``check`` runs INSIDE the write fence
    (`session_input.admit_native`) and must not take `session_input._lock` itself."""
    return ExecutionGuard(
        f"mission:{mission_id}",
        _in_process_only,
        admit=lambda: session_input.admit_native(key, check),
    )


async def _nothing_runs(key: str) -> bool:
    """Is it PROVED that no worker runs for ``key``? A probe that says `gone`, or no session record
    AND no worker directory naming it. Anything else, an unreadable answer included, is "it may
    be running"."""
    try:
        probed = await runtime.probe(key)
    except runtime.StructuredError as exc:
        if exc.status != 404:
            return False  # could not look: not a verdict
        # A 404 is a MISSING or mismatched session record, which is not proof that no worker
        # runs (Hermes on #1315; independent review on #1898): the worker side must agree.
        try:
            return not await runtime.may_have_worker(key)
        except Exception:  # noqa: BLE001
            return False
    except Exception:  # noqa: BLE001
        return False
    return probed.get("containment") == "gone"


def _seed_outcome(handoff_code: str | None) -> str:
    """The brief's delivery code in the dispatch record's existing vocabulary (`SEED_OUTCOMES`)."""
    if handoff_code == "sent":
        return "delivered"
    if handoff_code == "not_sent":
        return "zero_write"
    return "unknown"  # queued or uncertain: it may still reach the agent, so never re-sent


async def dispatch(
    *,
    mission_id: str,
    plan_id: str | None,
    engine: str,
    cwd: str,
    brief: str,
    on_key: Callable[[str], None],
    authorize: Callable[[str | None], str | None],
    model: str | None = None,
    effort: str | None = None,
    specialist: tuple[str, int] | None = None,
    step: tuple[str, int] | None = None,
) -> headless_dispatch.Dispatch:
    """Launch a mission's API session and hand it its brief. The structured twin of
    `headless_dispatch.dispatch`: it raises `DispatchError` only when nothing was attempted, and
    otherwise returns the facts (`launched`, `started`, `briefed`, `seed_outcome`, `reason`).

    ``authorize(observed_policy)`` is the dispatch's own fenced check; it runs inside the write
    fence for the create AND again for the brief, so a withdrawal between the two refuses it.

    A step launch (#1581) also names its ``specialist`` ``(id, version)`` and ``effort`` (the
    create reserves the specialist's capacity per generation, #1693 S4b) and its ``step``
    ``(step_id, episode)``, which rides on the brief turn's correlation."""
    prov = engines.get(engine)
    if prov is None:
        raise headless_dispatch.DispatchError(f"{engine!r} is not an available agent")
    ok, why = await asyncio.to_thread(admits, prov)  # it may probe the client's readiness
    if not ok:
        raise headless_dispatch.DispatchError(why or NOT_MISSION_READY)
    if not plan_id:
        raise headless_dispatch.DispatchError(
            "this dispatch has no plan id, so nothing was started"
        )
    try:
        text = handoff.sanitize_seed(brief)
    except handoff.HandoffError as exc:
        raise headless_dispatch.DispatchError(str(exc.detail)) from None
    session_id, brief_id = ids(plan_id)
    key = f"{engine}:{session_id}"
    out = headless_dispatch.Dispatch(key=key, engine=engine, native=session_id, cwd=cwd)

    def check() -> tuple[bool, str]:
        # THE MISSION IS STILL BEING DISPATCHED (a spawn: still running). An abandon or a stand-down
        # does not retire the dispatch row's `plan_id`, so `authorize` alone would still pass and
        # a fresh admission would hand out tokens issued AFTER the withdrawal (independent review
        # on #1898). Read in the fence, like everything `authorize` reads.
        try:
            state = str((missions.get_mission(mission_id) or {}).get("state") or "")
        except Exception:  # noqa: BLE001 — an unreadable mission is not permission
            state = ""
        if state not in LIVE_STATES:
            return False, "the mission is no longer being dispatched, so nothing was sent"
        why = authorize(session_input.policy_fingerprint())
        return why is None, why or ""

    mission_guard = guard(mission_id, key, check)
    try:
        on_key(key)  # the record is ahead of the session, never behind it
    except missions.MissionError as exc:
        raise headless_dispatch.DispatchError(str(exc)) from None
    context: dict = {"mission_id": mission_id, "reservation_id": plan_id}
    if step is not None:
        context["step_id"], context["episode"] = step[0], int(step[1])
    return await _create_and_brief(
        out,
        text=text,
        brief_id=brief_id,
        admission=mission_guard,
        context=context,
        model=model,
        effort=effort,
        specialist=specialist,
        # The mission's shelf tool on the first generation (#1900): the session joins the
        # roster only once it runs, and the broker re-checks membership on every add.
        shelf_mission=mission_id,
    )


async def _create_and_brief(
    out: headless_dispatch.Dispatch,
    *,
    text: str,
    brief_id: str,
    admission: ExecutionGuard,
    context: dict,
    model: str | None = None,
    shelf_mission: str | None = None,
    effort: str | None = None,
    specialist: tuple[str, int] | None = None,
) -> headless_dispatch.Dispatch:
    """The structured create and its ONE guarded first turn, shared by a mission's dispatch and an
    automation's start (#1901). Fills in ``out``'s facts; never raises for a refused effect."""
    key = out.key
    try:
        await runtime.create_session(
            out.engine,
            out.cwd,
            operation_id=out.native,
            model=model,
            # Sent only when chosen, so every other create is the call it always was.
            **({"effort": effort} if effort is not None else {}),
            **({"specialist": specialist} if specialist is not None else {}),
            bypass=False,
            execution_admission=admission,
            wait_ready=True,
            shelf_mission=shelf_mission,
        )
    except runtime.StructuredError as exc:
        out.seed_outcome = "not_attempted"
        if exc.reason == native_ipc.AUTHORITY_WITHDRAWN or await _nothing_runs(key):
            # Refused before anything ran (a withdrawn authority, the session limit, a refused
            # input), or nothing was left running: a refusal the operator can act on and press
            # again, never a failure that keeps a record of a worker that does not exist.
            out.reason = exc.detail
            return out
        # A create that may have launched a worker and never became ready: a failed dispatch,
        # its record kept until a teardown proves the worker gone (#898's "alive is not started").
        out.launched = True
        out.reason = f"the API session never became ready: {exc.detail}"
        out.seed_outcome = "not_attempted"
        return out
    out.launched = out.started = True
    try:
        sent = await runtime.submit_turn(
            key,
            operation_id=brief_id,
            text=text,
            context=context,
            execution_admission=admission,
        )
    except runtime.StructuredError as exc:
        # A 409/422 is a refusal before any claim (busy, withdrawn, an old worker, invalid text):
        # nothing reached the agent. Anything else (a lost answer, a timeout) may have.
        refused = exc.status in (409, 422)
        out.seed_outcome = "zero_write" if refused else "unknown"
        out.reason = f"the brief was not delivered: {exc.detail}"
        return out
    code = sent.get("handoff")
    out.seed_outcome = _seed_outcome(code)
    if code != "sent":
        out.reason = (
            "the brief was not delivered"
            if code == "not_sent"
            else "the brief's delivery could not be confirmed, so it is never sent again"
        )
        return out
    out.briefed = True
    return out


# --- automations (#1901) ---------------------------------------------------------------------
#
# An automation's run is its own durable claim (`automations_store.begin_run`), so its ids are
# derived from the RUN id: a repeated call for the same run replays the same create, the same
# first turn and the same send, and never makes a second of any of them. The guard binding is
# `automation:<automation id>:<run id>`; its domain check (the automation's authority, the scope,
# the pinned folder) is the runner's own, run INSIDE `session_input.admit_native`'s fence.


def automation_ids(run_id: str) -> tuple[str, str, str]:
    """``(session_id, first_turn_id, send_turn_id)`` for one automation run."""
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("a structured automation effect needs its run id")
    return tuple(  # type: ignore[return-value]
        str(uuid.uuid5(MISSION_NAMESPACE, f"automation-run:{run_id}:{part}"))
        for part in ("session", "brief", "send")
    )


def _fenced(check: Callable[[], tuple[bool, str]]) -> Callable[[], tuple[bool, str]]:
    def run() -> tuple[bool, str]:
        # Anything the check raises is "authority could not be verified": a known refusal before
        # any claim, never an ambiguous outcome.
        try:
            return check()
        except Exception as exc:  # noqa: BLE001
            return False, f"authority could not be verified ({type(exc).__name__})"

    return run


def automation_guard(
    binding: str, key: str, check: Callable[[], tuple[bool, str]]
) -> ExecutionGuard:
    """The #1897 guard for an automation's effect in ``key``. ``check`` runs INSIDE the write
    fence and must not take `session_input._lock` itself. Raises ``ValueError`` on a malformed
    binding, before anything is sent."""
    fenced = _fenced(check)
    return ExecutionGuard(
        binding, _in_process_only, admit=lambda: session_input.admit_native(key, fenced)
    )


async def automation_start(
    *,
    binding: str,
    run_id: str,
    engine: str,
    cwd: str,
    brief: str,
    on_key: Callable[[str], None],
    check: Callable[[], tuple[bool, str]],
) -> headless_dispatch.Dispatch:
    """An automation's new API session: one structured create and one guarded first turn, the
    same facts `headless_dispatch.dispatch` returns. Raises `DispatchError` only when nothing was
    attempted. Never permission-bypassed (#1897 refuses guarded work in such a session anyway).

    A worker left without its first turn — a create that never became ready, or a first turn
    refused before any claim — is CONTAINED and ``out.teardown`` says how that went: nothing is
    left running that the run does not account for. A first turn whose delivery could not be
    confirmed keeps its worker (it may be working on the brief) and is never re-sent."""
    prov = engines.get(engine)
    if prov is None or not is_api(prov):
        raise headless_dispatch.DispatchError(f"{engine!r} is not an available API client")
    ok, why = await asyncio.to_thread(admits, prov)  # it may probe the client's readiness
    if not ok:
        raise headless_dispatch.DispatchError(why or NOT_MISSION_READY)
    try:
        text = handoff.sanitize_seed(brief)
    except handoff.HandoffError as exc:
        raise headless_dispatch.DispatchError(str(exc.detail)) from None
    try:
        session_id, brief_id, _send_id = automation_ids(run_id)
        key = f"{engine}:{session_id}"
        admission = automation_guard(binding, key, check)
    except ValueError as exc:
        raise headless_dispatch.DispatchError(f"nothing was started: {exc}") from None
    out = headless_dispatch.Dispatch(key=key, engine=engine, native=session_id, cwd=cwd)
    on_key(key)  # the record is ahead of the session, never behind it
    out = await _create_and_brief(
        out,
        text=text,
        brief_id=brief_id,
        admission=admission,
        context={"reservation_id": run_id},
    )
    if out.launched and not out.briefed and out.seed_outcome in ("not_attempted", "zero_write"):
        from . import runtime_cleanup  # at call time, like `mission_fence`

        try:
            await runtime_cleanup.contain_native(engine, session_id)
            out.teardown = "stopped"
        except Exception as exc:  # noqa: BLE001 — recorded, never raised over the refusal
            log.warning("automation %s: containing %s failed", binding, key, exc_info=True)
            out.teardown = "error"
            out.reason = f"{out.reason}; stopping its worker failed: {exc}"
    return out


async def automation_target(key: str) -> dict:
    """What an automation send needs to know about API session ``key`` BEFORE it sends:
    ``{"cwd", "state", "pending", "live"}`` from the native record, or ``{"gone": True}`` for a
    session that no longer exists, or ``{"unreadable": detail}``. Never raises.

    ``cwd`` is the native record's (the create request's), never a console-transcript read."""
    try:
        snap = await runtime.snapshot(key)
    except runtime.StructuredError as exc:
        if exc.status == 404:
            return {"gone": True}
        return {"unreadable": str(exc.detail)}
    except Exception as exc:  # noqa: BLE001 — could not look, so nothing is sent
        return {"unreadable": type(exc).__name__}
    try:
        containment = (await runtime.probe(key)).get("containment")
    except Exception:  # noqa: BLE001 — could not look: not live
        containment = None
    return {
        "cwd": snap.get("cwd"),
        "state": snap.get("state"),
        "pending": bool(snap.get("pending_requests")),
        "read_only": snap.get("read_only"),
        "live": containment == "live",
    }


async def automation_send(
    *,
    binding: str,
    run_id: str,
    key: str,
    text: str,
    check: Callable[[], tuple[bool, str]],
) -> tuple[str, str]:
    """Deliver an automation's text into API session ``key`` as ONE guarded turn, its id derived
    from the run. Returns ``(code, detail)``; never raises. Codes, after #1899:

    * ``sent`` / ``queued`` — handed to the worker;
    * ``refused`` — nothing was sent (a refusal before any claim, ``not_sent``, a withdrawn
      authority, a malformed binding);
    * ``gone`` — the session no longer exists;
    * ``uncertain`` — the delivery could not be confirmed. It is never sent again.

    The caller has already checked that the session is idle, has no pending request and that its
    worker is live (`automation_target`): an automation never relaunches an agent (the window
    between that probe and the claim is #1983's)."""
    try:
        _sid, _brief, operation_id = automation_ids(run_id)
        admission = automation_guard(binding, key, check)
    except Exception as exc:  # noqa: BLE001 — a known refusal, never a stranded claim
        return "refused", f"nothing was sent: the send could not be identified ({exc})"
    try:
        sent = await runtime.submit_turn(
            key,
            operation_id=operation_id,
            text=text,
            context={"reservation_id": run_id},
            execution_admission=admission,
        )
    except runtime.StructuredError as exc:
        if exc.status == 404:
            return "gone", "the API session no longer exists"
        if exc.status in (409, 422):
            return "refused", f"nothing was sent: {exc.detail}"
        return "uncertain", f"delivery outcome is unknown ({exc.detail})"
    except Exception as exc:  # noqa: BLE001 — may have reached the worker
        return "uncertain", f"delivery outcome is unknown ({type(exc).__name__})"
    code = sent.get("handoff")
    if code == "sent":
        return "sent", "delivered as a structured turn"
    if code == "queued":
        return "queued", "queued behind the running turn"
    if code == "not_sent":
        return "refused", "the agent did not take the message; nothing was sent"
    return "uncertain", "delivery could not be confirmed"


# --- sends into an API session (#1899) -------------------------------------------------------


def is_api_key(key: str) -> bool:
    """Is ``key`` an API session (`engine:<uuid>` of a `runtime = "api"` client)? Never raises."""
    try:
        prov, _native = engines.parse_key(key)
    except Exception:  # noqa: BLE001 — a key that does not resolve is not an API session
        return False
    return is_api(prov)


def action_operation_id(action_id: str) -> str:
    """The one operation id a ledger action's structured turn may use, derived and never minted."""
    if not isinstance(action_id, str) or not action_id:
        raise ValueError("a structured send needs its action id")
    return str(uuid.uuid5(MISSION_NAMESPACE, f"action:{action_id}"))


def last_operator_turn(snap: dict) -> str | None:
    """The newest operator turn id a snapshot shows (an agent's own turns are not counted)."""
    for turn in reversed(snap.get("turns") or ()):
        if turn.get("kind") != "agent" and turn.get("turn_id"):
            return str(turn["turn_id"])
    return None


async def precondition(key: str) -> dict:
    """What a proposal into an API session believed: its last operator turn and whether it was
    idle. Recorded on the action at proposal time and compared by `send`."""
    snap = await runtime.snapshot(key)
    return {
        "key": key,
        "api_last_turn": last_operator_turn(snap),
        "api_idle": snap.get("state") == "idle" and not snap.get("pending_requests"),
    }


def _still_current(snap: dict, pre: dict, *, key: str, autonomous: bool) -> str | None:
    """Why a send's structured precondition no longer holds, or None.

    An AUTOMATIC send needs a proposal-time precondition for this very session, proposed against
    an idle session, with no newer operator turn, no pending request and still idle now. The
    operator's own words need only the first of those that was recorded."""
    if pre.get("key") not in (None, key):
        return "this was proposed against another session"
    if autonomous and "api_last_turn" not in pre:
        return "an automatic send needs the session as it was when proposed"
    if autonomous and pre.get("api_idle") is False:
        return "this was proposed while the session was busy"
    if autonomous and snap.get("pending_requests"):
        # The operator's own words may queue behind an open request; an automatic send may not.
        return "the session is waiting on an approval or a question"
    if "api_last_turn" in pre and last_operator_turn(snap) != pre.get("api_last_turn"):
        return "the session has a newer turn than the one this was proposed against"
    if autonomous and snap.get("state") != "idle":
        return "the session is busy; an automatic send waits for an idle session"
    return None


async def send(  # noqa: PLR0913 — every argument is part of one delivery's identity
    *,
    action_id: str,
    key: str,
    text: str,
    mission_id: str | None,
    check: Callable[[], tuple[bool, str]],
    precondition: dict | None,
    autonomous: bool,
) -> tuple[str, str]:
    """Deliver one ledger action's text into API session ``key`` as one GUARDED turn. Returns
    ``(ledger_state, detail)`` in the actuator's vocabulary; never raises.

    ``check`` is the action's own final guard (authority, policy, membership, render), run INSIDE
    the write fence by #1897's admission, so a withdrawal after it is refused at the worker's claim.
    The precondition is read just before: no newer operator turn, no pending request, and idle for
    an autonomous send. A turn the operator starts after that read makes this one queue behind it.

    An AUTOMATIC send also needs (#1900) a client still admitted to missions (the kind's
    `API_MISSION_READY` bit, so flipping it off stops every automatic send at once) and a worker
    that a probe says is `live`: a turn into an ended worker would relaunch it, and nothing
    automatic ever relaunches an agent. The operator's own words may resume it, as the pane does.
    """
    if autonomous and mission_id:
        try:
            prov, _native = engines.parse_key(key)
            admitted, why = await asyncio.to_thread(admits, prov)
        except Exception:  # noqa: BLE001 — a client that can't answer is not admitted
            admitted, why = False, NOT_MISSION_READY
        if not admitted:
            return "stale", why or NOT_MISSION_READY
    try:
        snap = await runtime.snapshot(key)
    except runtime.StructuredError as exc:
        if exc.status == 404:
            return "failed", "the API session no longer exists"
        return "stale", f"the session could not be read, so nothing was sent ({exc.detail})"
    except Exception as exc:  # noqa: BLE001 — could not look, so nothing is sent
        return "stale", f"the session could not be read, so nothing was sent ({type(exc).__name__})"
    why = _still_current(snap, precondition or {}, key=key, autonomous=autonomous)
    if why:
        return "stale", why
    if autonomous:
        try:
            containment = (await runtime.probe(key)).get("containment")
        except Exception:  # noqa: BLE001 — could not look, so nothing is sent
            containment = None
        if containment != "live":
            return "stale", (
                "the session's worker is not running, and an automatic send never restarts it; "
                "nothing was sent"
            )

    def fenced_check() -> tuple[bool, str]:
        # Anything the check raises is "authority could not be verified": a known refusal before
        # any claim, never an ambiguous outcome.
        try:
            return check()
        except Exception as exc:  # noqa: BLE001
            return False, f"authority could not be verified ({type(exc).__name__})"

    binding = f"mission:{mission_id}" if mission_id else "orchestrator"
    try:
        # Built before anything is sent, so a malformed binding or action id is a known refusal
        # rather than an exception that strands the caller's claim (Hermes on #1926).
        action_guard = ExecutionGuard(
            binding, _in_process_only, admit=lambda: session_input.admit_native(key, fenced_check)
        )
        operation_id = action_operation_id(action_id)
    except Exception as exc:  # noqa: BLE001
        return "stale", f"nothing was sent: the send could not be identified ({type(exc).__name__})"
    try:
        sent = await runtime.submit_turn(
            key,
            operation_id=operation_id,
            text=text,
            context={"mission_id": mission_id, "reservation_id": action_id}
            if mission_id
            else {"reservation_id": action_id},
            execution_admission=action_guard,
        )
    except runtime.StructuredError as exc:
        if exc.status == 404:
            return "failed", "the API session no longer exists"
        if exc.status in (409, 422):
            return "stale", f"nothing was sent: {exc.detail}"
        return "indeterminate", f"delivery outcome is unknown ({exc.detail}); not retried"
    except Exception as exc:  # noqa: BLE001 — may have reached the worker
        return "indeterminate", f"delivery outcome is unknown ({type(exc).__name__}); not retried"
    code = sent.get("handoff")
    if code == "sent":
        return "delivered", "delivered as a structured turn"
    if code == "queued":
        return "delivered", "queued behind the running turn"
    if code == "not_sent":
        return "stale", "the agent did not take the message; nothing was sent"
    return "indeterminate", "delivery could not be confirmed; not retried"
