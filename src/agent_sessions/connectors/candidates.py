"""Recipient candidates (#1832): what a messenger's bot can reach NOW, and the snapshot a save is
checked against.

`list_candidates` asks the adapter WITHOUT the store lock (no network call ever holds it) and keeps
a bounded snapshot in memory, keyed by instance and generation. `store.set_recipients` then checks,
under the lock, that the instance is still at that generation and that every id is an eligible
candidate in that snapshot (or already allowed and not listed ineligible now). So the client can
only ever choose among what the server itself listed, and labels are always the server's.
"""

from __future__ import annotations

import asyncio
import threading
import time

from .. import connector_http
from . import kinds, store
from . import manifest as manifest_mod
from .adapters import ADAPTERS

TTL_S = 600
SNAPSHOTS_MAX = 50
CANDIDATES_MAX = 50

_SNAP: dict[str, dict] = {}
_LOCK = threading.Lock()


def _bound(inst: dict) -> connector_http.Bound:
    m = manifest_mod.bundled()[inst["manifest_id"]]
    return connector_http.Bound(
        instance_id=inst["id"],
        generation=inst["generation"],
        origin=inst["origin"],
        auth_scheme=m.auth_scheme,
        label=m.label,
        adapter=m.adapter,
        for_read=False,
    )


def snapshot(iid: str) -> dict:
    """The live snapshot for ``iid``, or an empty one (generation None: any save is stale)."""
    with _LOCK:
        snap = _SNAP.get(iid)
        if snap is None or time.monotonic() - snap["at"] > TTL_S:
            return {"generation": None, "candidates": {}}
        return {"generation": snap["generation"], "candidates": dict(snap["candidates"])}


def _keep(iid: str, generation: int, rows: list[dict]) -> None:
    with _LOCK:
        _SNAP[iid] = {
            "generation": generation,
            "at": time.monotonic(),
            "candidates": {
                r["id"]: {
                    "label": r["label"],
                    "kind": r["kind"],
                    "eligible": not r.get("unavailable"),
                }
                for r in rows
            },
        }
        while len(_SNAP) > SNAPSHOTS_MAX:
            del _SNAP[min(_SNAP, key=lambda k: _SNAP[k]["at"])]


def forget(iid: str) -> None:
    with _LOCK:
        _SNAP.pop(iid, None)


async def list_candidates(iid: str) -> dict:
    """``{candidates: [{id, label, kind, members?, unavailable?, allowed}]}`` for a messenger."""
    inst = await asyncio.to_thread(store.snapshot_for_test, iid)
    if not store.MESSENGER_KEYS <= set(inst):
        raise store.Refused("this connector has no recipients", "bad_body")
    m = manifest_mod.bundled()[inst["manifest_id"]]
    if m.adapter in kinds.TYPED_RECIPIENTS:
        # Mail (#1836) has nothing to look up: its addresses are typed (`store.set_addresses`).
        raise store.Refused("an email connector's addresses are typed, not listed", "bad_body")
    rows = []
    for r in await ADAPTERS[m.adapter].candidates(_bound(inst)):
        fit = store.fit_recipient(r.get("id"), r.get("label"), r.get("kind"))
        if fit is not None:  # an id or kind the store can't hold is never offered
            r = {k: v for k, v in r.items() if k != "member_ids"}  # who is joined stays here
            rows.append({**r, **fit})
        if len(rows) >= CANDIDATES_MAX:
            break
    _keep(iid, inst["generation"], rows)
    allowed = {r["id"] for r in inst["recipients"]}
    return {
        "candidates": [
            {k: v for k, v in {**r, "allowed": r["id"] in allowed}.items() if v is not None}
            for r in rows
        ]
    }
