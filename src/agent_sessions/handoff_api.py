"""Handoff into a native API session (#2141).

A console target receives its brief as a bracketed paste into its PTY (`handoff`, `webterm`). An
API client has no terminal: its brief is the new session's FIRST structured turn, sent on the
operator path (`structured_runtime.create_session` then `submit_turn`, no execution guard), exactly
as if the operator had created the session on New session and typed it.

Everything a retry needs is fixed by the first commit (`handoff.commit_api`): the target id, the
turn's operation id (`uuid5` of the handle) and the options. So a repeated commit, a double tap or
a lost answer replays, and never makes a second session or a second send.

Delivery outcomes (the issue's failure table):

* refused before any claim (a 409/422 and NO turn with that id in the session) → ``retry``: the
  brief stays bound, and the next commit (or Start) submits it under the SAME operation id;
* a recorded ``not_sent`` (the turn exists, the agent never got it) → settled: a same-id submit
  would only replay that receipt, so the pane's existing Send again (a successor id) owns it, and
  its acceptance completes the handoff (`reconcile_successor`);
* ``uncertain`` (a lost answer, a 5xx, a delivery the worker could not confirm) → settled and never
  resent;
* ``withheld`` (a guarded brief, ``guard_secrets``, that now carries a known secret) → dropped
  unsent before the submit, on every attempt, a retry included: fail closed, and the session is
  not left behind a fence for a brief nothing may send;
* ``sent`` → provenance published and, when asked, the source archived; ``queued`` → provenance
  only (it may still end ``not_sent``, so the source is not archived behind it).

A skip-permissions target keeps the two-phase start (#1339): the create launches nothing, the
commit answers ``pending_start``, and the brief is sent only after the operator's own Start
(`after_start`) once that launch is ready. A discarded or expired start releases it.

Duplicate prevention, not durability: the binding is in memory and a broker restart loses an
undelivered brief (#709).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from . import handoff, structured_runtime, template_secrets

log = logging.getLogger("agent_sessions.handoff_api")

#: How long a deferred delivery waits for the operator's Start to become ready, and how often it
#: looks. The launch view settles to `ready` or `failed` by itself well within this (its readiness
#: waiter, #1505); the bound is the pending-start window, so a waiter never outlives its brief.
START_WAIT_S = 900.0
START_POLL_S = 0.5

#: Background deliveries after a Start, kept so they are not garbage-collected mid-flight.
_TASKS: set[asyncio.Task] = set()


def _turn_ids(snap: dict) -> set[str]:
    return {t.get("turn_id") for t in snap.get("turns") or () if isinstance(t, dict)}


async def _recorded(key: str, turn_id: str) -> bool | None:
    """Whether the session holds a turn with ``turn_id``; None when it can't be read."""
    try:
        snap = await structured_runtime.snapshot(key)
    except Exception:  # noqa: BLE001 — unreadable is not "absent"
        return None
    return turn_id in _turn_ids(snap)


def _holds_no_secret(text: str) -> bool:
    """A guarded brief is checked against the secrets known NOW, at every delivery attempt (a
    console seed is, when it is typed: ``webterm._deliver_seed``). Fails closed."""
    try:
        return template_secrets.redact_text(text) == text
    except template_secrets.RedactionUnavailable:
        return False


async def deliver(key: str) -> str | None:
    """Send the bound brief as ``key``'s first turn, once. Returns the binding's delivery state
    afterwards (``None`` = still bound, retryable)."""
    claimed = handoff.api_claim(key)
    if claimed is None:
        binding = handoff.api_binding(key)
        return binding["delivery"] if binding else None
    text, turn_id, guarded = claimed
    if guarded and not await asyncio.to_thread(_holds_no_secret, text):
        handoff.api_settle(key, "withheld")
        log.warning("handoff %s: brief withheld: it carries a known secret", key)
        return "withheld"
    try:
        sent = await structured_runtime.submit_turn(key, operation_id=turn_id, text=text)
    except structured_runtime.StructuredError as exc:
        if exc.status in (409, 422):
            recorded = await _recorded(key, turn_id)
            if recorded is False:
                handoff.api_settle(key, "retry")  # refused before any claim: same id next time
                log.info("handoff %s: brief not admitted yet: %s", key, exc.detail)
                return None
            # A recorded refusal (the turn exists) is the pane's Send again; unreadable is unknown.
            outcome = "not_sent" if recorded else "uncertain"
        else:
            outcome = "uncertain"
        handoff.api_settle(key, outcome)
        log.info("handoff %s: brief %s: %s", key, outcome, exc.detail)
        return outcome
    except Exception:  # noqa: BLE001 — a lost answer may have reached the agent: never resent
        handoff.api_settle(key, "uncertain")
        log.warning("handoff %s: brief delivery could not be confirmed", key, exc_info=True)
        return "uncertain"
    code = sent.get("handoff")
    outcome = code if code in ("sent", "queued", "not_sent") else "uncertain"
    handoff.api_settle(key, outcome)
    if outcome in ("sent", "queued"):
        await _accepted(key, archive=outcome == "sent")
    return outcome


async def _accepted(key: str, *, archive: bool) -> None:
    """Provenance (best-effort, retried by nothing: a failed sidecar write leaves no link rather
    than a wrong one) and the source archive the operator asked for."""
    try:
        await asyncio.to_thread(handoff.api_mark_accepted, key)
    except Exception:  # noqa: BLE001
        log.warning("handoff %s: provenance not written", key, exc_info=True)
    binding = handoff.api_binding(key)
    if not archive or binding is None or not binding["archive_source"]:
        return
    from .routes import sessions as sessions_routes  # the archive route's own implementation

    ok, why = await sessions_routes.archive_key(binding["source_key"])
    if not ok:
        # Best-effort, like the dialog's archive (#1274): the source stays active.
        log.info("handoff %s: source %s not archived: %s", key, binding["source_key"], why)


async def reconcile_successor(key: str, operation_id: str, text: str, code: str | None) -> None:
    """Called by `structured_runtime.submit_turn` after every accepted submit: when it is the pane's
    Send again of a brief whose first delivery was a recorded refusal, finish the handoff (the
    issue's failure table: provenance, and the archive once it is ``sent``). Exactly once; never
    for an uncertain result; never raises into the operator's send."""
    try:
        outcome = handoff.api_successor_accepted(key, operation_id, text, code)
        if outcome is not None:
            await _accepted(key, archive=outcome == "sent")
    except Exception:  # noqa: BLE001 — the operator's message was sent; this is follow-up work
        log.warning("handoff %s: completing after Send again failed", key, exc_info=True)


async def commit(handle: str, seed: str | None, *, bypass: bool, archive_source: bool) -> dict:
    """Bind, create and (unless the target skips permissions) deliver. Raises `HandoffError`.

    The create is idempotent by its operation id (the session id), so a retry after a failed or
    lost create reuses it. The answer's ``delivery``: ``sent`` / ``queued`` / ``not_sent`` /
    ``uncertain``, ``pending_start`` (a skip target, waiting for the operator's Start), or
    ``None`` with ``detail`` (not admitted yet: commit again), ``withheld`` with ``detail`` (a
    guarded brief that now carries a known secret: dropped unsent)."""
    view = handoff.commit_api(handle, seed, bypass=bypass, archive_source=archive_source)
    key = view["id"]
    if view["delivery"] in handoff.API_SETTLED:
        return _explained(view)  # a replay of a settled handoff: nothing more to do
    try:
        snap = await structured_runtime.create_session(
            view["engine"],
            view["cwd"],
            operation_id=view["native"],
            bypass=bypass,
            # The default `wait_ready=True`, as every internal caller: a guarded session is ready
            # before its first turn (a submit while it is still starting is refused), and a skip
            # creation launches nothing, so it returns at once and waits for Start.
        )
    except structured_runtime.StructuredError as exc:
        raise handoff.HandoffError(exc.status, exc.detail) from None
    if bypass:
        if snap.get("start_discarded") or snap.get("start_expired"):
            handoff.api_settle(key, "released")
            return {**view, "delivery": "released"}
        return {**view, "delivery": "pending_start"}
    return _explained({**view, "delivery": await deliver(key)})


def _explained(out: dict) -> dict:
    if out["delivery"] is None:
        out["detail"] = "the new session did not take the brief yet; hand off again to retry"
    elif out["delivery"] == "withheld":
        out["detail"] = "the brief now holds a known secret, so it was not sent"
    return out


async def _after_start(key: str) -> None:
    deadline = asyncio.get_running_loop().time() + START_WAIT_S
    while True:
        try:
            snap = await structured_runtime.snapshot(key)
        except Exception:  # noqa: BLE001 — a transient read; the deadline bounds it
            snap = {}
        if snap.get("start_discarded") or snap.get("start_expired"):
            handoff.api_settle(key, "released")
            return
        launch = snap.get("launch") or {}
        if launch.get("state") == "ready" and not snap.get("pending_start"):
            await deliver(key)
            return
        if launch.get("state") == "failed" or snap.get("start_failed"):
            return  # the brief stays bound: Start again delivers it
        if asyncio.get_running_loop().time() >= deadline:
            log.info("handoff %s: start not ready in time; the brief stays bound", key)
            return
        await asyncio.sleep(START_POLL_S)


def after_start(key: str) -> bool:
    """The operator's Start of ``key`` was accepted: deliver its bound brief once the launch is
    ready, in the background (the route answers at launch, #1505). True when one was scheduled."""
    binding = handoff.api_binding(key)
    if binding is None or binding["delivery"] is not None:
        return False
    handoff.api_touch(key)
    task = asyncio.get_running_loop().create_task(_after_start(key))
    _TASKS.add(task)

    def _done(t: asyncio.Task) -> None:
        _TASKS.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.warning("handoff %s: deferred delivery failed", key, exc_info=t.exception())

    task.add_done_callback(_done)
    return True


async def drain() -> None:
    """Cancel deferred deliveries (shutdown, tests)."""
    for t in list(_TASKS):
        t.cancel()
    for t in list(_TASKS):
        with contextlib.suppress(BaseException):
            await t
