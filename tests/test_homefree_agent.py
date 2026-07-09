"""End-to-end tests: real agent + a minimal blind relay stub + a Python viewer.

Proves the full path — agent registration, per-session handshake, encrypted
app-mode bridge — works, and that the relay sees only ciphertext (blindness
regression).
"""

import asyncio
import base64
import http.server
import json
import secrets
import struct
import threading
import urllib.parse

import websockets

from agent_sessions.homefree.agent import _APP_ADVERT, AgentConfig, HomeFreeAgent
from agent_sessions.homefree.handshake import Initiator, derive_psk
from agent_sessions.homefree.mux import Mux


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


def _agent(url: str, access_key: str, tmp_path, *, app_port: int | None = None) -> HomeFreeAgent:
    return HomeFreeAgent(
        AgentConfig(
            relay_url=url,
            console_name="viper-8231",
            access_key=access_key,
            identity_path=tmp_path / "identity.key",
            app_port=app_port,
        )
    )


# ---------------------------------------------------------------------------
# App-only dispatch + app advert validation
# ---------------------------------------------------------------------------


class _IdentityTransport:
    """A passthrough transport for unit-testing app advert handling in isolation."""

    def encrypt(self, data: bytes) -> bytes:
        return data

    def decrypt(self, data: bytes) -> bytes:
        return data


class _OneMsgWS:
    """A fake session WS whose recv() yields `first` once, then blocks (never a 2nd)."""

    def __init__(self, first: object | None) -> None:
        self._first = first
        self._served = False

    async def recv(self):
        if self._first is not None and not self._served:
            self._served = True
            return self._first
        await asyncio.Event().wait()  # block forever — simulates "no further frames"


def _mk_agent(tmp_path, **cfg) -> HomeFreeAgent:
    return HomeFreeAgent(
        AgentConfig(
            relay_url="ws://x/relay/ws",
            console_name="viper-8231",
            access_key="k",
            identity_path=tmp_path / "id.key",
            **cfg,
        )
    )


def test_expect_app_advert_accepts_expected_first_frame(tmp_path):
    agent = _mk_agent(tmp_path, app_port=9)
    asyncio.run(agent._expect_app_advert(_OneMsgWS(_APP_ADVERT), _IdentityTransport()))


def test_expect_app_advert_rejects_non_advert(tmp_path):
    agent = _mk_agent(tmp_path, app_port=9)
    try:
        asyncio.run(agent._expect_app_advert(_OneMsgWS(b"keystroke"), _IdentityTransport()))
    except RuntimeError as exc:
        assert "unexpected app-mode advert" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("non-advert frame was accepted")


def test_expect_app_advert_rejects_text_frame(tmp_path):
    agent = _mk_agent(tmp_path, app_port=9)
    try:
        asyncio.run(agent._expect_app_advert(_OneMsgWS("hello"), _IdentityTransport()))
    except RuntimeError as exc:
        assert "expected binary app-mode advert" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("text advert was accepted")


def test_bridge_refuses_without_app_port(tmp_path, caplog):
    agent = _mk_agent(tmp_path)  # app_port None
    with caplog.at_level("WARNING"):
        asyncio.run(agent._bridge(_OneMsgWS(_APP_ADVERT), _IdentityTransport()))
    assert "HOMEFREE_APP_PORT is not configured" in caplog.text


def test_app_stream_task_exceptions_are_observed(tmp_path, caplog):
    """A per-stream AppProxyTarget task that ends in an exception must be OBSERVED — no
    'Task exception was never retrieved'. StreamReset (normal abort) is silent; anything
    else is logged, and both are removed from the tracking set."""

    from agent_sessions.homefree.mux import StreamReset

    agent = _mk_agent(tmp_path, app_port=9)

    async def go() -> list[bool]:
        observed: list[bool] = []
        for exc in (StreamReset(0), RuntimeError("kaboom")):

            async def boom(e=exc):
                raise e

            stream_tasks: set = set()
            task = asyncio.ensure_future(boom())
            stream_tasks.add(task)
            try:
                await task
            except BaseException:  # noqa: BLE001 - we only care that it ran
                pass
            agent._on_app_stream_done(stream_tasks, task)
            assert task not in stream_tasks  # discarded
            observed.append(task.exception() is not None)  # retrievable == observed
        return observed

    with caplog.at_level("WARNING"):
        observed = asyncio.run(go())

    assert observed == [True, True]  # both exceptions observed, none left unretrieved
    assert any("kaboom" in r.getMessage() for r in caplog.records)  # unexpected → logged
    assert not any("StreamReset" in r.getMessage() for r in caplog.records)  # reset → silent


def _local_http_app():
    """A tiny real HTTP app the AppProxyTarget can proxy to (GET /ping → 200 PONG)."""

    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"PONG" if self.path == "/ping" else b"NO"
            self.send_response(200 if self.path == "/ping" else 404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # silence
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


async def _read_exact(stream, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        part = await stream.read(n - len(buf))
        if not part:
            break
        buf += part
    return bytes(buf)


async def _app_get_ping(vw, viewer: ViewerClient) -> bytes:
    await viewer.send(_APP_ADVERT)  # app-mode version handshake (counter 0, before mux frames)

    # A viewer-side mux (initiator) over the same E2E transport, sends serialized.
    out_q: asyncio.Queue[bytes] = asyncio.Queue()
    mux = Mux(is_initiator=True, on_send=out_q.put_nowait)

    async def writer():
        while True:
            await vw.send(viewer.transport.encrypt(await out_q.get()))

    async def reader():
        async for raw in vw:
            if isinstance(raw, bytes | bytearray):
                mux.feed(viewer.transport.decrypt(bytes(raw)))

    wt, rt = asyncio.create_task(writer()), asyncio.create_task(reader())
    try:
        open_info = {"k": "http", "method": "GET", "path": "/ping", "headers": {}}
        s = mux.open(json.dumps(open_info).encode())
        await s.end()
        meta_len = struct.unpack(">I", await _read_exact(s, 4))[0]
        meta = json.loads(await _read_exact(s, meta_len))
        body = bytearray()
        while True:
            part = await asyncio.wait_for(s.read(), 5)
            if not part:
                break
            body += part
        assert meta["status"] == 200
        return bytes(body)
    finally:
        wt.cancel()
        rt.cancel()
        await asyncio.gather(wt, rt, return_exceptions=True)


def test_app_mode_dispatches_http_request_through_the_tunnel(tmp_path):
    """Full app-mode path: viewer adverts, opens a mux HTTP stream, and the agent's
    AppProxyTarget proxies it to the box's local app — end to end, through the blind relay."""

    async def scenario():
        relay = RelayStub()
        server, url = await _serve(relay)
        app_srv, app_port = _local_http_app()
        access_key = "TEST-APP-MODE-KEY-01"
        agent = _mk_agent_relay(url, access_key, tmp_path, app_port)
        agent_task = asyncio.create_task(agent.run_once())
        await asyncio.sleep(0.3)

        async with websockets.connect(f"{url}?role=viewer&name=viper-8231", max_size=2**21) as vw:
            viewer = ViewerClient(vw, access_key)
            await viewer.handshake()
            assert await _app_get_ping(vw, viewer) == b"PONG"

            # Blindness still holds — no cleartext of the response rode the relay.
            assert relay.forwarded_frames
            assert b"PONG" not in b"".join(relay.forwarded_frames)

        agent_task.cancel()
        await asyncio.gather(agent_task, return_exceptions=True)
        app_srv.shutdown()
        server.close()
        await server.wait_closed()

    asyncio.run(scenario())


def _mk_agent_relay(url, access_key, tmp_path, app_port) -> HomeFreeAgent:
    return HomeFreeAgent(
        AgentConfig(
            relay_url=url,
            console_name="viper-8231",
            access_key=access_key,
            identity_path=tmp_path / "identity.key",
            app_port=app_port,
        )
    )


def test_wrong_access_key_cannot_handshake(tmp_path):
    async def scenario():
        relay = RelayStub()
        server, url = await _serve(relay)
        agent = _agent(url, "CORRECT-KEY-1234", tmp_path, app_port=9)
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
        app_srv, app_port = _local_http_app()
        access_key = "TEST-KEY-CONTAIN-01"
        agent = _agent(url, access_key, tmp_path, app_port=app_port)
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
            assert await _app_get_ping(vw, viewer) == b"PONG"

        agent_task.cancel()
        await asyncio.gather(agent_task, return_exceptions=True)
        app_srv.shutdown()
        server.close()
        await server.wait_closed()

    asyncio.run(scenario())
