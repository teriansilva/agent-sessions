"""The closed verb table of the peer channel (#1246 §3).

Every name a peer may call is listed here with the modes it is valid in and a validator that
returns cleaned arguments or raises :class:`channel.BadArgs`. Nothing else is callable: the
channel resets the stream for an unknown name, a name outside its mode, or refused arguments.

A handler receives the :class:`channel.Channel` it was called on; ``channel.context`` is what the
service attached when it built the channel (``(federation, pairing)`` in ``pair`` mode,
``(federation, peer_id)`` in ``peer`` mode). Handlers never take a path, a URL to fetch, a command
or a session key from a peer's arguments: a shared session is addressed by its **grant id**
(#2054), and the handlers behind those verbs live in :mod:`sharing`.

Input (#2057, handlers in :mod:`inputs`): ``input.suggest``, ``input.suggestions``,
``input.withdraw``, ``input.send`` and the read ``input.status`` (#2061). A peer never names an
operation id (the owner mints them) and **no verb changes a grant**: none extends, raises or
re-creates one.

Working together (#2059, handlers in :mod:`together` and :mod:`notes`): ``access.request``,
``access.status`` and ``access.withdraw`` (a request is a record; it changes no grant),
``decision.answer`` (the peer
names the request id it saw and a decision KIND, never a turn, a decision id, a payload or an
option's text), and the five ``note.*`` verbs. ``note.changed`` and ``decision.changed`` are
the OWNER's hints to the receiver: a grant id and nothing else, and the receiver reads the
owner before it believes anything.
"""

from __future__ import annotations

import asyncio
import math
import re
import uuid

from .. import chat_store, native_questions
from . import channel as ch
from . import endpoints as ep
from . import frames, grants, inbox, inputs, notes, offers, sharing, store, terminal, together

APP_PROTOCOL = 1


def _only(args: dict, *allowed: str) -> None:
    if set(args) - set(allowed):
        raise ch.BadArgs("unknown argument")


def _no_args(args: dict) -> dict:
    _only(args)
    return {}


def _endpoints(args: dict) -> list[dict]:
    raw = args.get("endpoints", [])
    if not isinstance(raw, list) or len(raw) > ep.ENDPOINTS_MAX * 4:
        raise ch.BadArgs("endpoints")
    return ep.clean(raw)


def _pair_hello_args(args: dict) -> dict:
    _only(args, "name", "endpoints")
    try:
        name = store.clean_name(args.get("name", ""), fallback="BattleLab")
    except store.Invalid as exc:
        raise ch.BadArgs("name") from exc
    return {"name": name, "endpoints": _endpoints(args)}


def _peer_hello_args(args: dict) -> dict:
    _only(args, "endpoints")
    return {"endpoints": _endpoints(args)}


def _grant_ref(args: dict) -> dict:
    """The two arguments every session verb carries: which grant, and the revision the caller was
    told. Shapes only; whether the grant serves this caller is the handler's per-call check."""
    grant_id, revision = args.get("grant_id"), args.get("revision")
    if not grants.valid_grant_id(grant_id) or not grants.valid_revision(revision):
        raise ch.BadArgs("grant")
    return {"grant_id": grant_id, "revision": revision}


def _int(args: dict, name: str, *, default: int, low: int, high: int) -> int:
    value = args.get(name, default)
    if type(value) is not int or not low <= value <= high:
        raise ch.BadArgs(name)
    return value


def _share_list_args(args: dict) -> dict:
    _only(args, "after", "limit")
    after = args.get("after")
    if after is not None and not grants.valid_grant_id(after):
        raise ch.BadArgs("after")
    limit = _int(args, "limit", default=sharing.LIST_LIMIT_MAX, low=1, high=sharing.LIST_LIMIT_MAX)
    return {"after": after, "limit": limit}


def _snapshot_args(args: dict) -> dict:
    _only(args, "grant_id", "revision")
    return _grant_ref(args)


def _wait(args: dict) -> float:
    """How long a call may be held: a finite number of seconds, at most ``WAIT_MAX_S``."""
    wait = args.get("wait", 0)
    if isinstance(wait, bool) or not isinstance(wait, int | float):
        raise ch.BadArgs("wait")
    if not math.isfinite(wait) or not 0 <= wait <= sharing.WAIT_MAX_S:
        raise ch.BadArgs("wait")
    return float(wait)


def _events_args(args: dict) -> dict:
    _only(args, "grant_id", "revision", "after", "limit", "wait")
    wait = _wait(args)
    return {
        **_grant_ref(args),
        "after": _int(args, "after", default=0, low=0, high=grants.REVISION_MAX),
        "limit": _int(
            args, "limit", default=sharing.EVENTS_LIMIT_MAX, low=1, high=sharing.EVENTS_LIMIT_MAX
        ),
        "wait": wait,
    }


def _frame_args(args: dict) -> dict:
    """``terminal.frame``: the grant, the fingerprint of the frame the caller already has (or
    none) and how long it may be held. Nothing else: no key, no size, no input."""
    _only(args, "grant_id", "revision", "have", "wait")
    have = args.get("have")
    if have is not None and not frames.valid_fingerprint(have):
        raise ch.BadArgs("have")
    return {**_grant_ref(args), "have": have, "wait": _wait(args)}


def _history_args(args: dict) -> dict:
    _only(args, "grant_id", "revision", "before")
    before = args.get("before")
    if not chat_store.valid_turn_id(before):
        raise ch.BadArgs("before")
    return {**_grant_ref(args), "before": before}


def _client_key(args: dict) -> str:
    """The caller's retry key: a UUID in its canonical form, kept exactly as it was sent."""
    raw = args.get("client_key")
    if not isinstance(raw, str) or len(raw) != 36:
        raise ch.BadArgs("client_key")
    try:
        canonical = str(uuid.UUID(raw))
    except ValueError:
        raise ch.BadArgs("client_key") from None
    if canonical != raw:
        raise ch.BadArgs("client_key")
    return raw


def _text(args: dict) -> str:
    text = args.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > inputs.MAX_TEXT:
        raise ch.BadArgs("text")
    return text


def _actor(args: dict) -> str:
    """An optional display label for who wrote the message on the peer's side. Cleaned like a
    peer name; shown and audited, never authority. Absent is empty."""
    raw = args.get("actor")
    if raw is None or raw == "":
        return ""
    if not isinstance(raw, str) or len(raw) > 4 * store.NAME_MAX:
        raise ch.BadArgs("actor")
    try:
        return store.clean_name(raw)
    except store.Invalid:
        raise ch.BadArgs("actor") from None


def _suggest_args(args: dict) -> dict:
    _only(args, "grant_id", "revision", "client_key", "text", "actor")
    return {
        **_grant_ref(args),
        "client_key": _client_key(args),
        "text": _text(args),
        "actor": _actor(args),
    }


def _send_args(args: dict) -> dict:
    _only(args, "grant_id", "revision", "client_key", "text", "actor", "expected_revision")
    expected = args.get("expected_revision")
    if expected is not None and (type(expected) is not int or not 0 <= expected <= 2**53):
        raise ch.BadArgs("expected_revision")
    return {
        **_grant_ref(args),
        "client_key": _client_key(args),
        "text": _text(args),
        "actor": _actor(args),
        "expected_revision": expected,
    }


def _suggestions_args(args: dict) -> dict:
    _only(args, "grant_id", "revision")
    return _grant_ref(args)


def _status_args(args: dict) -> dict:
    """``input.status``: the grant and ONE retry key. Nothing else is accepted: no text, no
    operation id, no suggestion id, no other grant."""
    _only(args, "grant_id", "revision", "client_key")
    return {**_grant_ref(args), "client_key": _client_key(args)}


def _withdraw_args(args: dict) -> dict:
    _only(args, "grant_id", "revision", "suggestion_id")
    suggestion_id = args.get("suggestion_id")
    if not grants.valid_grant_id(suggestion_id):  # the same shape: `store.new_grant_id`
        raise ch.BadArgs("suggestion_id")
    return {**_grant_ref(args), "suggestion_id": suggestion_id}


def _access_request_args(args: dict) -> dict:
    _only(args, "grant_id", "revision", "level", "note")
    level = args.get("level")
    # A level above the lowest: asking for `view` is asking for nothing.
    if not isinstance(level, str) or level not in grants.LEVELS[1:]:
        raise ch.BadArgs("level")
    note = args.get("note")
    if note is None or note == "":
        note = ""
    elif not notes.valid_text(note) or len(note) > store.ACCESS_NOTE_MAX:
        raise ch.BadArgs("note")
    return {**_grant_ref(args), "level": level, "note": note}


def _access_status_args(args: dict) -> dict:
    _only(args, "grant_id", "revision")
    return _grant_ref(args)


#: A request id as a runtime mints one: short, printable, one line. It is only ever COMPARED
#: with the ids of the live snapshot's pending requests; it is never a key, a path or a query.
REQUEST_ID_MAX = 128
_REQUEST_ID_RE = re.compile(r"\A[\x21-\x7e]{1,128}\Z")
#: A decision kind's shape. Which kinds exist, and what each does, is the runtime's.
_CHOICE_RE = re.compile(r"\A[a-z][a-z_]{0,31}\Z")


def _options(args: dict) -> list[list[int]] | None:
    """For a question: the picked option INDEXES, one list per question. Never a label."""
    raw = args.get("options")
    if raw is None:
        return None
    if not isinstance(raw, list) or not 1 <= len(raw) <= native_questions.CLAUDE_MAX_QUESTIONS:
        raise ch.BadArgs("options")
    out = []
    for picked in raw:
        if not isinstance(picked, list) or not 1 <= len(picked) <= native_questions.MAX_OPTIONS:
            raise ch.BadArgs("options")
        if any(type(i) is not int or not 0 <= i < native_questions.MAX_OPTIONS for i in picked):
            raise ch.BadArgs("options")
        if len(set(picked)) != len(picked):
            raise ch.BadArgs("options")
        out.append(sorted(picked))
    return out


def _decision_args(args: dict) -> dict:
    _only(
        args,
        "grant_id",
        "revision",
        "request_id",
        "offer",
        "choice",
        "options",
        "client_key",
        "actor",
    )
    request_id, choice = args.get("request_id"), args.get("choice")
    # The offer the peer was shown for this request (`offers.token`): required, by shape here.
    if not offers.valid_token(args.get("offer")):
        raise ch.BadArgs("offer")
    if not isinstance(request_id, str) or _REQUEST_ID_RE.fullmatch(request_id) is None:
        raise ch.BadArgs("request_id")
    if not isinstance(choice, str) or _CHOICE_RE.fullmatch(choice) is None:
        raise ch.BadArgs("choice")
    return {
        **_grant_ref(args),
        "request_id": request_id,
        "offer": args["offer"],
        "choice": choice,
        "options": _options(args),
        "client_key": _client_key(args),
        "actor": _actor(args),
    }


def _note_text(args: dict) -> str:
    text = args.get("text")
    if not notes.valid_text(text):
        raise ch.BadArgs("text")
    return text


def _note_id(args: dict) -> str:
    note_id = args.get("note_id")
    if not grants.valid_grant_id(note_id):  # the same shape: `store.new_grant_id`
        raise ch.BadArgs("note_id")
    return note_id


def _note_list_args(args: dict) -> dict:
    _only(args, "grant_id", "revision", "after_seq", "limit", "cursor")
    cursor = args.get("cursor")
    if cursor is not None and not grants.valid_grant_id(cursor):
        raise ch.BadArgs("cursor")
    return {
        **_grant_ref(args),
        "after_seq": _int(args, "after_seq", default=0, low=0, high=grants.REVISION_MAX),
        "limit": _int(args, "limit", default=notes.PAGE_MAX, low=0, high=notes.PAGE_MAX),
        "cursor": cursor,
    }


def _note_add_args(args: dict) -> dict:
    _only(args, "grant_id", "revision", "client_key", "text", "actor")
    return {
        **_grant_ref(args),
        "client_key": _client_key(args),
        "text": _note_text(args),
        "actor": _actor(args),
    }


def _note_edit_args(args: dict) -> dict:
    _only(args, "grant_id", "revision", "note_id", "text")
    return {**_grant_ref(args), "note_id": _note_id(args), "text": _note_text(args)}


def _note_delete_args(args: dict) -> dict:
    _only(args, "grant_id", "revision", "note_id")
    return {**_grant_ref(args), "note_id": _note_id(args)}


def _note_read_args(args: dict) -> dict:
    _only(args, "grant_id", "revision", "seq")
    if "seq" not in args:
        raise ch.BadArgs("seq")
    return {
        **_grant_ref(args),
        "seq": _int(args, "seq", default=0, low=0, high=grants.REVISION_MAX),
    }


def _hint_args(args: dict) -> dict:
    """A hint names a grant and carries nothing else: no revision, no count, no content."""
    _only(args, "grant_id")
    if not grants.valid_grant_id(args.get("grant_id")):
        raise ch.BadArgs("grant")
    return {"grant_id": args["grant_id"]}


# ── pair mode ─────────────────────────────────────────────────────────
async def _pair_hello(channel: ch.Channel, args: dict) -> dict:
    fed, pairing = channel.context
    if fed._expire_due(pairing):  # the deadline holds without anybody polling
        raise ch.VerbError("closed")
    pairing.remote_name = args["name"]
    if args["endpoints"]:
        pairing.remote_endpoints = args["endpoints"]
    return {}


async def _pair_confirm(channel: ch.Channel, args: dict) -> dict:
    """The other operator confirmed. The answer says whether the peer is stored HERE, because the
    caller stores it only on ``ok``: ``{}`` when stored or still waiting for this operator, an
    error when this side ended without storing it. None of these codes is ``closed`` or
    ``timeout``, which the caller reads as "no answer" (``service.UNANSWERED``)."""
    fed, pairing = channel.context
    if pairing.state in ("committing", "done"):
        # Both confirmations crossed: this side's own call was answered first.
        await fed._maybe_finish(pairing)  # waits for a write under way
        if pairing.state == "done":
            return {}
        raise ch.VerbError("not_stored")
    if fed._expire_due(pairing):
        raise ch.VerbError("ended")
    pairing.remote_confirmed = True
    await fed._maybe_finish(pairing)
    if pairing.local_confirmed and pairing.state != "done":
        raise ch.VerbError("not_stored")  # both confirmed and the write here failed
    return {}


async def _pair_deny(channel: ch.Channel, args: dict) -> dict:
    fed, pairing = channel.context
    fed._end_pairing(pairing, "denied", "the other operator declined")
    return {}


# ── peer mode ─────────────────────────────────────────────────────────
async def _peer_hello(channel: ch.Channel, args: dict) -> dict:
    fed, peer_id = channel.context
    if args["endpoints"]:
        await asyncio.to_thread(fed.store.set_endpoints, peer_id, args["endpoints"])
    return {"protocol": APP_PROTOCOL}


async def _peer_ping(channel: ch.Channel, args: dict) -> dict:
    return {}


async def _peer_bye(channel: ch.Channel, args: dict) -> dict:
    fed, peer_id = channel.context
    fed.peer_left(peer_id)
    asyncio.get_running_loop().call_later(0.5, channel.close, "the peer revoked this instance")
    return {}


async def _note_changed(channel: ch.Channel, args: dict) -> dict:
    """The owner's hint that a thread changed (this instance is the RECEIVER). It carries no
    data and is not believed: this side reads the owner, in the background."""
    fed, peer_id = channel.context
    fed.peer_hint(peer_id, args["grant_id"], inbox.NOTE)
    return {}


async def _decision_changed(channel: ch.Channel, args: dict) -> dict:
    """The owner's hint that a session's pending requests changed. As above."""
    fed, peer_id = channel.context
    fed.peer_hint(peer_id, args["grant_id"], inbox.DECISION)
    return {}


def _table(mode: str, *entries: tuple) -> dict[str, ch.Verb]:
    return {
        name: ch.Verb(name, frozenset({mode}), validate, handle)
        for name, validate, handle in entries
    }


PAIR = _table(
    ch.MODE_PAIR,
    ("pair.hello", _pair_hello_args, _pair_hello),
    ("pair.confirm", _no_args, _pair_confirm),
    ("pair.deny", _no_args, _pair_deny),
)
PEER = _table(
    ch.MODE_PEER,
    ("peer.hello", _peer_hello_args, _peer_hello),
    ("peer.ping", _no_args, _peer_ping),
    ("peer.bye", _no_args, _peer_bye),
    # Sharing an API session, view only (#2054). The first four are answered by the OWNER of a
    # session; `share.changed` is the owner's hint to the receiver and carries no data.
    ("share.list", _share_list_args, sharing.share_list),
    ("session.snapshot", _snapshot_args, sharing.session_snapshot),
    ("session.events", _events_args, sharing.session_events),
    ("session.history", _history_args, sharing.session_history),
    ("share.changed", _no_args, sharing.share_changed),
    # Input (#2057), answered by the OWNER. None of them changes a grant.
    ("input.suggest", _suggest_args, inputs.input_suggest),
    ("input.suggestions", _suggestions_args, inputs.input_suggestions),
    ("input.withdraw", _withdraw_args, inputs.input_withdraw),
    ("input.send", _send_args, inputs.input_send),
    # What became of one of the caller's own messages (#2061): a read, no text.
    ("input.status", _status_args, inputs.input_status),
    # Working together (#2059), answered by the OWNER. None of them changes a grant.
    ("access.request", _access_request_args, together.access_request),
    ("access.status", _access_status_args, together.access_status),
    # The peer takes its own pending request back (#2062): it narrows.
    ("access.withdraw", _access_status_args, together.access_withdraw),
    ("decision.answer", _decision_args, together.decision_answer),
    ("note.list", _note_list_args, notes.note_list),
    ("note.add", _note_add_args, notes.note_add),
    ("note.edit", _note_edit_args, notes.note_edit),
    ("note.delete", _note_delete_args, notes.note_delete),
    ("note.read", _note_read_args, notes.note_mark_read),
    # The owner's hints to the RECEIVER: a grant id, no data.
    ("note.changed", _hint_args, _note_changed),
    ("decision.changed", _hint_args, _decision_changed),
    # A terminal session's current screen as a redacted text frame, view only (#2056). Answered
    # by the OWNER; it reads, and nothing on this channel types into or resizes a terminal.
    ("terminal.frame", _frame_args, terminal.terminal_frame),
)
