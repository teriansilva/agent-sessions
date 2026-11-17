"""The closed verb table of the peer channel (#1246 §3).

Every name a peer may call is listed here with the modes it is valid in and a validator that
returns cleaned arguments or raises :class:`channel.BadArgs`. Nothing else is callable: the
channel resets the stream for an unknown name, a name outside its mode, or refused arguments.

A handler receives the :class:`channel.Channel` it was called on; ``channel.context`` is what the
service attached when it built the channel (``(federation, pairing)`` in ``pair`` mode,
``(federation, peer_id)`` in ``peer`` mode). Handlers never take a path, a URL to fetch, a command
or a session key from a peer's arguments: a shared session is addressed by its **grant id**
(#2054), and the handlers behind those verbs live in :mod:`sharing`.
"""

from __future__ import annotations

import asyncio
import math

from .. import chat_store
from . import channel as ch
from . import endpoints as ep
from . import frames, grants, sharing, store, terminal

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
    # A terminal session's current screen as a redacted text frame, view only (#2056). Answered
    # by the OWNER; it reads, and nothing on this channel types into or resizes a terminal.
    ("terminal.frame", _frame_args, terminal.terminal_frame),
)
