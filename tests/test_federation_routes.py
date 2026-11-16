"""Federation over real sockets (#1246 P1): the operator routes' auth surface, the `/ws/fed` door,
the dialer's WebSocket adapter, and two real app instances pairing end to end."""

from __future__ import annotations

import asyncio
import http
import threading
import time

import httpx
import pytest
import websockets
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi.testclient import TestClient
from websockets.asyncio.server import serve as ws_serve

from agent_sessions.federation import channel as ch
from agent_sessions.federation import service, store


def _client(cfg):
    from agent_sessions.main import create_app

    return TestClient(create_app(cfg), base_url="https://testserver")


def _login(c, cfg):
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": cfg.origin}


WRITES = [
    ("post", "/api/federation/invites", None),
    ("delete", "/api/federation/invites/abcdefgh", None),
    ("post", "/api/federation/pairings", {"invite": "blfed1:x"}),
    ("post", "/api/federation/pairings/abcdefgh/confirm", None),
    ("post", "/api/federation/pairings/abcdefgh/deny", None),
    ("patch", f"/api/federation/peers/{'a' * 32}", {"name": "x"}),
    ("post", f"/api/federation/peers/{'a' * 32}/revoke", None),
]


def _send(c, method, url, body, headers):
    kwargs = {"headers": headers}
    if body is not None:
        kwargs["json"] = body
    return getattr(c, method)(url, **kwargs)


def test_every_route_needs_a_session_and_every_write_csrf_and_origin(auth_cfg):
    c = _client(auth_cfg)
    assert c.get("/api/federation").status_code == 401
    for method, url, body in WRITES:
        assert _send(c, method, url, body, {"Origin": auth_cfg.origin}).status_code == 401, url
    hdr = _login(c, auth_cfg)
    for method, url, body in WRITES:
        assert _send(c, method, url, body, {"Origin": auth_cfg.origin}).status_code == 403, url
        evil = {**hdr, "Origin": "https://evil.example"}
        assert _send(c, method, url, body, evil).status_code == 403, url
    state = c.get("/api/federation")
    assert state.status_code == 200 and state.headers["cache-control"] == "no-store"
    body = state.json()
    assert body["peers"] == [] and body["invites"] == [] and body["pairings"] == []


def test_the_operator_api_reports_this_instance_and_maps_errors(auth_cfg):
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    me = c.get("/api/federation").json()
    assert me["owner"] is True and len(me["self"]["peer_id"]) == 32
    assert me["self"]["reachable_by"] == ["direct"]  # from AGENT_SESSIONS_ORIGIN

    made = c.post("/api/federation/invites", headers=hdr)
    assert made.status_code == 200 and made.headers["cache-control"] == "no-store"
    invite = made.json()
    assert invite["invite"].startswith("blfed1:")
    _, key, _, addresses = service.decode_invite(invite["invite"])
    assert addresses == [{"t": "direct", "url": "wss://your-domain.example/ws/fed"}]
    listed = c.get("/api/federation").json()
    assert listed["invites"] == [{"id": invite["id"], "expires_at": invite["expires_at"]}]
    assert invite["invite"] not in c.get("/api/federation").text  # shown once

    own = c.post("/api/federation/pairings", json={"invite": invite["invite"]}, headers=hdr)
    assert own.status_code == 422 and own.json()["code"] == "self"
    bad = c.post("/api/federation/pairings", json={"invite": "nope"}, headers=hdr)
    assert bad.status_code == 422 and bad.json()["code"] == "bad_invite"
    assert c.post("/api/federation/pairings", content=b"[1]", headers=hdr).status_code == 422
    big = c.post("/api/federation/pairings", content=b"x" * 20000, headers=hdr)
    assert big.status_code == 413

    assert c.delete(f"/api/federation/invites/{invite['id']}", headers=hdr).status_code == 200
    assert c.delete(f"/api/federation/invites/{invite['id']}", headers=hdr).status_code == 404
    assert c.post("/api/federation/pairings/nope/confirm", headers=hdr).status_code == 404
    assert c.post("/api/federation/pairings/nope/deny", headers=hdr).status_code == 404
    unknown = f"/api/federation/peers/{'a' * 32}"
    assert c.patch(unknown, json={"name": "x"}, headers=hdr).status_code == 404
    assert c.post(unknown + "/revoke", headers=hdr).status_code == 404
    assert c.post("/api/federation/peers/NOT-A-PEER-ID/revoke", headers=hdr).status_code == 404


def test_the_ws_door_refuses_the_upgrade_while_nobody_is_trusted_or_invited(auth_cfg):
    from starlette.websockets import WebSocketDisconnect

    c = _client(auth_cfg)
    with pytest.raises(WebSocketDisconnect), c.websocket_connect("/ws/fed"):
        pass
    hdr = _login(c, auth_cfg)
    c.post("/api/federation/invites", headers=hdr)
    # with an invite open the upgrade is accepted, and garbage ends it without an answer
    with c.websocket_connect("/ws/fed") as ws:
        ws.send_bytes(b"not a preamble")
        with pytest.raises(WebSocketDisconnect):
            ws.receive_bytes()
    with c.websocket_connect("/ws/fed") as ws:
        ws.send_text('{"v":1,"mode":"peer"}')  # a text frame is not this protocol
        with pytest.raises(WebSocketDisconnect):
            ws.receive_bytes()


def test_the_ws_door_ignores_cookies_and_the_none_mode_admin_splice(auth_cfg, monkeypatch):
    """`/ws/fed` is authenticated by the handshake only. A valid operator cookie buys nothing:
    with no peer and no invite the upgrade is still refused."""
    from starlette.websockets import WebSocketDisconnect

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    with pytest.raises(WebSocketDisconnect), c.websocket_connect("/ws/fed"):
        pass


# ── the dialer's WebSocket adapter ────────────────────────────────────


def run(coro):
    return asyncio.run(coro)


def test_the_dialer_connects_to_the_address_vetted_at_the_single_resolution(monkeypatch):
    async def go():
        seen: list[str] = []
        lookups: list[str] = []

        async def handler(conn):
            seen.append(conn.request.headers["Host"])
            await conn.send(await conn.recv())

        async def resolve(host, port):
            lookups.append(host)
            # The first answer is this machine. A rebinding name would answer differently the
            # second time; the dialer must never ask a second time.
            return ["127.0.0.1"] if len(lookups) == 1 else ["169.254.169.254"]

        monkeypatch.setattr(service, "_resolve", resolve)
        async with ws_serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            # `.invalid` never resolves in real DNS: reaching the server proves the socket went
            # to the vetted address and not to a second lookup of the name.
            url = f"ws://rebinding.invalid:{port}/ws/fed"
            transport = await service.dial_direct({"t": "direct", "url": url})
            await transport.send(b"ping")
            assert await transport.recv() == b"ping"
            await transport.close()
        assert lookups == ["rebinding.invalid"]
        assert seen == [f"rebinding.invalid:{port}"]  # the original Host is preserved

    run(go())


@pytest.mark.parametrize("target", ["same-origin", "elsewhere", "refused-address"])
def test_the_dialer_follows_no_redirect(monkeypatch, target):
    async def go():
        reached: list[str] = []

        async def elsewhere_handler(conn):
            reached.append("elsewhere")

        async def resolve(host, port):
            return ["127.0.0.1"]

        monkeypatch.setattr(service, "_resolve", resolve)
        async with ws_serve(elsewhere_handler, "127.0.0.1", 0) as other:
            other_port = other.sockets[0].getsockname()[1]
            hits: list[str] = []

            def redirect(conn, request):
                hits.append(request.path)
                if request.path != "/ws/fed":
                    return None
                location = {
                    "same-origin": "/moved/ws/fed",
                    "elsewhere": f"ws://127.0.0.1:{other_port}/ws/fed",
                    "refused-address": "ws://169.254.169.254/ws/fed",
                }[target]
                response = conn.respond(http.HTTPStatus.FOUND, "")
                response.headers["Location"] = location
                return response

            async def first_handler(conn):
                reached.append("first")

            async with ws_serve(first_handler, "127.0.0.1", 0, process_request=redirect) as first:
                port = first.sockets[0].getsockname()[1]
                with pytest.raises(service.DialError) as exc:
                    await service.dial_direct(
                        {"t": "direct", "url": f"ws://peer.invalid:{port}/ws/fed"}
                    )
                assert exc.value.code == "unreachable"
            assert hits == ["/ws/fed"]  # asked once; the Location was never requested
            assert reached == []

    run(go())


@pytest.mark.parametrize("address", ["169.254.169.254", "fe80::1", "0.0.0.0", "224.0.0.1"])  # noqa: S104
def test_the_dialer_refuses_a_link_local_or_metadata_destination(monkeypatch, address):
    async def go():
        async def resolve(host, port):
            return [address]

        monkeypatch.setattr(service, "_resolve", resolve)

        def never(*args, **kwargs):
            raise AssertionError("a refused destination must not be connected to")

        monkeypatch.setattr(service, "_Connect", never)
        with pytest.raises(service.DialError) as exc:
            await service.dial_direct({"t": "direct", "url": "wss://peer.example/ws/fed"})
        assert exc.value.code == "refused_destination"

    run(go())


# ── two real app instances ────────────────────────────────────────────


class Instance:
    """One real app under uvicorn with its own federation directory and a running dialer."""

    def __init__(self, cfg, directory) -> None:
        import uvicorn

        from agent_sessions.main import create_app

        self.cfg = cfg
        self.app = create_app(cfg)
        self.fed: service.Federation = self.app.state.federation
        self.fed._directory = directory
        self.fed.store = store.Store(directory)
        self.server = uvicorn.Server(
            uvicorn.Config(self.app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
        )
        self.thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not (self.server.started and self.server.servers) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{self.port}/ws/fed"
        self.fed.addresses = [{"t": "direct", "url": self.url}]
        self.http = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=20)
        r = self.http.post(
            "/login",
            data={"username": "marcus", "password": "hunter2"},
            headers={"Origin": cfg.origin},
            follow_redirects=False,
        )
        assert r.status_code in (302, 303), r.text
        # The cookie is `Secure`; carry it by hand over this loopback hop.
        cookie = "; ".join(v.split(";", 1)[0] for v in r.headers.get_list("set-cookie"))
        self.http.cookies.clear()
        self.read = {"Cookie": cookie}
        csrf = self.http.get("/api/config", headers=self.read).json()["csrf"]
        self.write = {**self.read, "X-CSRF-Token": csrf, "Origin": cfg.origin}

    async def _main(self) -> None:
        self.fed.start()
        try:
            await self.server.serve()
        finally:
            await self.fed.stop()

    def state(self) -> dict:
        r = self.http.get("/api/federation", headers=self.read)
        assert r.status_code == 200, r.text
        return r.json()

    def post(self, path: str, body: dict | None = None) -> httpx.Response:
        return self.http.post(f"/api/federation{path}", json=body or {}, headers=self.write)

    def stop(self) -> None:
        self.http.close()
        self.server.should_exit = True
        self.thread.join(timeout=15)


def _wait(predicate, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("timed out")


@pytest.fixture
def two_instances(auth_cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(service, "DIAL_TICK_S", 0.1)
    a = Instance(auth_cfg, tmp_path / "a" / "federation")
    b = Instance(auth_cfg, tmp_path / "b" / "federation")
    yield a, b
    a.stop()
    b.stop()


def test_two_real_instances_pair_connect_ping_and_revoke(two_instances):
    a, b = two_instances
    assert a.state()["self"]["peer_id"] != b.state()["self"]["peer_id"]

    invite = a.post("/invites").json()
    accepted = b.post("/pairings", {"invite": invite["invite"]})
    assert accepted.status_code == 200, accepted.text
    theirs = accepted.json()["id"]

    def confirming(inst, pairing_id):
        found = [p for p in inst.state()["pairings"] if p["id"] == pairing_id]
        return found[0] if found and found[0]["state"] == "confirm" else None

    on_b = _wait(lambda: confirming(b, theirs))
    on_a = _wait(lambda: confirming(a, invite["id"]))
    assert on_a["code"] == on_b["code"] and len(on_a["code"]) == 6
    assert on_a["peer_id"] == b.state()["self"]["peer_id"]
    assert a.state()["peers"] == [] and b.state()["peers"] == []

    assert a.post(f"/pairings/{invite['id']}/confirm").status_code == 200
    assert a.state()["peers"] == []  # one operator alone stores nothing
    assert b.post(f"/pairings/{theirs}/confirm").status_code == 200

    def connected(inst):
        peers = inst.state()["peers"]
        return peers and peers[0]["connected"] and peers[0]["last_seen_at"] and peers[0]

    peer_on_a, peer_on_b = _wait(lambda: connected(a)), _wait(lambda: connected(b))
    assert peer_on_a["state"] == "trusted" and peer_on_b["state"] == "trusted"
    assert peer_on_a["peer_id"] == b.state()["self"]["peer_id"]

    # a call over the real channel, from each side's own event loop
    for inst, other in ((a, peer_on_a), (b, peer_on_b)):
        channel = inst.fed.channel_for(other["peer_id"])
        loop = inst.server.servers[0].get_loop()
        assert asyncio.run_coroutine_threadsafe(channel.call("peer.ping"), loop).result(10) == {}

    # rename is local
    renamed = a.http.patch(
        f"/api/federation/peers/{peer_on_a['peer_id']}", json={"name": "GPU box"}, headers=a.write
    )
    assert renamed.status_code == 200
    assert a.state()["peers"][0]["name"] == "GPU box"
    assert b.state()["peers"][0]["name"] != "GPU box"

    # revoke: trust ends on alpha, the channel closes on both, and beta's key is refused after
    assert a.post(f"/peers/{peer_on_a['peer_id']}/revoke").status_code == 200
    assert a.state()["peers"][0]["state"] == "revoked"
    _wait(lambda: not b.state()["peers"][0]["connected"])
    _wait(lambda: b.state()["peers"][0]["refused"])
    b.fed._refused.clear()  # even if beta forgets it was told, alpha's door stays shut
    time.sleep(0.6)
    assert not a.state()["peers"][0]["connected"]
    assert not b.state()["peers"][0]["connected"]


def test_a_stranger_reaches_a_real_instance_and_gets_nothing(two_instances):
    a, b = two_instances

    async def knock(key=None) -> list[bytes]:
        """Complete a handshake as an unknown key and report every frame the server sent."""
        frames: list[bytes] = []
        async with websockets.connect(a.url, proxy=None) as conn:

            class T:
                async def send(self, data):
                    await conn.send(data)

                async def recv(self):
                    try:
                        data = await conn.recv()
                    except websockets.ConnectionClosed as exc:
                        raise ch.ChannelClosed() from exc
                    frames.append(data)
                    return data

                async def close(self):
                    await conn.close()

            est = await ch.initiate(
                T(),
                key or X25519PrivateKey.generate(),
                mode=ch.MODE_PEER,
                expect_static=a.fed.public_key,
            )
            channel = ch.Channel(T(), est, verbs={})
            runner = asyncio.create_task(channel.run())
            with pytest.raises(ch.CallError):
                await channel.call("peer.ping", timeout=3)
            await runner
        return frames

    # the door is shut entirely until alpha has an invite or a peer
    with pytest.raises(websockets.InvalidStatus) as exc:
        asyncio.run(knock())
    assert exc.value.response.status_code == 403

    a.post("/invites")
    frames = asyncio.run(knock())
    assert len(frames) == 1  # the handshake's second message; never an application frame
