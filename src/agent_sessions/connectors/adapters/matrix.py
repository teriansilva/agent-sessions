"""The `matrix-cs` adapter (#1832): a bot account on the Commander's homeserver, read for three
facts only, under ``/_matrix/client/v3``:

* ``GET account/whoami`` — the probe: the bot's user id, which `tested.login` keeps and the
  recipient allowlist is bound to (another account or homeserver is a new consent);
* ``GET joined_rooms`` — the rooms the bot is in;
* per room, ``GET rooms/{id}/state/m.room.name``, ``…/state/m.room.encryption`` and
  ``…/joined_members`` — its name, whether it is encrypted, and how many people would read.

**Encrypted rooms are not eligible** (#1746 D13), and neither is a room whose encryption could not
be CHECKED: a 403, a timeout or any other failure of that lookup makes the room unavailable, never
"unencrypted". Only a 404 for the encryption state (no such state event) means unencrypted.
"""

from __future__ import annotations

import re

from ... import connector_http
from ...connector_http import Bound, ConnectorError
from .common import seg, text

API = "/_matrix/client/v3"
ROOMS_MAX = 30
ROOM_ID_RE = re.compile(r"![A-Za-z0-9._=+/-]{1,200}:[A-Za-z0-9.-]{1,200}(?::[0-9]{1,5})?")
USER_ID_RE = re.compile(r"@[a-z0-9._=/+-]{1,200}:[A-Za-z0-9.-]{1,200}(?::[0-9]{1,5})?")


def valid_room(room_id: object) -> bool:
    return isinstance(room_id, str) and ROOM_ID_RE.fullmatch(room_id) is not None


async def _get(bound: Bound, path: str) -> object:
    return await connector_http._request(bound, "GET", f"{API}/{path}")


async def probe(bound: Bound) -> dict:
    me = await _get(bound, "account/whoami")
    uid = me.get("user_id") if isinstance(me, dict) else None
    if not isinstance(uid, str) or not USER_ID_RE.fullmatch(uid):
        raise ConnectorError("bad_reply", "the address did not answer as a Matrix homeserver does")
    return {"login": uid, "repos": 0}


async def room_facts(bound: Bound, room_id: str) -> dict:
    """``{label, members, unavailable}`` for one room the bot is in. ``unavailable`` is a short
    reason (encrypted, or could not be checked), or None when the room may receive messages."""
    if not valid_room(room_id):
        raise ConnectorError("refused", "not a room id")
    base = f"rooms/{seg(room_id)}"
    label = room_id
    try:
        name = await _get(bound, f"{base}/state/m.room.name")
        if isinstance(name, dict) and text(name.get("name"), 100).strip():
            label = text(name.get("name"), 100).strip()
    except ConnectorError:
        pass  # no name: the id stands in
    unavailable: str | None = None
    try:
        await _get(bound, f"{base}/state/m.room.encryption")
        unavailable = "encrypted, which isn't supported yet"
    except ConnectorError as e:
        if e.code != "not_found":
            unavailable = "its encryption couldn't be checked"
    # Who would read a message is part of the consent (#1832): a room whose members can't be
    # counted (an error, a timeout, a reply of the wrong shape) is not eligible (Hermes 7605).
    members: int | None = None
    try:
        joined = await _get(bound, f"{base}/joined_members")
        if isinstance(joined, dict) and isinstance(joined.get("joined"), dict):
            members = len(joined["joined"])
    except ConnectorError:
        members = None
    if members is None and unavailable is None:
        unavailable = "its members couldn't be checked"
    return {"label": label, "members": members, "unavailable": unavailable}


async def candidates(bound: Bound) -> list[dict]:
    """``[{id, label, kind, members, unavailable}]`` for the rooms the bot has joined."""
    got = await _get(bound, "joined_rooms")
    rooms = got.get("joined_rooms") if isinstance(got, dict) else None
    out: list[dict] = []
    for rid in rooms if isinstance(rooms, list) else []:
        if not valid_room(rid):
            continue
        facts = await room_facts(bound, rid)
        if not facts["label"].isprintable():
            facts["label"] = rid
        out.append({"id": rid, "kind": "room", **facts})
        if len(out) >= ROOMS_MAX:
            break
    return out
