"""Input from a peer: a message it SUGGESTS, and a message it SENDS (#1246 §5 and §7, #2057).

Five verbs answer here (``verbs.py`` holds their validators and the table):

* ``input.suggest``: record a message for the operator to decide (grant level ``suggest``);
* ``input.suggestions``: the caller's own suggestions under one grant, ids and states, no text;
* ``input.withdraw``: take a pending suggestion back;
* ``input.send``: deliver a message as a turn (grant level ``send``);
* ``input.status``: what became of one of the caller's own messages, by its retry key (#2061):
  a read with no text. ``{"known": false}`` is "this grant has no such key NOW", never "it did
  not arrive": a request still on its way is admitted after this answer.

**A suggestion's text goes nowhere but the store and the operator's screen.** It is not passed to
the agent, to HQ, to a summary, to retrieval or to any model while it is pending or rejected. No
module outside ``federation/`` and ``routes/federation.py`` reads it (a source scan pins that).

**Only the operator approves.** :func:`decide_approve` MAKES an approval (``pending →
approving``, recording the final text and who approved) and is called from the operator route
and from nothing else. :func:`resume_approving` REPLAYS one already made: it submits only the
text and identity recorded by the approval, with the suggestion's own operation id, and is
called only from inside this package (the next approve call, the start-up sweep, the round that
settles approvals whose grant ended). Nothing else moves a suggestion toward delivery. Two
source scans pin both.

**Every delivery runs inside its grant's fence** (``fence.py``): inside it the grant is
re-checked, the session is checked not to be mission-held, the operation is looked up or minted
and recorded with its author, and ``structured_runtime.submit_turn`` is called. The fence is
released when the submit has returned or raised, never earlier: a caller that timed out or was
cancelled leaves the delivery and its fence as they are, and learns the outcome by retrying with
the same ``client_key``.

**One operation id per message.** ``input.send`` records ``(grant, client_key) → operation id``
BEFORE the submit; a suggestion's id is minted with the suggestion. A retry (a lost answer, a
reconnect, a restart on either side) submits again with the SAME id, which the runtime answers
from its journal. This module never retries on its own, whatever the receipt says.

**The answer is the runtime's delivery receipt** (``handoff``: ``sent``, ``queued``,
``uncertain``, ``not_sent``), or ``null`` for a runtime that gives none. Nothing is inferred
from the session's lifecycle ``state``, and ``uncertain`` is reported as it is.

Everything a peer is told about a failure is a short code; the reasons stay in this log.
"""

from __future__ import annotations

import asyncio
import logging

from .. import orchestrator_ledger
from .. import structured_runtime as runtime
from . import channel as ch
from . import fence, grants, notices, sharing, store

log = logging.getLogger(__name__)

#: The public path's per-turn text budget (``routes/public_api.MAX_TEXT``; a test pins them
#: equal). The channel also bounds a call's arguments as serialized (``channel.OPEN_INFO_MAX``).
MAX_TEXT = 20_000
#: How long a verb waits for its own delivery before it answers ``timeout``. Under the channel's
#: handler timeout, so the answer is this module's and the delivery is never cancelled by it.
ANSWER_WAIT_S = 25.0
#: How long the operator's approve waits for the delivery it started.
APPROVE_WAIT_S = 120.0
#: Deliveries one peer may have in flight in this process; the next is ``busy``.
DELIVERIES_PER_PEER = 4
LIST_MAX = 100
#: The runtime's closed receipt codes (``native_journal.HANDOFFS``; a test pins them equal).
HANDOFFS = frozenset({"queued", "uncertain", "sent", "not_sent"})
HELD = "held"
TIMEOUT = "timeout"
BUSY = "busy"

_AUDIT_TEXT = {
    "delivered": "A paired instance's message was delivered",
    "approved": "The operator approved a paired instance's suggestion",
    "rejected": "The operator rejected a paired instance's suggestion",
}


class InputError(Exception):
    """An operator request about a suggestion that is refused. ``status`` is the HTTP status;
    ``state`` is the suggestion's state when another decision won."""

    def __init__(self, code: str, detail: str, status: int = 422, state: str | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status
        self.state = state


# ── shared pieces ─────────────────────────────────────────────────────
def receipt(out: object) -> str | None:
    """The runtime's ``handoff`` as it reported it, or ``None`` when it gave none. NEVER derived
    from ``state`` or anything else in the answer."""
    value = out.get("handoff") if isinstance(out, dict) else None
    return value if isinstance(value, str) and value in HANDOFFS else None


def _audit(event: str, *, session_key: str, peer_id: str, actor: str, grant_id: str, **extra):
    """One ledger entry for something a peer caused. Ids and fixed words only: the message's
    text is never copied here. Blocking; a failure is logged and never undoes the effect."""
    operation_id = extra.get("operation_id") or ""
    record = {
        "id": f"peer-{event}:{operation_id}",
        "state": "observed",
        "source": "peer",
        "verb": f"peer_{event}",
        "session_id": session_key,
        "engine": session_key.partition(":")[0],
        "peer_id": peer_id,
        "actor": actor,
        "grant_id": grant_id,
        "rationale": _AUDIT_TEXT[event],
        "evidence": "none",
        **extra,
    }
    try:
        orchestrator_ledger.append(record)
    except Exception:  # noqa: BLE001 — the audit trail is another store
        log.error("federation: a peer %s could not be written to the ledger", event)


def _held(session_key: str) -> bool:
    """:func:`grants.mission_held`; an unreadable membership raises ``GrantError`` (503)."""
    return grants.mission_held(session_key)


async def _submit(session_key: str, operation_id: str, text: str, **kwargs) -> str | None:
    """THE submit, inside the fence: the runtime's own seam, with the operation id this side
    recorded. Returns the receipt. Called once per call of this module; never in a loop."""
    out = await runtime.submit_turn(session_key, operation_id=operation_id, text=text, **kwargs)
    return receipt(out)


# ── input.send ────────────────────────────────────────────────────────
def _admit_send(st: store.Store, peer_id: str, args: dict) -> tuple[store.Grant, str]:
    """INSIDE the fence: the grant again (state, expiry, level, revision, the peer, the
    session), no mission, then the operation looked up or minted and recorded with its author.
    Blocking."""
    grant = grants.authorize(
        st, args["grant_id"], peer_id, args["revision"], kind=grants.KIND_API, need="send"
    )
    if grant is None:
        raise ch.VerbError(sharing.NO_GRANT)
    try:
        if _held(grant.session_key):
            raise ch.VerbError(HELD)
        operation_id, _fresh = st.record_peer_turn(
            grant_id=grant.grant_id,
            session_key=grant.session_key,
            peer_id=peer_id,
            client_key=args["client_key"],
            actor=args["actor"],
            via="send",
        )
    except (grants.GrantError, store.StoreError):
        raise ch.VerbError(sharing.UNAVAILABLE) from None
    return grant, operation_id


def _record_delivery(st: store.Store, grant: store.Grant, operation_id: str, handoff, actor: str):
    """INSIDE the fence, after the submit returned: the receipt, and one ledger entry per turn."""
    try:
        first = st.note_peer_turn(operation_id, handoff=handoff)
    except store.StoreError:
        log.error("federation: a peer turn's receipt could not be recorded")
        first = True
    if first:
        _audit(
            "delivered",
            session_key=grant.session_key,
            peer_id=grant.peer_id,
            actor=actor,
            grant_id=grant.grant_id,
            operation_id=operation_id,
            handoff=handoff,
        )


async def _send(fed, peer_id: str, args: dict) -> dict:
    """Deliver one message under its grant's fence and answer the runtime's receipt."""
    st = fed.store

    async def body() -> dict:
        grant, operation_id = await asyncio.to_thread(_admit_send, st, peer_id, args)
        try:
            handoff = await _submit(
                grant.session_key,
                operation_id,
                args["text"],
                expected_revision=args["expected_revision"],
            )
        except runtime.StructuredError as exc:
            raise sharing._runtime_refusal(exc) from None
        await asyncio.to_thread(_record_delivery, st, grant, operation_id, handoff, args["actor"])
        return {"operation_id_known": True, "handoff": handoff}

    if fed.deliveries.get(peer_id, 0) >= DELIVERIES_PER_PEER:
        raise ch.VerbError(BUSY)
    fed.deliveries[peer_id] = fed.deliveries.get(peer_id, 0) + 1

    def finished(_task: asyncio.Future) -> None:
        # When the DELIVERY is done, not when its caller stopped waiting.
        fed.deliveries[peer_id] -= 1
        if not fed.deliveries[peer_id]:
            del fed.deliveries[peer_id]

    task = fence.start_fenced(st.directory, args["grant_id"], body)
    task.add_done_callback(finished)
    try:
        async with asyncio.timeout(ANSWER_WAIT_S):
            return await asyncio.shield(task)
    except TimeoutError:
        # The delivery goes on under its fence. The peer retries with the same client_key.
        raise ch.VerbError(TIMEOUT) from None
    except fence.FenceBusy:
        raise ch.VerbError(BUSY) from None


async def _refuse_held(session_key: str) -> None:
    try:
        held = await asyncio.to_thread(_held, session_key)
    except grants.GrantError:
        raise ch.VerbError(sharing.UNAVAILABLE) from None
    if held:
        raise ch.VerbError(HELD)


async def input_send(channel: ch.Channel, args: dict) -> dict:
    fed, peer_id = channel.context
    grant = await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API, need="send")
    await _refuse_held(grant.session_key)  # early and cheap; decided again inside the fence
    answer = await _send(fed, peer_id, args)
    # The second check, directly before the answer is returned: nothing may wait after it.
    await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API, need="send")
    return answer


# ── input.suggest / input.suggestions / input.withdraw ────────────────
async def input_suggest(channel: ch.Channel, args: dict) -> dict:
    fed, peer_id = channel.context
    grant = await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API, need="suggest")
    await _refuse_held(grant.session_key)
    try:
        # One transaction: the grant re-read, the retry key, the five-pending bound, the insert.
        suggestion, fresh = await asyncio.to_thread(
            fed.store.add_suggestion,
            grant_id=grant.grant_id,
            peer_id=peer_id,
            revision=args["revision"],
            client_key=args["client_key"],
            text=args["text"],
            actor=args["actor"],
        )
    except store.Full:
        raise ch.VerbError(BUSY) from None
    except store.NotGranted:
        raise ch.VerbError(sharing.NO_GRANT) from None
    except store.StoreError:
        raise ch.VerbError(sharing.UNAVAILABLE) from None
    if fresh:
        # In the background: the bell and the push never delay or decide the answer.
        fed.background(asyncio.to_thread(notices.announce, fed.store, suggestion))
    answer = {"suggestion_id": suggestion.suggestion_id, "state": suggestion.state}
    # The second check, directly before the answer is returned: nothing may wait after it.
    await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API, need="suggest")
    return answer


async def input_suggestions(channel: ch.Channel, args: dict) -> dict:
    fed, peer_id = channel.context
    grant = await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API)
    try:
        rows = await asyncio.to_thread(fed.store.suggestion_states, grant.grant_id)
    except store.StoreError:
        raise ch.VerbError(sharing.UNAVAILABLE) from None
    answer = {"suggestions": rows}
    # The second check, directly before the answer is returned: nothing may wait after it.
    await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API)
    return answer


async def input_status(channel: ch.Channel, args: dict) -> dict:
    """What became of ONE message this peer submitted under this grant, by its ``client_key``:
    a sent message's stored receipt, a suggestion's state, or ``{"known": False}``. The lookup is
    keyed by the grant the call was authorized for, so a key of another grant (this peer's or
    anyone's) is unknown here exactly as a key nobody used is. No text and no operation id. An API
    grant's verb: a terminal grant is refused like an unknown one."""
    fed, peer_id = channel.context
    grant = await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API)
    try:
        found = await asyncio.to_thread(
            fed.store.operation_status, grant.grant_id, args["client_key"]
        )
    except store.StoreError:
        raise ch.VerbError(sharing.UNAVAILABLE) from None
    answer = {"known": False} if found is None else {"known": True, **found}
    # The second check, directly before the answer is returned: nothing may wait after it.
    await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API)
    return answer


async def input_withdraw(channel: ch.Channel, args: dict) -> dict:
    fed, peer_id = channel.context
    grant = await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API)
    try:
        suggestion = await asyncio.to_thread(
            fed.store.withdraw_suggestion, args["suggestion_id"], grant_id=grant.grant_id
        )
    except store.StateConflict:
        raise ch.VerbError("conflict") from None  # decided first; `input.suggestions` says how
    except store.NotFound:
        raise ch.VerbError("not_found") from None
    except store.StoreError:
        raise ch.VerbError(sharing.UNAVAILABLE) from None
    fed.background(asyncio.to_thread(notices.retract, fed.store))
    answer = {"suggestion_id": suggestion.suggestion_id, "state": suggestion.state}
    # The second check, directly before the answer is returned: nothing may wait after it.
    await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API)
    return answer


# ── the operator's side ───────────────────────────────────────────────
def view(suggestion: store.Suggestion, peers: dict[str, store.Peer] | None = None) -> dict:
    """A suggestion as the OPERATOR's screen shows it, text included (rendered as plain text)."""
    peer = (peers or {}).get(suggestion.peer_id)
    return {
        "suggestion_id": suggestion.suggestion_id,
        "grant_id": suggestion.grant_id,
        "session_key": suggestion.session_key,
        "peer_id": suggestion.peer_id,
        "peer_name": peer.name if peer else "",
        "actor": suggestion.actor,
        "text": suggestion.text,
        "state": suggestion.state,
        "approved_text": suggestion.approved_text,
        "approved_by": suggestion.approved_by,
        "approved_at": suggestion.approved_at,
        "edited": suggestion.edited,
        "handoff": suggestion.handoff,
        "end_reason": suggestion.end_reason,
        "created_at": suggestion.created_at,
        "updated_at": suggestion.updated_at,
        "expires_at": suggestion.expires_at,
    }


def listing(st: store.Store, session_key: object = None) -> list[dict]:
    """The suggestions (of one session), newest first, for the operator. Blocking."""
    if session_key is not None and not isinstance(session_key, str):
        raise InputError("invalid", "session must be a session key")
    peers = {p.peer_id: p for p in st.list_peers()}
    rows = st.list_suggestions(session_key=session_key or None, limit=LIST_MAX)
    return [view(row, peers) for row in rows]


def _known(st: store.Store, suggestion_id: object) -> store.Suggestion:
    if not grants.valid_grant_id(suggestion_id):  # the same shape: `store.new_grant_id`
        raise InputError("not_found", "unknown suggestion", status=404)
    row = st.get_suggestion(suggestion_id)  # type: ignore[arg-type]
    if row is None:
        raise InputError("not_found", "unknown suggestion", status=404)
    return row


def _decided(state: str) -> InputError:
    return InputError("conflict", f"this suggestion is already {state}", status=409, state=state)


def _clean_text(text: object) -> str | None:
    if text is None:
        return None
    if not isinstance(text, str) or not text.strip():
        raise InputError("invalid", "text must be a non-empty message")
    if len(text) > MAX_TEXT:
        raise InputError("invalid", "that message is too long")
    return text


def _begin_approval(st: store.Store, suggestion_id: str, text: str | None, approver: str):
    """INSIDE the fence, step 1 of an approval: eligibility, then the ONE transaction that moves
    ``pending → approving`` and records the final text and who approved. Blocking."""
    row = _known(st, suggestion_id)
    if row.state != "pending":
        raise _decided(row.state)
    standing = grants.standing(st, row.grant_id, row.peer_id, "suggest", kind=grants.KIND_API)
    if standing == "unreadable":
        raise InputError("unavailable", "the grant can't be read right now — try again", 503)
    if standing != "ok":
        raise InputError(
            "ended", "the share this was suggested under has ended", status=409, state=row.state
        )
    try:
        if _held(row.session_key):
            raise InputError(
                HELD, "this session is part of a mission right now — approve it later", status=409
            )
    except grants.GrantError as exc:
        raise InputError(exc.code, exc.detail, exc.status) from None
    final = row.text if text is None else text
    try:
        made = st.begin_approval(
            suggestion_id, text=final, approver=approver, edited=final != row.text
        )
    except store.StateConflict as exc:
        raise _decided(exc.state) from None
    except store.NotGranted:
        raise InputError(
            "ended", "the share this was suggested under has ended", status=409
        ) from None
    except store.StoreError as exc:
        raise InputError("store", str(exc), exc.status) from None
    _audit(
        "approved",
        session_key=made.session_key,
        peer_id=made.peer_id,
        actor=made.actor,
        grant_id=made.grant_id,
        operation_id=made.operation_id,
        edited=made.edited,
    )
    notices.retract(st)
    return made


def _recheck(st: store.Store, suggestion_id: str) -> store.Suggestion | None:
    """INSIDE the fence, before a REPLAY submits: is the approval still good? ``None`` means go
    on. Otherwise the suggestion as it now stands: no longer ``approving`` (someone finished it),
    or ``withdrawn`` here because the grant ended, the peer is no longer trusted, the session was
    archived or a mission holds it. An approval does not outlive the grant it was given under.
    Something that cannot be read decides nothing: it raises, and the row stays ``approving``."""
    row = _known(st, suggestion_id)
    if row.state != "approving":
        return row
    standing = grants.standing(st, row.grant_id, row.peer_id, "suggest", kind=grants.KIND_API)
    if standing == "unreadable":
        raise InputError("unavailable", "the grant can't be read right now — try again", 503)
    reason = None if standing == "ok" else "grant_ended"
    if reason is None:
        try:
            if _held(row.session_key):
                reason = "mission_held"
        except grants.GrantError as exc:
            raise InputError(exc.code, exc.detail, exc.status) from None
    if reason is None:
        return None
    try:
        gone = st.withdraw_approving(suggestion_id, reason=reason)
    except store.StateConflict:
        return _known(st, suggestion_id)
    log.info("federation: an approved suggestion was withdrawn before delivery (%s)", reason)
    notices.retract(st)
    return gone


async def _deliver_approved(st: store.Store, suggestion_id: str) -> store.Suggestion:
    """INSIDE the fence, step 2 of an approval and the whole of a replay's delivery: submit
    exactly what step 1 recorded (its text, its author, the suggestion's own operation id), then
    ``approving → approved`` with the receipt."""
    row = await asyncio.to_thread(_known, st, suggestion_id)
    if row.state != "approving":
        return row

    def record_author() -> None:
        st.record_peer_turn(
            grant_id=row.grant_id,
            session_key=row.session_key,
            peer_id=row.peer_id,
            client_key=f"suggestion:{row.suggestion_id}",
            actor=row.actor,
            via="suggestion",
            operation_id=row.operation_id,
            edited=row.edited,
        )

    try:
        await asyncio.to_thread(record_author)
        handoff = await _submit(row.session_key, row.operation_id, row.approved_text or "")
    except runtime.StructuredError as exc:
        # Still `approving`: the next approve call replays it with the same operation id.
        raise InputError("runtime", exc.detail, exc.status) from None
    except store.StoreError as exc:
        raise InputError("store", str(exc), exc.status) from None

    def finish() -> store.Suggestion:
        try:
            done = st.finish_approval(suggestion_id, handoff=handoff)
        except store.StateConflict:
            return _known(st, suggestion_id)
        if st.note_peer_turn(row.operation_id, handoff=handoff):
            _audit(
                "delivered",
                session_key=row.session_key,
                peer_id=row.peer_id,
                actor=row.actor,
                grant_id=row.grant_id,
                operation_id=row.operation_id,
                handoff=handoff,
                edited=row.edited,
            )
        return done

    return await asyncio.to_thread(finish)


async def _fenced(st: store.Store, grant_id: str, body) -> store.Suggestion:
    try:
        async with asyncio.timeout(APPROVE_WAIT_S):
            return await fence.run_fenced(st.directory, grant_id, body)
    except TimeoutError:
        raise InputError(
            TIMEOUT,
            "the session did not take the message in time — approve again to see the outcome",
            status=504,
        ) from None
    except fence.FenceBusy:
        raise InputError(
            BUSY, "a delivery under this share is in flight — try again", status=503
        ) from None


async def resume_approving(fed, suggestion_id: str) -> store.Suggestion:
    """REPLAY an approval that was already made. Not a decision: it takes the grant's fence,
    re-checks inside it that the approval is still good (:func:`_recheck`), and submits only what
    the approval recorded, with the suggestion's own operation id (the runtime answers a repeat
    from its journal). Called only from inside this package."""
    st = fed.store
    row = await asyncio.to_thread(_known, st, suggestion_id)
    if row.state != "approving":
        return row

    async def body() -> store.Suggestion:
        settled = await asyncio.to_thread(_recheck, st, suggestion_id)
        if settled is not None:
            return settled
        return await _deliver_approved(st, suggestion_id)

    return await _fenced(st, row.grant_id, body)


async def decide_approve(
    fed, suggestion_id: object, *, text: object = None, approver: str
) -> store.Suggestion:
    """THE OPERATOR APPROVES. Called from the operator route (session + CSRF + Origin) and from
    nothing else.

    Two durable steps under the grant's fence: (1) ``pending → approving``, with the final text
    (the operator's edit, or the peer's) and who approved; (2) the turn is submitted with the
    suggestion's own operation id and the row becomes ``approved`` with the receipt. A crash or
    a lost answer between them leaves ``approving``, which the next approve call (and the
    start-up sweep) REPLAYS. A second approve never makes a second turn: the same text or none
    replays or reports; a DIFFERENT text is refused (409)."""
    st = fed.store
    final = _clean_text(text)
    row = await asyncio.to_thread(_known, st, suggestion_id)
    if row.state in ("approving", "approved"):
        if final is not None and final != row.approved_text:
            raise InputError(
                "conflict", "this suggestion is already approved", status=409, state=row.state
            )
        if row.state == "approved":
            return row
        return await resume_approving(fed, row.suggestion_id)
    if row.state != "pending":
        raise _decided(row.state)

    async def body() -> store.Suggestion:
        await asyncio.to_thread(_begin_approval, st, row.suggestion_id, final, approver)
        return await _deliver_approved(st, row.suggestion_id)

    return await _fenced(st, row.grant_id, body)


async def decide_reject(fed, suggestion_id: object, *, approver: str) -> store.Suggestion:
    """THE OPERATOR DECLINES: ``pending → rejected``. It narrows, so it needs no fence."""
    st = fed.store

    def reject() -> store.Suggestion:
        row = _known(st, suggestion_id)
        try:
            gone = st.reject_suggestion(row.suggestion_id, approver=approver)
        except store.StateConflict as exc:
            raise _decided(exc.state) from None
        except store.StoreError as exc:
            raise InputError("store", str(exc), exc.status) from None
        _audit(
            "rejected",
            session_key=gone.session_key,
            peer_id=gone.peer_id,
            actor=gone.actor,
            grant_id=gone.grant_id,
            operation_id=gone.operation_id,
        )
        notices.retract(st)
        return gone

    return await asyncio.to_thread(reject)


# ── recovery and housekeeping ─────────────────────────────────────────
async def resume_all(fed) -> int:
    """The start-up sweep: replay every approval a crash or a lost answer left ``approving``.
    Each goes through :func:`resume_approving` (its fence, its re-check). Never raises."""
    try:
        rows = await asyncio.to_thread(fed.store.approving_suggestions)
    except Exception:  # noqa: BLE001
        log.warning("federation: approvals to resume could not be read")
        return 0
    resumed = 0
    for row in rows:
        try:
            await resume_approving(fed, row.suggestion_id)
            resumed += 1
        except InputError as exc:
            log.info("federation: an approval was not resumed (%s)", exc.code)
        except Exception:
            log.exception("federation: resuming an approval failed")
    return resumed


async def settle_ended(fed) -> None:
    """The dialer round's share: approvals whose grant has ENDED are withdrawn (through the
    replay's own fenced re-check, which for an ended grant can only withdraw), and the bell rows
    of suggestions that are no longer pending go. Never raises."""
    try:
        st = fed.store

        def ended() -> list[str]:
            return [
                row.suggestion_id
                for row in st.approving_suggestions()
                if grants.standing(st, row.grant_id, row.peer_id, "suggest", kind=grants.KIND_API)
                == "gone"
            ]

        for suggestion_id in await asyncio.to_thread(ended):
            try:
                await resume_approving(fed, suggestion_id)
            except InputError as exc:
                log.info("federation: an ended approval was not settled (%s)", exc.code)
        await asyncio.to_thread(notices.retract, st)
    except Exception:
        log.exception("federation: settling suggestions failed")
