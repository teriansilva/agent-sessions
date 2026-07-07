"""The Home Free home-side agent.

Holds an outbound control WebSocket to the blind relay, registers a console name
under an Ed25519 identity, and for each paired viewer dials a per-session leg,
runs the responder handshake, and bridges the decrypted stream to a local target
(the recovery shell by default). The relay only ever sees ciphertext.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .handshake import Responder, derive_psk
from .targets import LocalTarget, PtyShellTarget

log = logging.getLogger("battlelab.homefree.agent")

_MAX_FRAME = 2**21  # generous ceiling for a WS frame (transport frames are small)


def load_or_create_identity(path: Path) -> Ed25519PrivateKey:
    """Load the agent's long-term Ed25519 identity key, creating it 0600 if absent."""
    if path.exists():
        return Ed25519PrivateKey.from_private_bytes(path.read_bytes())
    key = Ed25519PrivateKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key.private_bytes_raw())
    finally:
        os.close(fd)
    return key


@dataclass
class AgentConfig:
    relay_url: str  # e.g. wss://battlelab.superstatus.io/relay/ws
    console_name: str
    access_key: str
    identity_path: Path
    appver: str = "battlelab-home-free/1"
    # Factory for the per-session local endpoint. Default: the recovery shell.
    target_factory: Callable[[], LocalTarget] = field(default=lambda: PtyShellTarget())


class HomeFreeAgent:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self._identity = load_or_create_identity(config.identity_path)
        self._psk = derive_psk(config.access_key)
        self._sessions: set[asyncio.Task] = set()

    def _control_url(self) -> str:
        return f"{self.config.relay_url}?role=agent"

    def _session_url(self) -> str:
        return f"{self.config.relay_url}?role=agent-session"

    async def run_once(self) -> None:
        """Connect, register, and serve sessions until the control WS closes."""
        async with websockets.connect(self._control_url(), max_size=_MAX_FRAME) as ws:
            await self._register(ws)
            async for message in ws:
                if isinstance(message, bytes):
                    continue
                try:
                    data = json.loads(message)
                except (ValueError, TypeError):
                    continue
                if data.get("t") == "session_start":
                    task = asyncio.create_task(self._handle_session(data))
                    self._sessions.add(task)
                    task.add_done_callback(self._on_session_done)
                elif data.get("t") == "ping":
                    await ws.send(json.dumps({"t": "pong"}))

    def _on_session_done(self, task: asyncio.Task) -> None:
        # Observe the task result so a session failure can never surface as an
        # unretrieved-exception warning; _handle_session already contains and
        # logs expected errors, so anything here is unexpected.
        self._sessions.discard(task)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                log.warning("unexpected session task failure: %r", exc)

    async def _register(self, ws) -> None:
        challenge = json.loads(await ws.recv())
        if challenge.get("t") != "challenge":
            raise RuntimeError(f"expected challenge, got {challenge!r}")
        nonce = base64.b64decode(challenge["nonce"])
        signature = self._identity.sign(nonce)
        idpub = self._identity.public_key().public_bytes_raw()
        await ws.send(
            json.dumps(
                {
                    "t": "register",
                    "name": self.config.console_name,
                    "idpub": base64.b64encode(idpub).decode(),
                    "sig": base64.b64encode(signature).decode(),
                    "appver": self.config.appver,
                }
            )
        )
        reply = json.loads(await ws.recv())
        if reply.get("t") != "registered":
            raise RuntimeError(f"registration refused: {reply.get('code', reply)}")
        log.info("registered console name=%s", self.config.console_name)

    async def _handle_session(self, start: dict) -> None:
        sid = start.get("sid")
        stoken = start.get("stoken")
        try:
            async with websockets.connect(self._session_url(), max_size=_MAX_FRAME) as ws:
                await ws.send(json.dumps({"t": "attach", "sid": sid, "stoken": stoken}))
                transport = await self._do_handshake(ws)
                await self._bridge(ws, transport)
        except (websockets.ConnectionClosed, OSError) as exc:
            log.info("session ended sid=%s (%s)", sid, exc)
        except Exception as exc:
            # Contain bad viewer input (malformed handshake frames, HandshakeError,
            # InvalidTag on transport decrypt, JSON/base64 errors, target failures):
            # log and drop this session — never let it escape as an unobserved
            # background-task exception, and never affect other sessions.
            log.warning("session error sid=%s: %r", sid, exc)

    async def _do_handshake(self, ws):
        responder = Responder(self._psk)
        msg1 = await ws.recv()
        if not isinstance(msg1, bytes | bytearray):
            raise RuntimeError("expected binary handshake msg1")
        msg2 = responder.respond(bytes(msg1))
        await ws.send(msg2)
        msg3 = await ws.recv()
        if not isinstance(msg3, bytes | bytearray):
            raise RuntimeError("expected binary handshake msg3")
        return responder.finish(bytes(msg3))

    async def _bridge(self, ws, transport) -> None:
        target = self.config.target_factory()
        out_queue: asyncio.Queue[bytes] = asyncio.Queue()
        await target.start(lambda data: out_queue.put_nowait(data))

        async def writer() -> None:
            while True:
                data = await out_queue.get()
                await ws.send(transport.encrypt(data))

        writer_task = asyncio.create_task(writer())
        try:
            async for message in ws:
                if isinstance(message, bytes | bytearray):
                    plaintext = transport.decrypt(bytes(message))
                    await target.feed(plaintext)
        finally:
            writer_task.cancel()
            await asyncio.gather(writer_task, return_exceptions=True)
            await target.close()
