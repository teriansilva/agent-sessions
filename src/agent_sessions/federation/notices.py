"""The bell row and push for a pending suggestion, and its retraction (#2057).

**What it carries:** the peer's display name on this instance, the session's title and the link
to the session. **Never the suggested text**, the actor label, or anything else the peer wrote:
the row's reason is the fixed ``notifications.EVENT_REASON["peer_suggestion"]``.

**One row per suggestion**, keyed ``peer-suggestion:<suggestion id>``. The store's ``notified``
flag is set with the suggestion's insert, so a row that may exist is always known to the store.

**Retraction** (:func:`retract`) removes the rows of every suggestion that is no longer pending
(decided, withdrawn by its peer, expired, or withdrawn because its grant ended) and clears the
flag only for the rows it could remove. It is idempotent and runs after every such change in this
process and on every dialer round, which covers a change another process wrote.

Everything here blocks (two stores): call it off the event loop. It is best effort and never
raises: the suggestion is in the store and the operator's list shows it whatever the bell does.
"""

from __future__ import annotations

import contextlib
import logging

from .. import engines, metadata, notifications, prefs
from . import store

log = logging.getLogger(__name__)

ACTION_PREFIX = "peer-suggestion:"
UNNAMED_PEER = "A paired instance"


def notice_key(suggestion_id: str) -> str:
    """The bell row's action id. Server-minted; no text from anyone."""
    return f"{ACTION_PREFIX}{suggestion_id}"


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


def announce(st: store.Store, suggestion: store.Suggestion) -> None:
    """One bell row and one push for a suggestion that was just made."""
    key = notice_key(suggestion.suggestion_id)
    try:
        peer = st.get(suggestion.peer_id)
        name = peer.name if peer is not None and peer.name else UNNAMED_PEER
        rec = notifications.add(
            title=f"{name} suggests a message",
            project=_session_title(suggestion.session_key),
            session_id=suggestion.session_key,
            engine=suggestion.session_key.partition(":")[0],
            reason=notifications.EVENT_REASON["peer_suggestion"],
            action_id=key,
        )
        if rec and notifications.list_subscriptions():
            with contextlib.suppress(Exception):
                notifications.fanout(rec)
    except Exception:  # noqa: BLE001 — the bell is another store
        log.warning("federation: a suggestion's notification failed", exc_info=True)
    # Decided or withdrawn while the row was being written: a retraction that ran before the
    # row existed removed nothing, so it runs again now.
    retract(st)


def retract(st: store.Store) -> int:
    """Remove the bell rows of suggestions that are no longer pending. Returns how many
    suggestions were settled."""
    try:
        settled: list[str] = []
        for suggestion_id in st.unretracted():
            try:
                notifications.dismiss_for_action(notice_key(suggestion_id))
            except Exception:  # noqa: BLE001 — kept flagged: the next round tries again
                log.debug("federation: a suggestion's notification was not retracted")
                continue
            settled.append(suggestion_id)
        if settled:
            st.mark_retracted(settled)
        return len(settled)
    except Exception:  # noqa: BLE001
        log.debug("federation: suggestion notifications not retracted", exc_info=True)
        return 0
