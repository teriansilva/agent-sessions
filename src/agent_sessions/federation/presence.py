"""Whether a peer is looking at a session it was granted (#1246 §6, #2059).

**In memory only, per grant, 30 seconds.** A read verb (``session.snapshot``, ``session.events``)
that passed the per-call check touches its grant here. Nothing is stored: a restart forgets
everyone, and the next read of each open pane brings them back. An entry is one timestamp under
a grant id; it holds no session key, no name, no count and no device.

**Only under a live grant.** A grant is touched only by a call :func:`grants.authorize` let
through, and the operator's listing asks about the grants it lists, which are the live ones. A
grant that ended is forgotten when it ends and can never be touched again.

**The other direction** (is this instance's operator looking?) is not kept here: it is the
operator's own entry in :mod:`agent_sessions.presence`, which the operator's pane refreshes on
every snapshot and events read. :func:`owner_present` asks for exactly that one bit.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from .. import presence as viewers

#: How long after its last read a peer still counts as watching.
TTL_S = 30.0
#: Grants remembered at once. Past it the stalest goes: the table is keyed by grant ids a peer
#: holds, and must not grow without bound.
MAX_GRANTS = 4096


class Watching:
    """``{grant id: when its peer last read}``, on a monotonic clock."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}

    def touch(self, grant_id: str) -> None:
        now = self._clock()
        with self._lock:
            self._seen.pop(grant_id, None)  # re-insert: dict order is then oldest touch first
            self._seen[grant_id] = now
            for stale in [g for g, at in self._seen.items() if now - at > TTL_S]:
                del self._seen[stale]
            while len(self._seen) > MAX_GRANTS:
                del self._seen[next(iter(self._seen))]

    def watching(self, grant_id: str) -> bool:
        with self._lock:
            at = self._seen.get(grant_id)
            if at is None:
                return False
            if self._clock() - at > TTL_S:
                del self._seen[grant_id]
                return False
            return True

    def forget(self, grant_id: str) -> None:
        """The grant ended: nothing about it is kept."""
        with self._lock:
            self._seen.pop(grant_id, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)


def owner_present(session_key: str) -> bool:
    """Whether this instance's operator has that session's pane open in a browser right now: the
    operator's pane read it within the last 30 seconds (``routes/structured.py`` touches
    ``presence.OPERATOR`` on every snapshot and events read). One bit; not how many tabs, not
    which device, and nothing about any other session."""
    return viewers.default().has(session_key, viewers.OPERATOR)
