"""Continue here: start a NEW LOCAL session briefed from a session a peer shares (#1246 §6,
#2059).

A receiving instance with at least ``view`` on an API session can start its own session from
what it was allowed to read. Three steps, all on the RECEIVING side:

1. :func:`prepare` reads the source through the channel (``session.snapshot``, the owner's
   per-call check), builds the seed with the remote builders of ``handoff.py`` (the record
   framed as data; Quick, or summarized by THIS instance's own AI endpoint) and returns a
   handle. The handle is bound to ``(peer_id, grant_id, revision)`` and to the projection the
   seed was built from, lives in memory, and expires after 10 minutes.
2. :func:`preview` shows the prepared seed again.
3. :func:`create` is the **ONE admission point**. It first re-reads the source through the
   channel with the BOUND revision. A refusal, an offline peer or a changed revision creates
   NOTHING and the operator is told the share is no longer available. That re-read is never
   re-resolved (``retry=False``): it gates a write, and a call under the refreshed revision
   would admit a handle bound to another one. :func:`prepare`'s read IS re-resolved like every
   receiving read, and the handle is bound to the revision that answered. Only when the owner
   answered is the seed handed to the existing handoff store and a target session id minted;
   nothing is awaited between that answer and the creation. A revoke during the summary or
   while the preview is open therefore never yields a session.

**The target decides how the session starts, as on the handoff route** (#2062). A console agent's
seed waits in the handoff store for the normal launch to type it; a native API client
(`handoff.is_api_target`) is created and gets the seed as its FIRST structured turn
(`handoff_api.commit`, guarded: no permission skip, no source to archive). Whether a target can
start is `handoff.target_present`, the answer `/api/engines` and the handoff route give.

**What it is not.** Nothing is sent to the owner beyond reads its grant already allows; the
source session is untouched. The new session is an ordinary local session: the normal launch
route, the normal working-directory checks, the normal tool and permission boundaries, no
added privilege, and no link back (``handoff.create_handle(None, …)`` writes no provenance).
A session created while the grant was live holds a copy of what the peer was allowed to read;
a later revoke does not erase it.

Not offered for a terminal grant: a single frame is not a brief.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass

from .. import engines, handoff, handoff_api, review, template_secrets
from . import remote, sharing

log = logging.getLogger(__name__)

TTL_S = 600.0
#: Prepared briefs kept at once; past it the oldest goes.
HANDLES_MAX = 32
MODES = ("quick", "ai")
CWD_MAX = 4096
GONE_DETAIL = "this share is no longer available — nothing was created"


class ContinueError(Exception):
    """A Continue-here request that is refused. ``status`` is the HTTP status to answer."""

    def __init__(self, code: str, detail: str, status: int = 422) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status


@dataclass
class _Prepared:
    handle: str
    peer_id: str
    grant_id: str
    revision: int
    target_engine: str
    seed: str
    meta: dict
    #: A digest of the projection the seed was built from: what the handle is bound to.
    projection: str
    created: float


_lock = threading.Lock()
_PREPARED: dict[str, _Prepared] = {}
_clock = time.monotonic


def _sweep_locked() -> None:
    now = _clock()
    for handle in [h for h, p in _PREPARED.items() if now - p.created > TTL_S]:
        del _PREPARED[handle]
    while len(_PREPARED) >= HANDLES_MAX:
        del _PREPARED[next(iter(_PREPARED))]


def _is_terminal(row: object) -> bool:
    """Whether a mirrored share is a TERMINAL one (#2056): no brief is made from a frame. Every
    mirror row carries its kind (schema 3); the reads below ask for an API grant as well, so a
    row whose kind changed after this check is refused there (``no_grant``)."""
    return getattr(row, "kind", None) == "terminal"


def _target(target_engine: object):
    """The engine the new session runs in, by the same capability source the handoff route
    uses. Blocking."""
    if not isinstance(target_engine, str) or not target_engine:
        raise ContinueError("invalid", "target_engine is required")
    prov = engines.get(target_engine)
    if prov is None:
        raise ContinueError("not_found", "unknown engine", status=404)
    ok, reason = handoff.handoff_target_state(prov, present=handoff.target_present(prov))
    if not ok:
        raise ContinueError("invalid", f"target engine unavailable: {reason}")
    return prov


async def _seed(snapshot: dict, mode: str, *, peer_name: str, title: str) -> tuple[str, dict]:
    def quick() -> tuple[str, dict]:
        return handoff.build_remote_quick_seed(snapshot, peer_name=peer_name, title=title)

    if mode != "ai":
        return await asyncio.to_thread(quick)
    try:
        return await handoff.build_remote_ai_seed(snapshot, peer_name=peer_name, title=title)
    except (review.ReviewError, handoff.HandoffError) as exc:
        if isinstance(exc, handoff.HandoffError) and exc.status in (409, 503):
            raise
        # Degrade, never fail (as the handoff route): the local record, with a notice.
        seed, meta = await asyncio.to_thread(quick)
        notice = (
            "AI review isn't configured — using the local record."
            if isinstance(exc, review.NotConfiguredError)
            else "AI summary failed — using the local record."
        )
        return seed, {**meta, "requested_mode": "ai", "degraded": True, "notice": notice}


def _view(p: _Prepared) -> dict:
    return {
        "handle": p.handle,
        "preview": p.seed,
        "meta": p.meta,
        "target_engine": p.target_engine,
        "expires_in": max(0.0, TTL_S - (_clock() - p.created)),
    }


async def prepare(fed, peer_id: str, grant_id: str, *, target_engine: object, mode: object) -> dict:
    """Read the source and build the seed. Creates no session and writes nothing durable."""
    if mode not in MODES:
        raise ContinueError("invalid", "mode must be quick or ai")
    row = await asyncio.to_thread(fed.store.get_remote, peer_id, grant_id)
    peer = await asyncio.to_thread(fed.store.get, peer_id)
    if row is None or peer is None or not peer.trusted:
        raise remote.ReadError(sharing.NO_GRANT)
    if _is_terminal(row):
        raise ContinueError("not_possible", "a shared terminal can't be continued here")
    prov = await asyncio.to_thread(_target, target_engine)
    # The read the grant allows, through the owner's per-call check. A READ: a refusal is
    # re-resolved once (#2055), and the handle is bound to the revision the owner ANSWERED under
    # (the refreshed one then), never to the one this function looked up before the call.
    snapshot, revision = await remote.read_bound(
        fed,
        peer_id,
        grant_id,
        "session.snapshot",
        {},
        check=remote.check_snapshot,
    )
    try:
        seed, meta = await _seed(snapshot, str(mode), peer_name=peer.name, title=row.title)
    except handoff.HandoffError as exc:
        code = "empty" if exc.status == 409 else "unavailable"
        raise ContinueError(code, exc.detail, status=exc.status) from None
    digest = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    prepared = _Prepared(
        handle=secrets.token_urlsafe(24),
        peer_id=peer_id,
        grant_id=grant_id,
        revision=revision,
        target_engine=prov.engine_id,
        seed=seed,
        meta={**meta, "revision": revision},
        projection=digest,
        created=_clock(),
    )
    with _lock:
        _sweep_locked()
        _PREPARED[prepared.handle] = prepared
    return _view(prepared)


def _held(handle: object, peer_id: str, grant_id: str, *, take: bool) -> _Prepared:
    """The prepared brief a handle names, when it is this grant's and has not expired. With
    ``take`` it is removed in the same step: a handle creates at most once."""
    gone = ContinueError("expired", "this brief has expired — prepare it again", status=410)
    if not isinstance(handle, str) or not handle:
        raise gone
    with _lock:
        prepared = _PREPARED.get(handle)
        if prepared is None:
            raise gone
        if (prepared.peer_id, prepared.grant_id) != (peer_id, grant_id):
            raise gone  # another grant's handle is not this one's: one answer
        if take:
            del _PREPARED[handle]
        if _clock() - prepared.created > TTL_S:
            _PREPARED.pop(handle, None)
            raise gone
        return prepared


def preview(handle: object, peer_id: str, grant_id: str) -> dict:
    return _view(_held(handle, peer_id, grant_id, take=False))


async def create(fed, handle: object, peer_id: str, grant_id: str, *, cwd: object) -> dict:
    """THE ADMISSION POINT. Re-read the source with the bound revision; only when the owner
    answers is the local session created. Whatever else happens, nothing is created."""
    if not isinstance(cwd, str) or not cwd or len(cwd) > CWD_MAX or "\x00" in cwd:
        raise ContinueError("invalid", "cwd must be the folder the new session starts in")
    prepared = _held(handle, peer_id, grant_id, take=True)
    try:
        # A read, pinned to the revision the handle was bound to: the owner's own per-call check
        # decides. Cursors and kinds come back; this side uses none of it. It is the gate of a
        # WRITE (the local session below), so a refusal is final and never re-resolved: a call
        # under another revision would let a handle minted for one revision create under another.
        await remote.read(
            fed,
            peer_id,
            grant_id,
            "session.events",
            {"after": 0, "limit": 1, "wait": 0},
            check=remote.check_events,
            revision=prepared.revision,
            retry=False,
        )
    except remote.ReadError as exc:
        log.info("federation: a brief was not used (%s); nothing was created", exc.code)
        raise ContinueError("gone", GONE_DETAIL, status=409) from None
    # Nothing is awaited from here on but the creation itself: the owner's answer above is what
    # admits it.
    prov = engines.get(prepared.target_engine)
    api = prov is not None and handoff.is_api_target(prov)
    try:
        if api:
            _refuse_a_known_secret(prepared.seed)
        seeded = handoff.create_handle(
            None,
            prepared.target_engine,
            f"remote-{prepared.meta.get('mode', 'quick')}",
            prepared.seed,
            cwd=cwd,
            guard_secrets=True,
        )
        if api:
            # The handoff route's own split (#2141): created, then the seed as its first turn.
            made = await handoff_api.commit(seeded, None, bypass=False, archive_source=False)
            if made["delivery"] is None:
                # Created, and the first turn not taken yet (refused before admission): the
                # brief stays bound and fences every other send. This handle is spent, so the
                # answer carries the handoff's own: committing it on the ordinary handoff route
                # re-sends the SAME brief, same operation id, into the SAME session, never a
                # second one (`handoff.commit_api` replays; its options and text are fixed). It
                # reaches the local session only, and lives as long as the handoff store keeps it.
                made = {**made, "retry_handle": seeded}
            return made
        return handoff.commit(seeded)
    except handoff.HandoffError as exc:
        raise ContinueError("invalid", exc.detail, status=exc.status) from None


def _refuse_a_known_secret(seed: str) -> None:
    """A console seed is checked against the secrets known when it is TYPED (``guard_secrets``,
    ``webterm._deliver_seed``). An API seed is sent as a structured turn, which has no such
    check, so it is checked here, the last step before the session is created: a value that
    became a secret after the preview was built refuses the create. Fails closed."""
    try:
        clean = template_secrets.redact_text(seed) == seed
    except template_secrets.RedactionUnavailable:
        clean = False
    if not clean:
        raise ContinueError(
            "invalid", "this brief now holds a known secret — nothing was created", status=422
        )


def reset_for_tests() -> None:
    with _lock:
        _PREPARED.clear()
