"""Who has an API session open right now (#1931): the "N connected" count.

**Advisory, in memory, process-local.** A viewer is connected while it has read the session within
``TTL_S``: the visitor view polls every 1–10 s (backing off to at most 15 s), the operator pane
long-polls without pause. Nothing is persisted: a restart forgets everyone, and the next poll of
each open page brings them back.

**Identities.** The operator is ONE viewer (``OPERATOR``) however many tabs are open. A visitor
is one viewer per unlocked browser, keyed by a hash of the share cookie's own random CSRF token, so
neither the token nor the cookie ever sits here.

**What leaves.** ``count`` is all a visitor is told. The operator's ``view`` adds the visitors'
self-chosen names — the operator already sees those on every turn a visitor sends.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

TTL_S = 30.0
#: Viewers kept per session. Past it the stalest is dropped: an unbounded table keyed by
#: cookie-derived ids would let one link's holder grow memory by unlocking over and over.
MAX_PER_SESSION = 200
#: Sessions kept at all; past it the session with the stalest newest touch goes.
MAX_SESSIONS = 1000
OPERATOR = "operator"


@dataclass
class _Seen:
    at: float
    name: str | None


class Presence:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._by_session: dict[str, dict[str, _Seen]] = {}

    def _live(self, seen: dict[str, _Seen], now: float) -> dict[str, _Seen]:
        for vid in [v for v, s in seen.items() if now - s.at > TTL_S]:
            del seen[vid]
        return seen

    def touch(self, session_key: str, viewer: str, *, name: str | None = None) -> None:
        now = self._clock()
        with self._lock:
            seen = self._by_session.get(session_key)
            if seen is None:
                if len(self._by_session) >= MAX_SESSIONS:
                    self._evict_session(now)
                seen = self._by_session[session_key] = {}
            self._live(seen, now)
            seen.pop(viewer, None)  # re-insert: dict order is then oldest-touch first
            seen[viewer] = _Seen(at=now, name=name)
            while len(seen) > MAX_PER_SESSION:
                # The stalest VISITOR goes; the operator is never pushed out by a crowd of
                # unlocks on one link (they'd otherwise vanish from their own count).
                victim = next(v for v in seen if v != OPERATOR)
                del seen[victim]

    def _evict_session(self, now: float) -> None:
        for key in [k for k, s in self._by_session.items() if not self._live(s, now)]:
            del self._by_session[key]
        if len(self._by_session) >= MAX_SESSIONS:
            stalest = min(
                self._by_session, key=lambda k: max(s.at for s in self._by_session[k].values())
            )
            del self._by_session[stalest]

    def _current(self, session_key: str) -> dict[str, _Seen]:
        now = self._clock()
        seen = self._by_session.get(session_key)
        if seen is None:
            return {}
        self._live(seen, now)
        if not seen:
            del self._by_session[session_key]
        return dict(seen)

    def count(self, session_key: str) -> int:
        with self._lock:
            return len(self._current(session_key))

    def view(self, session_key: str) -> dict:
        """The operator's view: the count, and each connected visitor's name (or None)."""
        with self._lock:
            seen = self._current(session_key)
        visitors = [{"name": s.name} for vid, s in seen.items() if vid != OPERATOR]
        return {"count": len(seen), "visitors": visitors}


def visitor_id(share_id: str, csrf: str) -> str:
    """One id per unlocked browser — never the token itself."""
    return "v:" + hashlib.sha256(f"{share_id}\0{csrf}".encode()).hexdigest()[:16]


_DEFAULT = Presence()


def default() -> Presence:
    return _DEFAULT
