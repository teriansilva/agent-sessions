"""Grants: what this instance shares with a peer, and THE check every peer request passes
(#1246 §5, #2054).

A grant is an owner-side record ``(session, peer, level, expiry, revision)`` in ``federation.db``.
Nothing is shared without one. It ends on operator revoke, on expiry, when its peer is revoked
(``Store.revoke``, the same transaction) and when its session is archived (``shares.archiving``).

**Granting shares the archive fence.** :func:`create` takes the per-session lock
``shares.archiving`` holds across an archive (the lock public-link creation takes), and re-checks
inside it that the session exists, is an API session and is not archived. So a grant is created
before the archive and ended by it, or after it and refused: never live across the move.

**One check.** :func:`authorize` is the only way a verb learns that a peer may read a session. It
runs inside every call, on this instance's clock, as plain reads: no transaction and no lock
outlives it. Whatever fails (an unknown id, another peer's id, a stale revision, an expired or
revoked grant, a revoked peer, a session that is gone or archived), it returns ``None``; the verb
answers the one code ``no_grant`` and the reason is logged here, never sent.

Everything in this module blocks (SQLite, a provider scan, a thread lock): call it off the event
loop.
"""

from __future__ import annotations

import logging
import math
import re
import time

from .. import engines, shares
from . import identity, store

log = logging.getLogger(__name__)

#: The levels this build grants. The store's column takes ``suggest`` and ``send`` later.
LEVELS = ("view",)
DEFAULT_EXPIRY_S = {"view": 7 * 86400}
MIN_EXPIRY_S = 60
MAX_EXPIRY_S = 90 * 86400
GRANT_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{16,64}\Z")
#: A revision is a small counter; anything a JSON number cannot hold exactly is not one.
REVISION_MAX = 2**53
#: The only runtime a grant can name in this phase. A terminal session is P4b.
SHAREABLE_RUNTIME = "api"


class GrantError(Exception):
    """An operator request the owner rules refuse. ``status`` is the HTTP status to answer."""

    def __init__(self, code: str, detail: str, status: int = 422) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status


def _clock() -> float:
    """The owner's clock: the only one that decides expiry."""
    return time.time()


def valid_grant_id(value: object) -> bool:
    return isinstance(value, str) and GRANT_ID_RE.fullmatch(value) is not None


def valid_revision(value: object) -> bool:
    return type(value) is int and 1 <= value <= REVISION_MAX


def _expiry(level: str, expires_in: object) -> float:
    """Seconds from now, as the operator picked them. Absent means the level's default."""
    if expires_in is None:
        return float(DEFAULT_EXPIRY_S[level])
    if isinstance(expires_in, bool) or not isinstance(expires_in, int | float):
        raise GrantError("invalid", "expires_in must be a number of seconds")
    try:
        seconds = float(expires_in)  # OverflowError on an absurd integer
    except OverflowError:
        raise GrantError("invalid", "expires_in is out of range") from None
    if not math.isfinite(seconds) or not MIN_EXPIRY_S <= seconds <= MAX_EXPIRY_S:
        raise GrantError(
            "invalid",
            f"expires_in must be between {MIN_EXPIRY_S} seconds and {MAX_EXPIRY_S // 86400} days",
        )
    return seconds


def _shareable(session_key: object):
    """``(provider, native id)`` of a local API session key, or :class:`GrantError`. A ``peer:``
    key (a session another instance shares) fails here like any unknown key: ``parse_key``
    refuses the prefix."""
    if not isinstance(session_key, str) or not session_key:
        raise GrantError("invalid", "session_key is required")
    try:
        prov, native = engines.parse_key(session_key)
    except engines.EngineError:
        raise GrantError("not_found", "unknown session", status=404) from None
    runtime = getattr(getattr(prov, "manifest", None), "runtime", None)
    if runtime != SHAREABLE_RUNTIME:
        raise GrantError("not_shareable", "only an API session can be shared with a peer")
    return prov, native


def create(
    st: store.Store, *, session_key: object, peer_id: object, level: object, expires_in: object
) -> store.Grant:
    """Grant ``peer_id`` access to one local API session. Under the session's archive fence."""
    if not isinstance(peer_id, str) or not identity.PEER_ID_RE.fullmatch(peer_id):
        raise GrantError("not_found", "unknown peer", status=404)
    if level not in LEVELS:
        raise GrantError("invalid", "this level cannot be granted")
    seconds = _expiry(level, expires_in)
    prov, native = _shareable(session_key)
    key = f"{prov.engine_id}:{native}"
    lock = shares.store().session_lock(key)
    if not lock.acquire(timeout=shares.SESSION_LOCK_TIMEOUT_S):
        raise GrantError("busy", "this session is busy — try again", status=503)
    try:
        # INSIDE the fence an archive holds across its revoke and its move: the session is still
        # an API session of a known agent, it exists, and it is not archived.
        prov, native = _shareable(key)
        state = shares.session_state(prov, native)
        if state == "unknown":
            raise GrantError("not_found", "unknown session", status=404)
        if state == "archived":
            raise GrantError("archived", "an archived session can't be shared", status=409)
        if state != "live":
            raise GrantError(
                "unreadable", "this session's state can't be read right now — try again", status=503
            )
        now = _clock()
        try:
            return st.create_grant(
                session_key=key, peer_id=peer_id, level=level, expires_at=now + seconds, now=now
            )
        except store.StoreError as exc:
            raise GrantError(_code(exc), str(exc), status=exc.status) from exc
    finally:
        lock.release()


def _code(exc: store.StoreError) -> str:
    if isinstance(exc, store.NotFound):
        return "not_found"
    if isinstance(exc, store.Conflict):
        return "conflict"
    return "store"


def change(
    st: store.Store, grant_id: object, *, expires_in: object, revision: object
) -> store.Grant:
    """A new expiry, counted from now. Bumps the revision."""
    if not valid_grant_id(grant_id):
        raise GrantError("not_found", "unknown grant", status=404)
    if not valid_revision(revision):
        raise GrantError("invalid", "revision must be the grant's current revision")
    current = st.get_grant(grant_id)
    if current is None:
        raise GrantError("not_found", "unknown grant", status=404)
    if current.level not in LEVELS:
        raise GrantError("invalid", "this grant's level cannot be changed by this build")
    if expires_in is None:
        raise GrantError("invalid", "expires_in is required")
    seconds = _expiry(current.level, expires_in)
    now = _clock()
    try:
        return st.change_grant(grant_id, expires_at=now + seconds, revision=revision, now=now)
    except store.StoreError as exc:
        raise GrantError(_code(exc), str(exc), status=exc.status) from exc


def revoke(st: store.Store, grant_id: object) -> store.Grant:
    """End one grant. Idempotent for a grant that already ended."""
    if not valid_grant_id(grant_id):
        raise GrantError("not_found", "unknown grant", status=404)
    try:
        return st.revoke_grant(grant_id, now=_clock())
    except store.StoreError as exc:
        raise GrantError(_code(exc), str(exc), status=exc.status) from exc


def listing(st: store.Store, session_key: object = None) -> list[store.Grant]:
    """The live grants, of one session when ``session_key`` is given."""
    key = None
    if session_key:
        prov, native = _shareable(session_key)
        key = f"{prov.engine_id}:{native}"
    return st.list_grants(session_key=key, now=_clock())


def sweep(st: store.Store) -> list[store.Grant]:
    """End what expired. The per-call check never depends on this having run."""
    return st.expire_due(now=_clock())


def public(grant: store.Grant) -> dict:
    """A grant as the operator API shows it."""
    return {
        "grant_id": grant.grant_id,
        "session_key": grant.session_key,
        "peer_id": grant.peer_id,
        "level": grant.level,
        "expires_at": grant.expires_at,
        "revision": grant.revision,
        "state": grant.state,
        "created_at": grant.created_at,
        "ended_at": grant.ended_at,
        "end_reason": grant.end_reason,
    }


def _refused(reason: str, peer_id: str, grant_id: object) -> None:
    # Local only: the peer is told `no_grant` whatever this says. `%r` and a cut keep a hostile
    # id from writing a second log line.
    log.info(
        "federation: request refused (%s) peer=%s grant=%.80r",
        reason,
        peer_id,
        grant_id,
    )


def _granted(
    st: store.Store, grant_id: object, peer_id: str, revision: object
) -> store.Grant | None:
    """The store's half of the check: the grant exists, its principal is this peer, it is live
    and not expired on this instance's clock, the revision is the current one, the peer is still
    trusted (a revoke by any process over this store counts) and the level is one this build
    serves. One statement reads the grant and the peer together."""
    if not valid_grant_id(grant_id) or not valid_revision(revision):
        return _refused("malformed", peer_id, grant_id)
    try:
        grant, peer_state = st.grant_and_peer_state(grant_id, peer_id)
    except (store.StoreError, OSError, ValueError) as exc:
        return _refused(f"store unreadable: {type(exc).__name__}", peer_id, grant_id)
    if peer_state != "trusted":
        return _refused("peer not trusted", peer_id, grant_id)
    if grant is None:
        return _refused("unknown grant", peer_id, grant_id)
    if grant.principal != store.principal_of(peer_id):
        return _refused("another principal's grant", peer_id, grant_id)
    if grant.state != "live":
        return _refused(f"grant ended ({grant.end_reason})", peer_id, grant_id)
    if grant.expires_at <= _clock():
        return _refused("grant expired", peer_id, grant_id)
    if grant.revision != revision:
        return _refused("stale revision", peer_id, grant_id)
    if grant.level not in LEVELS:
        return _refused("level not served by this build", peer_id, grant_id)
    return grant


def authorize(
    st: store.Store, grant_id: object, peer_id: str, revision: object
) -> store.Grant | None:
    """THE per-call check. The grant when ``peer_id`` (the channel's authenticated peer) may read
    its session RIGHT NOW with the revision it holds, else ``None``.

    Everything :func:`_granted` reads from the store, and the session itself: it is still a
    local API session of a known agent, present and not archived. Plain reads only. Every answer
    a peer gets about a session is sent on this function's word and on nothing else.

    **The store read is the LAST thing this function does.** The session check scans an engine's
    store and can take a while; a revoke, a revision change or an expiry that lands during it
    must not be missed. So the store is read once to learn which session to check, the session
    is checked, and then the store is read AGAIN: the grant returned is that second read's, and
    it must still name the same session. Nothing slow runs after it. Do not reorder these."""
    first = _granted(st, grant_id, peer_id, revision)
    if first is None:
        return None
    try:
        prov, native = _shareable(first.session_key)
    except GrantError:
        return _refused("session is no longer a local API session", peer_id, grant_id)
    state = shares.session_state(prov, native)
    if state != "live":
        return _refused(f"session {state}", peer_id, grant_id)
    grant = _granted(st, grant_id, peer_id, revision)
    if grant is None:
        return None
    if grant.session_key != first.session_key:
        return _refused("the grant names another session now", peer_id, grant_id)
    return grant


def still_granted(st: store.Store, grant_id: object, peer_id: str, revision: object) -> bool:
    """Whether a PARKED call should keep waiting: the store's half of :func:`authorize` (a revoke
    of the grant or the peer, an expiry, a revision change, and an archive, which ends the grant
    in the store). It only ever ENDS a wait early. It never lets an answer out: that is
    :func:`authorize`, which runs again directly before the answer is returned."""
    return _granted(st, grant_id, peer_id, revision) is not None
