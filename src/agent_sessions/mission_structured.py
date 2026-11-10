"""The ONE door between mission code and the structured facade (#1273 P3, #1898).

Missions dispatch to API clients through `structured_runtime`: a structured create, then the brief
as one turn. Everything mission code sends into an API session goes through this module and
nothing else, because the fence on those turns is caller-supplied (#1897): a guarded turn is
checked at the worker's claim, an unguarded one is not. So this module never submits unguarded,
and `tests/test_mission_structured_dispatch.py` fails if any mission, dispatch, actuator,
orchestrator or automation module calls `structured_runtime.create_session` / `submit_turn`
itself.

* `admits(prov)`: may this client hold a mission role? An API client needs its kind's
  `kinds.API_MISSION_READY` capability bit AND `registry.can_start`. A console client keeps
  today's seed + unattended checks.
* `ids(plan_id)`: the session and brief operation ids, derived from the dispatch ATTEMPT
  (`plan_id`), never the mission. A re-planned dispatch gets a new session; a recovered or
  retried one replays the same create and the same brief, and never makes a second of either.
* `guard(mission_id, key, check)`: the #1897 guard whose domain check runs inside the write fence.
* `dispatch(...)`: the structured launch, returning the same `headless_dispatch.Dispatch` facts
  the console launcher returns, so `mission_dispatch._conclude` settles both the same way.

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
    ok, why = handoff.seed_start_state(prov, present=prov.is_present())
    if not ok:
        return False, why
    return engines.unattended_start_state(prov)


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
) -> headless_dispatch.Dispatch:
    """Launch a mission's API session and hand it its brief. The structured twin of
    `headless_dispatch.dispatch`: it raises `DispatchError` only when nothing was attempted, and
    otherwise returns the facts (`launched`, `started`, `briefed`, `seed_outcome`, `reason`).

    ``authorize(observed_policy)`` is the dispatch's own fenced check; it runs inside the write
    fence for the create AND again for the brief, so a withdrawal between the two refuses it."""
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
    try:
        await runtime.create_session(
            engine,
            cwd,
            operation_id=session_id,
            model=model,
            bypass=False,
            execution_admission=mission_guard,
            wait_ready=True,
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
            context={"mission_id": mission_id, "reservation_id": plan_id},
            execution_admission=mission_guard,
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
