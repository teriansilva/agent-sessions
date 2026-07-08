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

from .appproxy import AppProxyTarget
from .handshake import Responder, derive_psk
from .mux import Mux, StreamReset
from .targets import LocalTarget, PtyShellTarget

log = logging.getLogger("battlelab.homefree.agent")

_MAX_FRAME = 2**21  # generous ceiling for a WS frame (transport frames are small)

# App-mode advert (#579 P4a): the viewer sends this exact plaintext as its FIRST encrypted
# frame to request full-app streaming (the mux tunnel → AppProxyTarget). Anything else — a
# non-advert first frame, a timeout, no configured app port, or a shell-only agent — falls
# back to the recovery shell. Fail-closed by construction; the relay never sees it (it rides
# inside the E2E transport, like everything else).
_APP_ADVERT = b"\x00HF-APP/1"


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
    # Factory for the per-session recovery-shell endpoint (the fallback / non-app path).
    target_factory: Callable[[], LocalTarget] = field(default=lambda: PtyShellTarget())
    # Full-app streaming (#579 P4a): the box's local app to proxy in app mode. When
    # ``app_port`` is None (or ``force_shell``), app-mode is unavailable and every viewer
    # gets the recovery shell — fail-closed.
    app_host: str = "127.0.0.1"
    app_port: int | None = None
    app_origin: str | None = None  # the origin the app expects (defaults to http://host:port)
    force_shell: bool = False  # operator override: recovery shell only, ignore app adverts
    # How long to wait for the viewer's first frame (the app advert) before defaulting to
    # the shell. Small: the app browser adverts immediately; an old recovery browser that
    # sends nothing on connect times out fast and gets its prompt.
    mode_negotiate_timeout: float = 0.75


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

    def _on_app_stream_done(self, stream_tasks: set[asyncio.Task], task: asyncio.Task) -> None:
        # Same contract as _on_session_done for the per-stream AppProxyTarget tasks:
        # always OBSERVE the result so a stream that ends in an exception can't surface as
        # an "unretrieved task exception". A StreamReset is the normal abort signal
        # (AppProxyTarget.serve re-raises it), so it's observed but not logged; anything
        # else is an unexpected app-proxy failure worth a warning. Never affects other streams.
        stream_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None and not isinstance(exc, StreamReset):
            log.warning("home-free app stream failed: %r", exc)

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
        mode, first = await self._negotiate_mode(ws, transport)
        if mode == "app":
            await self._bridge_app(ws, transport)
        else:
            await self._bridge_shell(ws, transport, first)

    async def _negotiate_mode(self, ws, transport) -> tuple[str, bytes | None]:
        """Decide app vs recovery-shell for this viewer, fail-closed.

        Returns ``("app", None)`` only when app-mode is configured AND the viewer's first
        frame is exactly the app advert. Otherwise ``("shell", first_plaintext_or_None)`` —
        any non-advert first frame is returned so the shell can replay it as terminal input.
        """
        if self.config.force_shell or self.config.app_port is None:
            return "shell", None  # app-mode not available → recovery shell
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=self.config.mode_negotiate_timeout)
        except (TimeoutError, websockets.ConnectionClosed):
            return "shell", None  # viewer sent nothing in time (e.g. old recovery browser)
        if not isinstance(raw, bytes | bytearray):
            return "shell", None
        plaintext = transport.decrypt(bytes(raw))
        if plaintext == _APP_ADVERT:
            return "app", None
        return "shell", plaintext  # not an advert → shell, replaying the first frame

    async def _bridge_shell(self, ws, transport, replay: bytes | None) -> None:
        target = self.config.target_factory()
        out_queue: asyncio.Queue[bytes] = asyncio.Queue()
        await target.start(lambda data: out_queue.put_nowait(data))
        if replay:
            await target.feed(replay)  # the first non-advert frame was terminal input

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

    async def _bridge_app(self, ws, transport) -> None:
        """Full-app mode: carry the P1 mux over the transport and reverse-proxy each opened
        stream to the box's local app via P2's AppProxyTarget. The relay still only sees the
        ciphertext of each mux frame."""
        assert self.config.app_port is not None  # guaranteed by _negotiate_mode
        proxy = AppProxyTarget(
            app_host=self.config.app_host,
            app_port=self.config.app_port,
            app_origin=self.config.app_origin,
        )
        out_queue: asyncio.Queue[bytes] = asyncio.Queue()
        stream_tasks: set[asyncio.Task] = set()

        def on_stream(stream) -> None:
            task = asyncio.create_task(proxy.serve(stream))
            stream_tasks.add(task)
            task.add_done_callback(lambda t: self._on_app_stream_done(stream_tasks, t))

        mux = Mux(
            is_initiator=False,
            on_send=lambda frame: out_queue.put_nowait(frame),
            on_stream=on_stream,
        )

        async def writer() -> None:
            while True:
                frame = await out_queue.get()
                await ws.send(transport.encrypt(frame))

        writer_task = asyncio.create_task(writer())
        try:
            async for message in ws:
                if isinstance(message, bytes | bytearray):
                    mux.feed(transport.decrypt(bytes(message)))
        finally:
            writer_task.cancel()
            for task in stream_tasks:
                task.cancel()
            await asyncio.gather(writer_task, *stream_tasks, return_exceptions=True)
            mux.close()
