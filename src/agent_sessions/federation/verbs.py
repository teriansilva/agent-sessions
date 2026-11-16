"""The closed verb table of the peer channel (#1246 §3).

Every name a peer may call is listed here with the modes it is valid in and a validator that
returns cleaned arguments or raises :class:`channel.BadArgs`. Nothing else is callable: the
channel resets the stream for an unknown name, a name outside its mode, or refused arguments.

A handler receives the :class:`channel.Channel` it was called on; ``channel.context`` is what the
service attached when it built the channel (``(federation, pairing)`` in ``pair`` mode,
``(federation, peer_id)`` in ``peer`` mode). Handlers never take a path, a URL to fetch, a command
or a session key from this phase's arguments.
"""

from __future__ import annotations

import asyncio

from . import channel as ch
from . import endpoints as ep
from . import store

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
    fed, pairing = channel.context
    if fed._expire_due(pairing):
        raise ch.VerbError("closed")
    pairing.remote_confirmed = True
    await fed._maybe_finish(pairing)
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
)
