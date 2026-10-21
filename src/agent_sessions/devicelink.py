"""Device-authorization challenge store for QR cross-device sign-in (#650).

A user already signed in on one device (their phone) authorizes a NEW client to sign in by
scanning a QR the new client shows and tapping Approve. The session cookie is a signed
``itsdangerous`` token that is ``HttpOnly`` (see ``auth.py``), so it can never be read by
JS and encoded in a QR — instead this module is the server-side source of truth:

  * the new client ``start``s a challenge and gets a **public** ``challenge_id`` (shown in
    the QR / the approve URL) plus a **private** ``claim_token`` it alone polls with — so a
    bystander who photographs the QR still cannot claim the session,
  * an authenticated phone flips the challenge ``pending -> approved`` (first wins),
  * the waiting client ``claim``s the approved challenge for a real session **exactly once**.

In-process and bounded — single admin, so a small map with a short TTL is enough and needs
no schema change. One lock guards every transition, so approve/deny/consume are atomic and
two racing pollers can never both mint a session.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

# Short TTL: a stale or shoulder-surfed QR is worthless within seconds. Abuse cap so the
# unauthenticated POST /link/start can't be turned into an unbounded challenge factory.
TTL_SECONDS = 90
MAX_OUTSTANDING = 20


@dataclass
class Challenge:
    challenge_id: str  # public — carried by the QR + the approve URL
    claim_token: str  # private — held only by the client that started it
    created_at: float
    expires_at: float
    state: str = "pending"  # pending -> approved | denied -> consumed
    requester_ip: str = ""
    requester_ua: str = ""


class DeviceLinkStore:
    """Thread-safe, bounded, in-process store of outstanding device-link challenges."""

    def __init__(
        self,
        *,
        ttl: int = TTL_SECONDS,
        max_outstanding: int = MAX_OUTSTANDING,
        clock=time.time,
    ) -> None:
        self._ttl = ttl
        self._max = max_outstanding
        self._clock = clock  # injectable for tests (drive expiry without real sleeps)
        self._lock = threading.Lock()
        self._by_id: dict[str, Challenge] = {}
        self._by_claim: dict[str, str] = {}  # claim_token -> challenge_id

    # -- internal (call under the lock) ------------------------------------------------

    def _prune(self, now: float) -> None:
        dead = [cid for cid, ch in self._by_id.items() if ch.expires_at <= now]
        for cid in dead:
            ch = self._by_id.pop(cid, None)
            if ch is not None:
                self._by_claim.pop(ch.claim_token, None)

    @staticmethod
    def _live(ch: Challenge | None, now: float) -> bool:
        return ch is not None and ch.expires_at > now

    # -- public API --------------------------------------------------------------------

    def start(self, *, ip: str = "", ua: str = "") -> Challenge | None:
        """Create a fresh pending challenge. Returns ``None`` when the outstanding-pending
        cap is already reached (caller -> 429), so an unauth flood can't exhaust memory."""
        now = self._clock()
        with self._lock:
            self._prune(now)
            pending = sum(1 for ch in self._by_id.values() if ch.state == "pending")
            if pending >= self._max:
                return None
            ch = Challenge(
                challenge_id=secrets.token_urlsafe(24),
                claim_token=secrets.token_urlsafe(24),
                created_at=now,
                expires_at=now + self._ttl,
                requester_ip=ip,
                requester_ua=ua,
            )
            self._by_id[ch.challenge_id] = ch
            self._by_claim[ch.claim_token] = ch.challenge_id
            return ch

    def get(self, challenge_id: str) -> Challenge | None:
        """The live challenge for the approval page to render, else ``None`` (unknown /
        expired). Read-only — never mutates state."""
        now = self._clock()
        with self._lock:
            ch = self._by_id.get(challenge_id)
            return ch if self._live(ch, now) else None

    def approve(self, challenge_id: str) -> bool:
        """Atomically flip ``pending -> approved``. First caller wins; a second approve, or
        approving something denied / consumed / expired, returns ``False``."""
        now = self._clock()
        with self._lock:
            ch = self._by_id.get(challenge_id)
            if not self._live(ch, now) or ch.state != "pending":
                return False
            ch.state = "approved"
            return True

    def deny(self, challenge_id: str) -> bool:
        """Atomically flip ``pending -> denied``. Returns ``False`` if not pending/live."""
        now = self._clock()
        with self._lock:
            ch = self._by_id.get(challenge_id)
            if not self._live(ch, now) or ch.state != "pending":
                return False
            ch.state = "denied"
            return True

    def claim(self, claim_token: str) -> tuple[str, bool]:
        """The waiting client's single atomic status+consume, keyed by the PRIVATE token.

        Returns ``(state, minted)``. When the challenge is ``approved`` and not yet
        consumed, it transitions to ``consumed`` and ``minted`` is ``True`` — for the first
        caller only; every later or concurrent caller gets ``("consumed", False)``, so two
        racing pollers can never both mint a session. Otherwise ``minted`` is ``False`` and
        ``state`` is one of ``pending`` / ``denied`` / ``consumed`` / ``expired``.
        """
        now = self._clock()
        with self._lock:
            cid = self._by_claim.get(claim_token)
            ch = self._by_id.get(cid) if cid else None
            if not self._live(ch, now):
                return ("expired", False)
            if ch.state == "approved":
                ch.state = "consumed"
                return ("approved", True)
            return (ch.state, False)
