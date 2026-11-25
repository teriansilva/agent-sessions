"""Working together on a shared session: asking for more access, and answering a pending
decision (#1246 §6 and §7, #2059).

Four verbs answer here (``verbs.py`` holds their validators and the table):

* ``access.request``: record that the peer asks for a higher level. **Only a record.** Nothing
  in this module changes a grant: approving is the operator changing the grant through the
  grant routes (with ``accept_risk`` for ``send``), and that change marks the request
  ``granted``;
* ``access.status``: the grant's level and the caller's newest request, id and state;
* ``access.withdraw`` (#2062): the peer takes its own pending request back. It narrows;
* ``decision.answer``: answer ONE pending question or tool permission (grant level ``send``).

**An answer is bound to the exact request the peer was shown.** A request id names nothing by
itself: native request ids repeat from turn to turn and from connection to connection. So the
snapshot gives every answerable request an ``offer`` (``offers.token``: grant, session, turn,
request id and payload digest, hashed), ``decision.answer`` must send it back, and it is
compared with the request that is pending NOW before anything is recorded: another request of
the same id is ``not_pending``. A retry must carry the offer its stored row was recorded for
(else ``conflict``), a late answer is told who settled only the request its offer names, and
the stored digest travels into the runtime's own admission (``expected_digest``), which
refuses a request whose payload is not that one. The offer is not a credential: the grant and
its revision are the authority.

**A decision's identity is the owner's, and it is stored before the runtime is called.** The
peer names the request id and the offer its projection showed. Inside the grant's fence
(``fence.py``) the grant is re-checked, the session is checked not to be mission-held, the
request is resolved to its exact turn from the LIVE snapshot, and a ``peer_decisions`` row is
looked up or written: the ``decision_id`` (minted once), the turn, the request, the choice, who.
Only then is ``structured_runtime.decide`` called, with that stored identity. A retry with the
same ``client_key`` replays the stored decision (the runtime answers a repeat of a decision id
from its journal); the same key with another choice or request is ``conflict`` and nothing is
called.

**A peer gives one-time answers only.** Which decision kinds settle exactly one request is
``structured_runtime.decision_effect``: this module holds no list of kinds. Anything the runtime
classifies as widening (a standing grant written into the agent's own settings) or as stopping
the turn is ``owner_only``.

**The runtime settles a request once; its journal says who.** The first valid answer wins, the
owner's or a peer's, under whichever grant. WHO won is never taken from who is asking:
:func:`_author` reads the journal's own record of the decision the agent received
(``structured_runtime.decision_record``), maps its decision id to the ``peer_decisions`` row
that minted it (or to the owner when no row did: the operator's route is the only other caller
of ``decide``, which a source scan pins), and writes ``decision_authors`` once. Every later
reader gets that stored row. So a crash between "the runtime accepted" and "the author was
recorded" loses nothing: the next call reads the same journal. Where the journal cannot say,
nothing is recorded and nothing is guessed.

**Never a peer's:** a mission-held session's decisions (``held``), a background task's request,
a terminal session's (a terminal grant is ``no_grant`` to every verb here except
``access.request``, which answers ``not_possible``), and anything that is not a pending request
of the session (plan approval and mission completion are not requests, and have no verb).

Everything a peer is told about a failure is a short code; the reasons stay in this log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from .. import orchestrator_ledger
from .. import structured_runtime as runtime
from . import channel as ch
from . import fence, grants, inputs, notices, offers, sharing, store

log = logging.getLogger(__name__)

NOT_POSSIBLE = "not_possible"
OWNER_ONLY = "owner_only"
NOT_PENDING = "not_pending"
CONFLICT = "conflict"
INVALID = "invalid"
ACCEPTED = "accepted"
SETTLED = "settled"
UNCERTAIN = "uncertain"
#: A decision whose outcome was never recorded is looked at by the round once it is this old
#: (past any answer still in flight) and for at most this long.
OPEN_AFTER_S = 60.0
OPEN_FOR_S = 24 * 3600.0
_AUDIT_TEXT = "A paired instance answered a pending request"


async def _terminal_refusal(fed, peer_id: str, args: dict) -> None:
    """``access.request`` was refused as an API grant. A grant that passes the WHOLE per-call
    check as a terminal grant (#2056) answers ``not_possible``: it is view-only for good, so no
    higher level can ever be asked for (the contract's one exception to ``no_grant``). Anything
    else raises ``no_grant`` from that check, so a peer learns nothing about a grant it does not
    hold. Always raises."""
    await sharing._authorize(fed, peer_id, args, kind=grants.KIND_TERMINAL)
    raise ch.VerbError(NOT_POSSIBLE)


# ── access.request / access.status ────────────────────────────────────
async def access_request(channel: ch.Channel, args: dict) -> dict:
    fed, peer_id = channel.context
    try:
        grant = await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API)
    except ch.VerbError:
        await _terminal_refusal(fed, peer_id, args)
        raise
    try:
        # One transaction: the grant re-read, the one-pending rule, the bounds, the insert. It
        # writes the request and nothing else: no column of any grant.
        request, fresh = await asyncio.to_thread(
            fed.store.add_access_request,
            grant_id=grant.grant_id,
            peer_id=peer_id,
            revision=args["revision"],
            level=args["level"],
            note=args["note"],
        )
    except store.Invalid:
        raise ch.VerbError(INVALID) from None
    except store.RateLimited:
        raise ch.VerbError(inputs.BUSY) from None
    except store.NotGranted:
        raise ch.VerbError(sharing.NO_GRANT) from None
    except store.StoreError:
        raise ch.VerbError(sharing.UNAVAILABLE) from None
    if fresh:
        # In the background: the bell and the push never delay or decide the answer.
        fed.background(asyncio.to_thread(notices.announce_access, fed.store, request))
    answer = {"request_id": request.request_id, "level": request.level, "state": request.state}
    # The second check, directly before the answer is returned: nothing may wait after it.
    await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API)
    return answer


async def access_status(channel: ch.Channel, args: dict) -> dict:
    fed, peer_id = channel.context
    grant = await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API)
    try:
        request = await asyncio.to_thread(fed.store.access_status, grant.grant_id)
    except store.StoreError:
        raise ch.VerbError(sharing.UNAVAILABLE) from None
    answer = {"level": grant.level, "request": request}
    # The second check, directly before the answer is returned: nothing may wait after it.
    await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API)
    return answer


async def access_withdraw(channel: ch.Channel, args: dict) -> dict:
    """The peer takes its own pending request back (#2062). It narrows: a record changes
    state, no grant does. Answers what ``access.status`` answers afterwards."""
    fed, peer_id = channel.context
    grant = await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API)
    try:
        gone = await asyncio.to_thread(
            fed.store.withdraw_access_request,
            grant_id=grant.grant_id,
            peer_id=peer_id,
            revision=args["revision"],
        )
        request = await asyncio.to_thread(fed.store.access_status, grant.grant_id)
    except store.NotGranted:
        raise ch.VerbError(sharing.NO_GRANT) from None
    except store.StoreError:
        raise ch.VerbError(sharing.UNAVAILABLE) from None
    if gone:
        # Its bell row on this instance goes; in the background, as when it was raised.
        fed.background(asyncio.to_thread(notices.retract, fed.store))
    answer = {"level": grant.level, "request": request}
    # The second check, directly before the answer is returned: nothing may wait after it.
    await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API)
    return answer


# ── the operator's side of an access request ──────────────────────────
def access_view(request: store.AccessRequest, peers: dict[str, store.Peer] | None = None) -> dict:
    """An access request as the OPERATOR's screen shows it, the peer's note included (plain
    text)."""
    peer = (peers or {}).get(request.peer_id)
    return {
        "request_id": request.request_id,
        "grant_id": request.grant_id,
        "session_key": request.session_key,
        "peer_id": request.peer_id,
        "peer_name": peer.name if peer else "",
        "level": request.level,
        "note": request.note,
        "state": request.state,
        "end_reason": request.end_reason,
        "created_at": request.created_at,
        "updated_at": request.updated_at,
        "expires_at": request.expires_at,
    }


def access_listing(st: store.Store, session_key: object = None) -> list[dict]:
    """The access requests (of one session), newest first, for the operator. Blocking."""
    if session_key is not None and not isinstance(session_key, str):
        raise inputs.InputError(INVALID, "session must be a session key")
    peers = {p.peer_id: p for p in st.list_peers()}
    rows = st.list_access_requests(session_key=session_key or None, limit=inputs.LIST_MAX)
    return [access_view(row, peers) for row in rows]


def decline_access(st: store.Store, request_id: object) -> store.AccessRequest:
    """THE OPERATOR DECLINES a request: ``pending → declined``. It narrows. Blocking. (There is
    no ``approve`` here on purpose: approving is a grant change, in the grant routes.)"""
    if not grants.valid_grant_id(request_id):  # the same shape: `store.new_grant_id`
        raise inputs.InputError("not_found", "unknown access request", status=404)
    try:
        gone = st.decline_access_request(request_id)  # type: ignore[arg-type]
    except store.StateConflict as exc:
        raise inputs.InputError(
            CONFLICT, f"this request is already {exc.state}", status=409, state=exc.state
        ) from None
    except store.NotFound:
        raise inputs.InputError("not_found", "unknown access request", status=404) from None
    notices.retract(st)
    return gone


# ── decision.answer ───────────────────────────────────────────────────
def canonical(args: dict) -> str:
    """What the peer chose, as it is stored and compared: the kind and, for a question, the
    option indexes per question. Never a label, a payload or any other text."""
    doc: dict = {"choice": args["choice"]}
    if args.get("options") is not None:
        doc["options"] = [sorted(picked) for picked in args["options"]]
    return json.dumps(doc, separators=(",", ":"), sort_keys=True)


def _resolve(
    snap: dict, request_id: str, turn_id: str | None = None, digest: str | None = None
) -> dict | None:
    """THE pending request a peer named, from the live snapshot, with its exact turn. ``None``
    when it is not pending there, when it belongs to a background task, or when the id names
    more than one request (nothing is guessed)."""
    found = [
        q
        for q in snap.get("pending_requests") or []
        if not q.get("background_task")
        and q.get("request_id") == request_id
        and isinstance(q.get("turn_id"), str)
        and (turn_id is None or q.get("turn_id") == turn_id)
        and (digest is None or (q.get("payload_digest") or "") == digest)
    ]
    return found[0] if len(found) == 1 else None


def _offer(grant: store.Grant, turn_id: str, request_id: str, digest: str) -> str | None:
    """The offer token of one exact request under this grant, from its stored identity."""
    return offers.token(
        grant.grant_id,
        grant.session_key,
        {"turn_id": turn_id, "request_id": request_id, "payload_digest": digest},
    )


def _translate(request: dict, choice: dict) -> tuple[str, list | None]:
    """The runtime's ``(decision, answers)`` for a stored choice against the LIVE request. The
    kind must be one the runtime offers for this request and one a peer may give; a question's
    indexes become the request's own labels, one per question unless it is multi-select."""
    decision, options = choice["choice"], choice.get("options")
    if not offers.peer_may(decision):
        raise ch.VerbError(OWNER_ONLY)
    if decision not in (request.get("choices") or []):
        raise ch.VerbError(INVALID)
    if runtime.decision_effect(decision) != "answer":
        if options is not None:
            raise ch.VerbError(INVALID)
        return decision, None
    questions = offers.questions(request)
    if questions is None or options is None or len(options) != len(questions):
        raise ch.VerbError(INVALID)
    answers = []
    for question, picked in zip(questions, options, strict=True):
        labels = [o["label"] for o in question["options"]]
        if any(i >= len(labels) for i in picked):
            raise ch.VerbError(INVALID)
        if len(picked) != 1 and not question.get("multiSelect", False):
            raise ch.VerbError(INVALID)
        answers.append({"selected": [labels[i] for i in sorted(picked)]})
    return decision, answers


def _actor(row: store.PeerDecision) -> str:
    """The runtime's ``user`` for a peer's decision: part of the decision's durable identity
    in the journal, so it is derived from the stored row and never from the call."""
    return row.principal


def author_view(entry: dict | None, *, viewer: str | None = None) -> dict | None:
    """Who settled a request, as a reader is told: the owner, or a peer by the display name
    THIS instance shows it under (never its id). ``mine`` for the peer that reads its own."""
    if not entry:
        return None
    if entry["author"] == store.AUTHOR_OWNER:
        return {"via": "owner", "name": None, "mine": False}
    view = {"via": "peer", "name": entry.get("name") or "", "mine": entry["author"] == viewer}
    if entry.get("actor"):
        view["actor"] = entry["actor"]
    return view


def _named(st: store.Store, entry: dict) -> dict:
    """A stored author row with the peer's display name here."""
    if entry["author"] == store.AUTHOR_OWNER:
        return {**entry, "name": ""}
    peer = st.get(entry["author"][len(store.PRINCIPAL_PEER) :])
    return {**entry, "name": peer.name if peer is not None else ""}


def _write_author(st: store.Store, session_key: str, turn_id: str, record: dict) -> dict:
    """Record the author of the decision the journal says the agent RECEIVED. Its decision id
    is a peer's when a ``peer_decisions`` row minted it; otherwise it came through this
    instance's own decide route. Returns the STORED row (the first write stays). Blocking."""
    peer_row = st.peer_decision(record["decision_id"])
    stored = st.record_decision_author(
        session_key=session_key,
        turn_id=turn_id,
        request_id=record["request_id"],
        digest=record["digest"],
        author=peer_row.principal if peer_row is not None else store.AUTHOR_OWNER,
        actor=peer_row.actor if peer_row is not None else "",
        decision_id=record["decision_id"],
        kind=runtime.decision_effect(record["decision"]),
    )
    return _named(st, stored)


async def _author(
    st: store.Store, session_key: str, turn_id: str, request_id: str, digest: str | None
) -> tuple[dict | None, bool]:
    """``(the stored author of one request, whether its outcome is uncertain)``.

    The store first. Otherwise the session's journal: the decision the agent received
    (``handoff == "sent"``) for exactly this turn and request (and digest, when the caller
    knows it) is the settlement, and its author is written once. A decision that was claimed
    and whose handoff is not known decides nothing: ``(None, True)``. Nothing found is
    ``(None, False)``: unknown, never "the caller"."""
    if digest is not None:
        stored = await asyncio.to_thread(
            st.decision_author, session_key, turn_id, request_id, digest
        )
        if stored is not None:
            return await asyncio.to_thread(_named, st, stored), False
    try:
        records = await runtime.decision_record(session_key, turn_id=turn_id, request_id=request_id)
    except runtime.StructuredError:
        return None, True  # the journal cannot be read: unknown, and said so
    records = [r for r in records if digest is None or r["digest"] == digest]
    sent = [r for r in records if r["handoff"] == "sent"]
    if not sent:
        return None, any(r["handoff"] == "uncertain" for r in records)
    # The journal's order: the first decision the agent received settled the request.
    record = {**sent[0], "request_id": request_id}
    try:
        return await asyncio.to_thread(_write_author, st, session_key, turn_id, record), False
    except store.StoreError:
        return None, True


def _audit(row: store.PeerDecision) -> None:
    """One ledger entry for a decision a peer gave. Ids, the choice's KIND and fixed words:
    never the tool payload, a question's text or an option's label. Blocking; a failure is
    logged and never undoes the effect."""
    record = {
        "id": f"peer-decision:{row.decision_id}",
        "state": "observed",
        "source": "peer",
        "verb": "peer_decision",
        "session_id": row.session_key,
        "engine": row.session_key.partition(":")[0],
        "peer_id": row.peer_id,
        "actor": row.actor,
        "grant_id": row.grant_id,
        "request_id": row.request_id,
        "decision_id": row.decision_id,
        "decision_kind": row.kind,
        "rationale": _AUDIT_TEXT,
        "evidence": "none",
    }
    try:
        orchestrator_ledger.append(record)
    except Exception:  # noqa: BLE001 — the audit trail is another store
        log.error("federation: a peer decision could not be written to the ledger")


def _admit(st: store.Store, peer_id: str, args: dict) -> store.Grant:
    """INSIDE the fence: the grant again (state, expiry, level, revision, the peer, the
    session), and no mission. Blocking."""
    grant = grants.authorize(
        st, args["grant_id"], peer_id, args["revision"], kind=grants.KIND_API, need="send"
    )
    if grant is None:
        raise ch.VerbError(sharing.NO_GRANT)
    try:
        if grants.mission_held(grant.session_key):
            raise ch.VerbError(inputs.HELD)
    except grants.GrantError:
        raise ch.VerbError(sharing.UNAVAILABLE) from None
    return grant


def _record(
    st: store.Store, grant: store.Grant, peer_id: str, args: dict, request: dict, kind: str
):
    """INSIDE the fence, BEFORE the runtime is called: the decision's identity, looked up or
    minted and written with who gave it. Blocking."""
    try:
        return st.record_peer_decision(
            grant_id=grant.grant_id,
            session_key=grant.session_key,
            peer_id=peer_id,
            client_key=args["client_key"],
            actor=args["actor"],
            turn_id=request["turn_id"],
            request_id=request["request_id"],
            digest=request.get("payload_digest") or "",
            choice=canonical(args),
            kind=kind,
        )[0]
    except store.NotGranted:
        raise ch.VerbError(sharing.NO_GRANT) from None
    except store.StoreError:
        raise ch.VerbError(sharing.UNAVAILABLE) from None


async def _snapshot(session_key: str) -> dict:
    try:
        return await runtime.snapshot(session_key)
    except runtime.StructuredError as exc:
        raise sharing._runtime_refusal(exc) from None


async def _finish(st: store.Store, row: store.PeerDecision, snap: dict) -> dict:
    """INSIDE the fence, with the stored identity: call the runtime while the request is still
    pending (a repeat of the decision id is answered from its journal, with no second effect),
    then report who the journal says settled it."""
    request = _resolve(snap, row.request_id, row.turn_id, row.digest)
    refused = called = False
    if request is not None:
        decision, answers = _translate(request, json.loads(row.choice))
        try:
            await runtime.decide(
                row.session_key,
                turn_id=row.turn_id,
                request_id=row.request_id,
                decision_id=row.decision_id,
                decision=decision,
                user=_actor(row),
                answers=answers,
                # The exact request this row was recorded for: the runtime refuses one whose
                # payload is another (a request replaced under the same id and turn after
                # the snapshot above was read).
                **({"expected_digest": row.digest} if row.digest else {}),
            )
        except runtime.StructuredError as exc:
            if exc.status != 409:
                raise sharing._runtime_refusal(exc) from None
            # Settled first by someone else, or the conversation moved. The journal says which.
            refused = True
        called = not refused
    author, uncertain = await _author(st, row.session_key, row.turn_id, row.request_id, row.digest)
    if author is None:
        if uncertain or called:
            # The runtime took the call and its journal does not (yet) say the agent received
            # it: reported as it is. The same key asks again; nothing here claims an author.
            return {"outcome": UNCERTAIN, "author": None}
        again = await _snapshot(row.session_key)
        if refused and _resolve(again, row.request_id, row.turn_id, row.digest):
            raise ch.VerbError(inputs.BUSY)  # still pending: the same key may be sent again
        await asyncio.to_thread(st.note_peer_decision, row.decision_id, "not_pending")
        raise ch.VerbError(NOT_PENDING)
    mine = author["decision_id"] == row.decision_id
    first = await asyncio.to_thread(
        st.note_peer_decision, row.decision_id, "accepted" if mine else "settled"
    )
    if mine and first:
        await asyncio.to_thread(_audit, row)
    return {
        "outcome": ACCEPTED if mine else SETTLED,
        "author": author_view(author, viewer=row.principal),
    }


def _current_turn(snap: dict) -> str | None:
    """The one turn a request the peer just saw can belong to: the turn in flight, else the
    newest turn. Native request ids repeat from turn to turn, so an older turn's request of
    the same id is another request, and is never answered for."""
    active = snap.get("active_turn")
    if isinstance(active, str):
        return active
    turns = [t.get("turn_id") for t in snap.get("turns") or [] if isinstance(t, dict)]
    return turns[-1] if turns and isinstance(turns[-1], str) else None


async def _late(st: store.Store, snap: dict, grant: store.Grant, args: dict, viewer: str):
    """A decision for a request that is not pending and that this caller never recorded: say
    who settled it when that is known for the session's CURRENT turn (the store, then the
    journal), else ``not_pending``.

    Only ever the author of THE request the peer was shown: a record counts when the offer
    recomputed from its own turn, request id and digest is the one the peer sent. A request of
    the same id with another payload, or of another turn, is another request, and who settled
    it is not this caller's to learn. A journal that cannot be read answers nothing."""
    session_key, request_id = grant.session_key, args["request_id"]
    turn_id = _current_turn(snap)
    if turn_id is None:
        raise ch.VerbError(NOT_PENDING)

    def shown(digest: object) -> bool:
        return (
            isinstance(digest, str)
            and _offer(grant, turn_id, request_id, digest) == (args["offer"])
        )

    stored = await asyncio.to_thread(st.decision_author_in_turn, session_key, turn_id, request_id)
    if stored is not None and shown(stored["digest"]):
        stored = await asyncio.to_thread(_named, st, stored)
    else:
        try:
            records = await runtime.decision_record(
                session_key, turn_id=turn_id, request_id=request_id
            )
        except runtime.StructuredError:
            raise ch.VerbError(NOT_PENDING) from None
        digest = next((r["digest"] for r in records if shown(r["digest"])), None)
        if digest is None:
            raise ch.VerbError(NOT_PENDING)
        stored, _uncertain = await _author(st, session_key, turn_id, request_id, digest)
    if stored is None:
        raise ch.VerbError(NOT_PENDING)
    return {"outcome": SETTLED, "author": author_view(stored, viewer=viewer)}


async def _decide(fed, peer_id: str, args: dict) -> dict:
    """Answer one request under its grant's fence."""
    st = fed.store

    async def body() -> dict:
        grant = await asyncio.to_thread(_admit, st, peer_id, args)
        snap = await _snapshot(grant.session_key)
        row = await asyncio.to_thread(st.get_peer_decision, grant.grant_id, args["client_key"])
        if row is None:
            request = _resolve(snap, args["request_id"])
            if request is None:
                return await _late(st, snap, grant, args, store.principal_of(peer_id))
            if offers.token(grant.grant_id, grant.session_key, request) != args["offer"]:
                # A request of this id is pending, and it is not the one the peer was shown
                # (another turn, or another payload): nothing is recorded, nothing is called.
                raise ch.VerbError(NOT_PENDING)
            decision, _answers = _translate(request, json.loads(canonical(args)))
            row = await asyncio.to_thread(
                _record, st, grant, peer_id, args, request, runtime.decision_effect(decision)
            )
        if (row.request_id, row.choice, _offer(grant, row.turn_id, row.request_id, row.digest)) != (
            args["request_id"],
            canonical(args),
            args["offer"],
        ):
            # The same retry key with another request, offer or choice: refused, never applied.
            raise ch.VerbError(CONFLICT)
        return await _finish(st, row, snap)

    if fed.deliveries.get(peer_id, 0) >= inputs.DELIVERIES_PER_PEER:
        raise ch.VerbError(inputs.BUSY)
    fed.deliveries[peer_id] = fed.deliveries.get(peer_id, 0) + 1

    def finished(_task: asyncio.Future) -> None:
        fed.deliveries[peer_id] -= 1
        if not fed.deliveries[peer_id]:
            del fed.deliveries[peer_id]

    task = fence.start_fenced(st.directory, args["grant_id"], body)
    task.add_done_callback(finished)
    try:
        async with asyncio.timeout(inputs.ANSWER_WAIT_S):
            return await asyncio.shield(task)
    except TimeoutError:
        # The decision goes on under its fence. The peer retries with the same client_key.
        raise ch.VerbError(inputs.TIMEOUT) from None
    except fence.FenceBusy:
        raise ch.VerbError(inputs.BUSY) from None


async def decision_answer(channel: ch.Channel, args: dict) -> dict:
    fed, peer_id = channel.context
    # An API grant at `send`: a terminal grant (view only, #2056) is `no_grant` here.
    grant = await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API, need="send")
    if not offers.peer_may(args["choice"]):
        # Widening (a standing grant), a stop, or a kind the runtime does not know: the owner's.
        raise ch.VerbError(OWNER_ONLY)
    await inputs._refuse_held(grant.session_key)  # early and cheap; decided again in the fence
    answer = await _decide(fed, peer_id, args)
    # The second check, directly before the answer is returned: nothing may wait after it.
    await sharing._authorize(fed, peer_id, args, kind=grants.KIND_API, need="send")
    return answer


# ── the owner's own answer, and recovery ──────────────────────────────
async def owner_decided(st: store.Store, session_key: str, turn_id: str, request_id: str) -> None:
    """The operator's own decide route accepted a decision: record who settled the request,
    from the same journal record every other path reads. Only for a session this instance
    ever shared (nobody else could ask), and never creating the store. Never raises: the
    operator's decision is made whatever becomes of its attribution."""
    try:
        if not await asyncio.to_thread(st.session_shared_ever, session_key):
            return
        await _author(st, session_key, turn_id, request_id, None)
    except Exception:  # noqa: BLE001 — attribution is a record, never a gate
        log.warning("federation: a decision's author could not be recorded", exc_info=True)


async def settle_open(fed) -> int:
    """The round's share: peer decisions whose outcome was never recorded (a crash, a lost
    answer) get the outcome the journal holds. It NEVER calls ``decide``: a decision is
    delivered by its caller's retry and by nothing else. Never raises."""
    settled = 0
    try:
        st, now = fed.store, time.time()
        for row in await asyncio.to_thread(st.open_peer_decisions):
            if not OPEN_AFTER_S <= now - row.created_at <= OPEN_FOR_S:
                continue
            try:
                snap = await runtime.snapshot(row.session_key)
            except runtime.StructuredError:
                continue  # unreadable decides nothing
            if _resolve(snap, row.request_id, row.turn_id, row.digest) is not None:
                continue  # still pending: its caller's retry delivers it
            author, uncertain = await _author(
                st, row.session_key, row.turn_id, row.request_id, row.digest
            )
            if author is None and uncertain:
                continue
            mine = author is not None and author["decision_id"] == row.decision_id
            outcome = "not_pending" if author is None else ("accepted" if mine else "settled")
            if await asyncio.to_thread(st.note_peer_decision, row.decision_id, outcome):
                settled += 1
                if mine:
                    await asyncio.to_thread(_audit, row)
    except Exception:
        log.exception("federation: settling open peer decisions failed")
    return settled


def pending_marks(snap: dict) -> frozenset:
    """The requests of a snapshot a ``send`` peer could answer: what the owner's round compares
    to decide whether to hint ``decision.changed``."""
    return frozenset(
        (q.get("turn_id"), q.get("request_id"))
        for q in snap.get("pending_requests") or []
        if not q.get("background_task")
    )
