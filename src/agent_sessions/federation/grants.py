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
revoked grant, a revoked peer, a level that does not include what the verb needs, a session that
is gone or archived), it returns ``None``; the verb answers the one code ``no_grant`` and the
reason is logged here, never sent.

**Three levels** (#2057). ``view`` reads. ``suggest`` also lets the peer propose a message that
waits for this instance's operator. ``send`` also delivers the peer's messages as turns: never a
default, at most 24 hours, only with ``accept_risk: true`` in the operator's own request, and
refused for a session a mission holds. Each level includes the ones below it. **A terminal
grant is ``view`` and nothing else**: creating one at, or changing one to, ``suggest`` or
``send`` is refused, and every input verb asks for an ``api`` grant. **Only the operator
functions here change a grant** (:func:`create`, :func:`change`, :func:`revoke`); no verb
extends, raises or re-creates one, and nothing renews one in the background.

**A peer can give a grant up** (:func:`leave`, the ``share.leave`` verb, #2201): the one thing a
verb may do to a grant, and it only narrows. The check and the end are one fenced transaction.

**Every change holds the grant's fence** (``fence.py``, taken inside the store's methods): a
revoke, a level or expiry change, a peer revoke, an archive and the expiry sweep land before a
delivery or after it. One that cannot get the fence commits nothing and is ``503 busy``.

Everything in this module blocks (SQLite, a provider scan, a thread lock): call it off the event
loop.
"""

from __future__ import annotations

import logging
import math
import re
import time

from .. import engines, metadata, missions, prefs, shares
from . import identity, store

log = logging.getLogger(__name__)

#: The levels this build grants, lowest first. Each includes the ones before it.
LEVELS = ("view", "suggest", "send")
#: What a request that names no level gets. Never ``send``: that level is always asked for.
DEFAULT_LEVEL = "view"
DEFAULT_EXPIRY_S = {"view": 7 * 86400, "suggest": 7 * 86400, "send": 3600}
MIN_EXPIRY_S = 60
#: The longest a grant of each level may last. ``send`` lets another machine drive an agent
#: here, so it is short and renewed only by the operator.
MAX_EXPIRY_BY_LEVEL = {"view": 90 * 86400, "suggest": 30 * 86400, "send": 24 * 3600}
MAX_EXPIRY_S = max(MAX_EXPIRY_BY_LEVEL.values())
#: Shown to the operator; a ``send`` grant exists only once their request carried
#: ``accept_risk: true`` (as a public ``send`` link, ``routes/shares.SEND_RISK``).
SEND_RISK = (
    "The other instance can instruct the agent with this session's permissions: a message to an"
    " agent can become actions on this host, including reaching saved secret values the agent"
    " can see."
)
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


def _span(seconds: int) -> str:
    return f"{seconds // 86400} days" if seconds >= 2 * 86400 else f"{seconds // 3600} hours"


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
    longest = MAX_EXPIRY_BY_LEVEL[level]
    if not math.isfinite(seconds) or not MIN_EXPIRY_S <= seconds <= longest:
        raise GrantError(
            "invalid",
            f"expires_in must be between {MIN_EXPIRY_S} seconds and {_span(longest)}"
            f" for a {level} grant",
        )
    return seconds


def _accepted(level: str, accept_risk: object) -> None:
    """``send`` exists only on the operator's explicit word, given in THIS request."""
    if level == "send" and accept_risk is not True:
        raise GrantError("risk", f"{SEND_RISK} Send accept_risk: true to grant it.")


def mission_held(session_key: str) -> bool:
    """Whether a mission holds this session right now. A membership that cannot be read is not
    "no mission": it raises (503), and the caller grants or delivers nothing."""
    try:
        return missions.session_mission(session_key) is not None
    except Exception:  # noqa: BLE001 — unreadable is not permission
        raise GrantError(
            "unreadable", "mission membership can't be read right now — try again", status=503
        ) from None


def _not_held(level: str, session_key: str) -> None:
    if level == "send" and mission_held(session_key):
        raise GrantError(
            "held",
            "this session is part of a mission — it can be shared to view or suggest, not send",
            status=409,
        )


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
    st: store.Store,
    *,
    session_key: object,
    peer_id: object,
    level: object,
    expires_in: object,
    accept_risk: object = False,
) -> store.Grant:
    """Grant ``peer_id`` access to one local session: an API session, or a terminal session
    (``view`` only). Under the session's archive fence."""
    if not isinstance(peer_id, str) or not identity.PEER_ID_RE.fullmatch(peer_id):
        raise GrantError("not_found", "unknown peer", status=404)
    if not isinstance(level, str) or level not in LEVELS:
        raise GrantError("invalid", "this level cannot be granted")
    prov, native, kind = _shareable(session_key)
    # Before the risk and the expiry: a terminal session is view only, whatever else was sent.
    if not _level_allowed(kind, level):
        raise GrantError("invalid", "a terminal session can only be shared view only")
    _accepted(level, accept_risk)
    seconds = _expiry(level, expires_in)
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
        _not_held(level, key)
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
    if isinstance(exc, store.Busy | store.StoreBusy):
        return "busy"  # a delivery under the grant, or the store's file locked (#2228)
    return "store"


def change(
    st: store.Store,
    grant_id: object,
    *,
    expires_in: object,
    revision: object,
    level: object = None,
    accept_risk: object = False,
) -> store.Grant:
    """A new expiry (counted from now), a new level, or both. Bumps the revision, under the
    grant's fence.

    This is the ONLY way a grant is extended or raised, and only the operator route calls it.
    A grant that ends up at ``send`` (raised to it, or a ``send`` grant given more time) needs
    ``accept_risk: true`` in this request and a session no mission holds, exactly as at creation.
    A level change without ``expires_in`` never extends: the grant keeps its expiry, or the new
    level's default from now when that is sooner."""
    if not valid_grant_id(grant_id):
        raise GrantError("not_found", "unknown grant", status=404)
    if not valid_revision(revision):
        raise GrantError("invalid", "revision must be the grant's current revision")
    current = st.get_grant(grant_id)
    if current is None:
        raise GrantError("not_found", "unknown grant", status=404)
    if not _level_allowed(current.kind, current.level):
        raise GrantError("invalid", "this grant's level cannot be changed by this build")
    if level is not None and (not isinstance(level, str) or level not in LEVELS):
        raise GrantError("invalid", "this level cannot be granted")
    target = current.level if level is None else level
    if not _level_allowed(current.kind, target):
        raise GrantError("invalid", "a terminal session can only be shared view only")
    if expires_in is None and level is None:
        raise GrantError("invalid", "expires_in or level is required")
    _accepted(target, accept_risk)
    now = _clock()
    if expires_in is None:
        expires_at = min(current.expires_at, now + DEFAULT_EXPIRY_S[target])
    else:
        expires_at = now + _expiry(target, expires_in)
    _not_held(target, current.session_key)
    try:
        return st.change_grant(
            grant_id, expires_at=expires_at, revision=revision, level=level, now=now
        )
    except store.StoreError as exc:
        raise GrantError(_code(exc), str(exc), status=exc.status) from exc


def revoke(st: store.Store, grant_id: object) -> store.Grant:
    """End one grant, under its fence. Idempotent for a grant that already ended. While a
    delivery under it is in flight it waits (``fence.REVOKE_WAIT_S``); past that it commits
    nothing and raises ``busy`` (503): the grant is still live and the operator retries."""
    if not valid_grant_id(grant_id):
        raise GrantError("not_found", "unknown grant", status=404)
    try:
        return st.revoke_grant(grant_id, now=_clock())
    except store.StoreError as exc:
        raise GrantError(_code(exc), str(exc), status=exc.status) from exc


#: How long ``share.leave`` waits for the grant's fence (a delivery under the grant is in
#: flight): well inside the peer's own call timeout (``channel.CALL_TIMEOUT_S``), so ``busy``
#: arrives as an answer and the peer is not left guessing.
LEAVE_WAIT_S = 10.0


def leave(st: store.Store, grant_id: object, peer_id: str, revision: object) -> store.Grant | None:
    """THE PEER ends a grant it holds (``share.leave``, #2201). The grant as it was when it
    ended, or ``None``: the one refusal, whatever failed.

    Two steps, and only the second decides. (1) The plain per-call read (:func:`_granted`: this
    peer's own grant, live, unexpired, at this revision, the peer trusted; any kind, any level).
    A call that fails it goes no further, so nobody waits on, or learns anything from, the
    fence of a grant that is not theirs. (2) ``Store.leave_grant``: INSIDE the grant's fence
    and one transaction all of that is read again and the grant is ended there, through the
    cascade a revoke uses. A grant that changed, was revoked or expired while this waited for
    the fence is refused and nothing ends. No session is read: giving a grant up needs none.

    A fence that cannot be had inside :data:`LEAVE_WAIT_S` raises ``store.Busy``: nothing was
    committed. Blocking."""
    if _granted(st, grant_id, peer_id, revision) is None:
        return None
    try:
        return st.leave_grant(
            grant_id, peer_id=peer_id, revision=revision, clock=_clock, wait=LEAVE_WAIT_S
        )
    except store.NotGranted as exc:
        return _refused(f"changed before the leave ({exc})", peer_id, grant_id)


def listing(st: store.Store, session_key: object = None) -> list[store.Grant]:
    """The live grants, of one session when ``session_key`` is given."""
    key = None
    if session_key:
        prov, native, _kind = _shareable(session_key)
        key = f"{prov.engine_id}:{native}"
    return st.list_grants(session_key=key, now=_clock())


def titles(session_keys: list[str]) -> dict[str, str]:
    """``{session key: display title}`` for the OPERATOR's own lists (never sent to a peer:
    ``sharing._list_entry`` builds that one, redacted). One provider scan per engine; a session
    that cannot be read is simply absent."""
    out: dict[str, str] = {}
    scans: dict[str, dict] = {}
    meta = None
    for key in session_keys:
        engine, _, native = key.partition(":")
        if engine not in scans:
            prov = engines.get_any(engine)
            try:
                scans[engine] = {s.uuid: s for s in prov.scan()} if prov else {}
            except Exception:  # noqa: BLE001 — an unreadable engine store titles nothing
                scans[engine] = {}
        row = scans[engine].get(native)
        if row is None:
            continue
        if meta is None:
            meta = metadata.load()
        out[key] = " ".join(
            metadata.display_title(
                meta.get(key) or metadata.SessionMeta(),
                getattr(row, "first_user_message", "") or "",
                use_ai_title=prefs.renames_sessions(),
            ).split()
        )[:200]
    return out


def sweep(st: store.Store) -> list[store.Grant]:
    """End what expired: grants (each under its fence), pending suggestions and pending access
    requests; and delete the notes of grants that ended more than seven days ago (#2059). The
    per-call check never depends on this having run."""
    now = _clock()
    ended = st.expire_due(now=now)
    st.expire_suggestions(now=now)
    st.expire_access_requests(now=now)
    st.purge_notes(now=now)
    return ended


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
    st: store.Store,
    grant_id: object,
    peer_id: str,
    revision: object,
    *,
    kind: str | None = None,
    need: str = "view",
) -> store.Grant | None:
    """The store's half of the check: the grant exists, its principal is this peer, it is live
    and not expired on this instance's clock, the revision is the current one, the peer is still
    trusted (a revoke by any process over this store counts), the level is one this build serves
    for the grant's kind and includes ``need`` and, when the caller names one, the grant is of
    that ``kind``. One statement reads the grant and the peer together."""
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
    if not store.level_includes(grant.level, need):
        return _refused(f"level {grant.level} does not include {need}", peer_id, grant_id)
    return grant


def authorize(
    st: store.Store,
    grant_id: object,
    peer_id: str,
    revision: object,
    *,
    kind: str,
    need: str = "view",
) -> store.Grant | None:
    """THE per-call check. The grant when ``peer_id`` (the channel's authenticated peer) may do
    what ``need`` names (``view``, ``suggest`` or ``send``) with its session RIGHT NOW with the
    revision it holds, through a verb that reads sessions of ``kind``, else ``None``. A terminal
    grant is ``view`` only, so it never passes a check that needs ``suggest`` or ``send``.

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
    first = _granted(st, grant_id, peer_id, revision, kind=kind, need=need)
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
    grant = _granted(st, grant_id, peer_id, revision, kind=kind, need=need)
    if grant is None:
        return None
    if grant.session_key != first.session_key:
        return _refused("the grant names another session now", peer_id, grant_id)
    return grant


def standing(st: store.Store, grant_id: str, peer_id: str, need: str, *, kind: str) -> str:
    """An OWNER-side re-check of a grant, for a delivery that holds no revision from a peer (an
    approval and its replay, inside the grant's fence): ``ok``, ``gone`` or ``unreadable``.

    ``gone`` is definite: the grant is not live, expired on this clock, below ``need``, not of
    ``kind`` (or of a kind or level this build does not serve), its peer is no longer trusted,
    or its session is archived, no longer there or no longer of the grant's kind.
    ``unreadable`` decides nothing (a store or a session state that could not be read): the
    caller neither delivers nor gives the approval up."""
    try:
        grant, peer_state = st.grant_and_peer_state(grant_id, peer_id)
    except (store.StoreError, OSError, ValueError):
        return "unreadable"
    if (
        peer_state != "trusted"
        or grant is None
        or grant.principal != store.principal_of(peer_id)
        or grant.state != "live"
        or grant.expires_at <= _clock()
        or grant.kind not in KINDS
        or grant.kind != kind
        or not _level_allowed(grant.kind, grant.level)
        or not store.level_includes(grant.level, need)
    ):
        return "gone"
    try:
        prov, native, now_kind = _shareable(grant.session_key)
    except GrantError:
        return "gone"
    if now_kind != grant.kind:
        return "gone"
    state = shares.session_state(prov, native)
    if state == "live":
        return "ok"
    return "unreadable" if state == "unreadable" else "gone"


def still_granted(st: store.Store, grant_id: object, peer_id: str, revision: object) -> bool:
    """Whether a PARKED call should keep waiting: the store's half of :func:`authorize` (a revoke
    of the grant or the peer, an expiry, a revision change, and an archive, which ends the grant
    in the store). It only ever ENDS a wait early. It never lets an answer out: that is
    :func:`authorize`, which runs again directly before the answer is returned."""
    return _granted(st, grant_id, peer_id, revision) is not None
