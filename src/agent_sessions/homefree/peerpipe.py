"""The Home Free agent's part in federation over the relay (#1246 P3): a blind pipe, both ways.

Two BattleLab instances keep an end-to-end encrypted peer channel between their **app**
processes (``federation/``). When no direct address works, that channel travels through the
relay, and this module is the only piece of the agent that touches it. It never holds a
federation key, runs no handshake on these legs, and forwards binary frames unmodified:

* **Being called.** The relay sends ``peer_start`` on the control connection. The agent dials the
  usual ``agent-session`` leg and pipes it to the app's own ``/ws/fed`` on loopback. If the app
  refuses the upgrade (nobody is trusted or invited, or it is not the federation owner), the leg
  is closed.
* **Calling out.** The relay key belongs to this process, not to the app. So the app asks: it
  connects to a unix socket this agent serves and sends ``{"op": "call", "target": name}``. The
  agent opens ``role=peer`` to ITS OWN configured relay, signs the relay's nonce, and pipes.
  ``{"op": "info"}`` answers this instance's own relay address, which the app puts into invites.

**Local wire.** Every message on the socket, in both directions, is one frame: a 4-byte
big-endian unsigned length, then exactly that many bytes. The announced length is checked against
the cap BEFORE anything is read or allocated. The first frame from the app is a JSON request (at
most :data:`REQUEST_MAX` bytes) and the first frame back is a JSON answer. After an ``ok`` answer
to ``call``, every frame is exactly one relay binary message and the reverse (at most
:data:`FRAME_MAX`); frames are never merged or split.

**Bounds that do not depend on an honest relay or app.** One budget of :data:`CALLS_MAX` legs
covers calls being placed, calls bridged, and inbound ``peer_start`` legs together; a leg takes
its slot before it dials anything and gives it back on every exit. At most
:data:`CLIENTS_MAX` socket connections exist at once, and one that has not sent its request
within :data:`REQUEST_TIMEOUT_S` is dropped. Pairing has a deadline. Both pipe directions await
the receiver (the kernel socket buffer and the WebSocket's own write limit are the only
buffers), so a slow side slows the other instead of growing memory here.

**Relay control.** The relay sends its own text notices (``warn``, ``expired``) to the caller of
a session. They are transport control: consumed here, never forwarded as federation frames.
``expired`` ends the pipe; the app's dialer reconnects.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import socket
import struct
from pathlib import Path

import websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

log = logging.getLogger("battlelab.homefree.peer")

SOCKET_NAME = "peer.sock"
#: The relay's own rule for a console name (lowercased). Checked before anything is dialed.
NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{2,30}[a-z0-9]\Z")
#: Domain separation for a peer call, exactly as the relay verifies it (battlelab-cloud#94).
PEER_CONTEXT = b"bl-relay peer v1"
REQUEST_MAX = 1024
#: One pipe frame. The federation channel caps its own frames far below this; the pipe only
#: needs a bound of its own, and it does not interpret what it carries.
FRAME_MAX = 64 * 1024
#: Peer legs at once: outbound calls (pending and bridged) and inbound ``peer_start`` legs.
CALLS_MAX = 8
#: Socket connections at once, including ones that have not sent a request yet.
CLIENTS_MAX = 16
REQUEST_TIMEOUT_S = 5.0
PAIR_TIMEOUT_S = 20.0
ATTACH_TIMEOUT_S = 10.0
#: Messages a WebSocket may hold received but not yet forwarded. With FRAME_MAX this bounds the
#: read side of every leg; the write side waits for the receiver.
RECV_QUEUE = 8
_UNIX_PATH_MAX = 100  # AF_UNIX sun_path is 108 bytes on Linux; stay clear of it


def peer_message(nonce: bytes, name: str, target: str) -> bytes:
    """The bytes a peer caller signs: context, nonce, own name, target name; names lowercased,
    parts separated by one zero byte. Pinned by the vector shared with the relay."""
    return b"\0".join((PEER_CONTEXT, nonce, name.lower().encode(), target.lower().encode()))


def socket_path(identity_path: str | os.PathLike[str]) -> Path:
    """Where the agent listens: beside its identity key, in the directory the installer creates
    ``0700``. Override: ``HOMEFREE_PEER_SOCKET``."""
    override = os.environ.get("HOMEFREE_PEER_SOCKET")
    return Path(override) if override else Path(identity_path).parent / SOCKET_NAME


async def read_frame(reader: asyncio.StreamReader, limit: int = FRAME_MAX) -> bytes | None:
    """One length-prefixed frame, or ``None`` at a clean end. Raises ``ValueError`` when the
    announced length is over ``limit``."""
    try:
        head = await reader.readexactly(4)
    except (asyncio.IncompleteReadError, ConnectionError):
        return None
    (size,) = struct.unpack(">I", head)
    if size > limit:
        raise ValueError("frame too large")
    try:
        return await reader.readexactly(size)
    except (asyncio.IncompleteReadError, ConnectionError):
        return None


def write_frame(writer: asyncio.StreamWriter, data: bytes) -> None:
    writer.write(struct.pack(">I", len(data)) + data)


def _peer_uid(writer: asyncio.StreamWriter) -> int | None:
    sock = writer.get_extra_info("socket")
    if sock is None:
        return None
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    except OSError:
        return None
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


class PeerPipe:
    def __init__(
        self,
        *,
        relay_url: str,
        console_name: str,
        identity: Ed25519PrivateKey,
        identity_path: str | os.PathLike[str],
        app_host: str,
        app_port: int | None,
    ) -> None:
        self._relay_url = relay_url
        self._name = console_name
        self._identity = identity
        self._path = socket_path(identity_path)
        self._app_host = app_host
        self._app_port = app_port
        self._calls = 0
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[asyncio.StreamWriter] = set()
        self._legs: set = set()  # every open relay leg, inbound and outbound

    # ── being called ──────────────────────────────────────────────────
    async def handle_peer_start(self, start: dict) -> None:
        """Dial the session leg for a peer call and pipe it to this box's own ``/ws/fed``."""
        sid, stoken = start.get("sid"), start.get("stoken")
        if self._app_port is None:
            log.info("peer call ignored: no app port is configured")
            return
        if not (isinstance(sid, str) and isinstance(stoken, str)):
            return
        # The slot is taken before anything is dialed: a relay that floods `peer_start` (or
        # repeats one) gets at most the budget's worth of legs out of this agent.
        if self._calls >= CALLS_MAX:
            log.info("peer call ignored: all %d peer legs are in use", CALLS_MAX)
            return
        self._calls += 1
        try:
            async with asyncio.timeout(ATTACH_TIMEOUT_S):
                leg = await websockets.connect(
                    f"{self._relay_url}?role=agent-session",
                    max_size=FRAME_MAX,
                    max_queue=RECV_QUEUE,
                )
            async with leg, self._tracked(leg):
                try:
                    async with asyncio.timeout(ATTACH_TIMEOUT_S):
                        await leg.send(json.dumps({"t": "attach", "sid": sid, "stoken": stoken}))
                        app = await websockets.connect(
                            f"ws://{self._app_host}:{self._app_port}/ws/fed",
                            max_size=FRAME_MAX,
                            max_queue=RECV_QUEUE,
                            proxy=None,
                            compression=None,
                        )
                except (OSError, TimeoutError, websockets.WebSocketException):
                    # The app said no (nobody trusted or invited, or not the owner) or is down.
                    # Closing the leg is the whole answer; the caller sees the channel end.
                    return
                async with app:
                    await _pipe_ws(leg, app)
        except (websockets.ConnectionClosed, OSError, TimeoutError) as exc:
            log.info("peer leg ended sid=%s (%s)", sid, type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 — one bad leg never takes the agent down
            log.warning("peer leg error sid=%s: %r", sid, exc)
        finally:
            self._calls -= 1

    @contextlib.asynccontextmanager
    async def _tracked(self, leg):
        self._legs.add(leg)
        try:
            yield
        finally:
            self._legs.discard(leg)

    async def close_legs(self) -> None:
        """End every peer leg. Called when this agent's control connection to the relay ends: a
        peer session belongs to that registration (the relay ends it too, battlelab-cloud#94),
        and the agent does not wait for the relay to say so."""
        for leg in list(self._legs):
            with contextlib.suppress(Exception):
                await leg.close()

    # ── calling out ───────────────────────────────────────────────────
    async def serve(self) -> None:
        """Serve the local socket until cancelled. A path that cannot be used (too long for
        AF_UNIX, unwritable directory) turns the relay transport off and nothing else."""
        if len(os.fsencode(self._path)) > _UNIX_PATH_MAX:
            log.warning("peer socket path is too long for a unix socket; relay federation is off")
            return
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self._path)
        old_umask = os.umask(0o177)  # the socket is created 0600
        try:
            self._server = await asyncio.start_unix_server(self._client, path=str(self._path))
        except OSError as exc:
            log.warning("peer socket unavailable (%s); relay federation is off", exc.strerror)
            return
        finally:
            os.umask(old_umask)
        try:
            await asyncio.Event().wait()  # until cancelled
        finally:
            # Stop accepting, then end the calls in flight: a piped call would otherwise keep
            # its connection (and, on newer Pythons, the server's shutdown) open forever.
            self._server.close()
            for writer in list(self._clients):
                writer.close()
            with contextlib.suppress(OSError):
                os.unlink(self._path)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if len(self._clients) >= CLIENTS_MAX:
            writer.close()  # bounded before a byte is read
            return
        self._clients.add(writer)
        try:
            if _peer_uid(writer) != os.getuid():
                return  # another user reached the socket: not a word
            try:
                raw = await asyncio.wait_for(read_frame(reader, REQUEST_MAX), REQUEST_TIMEOUT_S)
                request = json.loads(raw) if raw else None
            except (ValueError, TimeoutError):
                return
            if not isinstance(request, dict):
                return
            op = request.get("op")
            if op == "info" and set(request) == {"op"}:
                self._answer(writer, {"ok": True, "relay_url": self._relay_url, "name": self._name})
            elif op == "call" and set(request) == {"op", "target"}:
                await self._call(reader, writer, request["target"])
            else:
                self._answer(writer, {"ok": False, "code": "bad_request"})
            with contextlib.suppress(ConnectionError):
                await writer.drain()
        except Exception as exc:  # noqa: BLE001 — a bad local request never takes the agent down
            log.warning("peer socket request failed: %r", exc)
        finally:
            self._clients.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    @staticmethod
    def _answer(writer: asyncio.StreamWriter, doc: dict) -> None:
        write_frame(writer, json.dumps(doc, separators=(",", ":")).encode())

    async def _call(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, target: object
    ) -> None:
        if not isinstance(target, str) or not NAME_RE.fullmatch(target.lower()):
            return self._answer(writer, {"ok": False, "code": "bad_request"})
        if target.lower() == self._name.lower():
            return self._answer(writer, {"ok": False, "code": "offline"})
        if self._calls >= CALLS_MAX:
            return self._answer(writer, {"ok": False, "code": "busy"})
        self._calls += 1
        try:
            deadline = asyncio.get_running_loop().time() + PAIR_TIMEOUT_S
            try:
                async with asyncio.timeout_at(deadline):
                    leg = await websockets.connect(
                        f"{self._relay_url}?role=peer", max_size=FRAME_MAX, max_queue=RECV_QUEUE
                    )
            except (OSError, TimeoutError, websockets.WebSocketException):
                return self._answer(writer, {"ok": False, "code": "unreachable"})
            # The leg is OWNED from here: tracked (so `close_legs` ends it) and closed on every
            # way out of this block, whatever the relay sent and also on cancellation. Nothing
            # the relay answers is trusted to be well formed; anything unexpected ends the
            # call as unreachable.
            async with leg, self._tracked(leg):
                try:
                    async with asyncio.timeout_at(deadline):
                        code = await self._peer_hello(leg, target)
                except Exception:  # noqa: BLE001 — a relay's answer never takes the agent down
                    code = "unreachable"
                if code is not None:
                    return self._answer(writer, {"ok": False, "code": code})
                self._answer(writer, {"ok": True})
                await writer.drain()
                await _pipe_socket(leg, reader, writer)
        finally:
            self._calls -= 1

    async def _peer_hello(self, leg, target: str) -> str | None:
        """Authenticate to the relay for one call. ``None`` when paired, else the refusal code.
        Raises ``ValueError`` for an answer that is not the shape the protocol names; the caller
        owns the leg and closes it."""
        challenge = _control(await leg.recv())
        if challenge.get("t") != "challenge":
            # A relay without the role answers an error instead of a challenge.
            return _refusal(challenge)
        nonce = challenge.get("nonce")
        if not isinstance(nonce, str) or len(nonce) > 256:
            raise ValueError("challenge without a nonce")
        signature = self._identity.sign(
            peer_message(base64.b64decode(nonce, validate=True), self._name, target)
        )
        await leg.send(
            json.dumps(
                {
                    "t": "peer_hello",
                    "name": self._name,
                    "idpub": base64.b64encode(
                        self._identity.public_key().public_bytes_raw()
                    ).decode(),
                    "sig": base64.b64encode(signature).decode(),
                    "target": target,
                }
            )
        )
        reply = _control(await leg.recv())
        return None if reply.get("t") == "paired" else _refusal(reply)


#: A refusal code as the relay may name it. Anything else is reported as plain "unreachable".
_CODE_RE = re.compile(r"\A[a-z][a-z0-9_]{0,31}\Z")


def _control(raw: object) -> dict:
    """One control message from the relay: a JSON object in a text frame, or ``ValueError``."""
    if not isinstance(raw, str):
        raise ValueError("control message is not text")
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise ValueError("control message is not an object")
    return doc


def _refusal(doc: dict) -> str:
    code = doc.get("code")
    return code if isinstance(code, str) and _CODE_RE.fullmatch(code) else "unreachable"


async def _pipe_ws(a, b) -> None:
    """Forward binary frames between two WebSockets until either ends. Text frames (the relay's
    own ``warn`` / ``expired`` notices) are not forwarded."""

    async def pump(src, dst) -> None:
        async for message in src:
            if isinstance(message, bytes | bytearray):
                await dst.send(bytes(message))
            elif _relay_says_expired(message):
                return

    await _first_done(pump(a, b), pump(b, a))


def _relay_says_expired(message: object) -> bool:
    """Whether a TEXT message on a relay leg is the relay's ``expired`` notice. Every text
    message is relay control and is consumed here: none is ever forwarded as a frame."""
    if not isinstance(message, str) or len(message) > 256:
        return False
    try:
        doc = json.loads(message)
    except ValueError:
        return False
    return isinstance(doc, dict) and doc.get("t") == "expired"


async def _pipe_socket(leg, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Forward binary frames between a relay leg and the app's unix-socket connection."""

    async def leg_to_app() -> None:
        async for message in leg:
            if isinstance(message, bytes | bytearray):
                write_frame(writer, bytes(message))  # one relay message, one socket frame
                await writer.drain()
            elif _relay_says_expired(message):
                return

    async def app_to_leg() -> None:
        while True:
            frame = await read_frame(reader)
            if frame is None:
                return
            await leg.send(frame)

    await _first_done(leg_to_app(), app_to_leg())


async def _first_done(*coros) -> None:
    tasks = [asyncio.create_task(c) for c in coros]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
