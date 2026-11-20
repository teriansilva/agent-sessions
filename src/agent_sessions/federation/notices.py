"""The bell rows and pushes for what a paired instance does, and their retraction (#2057, #2059).

**What a row carries:** the peer's display name on this instance, the session's title and a
link. **Never** a suggested text, a note's text, an access request's note, a question's text,
a tool payload, an actor label or anything else the other party wrote. Every title is built
here from a fixed sentence and the peer's name; every reason is a fixed
``notifications.EVENT_REASON`` string.

**The owner's rows** (this instance shares the session; the link is the local session):

* ``peer-suggestion:<suggestion id>``: a pending suggestion (#2057);
* ``peer-access:<request id>``: a pending access request;
* ``peer-note:<grant id>``: a peer's note the operator has not read. One row per thread.

**The receiver's rows** (another instance shares the session; the action id names the peer and
the grant, and the link is the remote pane): ``peer-remote-share:…`` (a session was shared, or
its level changed), ``peer-remote-ended:…``, ``peer-remote-decision:…`` (a request this
instance may answer) and ``peer-remote-note:…``. One row per grant and kind.

**Raised once.** A row is added only while no row with its action id is on the bell.

**Retraction** removes a row when what it says stopped being true. The owner's
(:func:`retract`) is decided from this instance's own store, so it is local and immediate; it is
idempotent, runs after every change in this process and on every dialer round, and clears a
``notified`` flag only for the rows it could remove. The receiver's rows are retracted by
:mod:`inbox` when this instance LEARNS that a grant ended or changed (a refused read, a complete
list refresh) and at once when the operator revokes the peer here.

Everything here blocks (two stores): call it off the event loop. It is best effort and never
raises: the stores hold the truth and the operator's lists show it whatever the bell does.
"""

from __future__ import annotations

import contextlib
import logging
import threading

from .. import engines, metadata, notifications, prefs
from . import store

log = logging.getLogger(__name__)

ACTION_PREFIX = "peer-suggestion:"
ACCESS_PREFIX = "peer-access:"
NOTE_PREFIX = "peer-note:"
REMOTE_PREFIX = "peer-remote-"
REMOTE_KINDS = ("share", "ended", "decision", "note")
UNNAMED_PEER = "A paired instance"
#: The level a request asks for, as the bell says it. A fixed word per level.
_LEVEL_WORDS = {"suggest": "Suggest", "send": "Send"}
#: One announcement at a time: "is there a row" and "add the row" are two steps of one decision.
_announcing = threading.Lock()


def notice_key(suggestion_id: str) -> str:
    """The bell row's action id. Server-minted; no text from anyone."""
    return f"{ACTION_PREFIX}{suggestion_id}"


def access_key(request_id: str) -> str:
    return f"{ACCESS_PREFIX}{request_id}"


def note_key(grant_id: str) -> str:
    return f"{NOTE_PREFIX}{grant_id}"


def remote_key(kind: str, peer_id: str, grant_id: str) -> str:
    """A receiver-side row's action id (``notifications._REMOTE_ACTION_RE`` is its shape)."""
    if kind not in REMOTE_KINDS:
        raise ValueError("unknown notice kind")
    return f"{REMOTE_PREFIX}{kind}:{peer_id}:{grant_id}"


def _session_title(session_key: str) -> str:
    """The session's own display title (the owner's), or empty."""
    try:
        engine, _, native = session_key.partition(":")
        meta = metadata.load().get(session_key) or metadata.SessionMeta()
        first = ""
        prov = engines.get_any(engine)
        if prov is not None:
            for row in prov.scan():
                if getattr(row, "uuid", None) == native:
                    first = getattr(row, "first_user_message", "") or ""
                    break
        return " ".join(
            metadata.display_title(meta, first, use_ai_title=prefs.renames_sessions()).split()
        )
    except Exception:  # noqa: BLE001 — a title that cannot be read is an empty title
        return ""


def _peer_name(st: store.Store, peer_id: str) -> str:
    peer = st.get(peer_id)
    return peer.name if peer is not None and peer.name else UNNAMED_PEER


def _post(
    *, key: str, title: str, project: str, reason: str, session_id: str = "", engine: str = ""
) -> bool:
    """ONE bell row and one push under ``key``, unless a row with that key is already there.
    Named, fixed fields only: nothing a caller holds of the other party's text fits in here."""
    with _announcing:
        if key in notifications.actions_with_prefix(key):
            return False
        rec = notifications.add(
            title=title,
            project=project,
            session_id=session_id,
            engine=engine,
            reason=notifications.EVENT_REASON[reason],
            action_id=key,
        )
    if rec and notifications.list_subscriptions():
        with contextlib.suppress(Exception):
            notifications.fanout(rec)
    return bool(rec)


def _dismiss(key: str) -> int | None:
    """How many rows went, or ``None`` when the bell could not be written (tried again later)."""
    try:
        return notifications.dismiss_for_action(key)
    except Exception:  # noqa: BLE001 — the next round tries again
        log.debug("federation: a notification was not retracted")
        return None


# ── the owner's rows ──────────────────────────────────────────────────
def announce(st: store.Store, suggestion: store.Suggestion) -> None:
    """One bell row and one push for a suggestion that was just made."""
    try:
        _post(
            key=notice_key(suggestion.suggestion_id),
            title=f"{_peer_name(st, suggestion.peer_id)} suggests a message",
            project=_session_title(suggestion.session_key),
            session_id=suggestion.session_key,
            engine=suggestion.session_key.partition(":")[0],
            reason="peer_suggestion",
        )
        # Decided or withdrawn while the row was being written: a retraction that ran before
        # the row existed removed nothing (and cleared its flag), so the row is judged again
        # now, against the suggestion as it stands.
        if st.suggestion_state(suggestion.suggestion_id) != "pending":
            _dismiss(notice_key(suggestion.suggestion_id))
    except Exception:  # noqa: BLE001 — the bell is another store
        log.warning("federation: a suggestion's notification failed", exc_info=True)
    retract(st)


def announce_access(st: store.Store, request: store.AccessRequest) -> None:
    """One bell row and one push for an access request that was just made. Never its note."""
    try:
        _post(
            key=access_key(request.request_id),
            # The level is one of two server-validated words (`suggest`, `send`), never text
            # the peer wrote (#2062: the row says what is asked for).
            title=(
                f"{_peer_name(st, request.peer_id)} asks for"
                f" {_LEVEL_WORDS.get(request.level, 'more')} access"
            ),
            project=_session_title(request.session_key),
            session_id=request.session_key,
            engine=request.session_key.partition(":")[0],
            reason="peer_access_request",
        )
        # Answered while the row was being written (as for a suggestion): judged again now.
        current = st.get_access_request(request.request_id)
        if current is None or current.state != "pending":
            _dismiss(access_key(request.request_id))
    except Exception:  # noqa: BLE001 — the bell is another store
        log.warning("federation: an access request's notification failed", exc_info=True)
    retract(st)


def announce_note(st: store.Store, grant: store.Grant) -> None:
    """One bell row and one push for a thread that holds a peer's note the operator has not
    read. Never the note's text. A second unread note adds nothing."""
    try:
        if st.unread_notes(grant.grant_id, store.AUTHOR_OWNER) > 0:
            _post(
                key=note_key(grant.grant_id),
                title=f"{_peer_name(st, grant.peer_id)} left a note",
                project=_session_title(grant.session_key),
                session_id=grant.session_key,
                engine=grant.session_key.partition(":")[0],
                reason="peer_note",
            )
    except Exception:  # noqa: BLE001 — the bell is another store
        log.warning("federation: a note's notification failed", exc_info=True)
    retract(st)


def _retract_flagged(pending: list[str], key, mark) -> int:
    settled = [item for item in pending if _dismiss(key(item)) is not None]
    if settled:
        mark(settled)
    return len(settled)


def retract(st: store.Store) -> int:
    """Remove the owner's rows that stopped being true: a suggestion or an access request that
    is no longer pending (decided, granted, declined, withdrawn, expired, or ended with its
    grant), and a thread's note row once nothing unread is left in it (read, deleted, hidden)
    or its grant is not live. Returns how many rows' subjects were settled."""
    count = 0
    try:
        count += _retract_flagged(st.unretracted(), notice_key, st.mark_retracted)
    except Exception:  # noqa: BLE001
        log.debug("federation: suggestion notifications not retracted", exc_info=True)
    try:
        count += _retract_flagged(st.unretracted_access(), access_key, st.mark_access_retracted)
    except Exception:  # noqa: BLE001
        log.debug("federation: access notifications not retracted", exc_info=True)
    try:
        rows = notifications.actions_with_prefix(NOTE_PREFIX)
        if rows:
            unread = st.note_grants_unread()
            for key in rows:
                grant_id = key[len(NOTE_PREFIX) :]
                # Only a row about a grant of THIS store is this store's to judge.
                if grant_id not in unread and st.get_grant(grant_id) is not None:
                    count += _dismiss(key) or 0
    except Exception:  # noqa: BLE001
        log.debug("federation: note notifications not retracted", exc_info=True)
    return count


# ── the receiver's rows ───────────────────────────────────────────────
_REMOTE_TITLES = {
    "shared": ("share", "peer_shared", "{name} shared a session with you"),
    "changed": ("share", "peer_share_changed", "{name} changed what it shares with you"),
    "ended": ("ended", "peer_share_ended", "{name} stopped sharing a session"),
    "decision": ("decision", "peer_decision", "{name}: a decision is waiting"),
    "note": ("note", "peer_note", "{name} left a note"),
}


def announce_remote(
    st: store.Store, event: str, peer_id: str, grant_id: str, title: str, *, replace: bool = False
) -> bool:
    """One bell row and one push about a session ``peer_id`` shares with this instance.
    ``title`` is the session's title as the mirror holds it (validated when it was stored).
    ``replace`` first removes the row of the same kind (a share that changed again)."""
    kind, reason, sentence = _REMOTE_TITLES[event]
    try:
        key = remote_key(kind, peer_id, grant_id)
        if replace:
            _dismiss(key)
        return _post(
            key=key,
            title=sentence.format(name=_peer_name(st, peer_id)),
            project=title,
            reason=reason,
        )
    except Exception:  # noqa: BLE001 — the bell is another store
        log.warning("federation: a remote session's notification failed", exc_info=True)
        return False


def retract_remote(peer_id: str, grant_id: str, kinds: tuple[str, ...] = REMOTE_KINDS) -> int:
    """Remove this instance's rows about one grant of a peer (the kinds named)."""
    return sum(_dismiss(remote_key(kind, peer_id, grant_id)) or 0 for kind in kinds)


def retract_peer(peer_id: str) -> int:
    """Remove EVERY receiver-side row about ``peer_id``: the operator revoked it here."""
    try:
        marker = f":{peer_id}:"
        rows = [k for k in notifications.actions_with_prefix(REMOTE_PREFIX) if marker in k]
        return sum(_dismiss(key) or 0 for key in rows)
    except Exception:  # noqa: BLE001
        log.debug("federation: a revoked peer's notifications not retracted", exc_info=True)
        return 0
