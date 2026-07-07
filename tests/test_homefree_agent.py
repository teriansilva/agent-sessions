"""End-to-end tests: real agent + a minimal blind relay stub + a Python viewer.

Proves the full path — agent registration, per-session handshake, encrypted
bridge — works, that the relay sees only ciphertext (blindness regression), and
that the PTY recovery shell round-trips.
"""

import asyncio
import base64
import json
import secrets
import urllib.parse

import websockets

from agent_sessions.homefree.agent import AgentConfig, HomeFreeAgent
from agent_sessions.homefree.handshake import Initiator, derive_psk
from agent_sessions.homefree.targets import EchoTarget, PtyShellTarget


class RelayStub:
    """A minimal blind relay: registers agents, pairs viewers, bridges binary
    frames verbatim, and records every forwarded frame for the blindness check."""

    def __init__(self) -> None:
        self.agents: dict[str, object] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self.forwarded_frames: list[bytes] = []

    async def handler(self, ws) -> None:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(ws.request.path).query)
        role = params.get("role", [None])[0]
        if role == "agent":
            await self._agent(ws)
        elif role == "agent-session":
            await self._agent_session(ws)
        elif role == "viewer":
            await self._viewer(ws, params)

    async def _agent(self, ws) -> None:
        nonce = secrets.token_bytes(32)
        await ws.send(json.dumps({"t": "challenge", "nonce": base64.b64encode(nonce).decode()}))
        reg = json.loads(await ws.recv())
        name = reg["name"]
        self.agents[name] = ws
        await ws.send(json.dumps({"t": "registered", "name": name}))
        try:
            async for _ in ws:
                pass
        finally:
            self.agents.pop(name, None)

    async def _agent_session(self, ws) -> None:
        attach = json.loads(await ws.recv())
        fut = self.pending.get(attach["sid"])
        if fut is not None and not fut.done():
            fut.set_result(ws)
        await ws.wait_closed()

    async def _viewer(self, ws, params) -> None:
        await ws.recv()  # hello (captcha skipped in the stub)
        name = params["name"][0]
        agent_ws = self.agents[name]
        sid = secrets.token_hex(8)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[sid] = fut
        await agent_ws.send(
            json.dumps({"t": "session_start", "sid": sid, "stoken": "tok", "deadline": 0})
        )
        agent_sess = await asyncio.wait_for(fut, 5)
        await ws.send(json.dumps({"t": "paired", "sid": sid}))

        async def pump(src, dst) -> None:
            async for msg in src:
                if isinstance(msg, bytes | bytearray):
                    self.forwarded_frames.append(bytes(msg))
                    await dst.send(msg)

        t1 = asyncio.create_task(pump(ws, agent_sess))
        t2 = asyncio.create_task(pump(agent_sess, ws))
        _, pend = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        for t in pend:
            t.cancel()
        await agent_sess.close()


class ViewerClient:
    """A Python stand-in for the browser: does the initiator handshake."""

    def __init__(self, ws, access_key: str) -> None:
        self._ws = ws
        self._ini = Initiator(derive_psk(access_key))
        self.transport = None

    async def handshake(self) -> None:
        await self._ws.send(json.dumps({"t": "hello", "captcha": "x"}))
        paired = json.loads(await self._ws.recv())
        assert paired["t"] == "paired"
        await self._ws.send(self._ini.start())  # msg1
        msg2 = await self._ws.recv()
        self.transport, msg3 = self._ini.finish(msg2)
        await self._ws.send(msg3)

    async def send(self, data: bytes) -> None:
        await self._ws.send(self.transport.encrypt(data))

    async def recv(self) -> bytes:
        return self.transport.decrypt(await self._ws.recv())


async def _serve(relay: RelayStub):
    server = await websockets.serve(relay.handler, "127.0.0.1", 0, max_size=2**21)
    port = server.sockets[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}/relay/ws"


def _agent(url: str, access_key: str, tmp_path, target_factory) -> HomeFreeAgent:
    return HomeFreeAgent(
        AgentConfig(
            relay_url=url,
            console_name="viper-8231",
            access_key=access_key,
            identity_path=tmp_path / "identity.key",
            target_factory=target_factory,
        )
    )


def test_end_to_end_echo_and_blindness(tmp_path):
    async def scenario():
        relay = RelayStub()
        server, url = await _serve(relay)
        access_key = "TEST-ACCESS-KEY-7Q2X"
        agent = _agent(url, access_key, tmp_path, lambda: EchoTarget())
        agent_task = asyncio.create_task(agent.run_once())
        await asyncio.sleep(0.3)  # let it register

        async with websockets.connect(f"{url}?role=viewer&name=viper-8231", max_size=2**21) as vw:
            viewer = ViewerClient(vw, access_key)
            await viewer.handshake()

            marker = b"SECRET_MARKER_ABCDEF123456"
            await viewer.send(marker)
            reply = await asyncio.wait_for(viewer.recv(), 5)
            assert reply == b"echo:" + marker

            # Blindness: the relay forwarded only ciphertext — the marker (and its
            # echo) never appear in cleartext in any forwarded frame.
            assert relay.forwarded_frames, "relay forwarded nothing"
            joined = b"".join(relay.forwarded_frames)
            assert marker not in joined
            assert b"echo:" + marker not in joined

        agent_task.cancel()
        await asyncio.gather(agent_task, return_exceptions=True)
        server.close()
        await server.wait_closed()

    asyncio.run(scenario())


def test_end_to_end_pty_recovery_shell(tmp_path):
    async def scenario():
        relay = RelayStub()
        server, url = await _serve(relay)
        access_key = "TEST-ACCESS-KEY-PTY99"
        agent = _agent(url, access_key, tmp_path, lambda: PtyShellTarget(["/bin/sh"]))
        agent_task = asyncio.create_task(agent.run_once())
        await asyncio.sleep(0.3)

        async with websockets.connect(f"{url}?role=viewer&name=viper-8231", max_size=2**21) as vw:
            viewer = ViewerClient(vw, access_key)
            await viewer.handshake()

            await viewer.send(b"echo HFPTYOK\n")
            collected = b""
            deadline = asyncio.get_running_loop().time() + 5
            while b"HFPTYOK" not in collected and asyncio.get_running_loop().time() < deadline:
                try:
                    collected += await asyncio.wait_for(viewer.recv(), 1)
                except TimeoutError:
                    break
            assert b"HFPTYOK" in collected

        agent_task.cancel()
        await asyncio.gather(agent_task, return_exceptions=True)
        server.close()
        await server.wait_closed()

    asyncio.run(scenario())


def test_wrong_access_key_cannot_handshake(tmp_path):
    async def scenario():
        relay = RelayStub()
        server, url = await _serve(relay)
        agent = _agent(url, "CORRECT-KEY-1234", tmp_path, lambda: EchoTarget())
        agent_task = asyncio.create_task(agent.run_once())
        await asyncio.sleep(0.3)

        raised = False
        async with websockets.connect(f"{url}?role=viewer&name=viper-8231", max_size=2**21) as vw:
            viewer = ViewerClient(vw, "WRONG-KEY-9999")
            try:
                await viewer.handshake()  # responder confirmation won't verify
            except Exception:
                raised = True
        assert raised

        agent_task.cancel()
        await asyncio.gather(agent_task, return_exceptions=True)
        server.close()
        await server.wait_closed()

    asyncio.run(scenario())


def test_malformed_handshake_is_contained(tmp_path):
    """A bad viewer frame must be contained (logged, session dropped) — the agent
    survives and still serves a subsequent good session."""

    async def scenario():
        relay = RelayStub()
        server, url = await _serve(relay)
        access_key = "TEST-KEY-CONTAIN-01"
        agent = _agent(url, access_key, tmp_path, lambda: EchoTarget())
        agent_task = asyncio.create_task(agent.run_once())
        await asyncio.sleep(0.3)

        # Bad viewer: valid hello, then garbage in place of the handshake msg1.
        async with websockets.connect(f"{url}?role=viewer&name=viper-8231", max_size=2**21) as bad:
            await bad.send(json.dumps({"t": "hello", "captcha": "x"}))
            assert json.loads(await bad.recv())["t"] == "paired"
            await bad.send(b"not-a-valid-x25519-key")  # bad msg1 -> HandshakeError, contained
            try:
                await asyncio.wait_for(bad.recv(), 2)
            except (TimeoutError, websockets.ConnectionClosed):
                pass

        await asyncio.sleep(0.1)
        # The agent survived: a good session still works.
        async with websockets.connect(f"{url}?role=viewer&name=viper-8231", max_size=2**21) as vw:
            viewer = ViewerClient(vw, access_key)
            await viewer.handshake()
            await viewer.send(b"still-works")
            assert await asyncio.wait_for(viewer.recv(), 5) == b"echo:still-works"

        agent_task.cancel()
        await asyncio.gather(agent_task, return_exceptions=True)
        server.close()
        await server.wait_closed()

    asyncio.run(scenario())
