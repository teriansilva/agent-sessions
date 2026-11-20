"""The receiving side's bell: what a peer shares with this instance, a decision this instance
may answer, a note from the other operator (#1246 §6, #2059).

This instance holds **nothing durable** about any of it beyond the mirror's listing rows: no
note, no question, no pending request. A row on the bell is decided by READING the owner, and a
hint from the owner (``note.changed``, ``decision.changed``; a grant id and nothing else) only
says "read sooner". A hint is never believed: :func:`check` asks the owner through the same
per-call check as every read, and the row follows the answer.

**Raised**: when a complete list refresh shows a grant this instance did not hold (``shared``)
or one whose level changed (``changed``); when a read shows a request this instance may answer
under a ``send`` grant; when a read shows a note of the other side this instance has not read.

**Retracted when this instance LEARNS it stopped being true:**

* a read the owner refuses (``no_grant``) retracts every row about that grant (:func:`refused`);
* a **complete** refresh that no longer lists a grant retracts its rows and says the share
  ended; one that lists it below ``send`` retracts its decision row (:func:`refreshed`);
* a refresh that FAILED retracts nothing: the mirror keeps its rows on purpose, and a revoke
  this instance has not heard of cannot retract anything yet. Until then a row can outlive a
  revoke; it holds no content and its link leads to the pane's unavailable state;
* the operator revoking the peer HERE retracts every row about it at once
  (``notices.retract_peer``, called by the service).

Blocking functions say so; the rest run on the event loop and never raise.
"""

from __future__ import annotations

import asyncio
import logging

from . import notices, remote, sharing, store

log = logging.getLogger(__name__)

NOTE, DECISION = "note", "decision"
HINTS = (NOTE, DECISION)
CHECK_TIMEOUT_S = 30.0
#: Between two checks of one grant and kind that hints kept asking for.
CHECK_MIN_GAP_S = 1.0


def refreshed(fed, peer_id: str, before: list[store.RemoteShare], after: list[dict]) -> None:
    """A COMPLETE refresh replaced ``peer_id``'s rows: announce what is new or changed, and
    retract the rows of every grant the new list no longer has. Blocking."""
    st = fed.store
    old = {row.grant_id: row for row in before}
    new = {entry["grant_id"]: entry for entry in after}
    for grant_id, entry in new.items():
        was = old.get(grant_id)
        if was is None:
            notices.announce_remote(st, "shared", peer_id, grant_id, entry["title"])
            _still_shared(st, peer_id, grant_id, "share")
        elif was.level != entry["level"]:
            notices.announce_remote(st, "changed", peer_id, grant_id, entry["title"], replace=True)
            _still_shared(st, peer_id, grant_id, "share")
        if not store.level_includes(entry["level"], "send"):
            notices.retract_remote(peer_id, grant_id, ("decision",))
    for grant_id, was in old.items():
        if grant_id not in new:
            notices.retract_remote(peer_id, grant_id, ("share", "decision", "note"))
            # A share this instance LEFT (#2201) did not "stop being shared": nobody is told.
            # Looked at before and AFTER the row is posted: the leave marks first and retracts
            # last, so a row posted around it is taken back here or there.
            if fed.has_left(peer_id, grant_id):
                continue
            notices.announce_remote(st, "ended", peer_id, grant_id, was.title)
            if fed.has_left(peer_id, grant_id):
                notices.retract_remote(peer_id, grant_id, ("ended",))


def _still_shared(st: store.Store, peer_id: str, grant_id: str, kind: str) -> None:
    """A row about a grant was just posted. This runs beside the request that may have LEFT
    that grant meanwhile (#2201): if the mirror no longer has it, the row is taken back. The
    leave drops the mirror row and only then retracts (`remote._forget`), so a row posted
    before its retraction goes there and one posted after it goes here. Blocking."""
    if st.get_remote(peer_id, grant_id) is None:
        notices.retract_remote(peer_id, grant_id, (kind,))


def newly_answerable(before: list[store.RemoteShare], after: list[dict]) -> list[str]:
    """The grants a complete refresh shows at ``send`` that this instance did not hold at
    ``send`` before. The owner's hint about a pending request may have arrived while the mirror
    still said otherwise (and was then rightly not believed), so these are asked about again."""
    old = {row.grant_id: row.level for row in before}
    return [
        entry["grant_id"]
        for entry in after
        if store.level_includes(entry["level"], "send")
        and not store.level_includes(old.get(entry["grant_id"]), "send")
    ]


def refused(peer_id: str, grant_id: str) -> None:
    """The owner refused a read of this grant: whatever this instance's bell says about it is
    no longer true. Blocking."""
    notices.retract_remote(peer_id, grant_id, ("share", "decision", "note"))


def opened(peer_id: str, grant_id: str) -> None:
    """The operator read the shared session: the "shared with you" row is read. Blocking."""
    notices.retract_remote(peer_id, grant_id, ("share",))


def notes_read(peer_id: str, grant_id: str, unread: int) -> None:
    """The operator read the thread; the owner says how much is still unread. Blocking."""
    if unread <= 0:
        notices.retract_remote(peer_id, grant_id, ("note",))


async def _answerable(fed, peer_id: str, grant_id: str) -> bool:
    """Whether the owner's snapshot lists a request this instance may answer."""
    snap = await remote.read(
        fed, peer_id, grant_id, "session.snapshot", {}, check=remote.check_snapshot
    )
    return any(
        isinstance(q.get("choices"), list) and q["choices"] for q in snap["pending_requests"]
    )


async def _unread(fed, peer_id: str, grant_id: str) -> bool:
    """Whether the owner's thread holds a note of the other side this instance has not read.
    ``limit: 0``: the count, and not one note's text, crosses the channel for this."""
    page = await remote.read(
        fed,
        peer_id,
        grant_id,
        "note.list",
        {"after_seq": 0, "limit": 0},
        check=remote.check_notes,
    )
    return page["unread"] > 0


async def check(fed, peer_id: str, grant_id: str, kind: str) -> None:
    """Ask the owner whether the row of ``kind`` for this grant should be on the bell, and make
    it so. A refusal retracts the grant's rows; an owner that cannot be asked changes nothing."""
    try:
        row = await asyncio.to_thread(fed.store.get_remote, peer_id, grant_id)
        if row is None:
            return  # not a grant this instance holds: a hint about it means nothing
        try:
            async with asyncio.timeout(CHECK_TIMEOUT_S):
                if kind == DECISION:
                    wanted = store.level_includes(row.level, "send") and await _answerable(
                        fed, peer_id, grant_id
                    )
                else:
                    wanted = await _unread(fed, peer_id, grant_id)
        except remote.ReadError as exc:
            if exc.code == sharing.NO_GRANT:
                await asyncio.to_thread(refused, peer_id, grant_id)
            return
        except TimeoutError:
            return
        if wanted:
            await asyncio.to_thread(
                notices.announce_remote, fed.store, kind, peer_id, grant_id, row.title
            )
            # The share may have been left while the owner was asked (#2201).
            await asyncio.to_thread(_still_shared, fed.store, peer_id, grant_id, kind)
        else:
            await asyncio.to_thread(notices.retract_remote, peer_id, grant_id, (kind,))
    except Exception:
        log.exception("federation: checking a shared session's notice failed")
