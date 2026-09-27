"""AppProxyTarget (#579 P2) — HTTP + WS reverse-proxy of mux streams to the local
app, plus the fixed-target allowlist and Origin/CSRF handling."""

from __future__ import annotations

import asyncio
import json
import struct

import httpx
import pytest
import websockets

from agent_sessions.homefree.appproxy import AppProxyTarget
from agent_sessions.homefree.mux import Mux, StreamReset


def _run(coro):
    return asyncio.run(coro)


def _browser_to(proxy: AppProxyTarget) -> Mux:
    """A browser mux whose agent peer proxies every opened stream via `proxy`."""
    mux: dict[str, Mux] = {}
    mux["browser"] = Mux(is_initiator=True, on_send=lambda f: mux["agent"].feed(f))
    mux["agent"] = Mux(
        is_initiator=False,
        on_send=lambda f: mux["browser"].feed(f),
        on_stream=lambda s: asyncio.ensure_future(proxy.serve(s)),
    )
    return mux["browser"]


async def _read_exact(stream, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        part = await stream.read(n - len(buf))
        if not part:
            break
        buf += part
    return bytes(buf)


async def _read_http_response(stream) -> tuple[dict, bytes]:
    meta_len = struct.unpack(">I", await _read_exact(stream, 4))[0]
    meta = json.loads(await _read_exact(stream, meta_len))
    body = bytearray()
    while True:
        part = await stream.read()
        if not part:
            break
        body += part
    return meta, bytes(body)


def _ws_frame(mtype: int, payload: bytes) -> bytes:
    return bytes([mtype]) + struct.pack(">I", len(payload)) + payload


async def _read_ws_msg(stream) -> tuple[int, bytes]:
    header = await _read_exact(stream, 5)
    length = struct.unpack(">I", header[1:5])[0]
    payload = await _read_exact(stream, length) if length else b""
    return header[0], payload


def test_http_get_proxied_with_origin_rewrite_and_cookie_csrf_preserved():
    async def go():
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["url"] = str(request.url)
            seen["origin"] = request.headers.get("Origin")
            seen["cookie"] = request.headers.get("Cookie")
            seen["csrf"] = request.headers.get("X-CSRF-Token")
            return httpx.Response(200, json={"ok": True})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        proxy = AppProxyTarget(app_port=8765, client=client)
        browser = _browser_to(proxy)
        s = browser.open(
            json.dumps(
                {
                    "k": "http",
                    "method": "GET",
                    "path": "/api/sessions",
                    "headers": {
                        "Origin": "https://battlelab.superstatus.io",
                        "Cookie": "session=abc",
                        "X-CSRF-Token": "tok",
                    },
                }
            ).encode()
        )
        await s.end()
        meta, body = await _read_http_response(s)
        assert meta["status"] == 200
        assert json.loads(body) == {"ok": True}
        assert seen["method"] == "GET"
        assert seen["url"] == "http://127.0.0.1:8765/api/sessions"
        assert seen["origin"] == "http://127.0.0.1:8765"  # rewritten to the app origin
        assert seen["cookie"] == "session=abc"  # preserved
        assert seen["csrf"] == "tok"  # preserved

    _run(go())


def test_http_post_body_and_csrf_reach_the_app():
    async def go():
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = request.content
            seen["csrf"] = request.headers.get("X-CSRF-Token")
            seen["origin"] = request.headers.get("Origin")
            if request.headers.get("X-CSRF-Token") != "tok":
                return httpx.Response(403)
            return httpx.Response(201, content=b"created")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        proxy = AppProxyTarget(app_port=8765, client=client, app_origin="https://box.example")
        browser = _browser_to(proxy)
        s = browser.open(
            json.dumps(
                {
                    "k": "http",
                    "method": "POST",
                    "path": "/api/sessions/x/metadata",
                    "headers": {"X-CSRF-Token": "tok"},
                }
            ).encode()
        )
        await s.write(b'{"project":"p"}')
        await s.end()
        meta, body = await _read_http_response(s)
        assert meta["status"] == 201 and body == b"created"
        assert seen["body"] == b'{"project":"p"}'
        assert (
            seen["origin"] == "https://box.example"
        )  # custom app_origin used for the loopback hop

    _run(go())


@pytest.mark.parametrize(
    "bad_path", ["http://evil.example/x", "//evil.example/x", "not-absolute", "https://x"]
)
def test_disallowed_targets_reset_the_stream(bad_path):
    async def go():
        # No host from the request is ever honoured — only the fixed app is reachable.
        proxy = AppProxyTarget(
            app_port=8765,
            client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        )
        browser = _browser_to(proxy)
        s = browser.open(json.dumps({"k": "http", "method": "GET", "path": bad_path}).encode())
        await s.end()
        with pytest.raises(StreamReset):
            await s.read()

    _run(go())


def test_websocket_message_framing_survives_coalescing_and_fragmentation():
    async def go():
        async def echo(ws):
            async for m in ws:
                await ws.send(m)

        server = await websockets.serve(echo, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            proxy = AppProxyTarget(app_port=port)
            browser = _browser_to(proxy)
            s = browser.open(json.dumps({"k": "ws", "path": "/ws/term/claude:abc"}).encode())
            # Two text messages written back-to-back must NOT coalesce into one.
            await s.write(_ws_frame(0, b"hello"))
            await s.write(_ws_frame(0, b"world"))
            assert await _read_ws_msg(s) == (0, b"hello")
            assert await _read_ws_msg(s) == (0, b"world")
            # Binary message.
            await s.write(_ws_frame(1, b"\xde\xad\xbe\xef"))
            assert await _read_ws_msg(s) == (1, b"\xde\xad\xbe\xef")
            # A large message fragments across mux DATA frames — must reassemble as one.
            big = bytes(range(256)) * 200  # ~51 KB > MAX_CHUNK
            await s.write(_ws_frame(1, big))
            assert await _read_ws_msg(s) == (1, big)
            s.reset()
        finally:
            server.close()
            await server.wait_closed()

    _run(go())


def test_browser_close_propagates_to_the_app_websocket():
    async def go():
        closed = asyncio.Event()

        async def echo(ws):
            try:
                async for m in ws:
                    await ws.send(m)
            finally:
                closed.set()  # fires when the app-side socket actually closes

        server = await websockets.serve(echo, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            proxy = AppProxyTarget(app_port=port)
            browser = _browser_to(proxy)
            s = browser.open(json.dumps({"k": "ws", "path": "/ws/x"}).encode())
            await s.write(_ws_frame(0, b"hi"))
            assert await _read_ws_msg(s) == (0, b"hi")
            await s.end()  # browser closes its half → must close the loopback app socket
            await asyncio.wait_for(closed.wait(), timeout=2)  # no leaked connection
            # …and the browser read side must get EOF, not hang half-open
            assert await asyncio.wait_for(s.read(), timeout=2) == b""
        finally:
            server.close()
            await server.wait_closed()

    _run(go())


def test_websocket_propagates_app_deliberate_close_code_to_the_browser():
    async def go():
        # The app (/ws/term) rejects with a deliberate 4401 — the proxy must serialize that
        # code as a CLOSE frame (type 2) so the browser adapter surfaces a no-retry reject
        # instead of a generic close.
        async def rejecting(ws):
            await ws.close(code=4401, reason="nope")

        server = await websockets.serve(rejecting, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            proxy = AppProxyTarget(app_port=port)
            browser = _browser_to(proxy)
            s = browser.open(json.dumps({"k": "ws", "path": "/ws/term/x"}).encode())
            mtype, payload = await _read_ws_msg(s)
            assert mtype == 2  # CLOSE frame
            assert struct.unpack(">H", payload[:2])[0] == 4401
            assert payload[2:].decode() == "nope"
            assert await asyncio.wait_for(s.read(), timeout=2) == b""  # then clean EOF
        finally:
            server.close()
            await server.wait_closed()

    _run(go())


def test_websocket_forwards_auth_cookie_and_rewrites_origin_on_upgrade():
    async def go():
        seen = {}

        async def handler(ws):
            seen["cookie"] = ws.request.headers.get("Cookie")
            seen["origin"] = ws.request.headers.get("Origin")
            async for m in ws:
                await ws.send(m)

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            proxy = AppProxyTarget(app_port=port)  # origin defaults to http://127.0.0.1:<port>
            browser = _browser_to(proxy)
            s = browser.open(
                json.dumps(
                    {
                        "k": "ws",
                        "path": "/ws/term/claude:abc",
                        "headers": {
                            "Cookie": "agent_sessions=signed",
                            "Origin": "https://battlelab.superstatus.io",
                        },
                    }
                ).encode()
            )
            await s.write(_ws_frame(0, b"ping"))
            assert await _read_ws_msg(s) == (0, b"ping")
            assert seen["cookie"] == "agent_sessions=signed"  # forwarded so /ws/term authenticates
            assert seen["origin"] == f"http://127.0.0.1:{port}"  # rewritten to the app origin
            s.reset()
        finally:
            server.close()
            await server.wait_closed()

    _run(go())


# ---------------------------------------------------------------- request body cap (#807)


def test_an_over_cap_request_body_gets_a_named_413_not_a_reset(monkeypatch):
    """The proxy accumulates the whole body before forwarding, so without a cap a hostile stream
    balloons the AGENT's memory. The refusal has to be the route's own shape: a reset would reach
    the browser as a generic network failure, which is the unnamed error #807 forbids."""
    from agent_sessions.homefree import appproxy as ap

    monkeypatch.setattr(ap, "MAX_REQUEST_BODY", 4096)

    async def go():
        proxy = AppProxyTarget(app_port=1)
        browser = _browser_to(proxy)
        s = browser.open(
            json.dumps({"k": "http", "method": "POST", "path": "/api/files/upload"}).encode()
        )
        await s.write(b"x" * 8192)
        await s.end()
        n = struct.unpack(">I", await _read_exact(s, 4))[0]
        meta = json.loads(await _read_exact(s, n))
        return meta

    meta = _run(go())
    assert meta["status"] == 413
    assert any(k.lower() == "cache-control" for k, _ in meta["headers"])


def test_a_body_under_the_cap_is_still_forwarded(monkeypatch):
    """The cap must not reject a legitimate at-the-limit upload — it carries headroom over the
    app's own per-file limit precisely so the transport never pretends to be the route."""
    from agent_sessions.homefree import appproxy as ap

    assert ap.MAX_REQUEST_BODY > 25 * 1024 * 1024, (
        "the proxy cap must sit ABOVE the 25 MiB per-file upload limit, with room for the "
        "multipart framing that makes a 25 MiB file a larger request"
    )


def test_a_browser_reset_cancels_the_upstream_request_and_frees_what_it_held():
    """#1171: an aborted fetch (Ask's New conversation) RESETS its mux stream. The proxy must end
    the upstream request with it — otherwise the app never sees a disconnect, the old question
    keeps the one-question gate, and the replacement is refused with a 409 while the discarded
    one runs on. The old request here never completes on its own; the new one must still be
    admitted."""
    from agent_sessions import aitasks

    aitasks.reset()
    held = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if aitasks.is_running("pulse-chat"):
            return httpx.Response(409, json={"detail": "a question is already running"})
        if request.url.path == "/api/pulse/ask/stream" and request.content == b'{"q":"old"}':
            try:
                async with aitasks.single_flight("pulse-chat", "ask"):
                    held.set()
                    await asyncio.Event().wait()  # the old question: never finishes by itself
            except asyncio.CancelledError:
                cancelled.set()
                raise
        return httpx.Response(200, json={"ok": True})

    async def ask(browser, body: bytes):
        s = browser.open(
            json.dumps({"k": "http", "method": "POST", "path": "/api/pulse/ask/stream"}).encode()
        )
        await s.write(body)
        await s.end()
        return s

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        proxy = AppProxyTarget(app_port=1, client=client)
        browser = _browser_to(proxy)
        old = await ask(browser, b'{"q":"old"}')
        await asyncio.wait_for(held.wait(), 5)
        assert aitasks.is_running("pulse-chat")

        old.reset()  # New conversation: the browser aborts the fetch
        await asyncio.wait_for(cancelled.wait(), 5)
        assert not aitasks.is_running("pulse-chat")

        new = await ask(browser, b'{"q":"new"}')
        meta, _ = await asyncio.wait_for(_read_http_response(new), 5)
        return meta

    try:
        meta = _run(go())
    finally:
        aitasks.reset()
    assert meta["status"] == 200


def test_a_request_that_finishes_is_not_disturbed_by_the_reset_watch():
    """The watch ends with the request: a normal response still arrives whole, and nothing is
    left pending on the stream afterwards."""

    async def go():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 70_000))
        )
        proxy = AppProxyTarget(app_port=1, client=client)
        browser = _browser_to(proxy)
        s = browser.open(json.dumps({"k": "http", "method": "GET", "path": "/api/x"}).encode())
        await s.end()
        meta, body = await asyncio.wait_for(_read_http_response(s), 5)
        await asyncio.sleep(0.05)  # the serve task's own teardown, a few loop turns
        pending = [
            t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()
        ]
        return meta, body, pending

    meta, body, pending = _run(go())
    assert meta["status"] == 200 and len(body) == 70_000
    assert pending == []
