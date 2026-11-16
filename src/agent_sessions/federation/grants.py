"""Grants: what this instance shares with a peer, and THE check every peer request passes
(#1246 §5, #2054, #2056).

A grant is an owner-side record ``(session, peer, kind, level, expiry, revision)`` in
``federation.db``. Nothing is shared without one. Its **kind** is what it shares: an ``api``
session's projection, or a ``terminal`` session's screen as a redacted text frame (view only,
always). The kind is stored when the operator grants, and every check also asks the session what
it is now: a verb serves a grant only when the stored kind, the session's kind today and the kind
the verb reads all agree, so an API grant never yields a screen and a terminal grant never yields
a projection, whatever an agent's manifest becomes later.

A grant ends on operator revoke, on expiry, when its peer is revoked (``Store.revoke``, the same
transaction) and when its session is archived (``shares.archiving``).

**Granting shares the archive fence.** :func:`create` takes the per-session lock
``shares.archiving`` holds across an archive (the lock public-link creation takes), and re-checks
inside it that the session exists, is still of the kind being granted and is not archived. So a
grant is created before the archive and ended by it, or after it and refused: never live across
the move.

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
KIND_API = store.KIND_API
KIND_TERMINAL = store.KIND_TERMINAL
KINDS = store.GRANT_KINDS
#: The kind of session each agent runtime is. Any other runtime (``chat``) cannot be shared.
KIND_OF_RUNTIME = {"api": KIND_API, "pty": KIND_TERMINAL}
#: A terminal session is shared view only, now and later (#1246 §5, D17): whatever ``LEVELS``
#: grows to, nothing but ``view`` is granted or served for one.
TERMINAL_LEVEL = "view"


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
    """``(provider, native id, kind)`` of a local session key that can be shared, or
    :class:`GrantError`. The kind is what the session's agent is TODAY (its manifest's runtime).
    A ``peer:`` key (a session another instance shares) fails here like any unknown key:
    ``parse_key`` refuses the prefix."""
    if not isinstance(session_key, str) or not session_key:
        raise GrantError("invalid", "session_key is required")
    try:
        prov, native = engines.parse_key(session_key)
    except engines.EngineError:
        raise GrantError("not_found", "unknown session", status=404) from None
    runtime = getattr(getattr(prov, "manifest", None), "runtime", None)
    kind = KIND_OF_RUNTIME.get(runtime) if isinstance(runtime, str) else None
    if kind is None:
        raise GrantError(
            "not_shareable", "only an API session or a terminal session can be shared with a peer"
        )
    return prov, native, kind


def _level_allowed(kind: str, level: object) -> bool:
    """Whether this build grants and serves ``level`` for a session of ``kind``."""
    if kind == KIND_TERMINAL:
        return level == TERMINAL_LEVEL
    return level in LEVELS


def create(
    st: store.Store, *, session_key: object, peer_id: object, level: object, expires_in: object
) -> store.Grant:
    """Grant ``peer_id`` access to one local session: an API session, or a terminal session
    (``view`` only). Under the session's archive fence."""
    if not isinstance(peer_id, str) or not identity.PEER_ID_RE.fullmatch(peer_id):
        raise GrantError("not_found", "unknown peer", status=404)
    if level not in LEVELS:
        raise GrantError("invalid", "this level cannot be granted")
    seconds = _expiry(level, expires_in)
    prov, native, kind = _shareable(session_key)
    if not _level_allowed(kind, level):
        raise GrantError("invalid", "a terminal session can only be shared view only")
    key = f"{prov.engine_id}:{native}"
    lock = shares.store().session_lock(key)
    if not lock.acquire(timeout=shares.SESSION_LOCK_TIMEOUT_S):
        raise GrantError("busy", "this session is busy — try again", status=503)
    try:
        # INSIDE the fence an archive holds across its revoke and its move: the session is still
        # a session of a known agent and of the same kind, it exists, and it is not archived.
        prov, native, now_kind = _shareable(key)
        if now_kind != kind:
            raise GrantError("not_shareable", "this session changed — try again", status=409)
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
                session_key=key,
                peer_id=peer_id,
                level=level,
                expires_at=now + seconds,
                kind=kind,
                now=now,
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
    if not _level_allowed(current.kind, current.level):
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
        prov, native, _kind = _shareable(session_key)
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
        "kind": grant.kind,
        "level": grant.level,
        "expires_at": grant.expires_at,
        "revision": grant.revision,
        "state": grant.state,
        "created_at": grant.created_at,
        "ended_at": grant.ended_at,
        "end_reason": grant.end_reason,
    }


def serves(grant: store.Grant) -> bool:
    """Whether this build lists and serves ``grant`` at all: a kind and a level it knows, and a
    session whose agent is of that kind today. The store's rows and the session's state are the
    per-call check's to read; this is only what a LISTING leaves out."""
    if grant.kind not in KINDS or not _level_allowed(grant.kind, grant.level):
        return False
    try:
        return _shareable(grant.session_key)[2] == grant.kind
    except GrantError:
        return False


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
    st: store.Store, grant_id: object, peer_id: str, revision: object, kind: str | None = None
) -> store.Grant | None:
    """The store's half of the check: the grant exists, its principal is this peer, it is live
    and not expired on this instance's clock, the revision is the current one, the peer is still
    trusted (a revoke by any process over this store counts), the level is one this build serves
    for the grant's kind and, when the caller names one, the grant is of that ``kind``. One
    statement reads the grant and the peer together."""
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
    if grant.kind not in KINDS or not _level_allowed(grant.kind, grant.level):
        return _refused("kind or level not served by this build", peer_id, grant_id)
    if kind is not None and grant.kind != kind:
        return _refused(f"a {grant.kind} grant asked for a {kind} read", peer_id, grant_id)
    return grant


def authorize(
    st: store.Store, grant_id: object, peer_id: str, revision: object, *, kind: str
) -> store.Grant | None:
    """THE per-call check. The grant when ``peer_id`` (the channel's authenticated peer) may read
    its session RIGHT NOW with the revision it holds, through a verb that reads sessions of
    ``kind``, else ``None``.

    Everything :func:`_granted` reads from the store, and the session itself: it is still a
    local session of a known agent, of the kind the grant was made for, present and not archived.
    ``kind`` is required and has no default: every verb states what it reads, and a grant of the
    other kind is refused like an unknown one. Plain reads only. Every answer a peer gets about a
    session is sent on this function's word and on nothing else.

    **The store read is the LAST thing this function does.** The session check scans an engine's
    store and can take a while; a revoke, a revision change or an expiry that lands during it
    must not be missed. So the store is read once to learn which session to check, the session
    is checked, and then the store is read AGAIN: the grant returned is that second read's, and
    it must still name the same session. Nothing slow runs after it. Do not reorder these."""
    if kind not in KINDS:
        return _refused("unknown kind asked for", peer_id, grant_id)
    first = _granted(st, grant_id, peer_id, revision, kind)
    if first is None:
        return None
    try:
        prov, native, now_kind = _shareable(first.session_key)
    except GrantError:
        return _refused("session is no longer a local shareable session", peer_id, grant_id)
    if now_kind != first.kind:
        return _refused(f"session is a {now_kind} session now", peer_id, grant_id)
    state = shares.session_state(prov, native)
    if state != "live":
        return _refused(f"session {state}", peer_id, grant_id)
    grant = _granted(st, grant_id, peer_id, revision, kind)
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
