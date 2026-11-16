"""The Home Free agent's peer pipe (#1246 P3) on its own: the local wire framing, its bounds, and
how it treats the relay's control messages. One event loop: a relay double, a stand-in for the
box's app, and the real :class:`PeerPipe`."""

from __future__ import annotations

import asyncio
import json
import struct
import tempfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from websockets.asyncio.client import connect as ws_connect
from websockets.asyncio.server import serve as ws_serve

from agent_sessions.homefree import peerpipe
from federation_helpers import until
from relay_double import RelayDouble


def run(coro):
    return asyncio.run(coro)


# ── framing ───────────────────────────────────────────────────────────


def _reader(*chunks: bytes, eof: bool = True) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    if eof:
        reader.feed_eof()
    return reader


def _frame(payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + payload


def test_frames_survive_fragmented_and_coalesced_reads():
    async def go():
        wire = _frame(b"one") + _frame(b"") + _frame(b"three" * 100)
        # coalesced: all three in one read
        reader = _reader(wire)
        assert [await peerpipe.read_frame(reader) for _ in range(3)] == [
            b"one",
            b"",
            b"three" * 100,
        ]
        assert await peerpipe.read_frame(reader) is None
        # fragmented: one byte at a time
        reader = _reader(*[wire[i : i + 1] for i in range(len(wire))])
        assert [await peerpipe.read_frame(reader) for _ in range(3)] == [
            b"one",
            b"",
            b"three" * 100,
        ]

    run(go())


def test_a_truncated_prefix_or_payload_is_a_clean_end():
    async def go():
        assert await peerpipe.read_frame(_reader(b"\x00\x00")) is None
        assert await peerpipe.read_frame(_reader(_frame(b"abcdef")[:-2])) is None
        assert await peerpipe.read_frame(_reader(b"")) is None

    run(go())


def test_the_cap_is_exact_and_checked_before_the_payload_is_read():
    async def go():
        exact = b"x" * peerpipe.FRAME_MAX
        assert await peerpipe.read_frame(_reader(_frame(exact))) == exact
        # Only the 4-byte prefix has arrived and the stream is still open: the refusal cannot
        # have waited for (or allocated) the announced payload.
        too_big = _reader(struct.pack(">I", peerpipe.FRAME_MAX + 1), eof=False)
        with pytest.raises(ValueError):
            await asyncio.wait_for(peerpipe.read_frame(too_big), 1)
        huge = _reader(struct.pack(">I", 0xFFFFFFFF), eof=False)
        with pytest.raises(ValueError):
            await asyncio.wait_for(peerpipe.read_frame(huge), 1)
        small_cap = _reader(struct.pack(">I", peerpipe.REQUEST_MAX + 1), eof=False)
        with pytest.raises(ValueError):
            await asyncio.wait_for(peerpipe.read_frame(small_cap, peerpipe.REQUEST_MAX), 1)

    run(go())


# ── a world in one loop ───────────────────────────────────────────────


class World:
    """A relay double, two agents' pipes (alpha, beta) registered on it, and beta's "app": a
    WebSocket server at /ws/fed that echoes, or refuses the upgrade."""

    def __init__(self) -> None:
        self.relay = RelayDouble()
        self.dir = Path(tempfile.mkdtemp(prefix="pp"))
        self.app_refuses = False
        self.app_received: list[bytes] = []
        self.app_conns: list = []
        self._tasks: list[asyncio.Task] = []
        self._controls: list = []

    async def __aenter__(self) -> World:
        await self.relay.__aenter__()

        def gate(conn, request):
            if self.app_refuses or request.path != "/ws/fed":
                return conn.respond(403, "")
            return None

        async def app(conn):
            self.app_conns.append(conn)
            async for message in conn:
                self.app_received.append(message)
                await conn.send(b"app:" + message)

        self.app_server = await ws_serve(app, "127.0.0.1", 0, process_request=gate)
        app_port = self.app_server.sockets[0].getsockname()[1]
        self.alpha = await self._pipe("alpha-box", None)
        self.beta = await self._pipe("beta-box", app_port)
        return self

    async def _pipe(self, name: str, app_port) -> peerpipe.PeerPipe:
        key = Ed25519PrivateKey.generate()
        pipe = peerpipe.PeerPipe(
            relay_url=self.relay.url,
            console_name=name,
            identity=key,
            identity_path=self.dir / name / "identity",
            app_host="127.0.0.1",
            app_port=app_port,
        )
        (self.dir / name).mkdir()
        # register on the relay and route peer_start to the pipe, as the agent's control loop does
        control = await ws_connect(self.relay.url + "?role=agent")
        import base64

        nonce = base64.b64decode(json.loads(await control.recv())["nonce"])
        await control.send(
            json.dumps(
                {
                    "t": "register",
                    "name": name,
                    "idpub": base64.b64encode(key.public_key().public_bytes_raw()).decode(),
                    "sig": base64.b64encode(key.sign(nonce)).decode(),
                }
            )
        )
        assert json.loads(await control.recv())["t"] == "registered"
        self._controls.append(control)

        async def loop() -> None:
            async for raw in control:
                data = json.loads(raw)
                if data.get("t") == "peer_start":
                    task = asyncio.create_task(pipe.handle_peer_start(data))
                    self._tasks.append(task)

        self._tasks.append(asyncio.create_task(loop()))
        serving = asyncio.create_task(pipe.serve())
        self._tasks.append(serving)
        pipe.serving = serving
        await until(lambda: pipe._path.exists())
        return pipe

    async def __aexit__(self, *exc) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for control in self._controls:
            await control.close()
        self.app_server.close()
        await self.relay.__aexit__()


async def _request(pipe, request: dict | None, *, raw: bytes | None = None):
    reader, writer = await asyncio.open_unix_connection(str(pipe._path))
    if raw is not None:
        writer.write(raw)
    elif request is not None:
        peerpipe.write_frame(writer, json.dumps(request).encode())
    await writer.drain()
    return reader, writer


async def _call(pipe, target: str = "beta-box"):
    reader, writer = await _request(pipe, {"op": "call", "target": target})
    answer = json.loads(await asyncio.wait_for(peerpipe.read_frame(reader), 10))
    return answer, reader, writer


def test_one_socket_frame_is_one_relay_message_in_both_directions():
    async def go():
        async with World() as w:
            answer, reader, writer = await _call(w.alpha)
            assert answer == {"ok": True}
            payloads = [b"a", b"", b"\x00" * 1000, bytes(range(256)) * 40, b"z" * 3]
            for payload in payloads:
                peerpipe.write_frame(writer, payload)
            await writer.drain()
            got = [await asyncio.wait_for(peerpipe.read_frame(reader), 5) for _ in payloads]
            assert got == [b"app:" + p for p in payloads]  # never merged, never split
            assert w.app_received == payloads
            assert w.relay.forwarded[: len(payloads)] == payloads or set(payloads) <= set(
                w.relay.forwarded
            )
            writer.close()

    run(go())


def test_relay_control_text_is_consumed_and_expired_ends_the_pipe():
    async def go():
        async with World() as w:
            answer, reader, writer = await _call(w.alpha)
            assert answer == {"ok": True}
            caller_leg = w.relay.caller_legs[0]
            await caller_leg.send(json.dumps({"t": "warn", "remaining": 300}))
            peerpipe.write_frame(writer, b"still open")
            await writer.drain()
            # the warn notice did not arrive as a frame: the next frame is the echo
            assert await asyncio.wait_for(peerpipe.read_frame(reader), 5) == b"app:still open"
            await caller_leg.send(json.dumps({"t": "expired"}))
            assert await asyncio.wait_for(peerpipe.read_frame(reader), 5) is None  # pipe ended
            await until(lambda: w.alpha._calls == 0)
            writer.close()

    run(go())


def test_the_budget_covers_outbound_and_inbound_legs_together(monkeypatch):
    monkeypatch.setattr(peerpipe, "CALLS_MAX", 2)

    async def go():
        async with World() as w:
            # alpha calls beta twice at the same moment, plus a third: two legs, one refusal
            results = await asyncio.gather(_call(w.alpha), _call(w.alpha), _call(w.alpha))
            answers = [a for a, _, _ in results]
            assert answers.count({"ok": True}) == 2
            assert answers.count({"ok": False, "code": "busy"}) == 1
            assert w.alpha._calls == 2
            # beta holds two INBOUND legs, so its own outbound call is refused too
            await until(lambda: w.beta._calls == 2)
            answer, _, own = await _call(w.beta, "alpha-box")
            assert answer == {"ok": False, "code": "busy"}
            # every exit gives the slot back, on both ends
            for _, _, writer in results:
                writer.close()
            own.close()
            await until(lambda: w.alpha._calls == 0 and w.beta._calls == 0)
            answer, _, writer = await _call(w.alpha)
            assert answer == {"ok": True}
            writer.close()

    run(go())


def test_a_repeated_or_malformed_peer_start_leaves_nothing_behind():
    async def go():
        async with World() as w:
            answer, reader, writer = await _call(w.alpha)
            assert answer == {"ok": True}
            await until(lambda: w.beta._calls == 1)
            # A start for a session the relay does not have (a repeat, or a made-up one): the
            # relay closes that leg at once, so it costs one short-lived leg inside the budget
            # and is gone again. A malformed start dials nothing at all.
            await w.beta.handle_peer_start({"sid": "0" * 32, "stoken": "1" * 32})
            await w.beta.handle_peer_start({"sid": 5, "stoken": None})
            assert w.beta._calls == 1
            # the real session is untouched
            peerpipe.write_frame(writer, b"ok")
            await writer.drain()
            assert await asyncio.wait_for(peerpipe.read_frame(reader), 5) == b"app:ok"
            writer.close()
            await until(lambda: w.beta._calls == 0)

    run(go())


def test_a_flood_of_peer_starts_is_capped_by_the_budget(monkeypatch):
    monkeypatch.setattr(peerpipe, "CALLS_MAX", 2)

    async def go():
        async with World() as w:
            dialed = 0
            real_connect = peerpipe.websockets.connect

            def counting(*args, **kwargs):
                nonlocal dialed
                dialed += 1
                return real_connect(*args, **kwargs)

            monkeypatch.setattr(peerpipe.websockets, "connect", counting)
            hold = asyncio.Event()
            w.beta._calls = 2  # the budget is full
            await asyncio.gather(
                *(
                    w.beta.handle_peer_start({"sid": f"{i:032x}", "stoken": "t" * 32})
                    for i in range(50)
                )
            )
            assert dialed == 0  # not one socket was opened for the flood
            w.beta._calls = 0
            hold.set()

    run(go())


def test_an_app_that_refuses_the_upgrade_ends_the_leg_and_frees_the_slot():
    async def go():
        async with World() as w:
            w.app_refuses = True
            answer, reader, writer = await _call(w.alpha)
            assert answer == {"ok": True}  # the relay paired; the far app then said no
            assert await asyncio.wait_for(peerpipe.read_frame(reader), 5) is None
            await until(lambda: w.alpha._calls == 0 and w.beta._calls == 0)
            assert w.app_conns == []
            writer.close()

    run(go())


def test_idle_and_excess_socket_connections_are_bounded(monkeypatch):
    monkeypatch.setattr(peerpipe, "CLIENTS_MAX", 3)
    monkeypatch.setattr(peerpipe, "REQUEST_TIMEOUT_S", 0.2)

    async def go():
        async with World() as w:
            idle = [await _request(w.alpha, None) for _ in range(3)]
            await until(lambda: len(w.alpha._clients) == 3)
            reader, writer = await _request(w.alpha, {"op": "info"})
            assert await asyncio.wait_for(peerpipe.read_frame(reader), 2) is None  # over the cap
            writer.close()
            # the idle ones are dropped at the request deadline, and the socket serves again
            for reader, _ in idle:
                assert await asyncio.wait_for(peerpipe.read_frame(reader), 2) is None
            await until(lambda: len(w.alpha._clients) == 0)
            reader, writer = await _request(w.alpha, {"op": "info"})
            assert json.loads(await peerpipe.read_frame(reader))["ok"] is True
            writer.close()

    run(go())


@pytest.mark.parametrize(
    "raw",
    [
        _frame(b"not json"),
        _frame(b"[1,2]"),
        struct.pack(">I", peerpipe.REQUEST_MAX + 1) + b"x" * 16,
        struct.pack(">I", 0xFFFFFFFF),
        b"\x00\x00\x00",
        _frame(json.dumps({"op": "call", "target": "x" * 2000}).encode()),
    ],
)
def test_a_malformed_or_oversize_request_gets_no_leg(raw):
    async def go():
        async with World() as w:
            reader, writer = await _request(w.alpha, None, raw=raw)
            writer.write_eof()
            answer = await asyncio.wait_for(peerpipe.read_frame(reader), 8)
            assert answer is None or json.loads(answer) == {"ok": False, "code": "bad_request"}
            assert w.relay.peer_calls == [] and w.alpha._calls == 0
            writer.close()

    run(go())


def test_stopping_the_agent_ends_the_calls_in_flight():
    async def go():
        async with World() as w:
            answer, reader, writer = await _call(w.alpha)
            assert answer == {"ok": True}
            w.alpha.serving.cancel()
            assert await asyncio.wait_for(peerpipe.read_frame(reader), 5) is None
            await until(lambda: w.alpha._calls == 0)
            assert not w.alpha._path.exists()
            writer.close()

    run(go())


def test_the_viewer_path_is_untouched_by_peer_legs(monkeypatch, tmp_path):
    """`peer_start` goes to the pipe and never to the viewer handler, and a full peer budget
    does not stop a browser session from being served."""
    from agent_sessions.homefree.agent import AgentConfig, HomeFreeAgent

    async def go():
        async def relay(ws):
            import base64

            await ws.send(
                json.dumps({"t": "challenge", "nonce": base64.b64encode(b"n" * 32).decode()})
            )
            await ws.recv()
            await ws.send(json.dumps({"t": "registered", "name": "alpha-box"}))
            await ws.send(json.dumps({"t": "peer_start", "sid": "p" * 32, "stoken": "t" * 32}))
            await ws.send(json.dumps({"t": "session_start", "sid": "v" * 32, "stoken": "t" * 32}))
            await ws.send(json.dumps({"t": "something_new"}))
            await asyncio.sleep(0.2)

        async with ws_serve(relay, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            agent = HomeFreeAgent(
                AgentConfig(
                    relay_url=f"ws://127.0.0.1:{port}/relay/ws",
                    console_name="alpha-box",
                    access_key="k" * 32,
                    identity_path=tmp_path / "identity",
                    app_port=1,
                )
            )
            seen: list[tuple[str, str]] = []

            async def viewer(start):
                seen.append(("viewer", start["sid"]))

            async def peer(start):
                seen.append(("peer", start["sid"]))

            agent._handle_session = viewer
            agent.peer.handle_peer_start = peer
            agent.peer._calls = peerpipe.CALLS_MAX  # every peer leg is taken
            await agent.run_once()
            await asyncio.sleep(0)
            assert sorted(seen) == [("peer", "p" * 32), ("viewer", "v" * 32)]

    run(go())


def test_peer_legs_end_with_the_control_registration_and_reads_are_bounded():
    assert peerpipe.RECV_QUEUE * peerpipe.FRAME_MAX <= 1 << 20  # at most 512 KiB held per leg

    async def go():
        async with World() as w:
            answer, reader, writer = await _call(w.alpha)
            assert answer == {"ok": True}
            await until(lambda: len(w.alpha._legs) == 1 and len(w.beta._legs) == 1)
            # the agent's control connection to the relay ended: it closes its legs itself
            await w.beta.close_legs()
            assert await asyncio.wait_for(peerpipe.read_frame(reader), 5) is None
            await until(lambda: w.alpha._calls == 0 and w.beta._calls == 0)
            assert w.alpha._legs == set() and w.beta._legs == set()
            writer.close()

    run(go())


# ── a relay that answers nonsense (#2053 review) ──────────────────────

_NONCE = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
#: (what the relay sends as its "challenge", what it sends after the hello or None)
HOSTILE = {
    "a list for a challenge": ("[]", None),
    "a number for a challenge": ("7", None),
    "not json": ("{", None),
    "a binary challenge": (b"\x00\x01", None),
    "a challenge without a nonce": ('{"t":"challenge"}', None),
    "a nonce that is not text": ('{"t":"challenge","nonce":[1]}', None),
    "a nonce that is not base64": ('{"t":"challenge","nonce":"***"}', None),
    "a list for the reply": (json.dumps({"t": "challenge", "nonce": _NONCE}), "[]"),
    "a string for the reply": (json.dumps({"t": "challenge", "nonce": _NONCE}), '"paired"'),
    "a refusal whose code is an object": ('{"t":"error","code":{"a":1}}', None),
    "a refusal whose code is not a code": ('{"t":"error","code":"<b>x</b> y"}', None),
}


@pytest.mark.parametrize("shape", sorted(HOSTILE))
def test_a_relay_that_answers_nonsense_leaks_no_leg_and_no_slot(monkeypatch, shape):
    """The relay is not trusted to be well formed. Whatever it answers, the outbound leg is
    closed, the slot comes back, and the app is told `unreachable`: repeated past the cap."""
    monkeypatch.setattr(peerpipe, "CALLS_MAX", 3)
    first, second = HOSTILE[shape]

    async def go():
        conns: list = []

        async def relay(conn):
            conns.append(conn)
            await conn.send(first)
            if second is not None:
                await conn.recv()
                await conn.send(second)
            await conn.wait_closed()  # a relay that never hangs up by itself

        server = await ws_serve(relay, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        directory = Path(tempfile.mkdtemp(prefix="pp"))
        (directory / "alpha-box").mkdir()
        pipe = peerpipe.PeerPipe(
            relay_url=f"ws://127.0.0.1:{port}/relay/ws",
            console_name="alpha-box",
            identity=Ed25519PrivateKey.generate(),
            identity_path=directory / "alpha-box" / "identity",
            app_host="127.0.0.1",
            app_port=None,
        )
        serving = asyncio.create_task(pipe.serve())
        try:
            await until(lambda: pipe._path.exists())
            for _ in range(peerpipe.CALLS_MAX + 3):
                answer, _reader, writer = await _call(pipe)
                assert answer == {"ok": False, "code": "unreachable"}
                writer.close()
            assert len(conns) == peerpipe.CALLS_MAX + 3
            await until(lambda: pipe._calls == 0 and not pipe._legs)
            # every relay connection was closed BY THE AGENT; none is left for close_legs
            await until(lambda: all(c.close_code is not None for c in conns))
        finally:
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)
            server.close()

    run(go())


def test_a_leg_is_tracked_while_the_relay_is_still_answering(monkeypatch):
    """A relay that sends its challenge and then stalls: the leg is one `close_legs` can end,
    and ending it frees the slot."""

    async def go():
        conns: list = []

        async def relay(conn):
            conns.append(conn)
            await conn.send(json.dumps({"t": "challenge", "nonce": _NONCE}))
            await conn.recv()
            await conn.wait_closed()  # never answers the hello

        server = await ws_serve(relay, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        directory = Path(tempfile.mkdtemp(prefix="pp"))
        (directory / "alpha-box").mkdir()
        pipe = peerpipe.PeerPipe(
            relay_url=f"ws://127.0.0.1:{port}/relay/ws",
            console_name="alpha-box",
            identity=Ed25519PrivateKey.generate(),
            identity_path=directory / "alpha-box" / "identity",
            app_host="127.0.0.1",
            app_port=None,
        )
        serving = asyncio.create_task(pipe.serve())
        try:
            await until(lambda: pipe._path.exists())
            reader, writer = await _request(pipe, {"op": "call", "target": "beta-box"})
            await until(lambda: len(pipe._legs) == 1 and pipe._calls == 1)
            await pipe.close_legs()
            answer = json.loads(await asyncio.wait_for(peerpipe.read_frame(reader), 10))
            assert answer == {"ok": False, "code": "unreachable"}
            await until(lambda: pipe._calls == 0 and not pipe._legs)
            await until(lambda: conns[0].close_code is not None)
            writer.close()
        finally:
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)
            server.close()

    run(go())
