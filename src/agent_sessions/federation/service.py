"""The federation service: invites, pairing, live peer channels and the dialer (#1246 §2–§4).

One :class:`Federation` per app process. It owns

* this instance's static key and peer id (:mod:`identity`),
* the pending **invites** it minted and the **pairings** in progress, both in memory and bounded
  (a restart drops them; the operator makes a new invite),
* one live :class:`channel.Channel` per trusted peer,
* the **dialer**, whose round also keeps shared sessions current (#2054): it ends expired
  grants, tells a connected peer when its grants changed (also when another process or an archive
  wrote the change), and refreshes the mirror of what each peer shares with this instance.

**One owner process per store.** Invites, pairings and channels are in-process state, so exactly
one app process may serve federation for a store: the one holding ``owner.lock`` (a ``flock``,
released by the kernel when that process dies, and then taken by the next one to ask). Every
other process over the same store fails closed: its ``/ws/fed`` refuses the upgrade and its
pairing routes answer 409. The one thing any process may do is **revoke**: that is a store write,
trust ends at once for every process, and the owner closes the live channel on its next sweep.

Trust is decided here and nowhere else: a ``peer`` channel is built only for a key
:func:`self.store.trusted_key` returns, and a ``pair`` channel only for a live, unused invite. Until
then the other side has received handshake frames and nothing else.

Pairing stores a peer only when BOTH operators confirmed the same short code. If the channel
drops between the two stores, one side trusts and the other does not: the untrusting side refuses
the next handshake, and the operator revokes and pairs again. Pairing alone shares nothing.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import fcntl
import json
import logging
import os
import random
import secrets
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import websockets
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .. import net_vetting
from ..homefree import peerpipe
from . import channel as ch
from . import endpoints as ep
from . import grants, identity, remote, store, terminal, verbs

log = logging.getLogger(__name__)

INVITE_PREFIX = "blfed1:"
INVITE_TTL_S = 600
INVITES_MAX = 5
INVITE_ATTEMPTS_MAX = 5
INVITE_TEXT_MAX = 4096
PAIRINGS_MAX = 8
HANDSHAKES_IN_FLIGHT_MAX = 8
#: How long a finished pairing stays listed, so both UIs can show how it ended.
PAIRING_LINGER_S = 120
SAS_INFO = b"battlelab-fed sas v1"
DIAL_TICK_S = 5.0
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 60.0
PING_EVERY_S = 30.0
DIAL_OPEN_TIMEOUT_S = 10.0
LOCK_FILE = "owner.lock"

PAIRING_STATES = ("dialing", "confirm", "done", "denied", "failed", "expired")


class FederationError(Exception):
    """An operator request this service refuses. ``status`` is the HTTP status to answer."""

    def __init__(self, code: str, detail: str, status: int = 422) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status


class DialError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


def short_code(handshake_hash: bytes) -> str:
    """The code both operators compare: six digits from the handshake transcript."""
    raw = HKDF(algorithm=hashes.SHA256(), length=4, salt=None, info=SAS_INFO).derive(handshake_hash)
    return f"{int.from_bytes(raw, 'big') % 1_000_000:06d}"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: object, size: int) -> bytes:
    if not isinstance(text, str) or len(text) > 2 * size:
        raise ValueError("bad field")
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if len(raw) != size:
        raise ValueError("bad field")
    return raw


def encode_invite(*, invite_id: str, public_key: bytes, secret: bytes, addresses: list[dict]):
    doc = {"v": 1, "i": invite_id, "k": _b64(public_key), "s": _b64(secret), "e": addresses}
    return INVITE_PREFIX + _b64(json.dumps(doc, separators=(",", ":")).encode())


def decode_invite(text: object) -> tuple[str, bytes, bytes, list[dict]]:
    """``(invite_id, public_key, secret, endpoints)`` or :class:`FederationError`."""
    bad = FederationError("bad_invite", "this is not a BattleLab pairing invite")
    if not isinstance(text, str):
        raise bad
    text = "".join(text.split())
    if not text.startswith(INVITE_PREFIX) or len(text) > INVITE_TEXT_MAX:
        raise bad
    try:
        body = text[len(INVITE_PREFIX) :]
        doc = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if not isinstance(doc, dict) or doc.get("v") != 1:
            raise ValueError("version")
        invite_id = doc.get("i")
        if not isinstance(invite_id, str) or not (8 <= len(invite_id) <= 64):
            raise ValueError("id")
        if not invite_id.isascii() or not invite_id.isalnum():
            raise ValueError("id")
        return invite_id, _unb64(doc.get("k"), 32), _unb64(doc.get("s"), 32), ep.clean(doc.get("e"))
    except (ValueError, TypeError) as exc:
        raise bad from exc


@dataclass
class Invite:
    id: str
    secret: bytes
    expires_at: float
    attempts: int = 0
    used: bool = False

    def live(self, now: float) -> bool:
        return not self.used and now < self.expires_at and self.attempts < INVITE_ATTEMPTS_MAX


@dataclass
class Pairing:
    id: str
    role: str  # "inviter" | "acceptor"
    expires_at: float
    state: str = "dialing"
    code: str = ""
    remote_key: bytes = b""
    remote_name: str = ""
    remote_endpoints: list[dict] = field(default_factory=list)
    local_confirmed: bool = False
    remote_confirmed: bool = False
    error: str = ""
    channel: ch.Channel | None = None
    ended_at: float = 0.0
    #: The acceptor's background dial, cancelled when the pairing ends while it still runs.
    task: asyncio.Task | None = None
    #: The timer that expires the pairing whether or not anybody polls.
    timer: asyncio.TimerHandle | None = None
    #: Set once the pairing reached a terminal state (also after the trust write of
    #: ``committing``). A denial that arrives during the write waits for this.
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    #: `store.fence` of the remote key, read when the LOCAL operator confirmed. The trust
    #: write is refused if the row changed since (a revoke above all).
    fence: int = 0
    #: The trust write, once claimed. Owned by the service, not by whoever triggered it.
    commit: asyncio.Task | None = None

    @property
    def open(self) -> bool:
        """Still undecided. ``committing`` is NOT open: both operators confirmed and the trust
        write is under way, so from that point nothing can end the pairing any other way."""
        return self.state in ("dialing", "confirm")

    def public(self) -> dict:
        # ``committing`` lasts for one store write and is shown as the state it came from.
        state = "confirm" if self.state == "committing" else self.state
        return {
            "id": self.id,
            "role": self.role,
            "state": state,
            "code": self.code if state == "confirm" else "",
            "peer_id": identity.peer_id(self.remote_key) if self.remote_key else "",
            "name": self.remote_name,
            "local_confirmed": self.local_confirmed,
            "remote_confirmed": self.remote_confirmed,
            "expires_at": self.expires_at,
            "error": self.error,
        }


class _ClientTransport:
    """A dialed ``websockets`` connection as a :class:`channel.FrameTransport`."""

    def __init__(self, conn) -> None:
        self._conn = conn

    async def send(self, data: bytes) -> None:
        try:
            await self._conn.send(data)
        except (websockets.ConnectionClosed, OSError) as exc:
            raise ch.ChannelClosed() from exc

    async def recv(self) -> bytes:
        try:
            data = await self._conn.recv()
        except (websockets.ConnectionClosed, OSError) as exc:
            raise ch.ChannelClosed() from exc
        if not isinstance(data, bytes):
            raise ch.ChannelClosed()  # a text frame is not part of this protocol
        return data

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._conn.close()


class _Connect(websockets.connect):
    """``websockets.connect`` that follows NO redirect. The address was vetted for this URL; a
    ``Location`` header is the other side choosing a new destination, which nobody vetted."""

    def process_redirect(self, exc: Exception) -> Exception | str:
        return exc


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def dial_direct(endpoint: dict) -> ch.FrameTransport:
    """Open a WebSocket to a peer's direct endpoint.

    ``net_vetting.vet`` speaks http(s), so the URL is vetted as its http(s) twin (``wss`` as
    ``https``, ``ws`` as ``http``) and ``net_vetting`` itself is unchanged for its other callers.
    The host is resolved ONCE there (no link-local or metadata addresses), and the TCP connection
    goes to that vetted address (``host=``/``port=``), so a name that answers differently a
    second time is never asked twice. The original URL still supplies the ``Host`` header and,
    for ``wss``, the TLS server name the certificate is verified against. No redirect is followed.
    ``ws://`` is allowed: the Noise handshake, not TLS, authenticates the peer and encrypts the
    channel.
    """
    url = endpoint["url"]
    parts = urlsplit(url)
    secure = parts.scheme == "wss"
    pinned = await net_vetting.vet(
        ("https" if secure else "http") + url[len(parts.scheme) :],
        resolve=_resolve,
        error=DialError,
    )
    port = parts.port or (443 if secure else 80)
    try:
        conn = await _Connect(
            url,
            host=pinned.address,
            port=port,
            proxy=None,
            compression=None,
            max_size=ch.WIRE_MAX,
            open_timeout=DIAL_OPEN_TIMEOUT_S,
            ping_interval=PING_EVERY_S,
        )
    except (OSError, TimeoutError, websockets.WebSocketException) as exc:
        raise DialError("unreachable", type(exc).__name__) from exc
    return _ClientTransport(conn)


def relay_socket_path() -> Path:
    """The local socket of this instance's Home Free agent (``homefree/peerpipe.py``): beside
    the agent's identity, where the installer keeps it. Override:
    ``AGENT_SESSIONS_HOMEFREE_PEER_SOCKET``."""
    override = os.environ.get("AGENT_SESSIONS_HOMEFREE_PEER_SOCKET")
    if override:
        return Path(override)
    home = os.environ.get("AGENT_SESSIONS_HOME") or str(
        Path.home() / ".local" / "share" / "agent-sessions"
    )
    return Path(home) / "homefree" / peerpipe.SOCKET_NAME


async def _agent_request(request: dict, path: Path | None = None):
    """Ask the local Home Free agent. Returns ``(answer, reader, writer)``; raises
    :class:`DialError` when there is no agent to ask."""
    try:
        async with asyncio.timeout(peerpipe.PAIR_TIMEOUT_S + 5):
            reader, writer = await asyncio.open_unix_connection(str(path or relay_socket_path()))
            peerpipe.write_frame(writer, json.dumps(request, separators=(",", ":")).encode())
            await writer.drain()
            raw = await peerpipe.read_frame(reader, peerpipe.REQUEST_MAX)
    except (OSError, TimeoutError, ValueError) as exc:
        raise DialError("no_relay", type(exc).__name__) from exc
    try:
        answer = json.loads(raw) if raw else None
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        writer.close()
        raise DialError("no_relay", "the Home Free agent did not answer")
    return answer, reader, writer


async def relay_info(path: Path | None = None) -> dict | None:
    """This instance's own relay endpoint, or ``None`` when Home Free is not running here (or
    its agent predates federation)."""
    try:
        answer, _reader, writer = await _agent_request({"op": "info"}, path)
    except DialError:
        return None
    writer.close()
    found = ep.clean([{"t": "relay", "url": answer.get("relay_url"), "name": answer.get("name")}])
    return found[0] if answer.get("ok") is True and found else None


class _AgentTransport:
    """A peer call placed by the local Home Free agent, as a :class:`channel.FrameTransport`.
    The agent is a blind pipe: what travels here is already Noise ciphertext."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader, self._writer = reader, writer

    async def send(self, data: bytes) -> None:
        try:
            peerpipe.write_frame(self._writer, data)
            await self._writer.drain()
        except (ConnectionError, OSError) as exc:
            raise ch.ChannelClosed() from exc

    async def recv(self) -> bytes:
        try:
            frame = await peerpipe.read_frame(self._reader, ch.WIRE_MAX)
        except ValueError as exc:
            raise ch.ChannelClosed() from exc
        if frame is None:
            raise ch.ChannelClosed()
        return frame

    async def close(self) -> None:
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()


async def dial_relay(
    endpoint: dict, own: dict | None, path: Path | None = None
) -> ch.FrameTransport:
    """Reach a peer by its console name through the relay. Only on THIS instance's own relay:
    the agent dials nothing but the relay it is configured for, so an endpoint naming another
    relay is not reachable from here."""
    if own is None:
        raise DialError("no_relay", "Home Free is not set up on this instance")
    if endpoint["url"] != own["url"]:
        raise DialError("other_relay", "the peer is on a different relay")
    answer, reader, writer = await _agent_request({"op": "call", "target": endpoint["name"]}, path)
    if answer.get("ok") is not True:
        writer.close()
        code = answer.get("code")
        raise DialError(code if isinstance(code, str) and len(code) <= 32 else "unreachable")
    return _AgentTransport(reader, writer)


Dial = Callable[[dict], Awaitable[ch.FrameTransport]]


def _current_task() -> asyncio.Task | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _open_pairings(pairings: dict) -> int:
    return sum(1 for p in pairings.values() if p.open or p.state == "committing")


class Federation:
    def __init__(
        self,
        *,
        origin: str | None = None,
        addresses: list[dict] | None = None,
        name: str | None = None,
        dial: Dial | None = None,
        static: X25519PrivateKey | None = None,
        directory: Path | None = None,
    ) -> None:
        self._static = static
        #: Where the key, the store and the dialer lock live. ``None`` follows the environment.
        self._directory = directory
        self.store = store.Store(directory)
        self.error = ""
        self.addresses = ep.clean(addresses) if addresses is not None else ep.from_origin(origin)
        self._name = name
        #: This instance's own relay endpoint, refreshed from the local Home Free agent. ``None``
        #: without Home Free. ``addresses`` stays what the operator's origin (or a test) gave.
        self.relay_address: dict | None = None
        #: The local Home Free agent's socket. ``None`` follows the environment.
        self.relay_socket: Path | None = None
        self._custom_dial = dial
        self._invites: dict[str, Invite] = {}
        self._pairings: dict[str, Pairing] = {}
        self._channels: dict[str, ch.Channel] = {}
        self._refused: set[str] = set()
        self._handshakes = 0
        #: Inbound PAIRING handshakes in flight; each holds one pairing slot (`_slots_used`).
        self._pair_handshakes = 0
        #: Trust writes under way. `stop` waits for them instead of cancelling.
        self._commits: set[asyncio.Task] = set()
        self._tasks: set[asyncio.Task] = set()
        self._dialing: set[str] = set()
        self._backoff: dict[str, tuple[float, float]] = {}  # peer id -> (delay, not before)
        self._lock_fd: int | None = None
        self._dialer_task: asyncio.Task | None = None
        self._stopped = False
        self._kick = asyncio.Event()
        #: Parked `session.events` waits per peer (`sharing.WAITS_PER_PEER` bounds it).
        self.waits: dict[str, int] = {}
        #: Parked `terminal.frame` waits per peer (`terminal.FRAME_WAITS_PER_PEER` bounds it),
        #: and the shared renders of the terminal screens peers watch (#2056).
        self.frame_waits: dict[str, int] = {}
        self.renders = terminal.Renders()
        # Shared sessions (#2054): the newest grant event seen per principal, and the mirror's
        # refresh bookkeeping (one refresh per peer at a time; a hint during one runs another).
        self._generation: int | None = None
        self._marks: dict[str, int] = {}
        self._refreshing: set[str] = set()
        self._refresh_again: set[str] = set()
        self._refreshed_at: dict[str, float] = {}

    # ── addresses and dialing ─────────────────────────────────────────
    def reachable(self) -> list[dict]:
        """Every way another instance can reach this one, direct first."""
        extra = [self.relay_address] if self.relay_address else []
        return ep.clean([*self.addresses, *extra])

    async def refresh_relay_address(self) -> None:
        if self._custom_dial is None:
            self.relay_address = await relay_info(self.relay_socket)

    async def _dial(self, endpoint: dict) -> ch.FrameTransport:
        if self._custom_dial is not None:
            return await self._custom_dial(endpoint)
        if endpoint.get("t") == "relay":
            return await dial_relay(endpoint, self.relay_address, self.relay_socket)
        return await dial_direct(endpoint)

    # ── identity ──────────────────────────────────────────────────────
    def _key(self) -> X25519PrivateKey:
        if self._static is None:
            try:
                self._static = identity.load_or_create(self._directory)
            except (identity.IdentityError, OSError) as exc:
                self.error = str(exc)
                raise FederationError("unavailable", self.error, status=503) from exc
        return self._static

    @property
    def public_key(self) -> bytes:
        return identity.public_key(self._key())

    @property
    def peer_id(self) -> str:
        return identity.peer_id(self.public_key)

    @property
    def name(self) -> str:
        raw = self._name or socket.gethostname() or ""
        try:
            return store.clean_name(raw, fallback="BattleLab")
        except store.Invalid:
            return "BattleLab"

    # ── lifecycle ─────────────────────────────────────────────────────
    @property
    def owner(self) -> bool:
        """Whether THIS process serves federation for the store (see the module docstring)."""
        return not self._stopped and self._hold_owner_lock()

    def _require_owner(self) -> None:
        if not self.owner:
            raise FederationError(
                "not_owner",
                "another BattleLab process on this store is running federation",
                status=409,
            )

    def start(self) -> None:
        """Take ownership if it is free and start the dialer. A process that is not the owner
        keeps asking, so it takes over when the owner goes away."""
        self._hold_owner_lock()
        if self._dialer_task is None:
            self._dialer_task = asyncio.create_task(self._dialer())

    async def stop(self) -> None:
        self._stopped = True
        for channel in list(self._channels.values()):
            channel.close("shutdown")
        for pairing in self._pairings.values():
            if pairing.timer is not None:
                pairing.timer.cancel()
                pairing.timer = None
            if pairing.channel is not None:
                pairing.channel.close("shutdown")
        # A trust write under way is never abandoned: its thread would finish the write anyway,
        # so it is waited for and the pairing settles from what was stored.
        for commit in list(self._commits):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(commit)
        tasks = [
            t for t in (*self._tasks, self._dialer_task) if t is not None and t not in self._commits
        ]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._lock_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._lock_fd)
            self._lock_fd = None

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ── housekeeping ──────────────────────────────────────────────────
    def _sweep(self) -> None:
        now = time.time()
        for invite_id in [i for i, inv in self._invites.items() if not inv.live(now)]:
            del self._invites[invite_id]
        for pairing in list(self._pairings.values()):
            if pairing.open and now >= pairing.expires_at:
                self._end_pairing(pairing, "expired", "the pairing timed out")
            elif pairing.settled.is_set() and now - pairing.ended_at > PAIRING_LINGER_S:
                del self._pairings[pairing.id]

    def _slots_used(self) -> int:
        """Pairings waiting on an operator PLUS inbound pairing handshakes still in flight. A
        handshake holds its slot from its first message, so parallel handshakes on distinct
        invites cannot all pass a check made before any of them finished."""
        return _open_pairings(self._pairings) + self._pair_handshakes

    def _register(self, pairing: Pairing) -> None:
        """Add a pairing and arm its deadline. The timer makes expiry independent of anybody
        polling; every path that could finish a pairing checks the deadline itself as well."""
        self._pairings[pairing.id] = pairing
        delay = max(0.0, pairing.expires_at - time.time()) + 0.05
        pairing.timer = asyncio.get_running_loop().call_later(delay, self._expire_due, pairing)

    def _expire_due(self, pairing: Pairing) -> bool:
        """End ``pairing`` as expired when its deadline has passed. True when it is not open
        afterwards."""
        if pairing.open and time.time() >= pairing.expires_at:
            self._end_pairing(pairing, "expired", "the pairing timed out")
        return not pairing.open

    def _end_pairing(self, pairing: Pairing, state: str, error: str = "") -> None:
        """Move an OPEN pairing to a terminal state. A pairing that already ended, or whose
        trust write is under way (``committing``), is left alone: a terminal state is never
        replaced and never reopened."""
        if not pairing.open:
            return
        dialing = pairing.state == "dialing"
        pairing.state = state
        self._settle(pairing, error)
        task = pairing.task
        if dialing and task is not None and not task.done() and task is not _current_task():
            task.cancel()  # a dial still under way must not bring the pairing back

    def _settle(self, pairing: Pairing, error: str = "") -> None:
        pairing.error = error
        pairing.ended_at = time.time()
        pairing.settled.set()
        if pairing.timer is not None:
            pairing.timer.cancel()
            pairing.timer = None
        if pairing.channel is not None:
            # Let an answer already queued reach the other side before the socket goes.
            asyncio.get_running_loop().call_later(0.5, pairing.channel.close, pairing.state)

    # ── operator actions ──────────────────────────────────────────────
    def snapshot(self) -> dict:
        self._sweep()
        owner = self.owner
        peers = []
        for peer in self.store.list_peers():
            live = self._channels.get(peer.peer_id)
            peers.append(
                {
                    "peer_id": peer.peer_id,
                    "name": peer.name,
                    "state": peer.state,
                    # Channels live in the owner process. Any other process cannot know, and
                    # says so with null rather than claiming "offline".
                    "connected": bool(live and not live.closed) if owner else None,
                    "refused": peer.peer_id in self._refused,
                    "reachable_by": sorted({e["t"] for e in peer.endpoints}),
                    "created_at": peer.created_at,
                    "last_seen_at": peer.last_seen_at,
                    "revision": peer.revision,
                }
            )
        return {
            "owner": owner,
            "self": {
                "peer_id": self.peer_id,
                "name": self.name,
                "reachable_by": sorted({e["t"] for e in self.reachable()}),
            },
            "peers": peers,
            "invites": [
                {"id": inv.id, "expires_at": inv.expires_at} for inv in self._invites.values()
            ],
            "pairings": [p.public() for p in self._pairings.values()],
        }

    def create_invite(self) -> dict:
        self._require_owner()
        self._sweep()
        if not self.reachable():
            raise FederationError(
                "no_address",
                "this instance has no address another instance can reach",
                status=409,
            )
        if len(self._invites) >= INVITES_MAX:
            raise FederationError("too_many", "too many open invites", status=409)
        if self._slots_used() >= PAIRINGS_MAX:
            raise FederationError("too_many", "too many pairings in progress", status=409)
        invite = Invite(
            id=secrets.token_hex(8),
            secret=secrets.token_bytes(32),
            expires_at=time.time() + INVITE_TTL_S,
        )
        self._invites[invite.id] = invite
        return {
            "id": invite.id,
            "expires_at": invite.expires_at,
            # Shown once. It is never returned again and never logged.
            "invite": encode_invite(
                invite_id=invite.id,
                public_key=self.public_key,
                secret=invite.secret,
                addresses=self.reachable(),
            ),
        }

    def cancel_invite(self, invite_id: str) -> None:
        self._require_owner()
        if self._invites.pop(invite_id, None) is None:
            raise FederationError("not_found", "unknown invite", status=404)

    def accept_invite(self, text: object) -> dict:
        """Start pairing with the instance that minted ``text``. Returns the pairing; the dial
        runs in the background and the pairing's ``state`` says how it went."""
        self._require_owner()
        self._sweep()
        invite_id, key, secret, addresses = decode_invite(text)
        if key == self.public_key:
            raise FederationError("self", "this invite was made by this instance")
        if not addresses:
            raise FederationError("no_address", "this invite has no address this build can dial")
        if self._slots_used() >= PAIRINGS_MAX:
            raise FederationError("too_many", "too many pairings in progress", status=409)
        pairing = Pairing(
            id=secrets.token_hex(8),
            role="acceptor",
            expires_at=time.time() + INVITE_TTL_S,
            remote_key=key,
            remote_endpoints=addresses,
        )
        self._register(pairing)
        pairing.task = self._spawn(self._accept(pairing, invite_id, secret))
        return pairing.public()

    def _pairing(self, pairing_id: str) -> Pairing:
        self._require_owner()
        self._sweep()
        pairing = self._pairings.get(pairing_id)
        if pairing is None:
            raise FederationError("not_found", "unknown pairing", status=404)
        return pairing

    async def confirm(self, pairing_id: str) -> dict:
        pairing = self._pairing(pairing_id)
        if pairing.state != "confirm" or pairing.channel is None:
            raise FederationError("not_ready", "this pairing cannot be confirmed", status=409)
        if not pairing.local_confirmed:
            # What this confirmation is bound to, read BEFORE it counts: a revoke of this key
            # from here on (by any process) makes the trust write refuse.
            fence = await asyncio.to_thread(self.store.fence, pairing.remote_key)
            if pairing.state != "confirm" or pairing.channel is None:
                raise FederationError("not_ready", "this pairing cannot be confirmed", status=409)
            if not pairing.local_confirmed:
                pairing.fence = fence
            pairing.local_confirmed = True
            try:
                await pairing.channel.call("pair.confirm")
            except ch.CallError:
                self._end_pairing(pairing, "failed", "the other instance went away")
                raise FederationError(
                    "peer_gone", "the other instance went away; pair again", status=409
                ) from None
            await self._maybe_finish(pairing)
        return pairing.public()

    async def deny(self, pairing_id: str) -> dict:
        """Decline a pairing. The answer is the pairing's REAL outcome: ``denied`` only when
        nothing was stored. The state changes before anything is awaited, so a confirmation
        or a dial that completes afterwards finds the pairing closed. When both operators had
        already confirmed and the trust write is under way, the denial came too late: it waits
        for the write and reports ``done`` (the peer is then removed with Revoke)."""
        pairing = self._pairing(pairing_id)
        if pairing.state == "committing":
            await pairing.settled.wait()
            return pairing.public()
        if pairing.open:
            channel = pairing.channel
            self._end_pairing(pairing, "denied")
            if channel is not None:
                # Best effort, inside the half second before the channel closes; without it
                # the other side still ends, as "closed the pairing".
                with contextlib.suppress(ch.CallError):
                    await channel.call("pair.deny", timeout=0.4)
        return pairing.public()

    async def _maybe_finish(self, pairing: Pairing) -> None:
        """Store trust once both operators confirmed. THE one place trust is written for a
        pairing. Everything that could end the pairing (a denial from either side, the
        deadline, a dropped channel) is decided BEFORE the write, in the same step that claims
        it: the state moves to ``committing`` with nothing awaited in between, and from then on
        `_end_pairing` leaves the pairing alone. So a pairing either ends without trust, or
        commits and ends ``done``; never both."""
        if pairing.state == "committing" and pairing.commit is not None:
            await asyncio.shield(pairing.commit)  # the other confirmation's caller: same outcome
            return
        if self._expire_due(pairing):
            return
        if not (pairing.local_confirmed and pairing.remote_confirmed):
            return
        pairing.state = "committing"
        # The write belongs to the service, not to this caller. The caller is often a verb
        # handler, which the channel cancels when it closes; a cancelled caller must not leave
        # a worker thread writing behind a pairing already reported as failed. So the write
        # runs in its own task, the caller only waits for it, and the pairing is settled from
        # what the store actually did.
        pairing.commit = self._spawn(self._commit(pairing))
        self._commits.add(pairing.commit)
        pairing.commit.add_done_callback(self._commits.discard)
        await asyncio.shield(pairing.commit)

    async def _commit(self, pairing: Pairing) -> None:
        """The trust write of a ``committing`` pairing, and its settlement from the real
        outcome. Never cancelled by the service: `stop` waits for it."""
        try:
            peer = await asyncio.to_thread(
                self.store.trust,
                pairing.remote_key,
                name=pairing.remote_name or identity.peer_id(pairing.remote_key)[:8],
                endpoints=pairing.remote_endpoints,
                expect=pairing.fence,
            )
        except store.Conflict as exc:
            pairing.state = "failed"
            self._settle(pairing, str(exc))
            return
        except Exception:
            log.exception("federation pairing could not be stored")
            pairing.state = "failed"
            self._settle(pairing, "the pairing could not be stored")
            return
        self._refused.discard(peer.peer_id)
        self._backoff.pop(peer.peer_id, None)
        pairing.state = "done"
        self._settle(pairing)
        self._kick.set()

    def rename(self, peer_id: str, name: object) -> dict:
        self._check_id(peer_id)
        try:
            self.store.rename(peer_id, name)  # type: ignore[arg-type]
        except store.StoreError as exc:
            raise FederationError("invalid", str(exc), status=exc.status) from exc
        return self.snapshot()

    async def revoke(self, peer_id: str) -> dict:
        self._check_id(peer_id)
        try:
            # Trust ends FIRST: from here a handshake with this key is refused, whatever happens
            # to the goodbye below.
            await asyncio.to_thread(self.store.revoke, peer_id)
        except store.StoreError as exc:
            raise FederationError("not_found", str(exc), status=exc.status) from exc
        channel = self._channels.pop(peer_id, None)
        if channel is not None and not channel.closed:
            with contextlib.suppress(ch.CallError):
                await channel.call("peer.bye", timeout=3)
            channel.close("revoked")
        return self.snapshot()

    @staticmethod
    def _check_id(peer_id: str) -> None:
        if not identity.PEER_ID_RE.fullmatch(peer_id or ""):
            raise FederationError("not_found", "unknown peer", status=404)

    # ── inbound ───────────────────────────────────────────────────────
    def accepting(self) -> bool:
        """Whether a handshake could possibly succeed. With no trusted peer and no open invite
        the door stays shut and no byte is read."""
        if not self.owner:
            return False
        self._sweep()
        if self._invites:
            return True
        try:
            return self.store.any_trusted()
        except (store.StoreError, OSError):
            return False

    async def serve(self, transport: ch.FrameTransport) -> None:
        """Run one inbound connection to its end. Never raises."""
        used: list[Invite] = []

        def psk_for(mode: str, invite_id: str | None) -> bytes | None:
            if mode == ch.MODE_PEER:
                return None
            invite = self._invites.get(invite_id or "")
            if invite is None or not invite.live(time.time()):
                raise ch.HandshakeRefused("no such invite")
            if used:
                raise ch.HandshakeRefused("one invite per connection")
            if self._slots_used() >= PAIRINGS_MAX:
                raise ch.HandshakeRefused("too many pairings in progress")
            # Counted when the attempt STARTS, so parallel guesses cannot outrun the cap; and
            # the slot is held until this handshake ends, so parallel handshakes on distinct
            # invites cannot outrun the pairing cap either.
            invite.attempts += 1
            used.append(invite)
            self._pair_handshakes += 1
            return invite.secret

        try:
            if not self.accepting() or self._handshakes >= HANDSHAKES_IN_FLIGHT_MAX:
                return
            self._handshakes += 1
            try:
                est = await ch.respond(transport, self._key(), psk_for=psk_for)
            except (ch.HandshakeRefused, FederationError):
                return
            finally:
                self._handshakes -= 1
                # Released here; nothing is awaited between this and the registration below,
                # so the slot passes from the handshake to its pairing without a gap.
                self._pair_handshakes -= len(used)
            if est.remote_static == self.public_key:
                return
            if est.mode == ch.MODE_PEER:
                peer = await asyncio.to_thread(self.store.trusted_key, est.remote_static)
                if peer is None:
                    return  # unknown or revoked key: not one application frame
                await self._run_peer(transport, est, peer)
                return
            invite = used[0]
            # The invite is checked AGAIN now that the handshake is over, and claimed in the
            # same step: it must still be the registered one (not withdrawn meanwhile), not
            # expired, and not consumed by a handshake that finished first.
            if (
                self._invites.get(invite.id) is not invite
                or invite.used
                or not invite.live(time.time())
                or _open_pairings(self._pairings) >= PAIRINGS_MAX
            ):
                return
            invite.used = True
            del self._invites[invite.id]
            pairing = Pairing(
                id=invite.id, role="inviter", expires_at=invite.expires_at,
                remote_key=est.remote_static,
            )  # fmt: skip
            self._register(pairing)
            await self._run_pairing(transport, est, pairing)
        except Exception:
            log.exception("federation inbound connection failed")
        finally:
            with contextlib.suppress(Exception):
                await transport.close()

    # ── pairing ───────────────────────────────────────────────────────
    async def _accept(self, pairing: Pairing, invite_id: str, secret: bytes) -> None:
        last = "unreachable"
        for endpoint in ep.ordered(pairing.remote_endpoints):
            if not pairing.open:
                return  # denied or expired while dialing
            transport = None
            try:
                transport = await self._dial(endpoint)
                est = await ch.initiate(
                    transport,
                    self._key(),
                    mode=ch.MODE_PAIR,
                    invite=invite_id,
                    psk=secret,
                    expect_static=pairing.remote_key,
                )
            except asyncio.CancelledError:
                # The pairing ended while this dial was under way (`_end_pairing`).
                if transport is not None:
                    with contextlib.suppress(Exception):
                        await transport.close()
                raise
            except DialError as exc:
                last = exc.code
                continue
            except ch.HandshakeRefused:
                last = "refused"
                if transport is not None:
                    await transport.close()
                continue
            except Exception:
                log.exception("federation pairing dial failed")
                if transport is not None:
                    await transport.close()
                continue
            await self._run_pairing(transport, est, pairing)
            return
        detail = {
            "refused": "the other instance refused this invite (used, expired or wrong)",
            "refused_destination": "the invite points at an address this instance will not dial",
            "no_relay": "the invite needs the relay, and Home Free is not set up on this instance",
            "other_relay": "the other instance is on a different relay",
        }.get(last, "the other instance could not be reached")
        self._end_pairing(pairing, "failed", detail)

    async def _run_pairing(
        self, transport: ch.FrameTransport, est: ch.Established, pairing: Pairing
    ) -> None:
        # Only a pairing that is still waiting for its channel gets one. One that ended while
        # the dial or the handshake ran (denied, expired) stays ended: a terminal state is
        # never brought back to ``confirm``.
        if pairing.state != "dialing" or self._expire_due(pairing):
            with contextlib.suppress(Exception):
                await transport.close()
            return
        channel = ch.Channel(transport, est, verbs=verbs.PAIR, context=(self, pairing))
        pairing.channel = channel
        pairing.code = short_code(est.handshake_hash)
        pairing.state = "confirm"
        runner = asyncio.create_task(channel.run())
        try:
            with contextlib.suppress(ch.CallError):
                await channel.call("pair.hello", {"name": self.name, "endpoints": self.reachable()})
            await channel.wait_closed()
        finally:
            channel.close()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await runner
            self._end_pairing(pairing, "failed", "the other instance closed the pairing")

    # ── peer channels ─────────────────────────────────────────────────
    def _adopt(self, peer_id: str, channel: ch.Channel) -> bool:
        """Make ``channel`` THE channel for this peer, or refuse it. When both sides dialed at
        once, the channel opened by the lower peer id is kept."""
        current = self._channels.get(peer_id)
        if current is not None and not current.closed:
            preferred_is_me = self.peer_id < peer_id
            if current.initiator == preferred_is_me and channel.initiator != preferred_is_me:
                return False
            current.close("replaced")
        self._channels[peer_id] = channel
        return True

    async def _run_peer(
        self, transport: ch.FrameTransport, est: ch.Established, peer: store.Peer
    ) -> None:
        channel = ch.Channel(transport, est, verbs=verbs.PEER, context=(self, peer.peer_id))
        if not self._adopt(peer.peer_id, channel):
            await transport.close()
            return
        runner = asyncio.create_task(channel.run())
        pinger = asyncio.create_task(self._keepalive(peer.peer_id, channel))
        try:
            await channel.wait_closed()
        finally:
            pinger.cancel()
            channel.close()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await runner
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pinger
            if self._channels.get(peer.peer_id) is channel:
                del self._channels[peer.peer_id]

    async def _keepalive(self, peer_id: str, channel: ch.Channel) -> None:
        try:
            hello = await channel.call("peer.hello", {"endpoints": self.reachable()})
            await self._peer_said_hello(peer_id, hello)
            while True:
                await asyncio.sleep(PING_EVERY_S)
                await channel.call("peer.ping")
                await asyncio.to_thread(self.store.touch_seen, peer_id)
        except ch.CallError as exc:
            channel.close(f"keepalive: {exc.code}")

    async def _peer_said_hello(self, peer_id: str, hello: dict) -> None:
        await asyncio.to_thread(self.store.touch_seen, peer_id)
        self._refused.discard(peer_id)
        self._backoff.pop(peer_id, None)
        # The channel is up: ask what this peer shares with this instance.
        self.refresh_remote(peer_id)

    def peer_left(self, peer_id: str) -> None:
        """The peer told us, over its authenticated channel, that it no longer trusts this
        instance. Shown to the operator; this side's own trust is the operator's to end."""
        self._refused.add(peer_id)

    def channel_for(self, peer_id: str) -> ch.Channel | None:
        channel = self._channels.get(peer_id)
        return channel if channel is not None and not channel.closed else None

    # ── shared sessions (#2054) ───────────────────────────────────────
    def notify_share_changed(self, peer_id: str) -> None:
        """Tell ``peer_id`` that its grants changed. A hint with no data, best effort: the peer
        asks ``share.list`` again, and its own periodic refresh covers a hint that was lost."""
        channel = self.channel_for(peer_id)
        if channel is None or self._stopped:
            return

        async def hint() -> None:
            with contextlib.suppress(ch.CallError):
                await channel.call("share.changed", timeout=5)

        self._spawn(hint())

    def refresh_remote(self, peer_id: str) -> None:
        """Refresh the mirror of what ``peer_id`` shares with this instance, in the background.
        One refresh per peer at a time; a request during one runs another when it ends."""
        if self._stopped:
            return
        if peer_id in self._refreshing:
            self._refresh_again.add(peer_id)
            return
        self._refreshing.add(peer_id)
        self._spawn(self._refresh_remote(peer_id))

    async def _refresh_remote(self, peer_id: str) -> None:
        try:
            while True:
                self._refresh_again.discard(peer_id)
                self._refreshed_at[peer_id] = time.monotonic()
                await remote.refresh(self, peer_id)
                if peer_id not in self._refresh_again:
                    return
                # A peer's hints cannot make this side read its list faster than this.
                await asyncio.sleep(remote.REFRESH_MIN_GAP_S)
        except Exception:
            log.exception("federation: refreshing a peer's shared sessions failed")
        finally:
            self._refreshing.discard(peer_id)
            self._refresh_again.discard(peer_id)

    async def _shares_round(self) -> None:
        """The dialer round's share of the work. Never raises: sharing must not stop dialing."""
        try:
            await asyncio.to_thread(grants.sweep, self.store)
            generation = await asyncio.to_thread(self.store.generation)
            if generation != self._generation:
                # Something changed since the last round, written by this process, another one
                # over the same store, or an archive. Tell exactly the peers it concerns.
                marks = await asyncio.to_thread(self.store.grant_marks)
                first = self._generation is None
                changed = [p for p, mark in marks.items() if self._marks.get(p) != mark]
                self._generation, self._marks = generation, marks
                for principal in () if first else changed:
                    if principal.startswith(store.PRINCIPAL_PEER):
                        self.notify_share_changed(principal[len(store.PRINCIPAL_PEER) :])
            now = time.monotonic()
            for peer_id in list(self._channels):
                if self.channel_for(peer_id) is None:
                    continue
                if now - self._refreshed_at.get(peer_id, -remote.REFRESH_EVERY_S) >= (
                    remote.REFRESH_EVERY_S
                ):
                    self.refresh_remote(peer_id)
        except Exception:
            log.exception("federation: the shared-sessions round failed")

    # ── dialer ────────────────────────────────────────────────────────
    def _hold_owner_lock(self) -> bool:
        if self._lock_fd is not None:
            return True
        directory = self._directory or identity.state_dir()
        try:
            directory.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(FileExistsError):
                os.mkdir(directory, identity.DIR_MODE)
            fd = os.open(directory / LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._lock_fd = fd
        return True

    async def _dialer(self) -> None:
        while not self._stopped:
            try:
                if self._hold_owner_lock():
                    await self.refresh_relay_address()
                    await self._dial_round()
            except Exception:
                log.exception("federation dialer round failed")
            self._kick.clear()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(DIAL_TICK_S):
                    await self._kick.wait()

    async def _dial_round(self) -> None:
        now = time.monotonic()
        peers = await asyncio.to_thread(self.store.list_peers)
        # A revoke may have been written by another process over this store: its channel ends
        # here, on the owner's next round, without waiting for the peer to call anything.
        trusted = {p.peer_id for p in peers if p.trusted}
        for pid, channel in list(self._channels.items()):
            if pid not in trusted:
                channel.close("revoked")
        await self._shares_round()
        for peer in peers:
            pid = peer.peer_id
            if not peer.trusted or not peer.endpoints or pid in self._dialing:
                continue
            if pid in self._refused or self.channel_for(pid) is not None:
                continue
            if self._backoff.get(pid, (0.0, 0.0))[1] > now:
                continue
            self._dialing.add(pid)
            self._spawn(self._dial_peer(peer))

    def _failed(self, peer_id: str) -> None:
        delay = min(BACKOFF_MAX_S, self._backoff.get(peer_id, (BACKOFF_MIN_S / 2, 0.0))[0] * 2)
        self._backoff[peer_id] = (delay, time.monotonic() + delay * random.uniform(0.5, 1.5))  # noqa: S311 — jitter

    async def _dial_peer(self, peer: store.Peer) -> None:
        try:
            for endpoint in ep.ordered(peer.endpoints):
                transport = None
                try:
                    transport = await self._dial(endpoint)
                    est = await ch.initiate(
                        transport, self._key(), mode=ch.MODE_PEER, expect_static=peer.public_key
                    )
                except (DialError, ch.HandshakeRefused):
                    if transport is not None:
                        await transport.close()
                    continue
                # Re-read trust: the operator may have revoked while the dial was in flight.
                if await asyncio.to_thread(self.store.trusted_key, peer.public_key) is None:
                    await transport.close()
                    return
                self._dialing.discard(peer.peer_id)
                await self._run_peer(transport, est, peer)
                return
            self._failed(peer.peer_id)
        except Exception:
            log.exception("federation dial failed")
            self._failed(peer.peer_id)
        finally:
            self._dialing.discard(peer.peer_id)
