"""Federation over real sockets (#1246 P1): the operator routes' auth surface, the `/ws/fed` door,
the dialer's WebSocket adapter, and two real app instances pairing end to end. And sharing an API
session, view only (#2054): the grant and remote routes, and two real instances from grant to
revoke (that last one lives in `test_federation_sharing.py`, beside the runtime fixtures)."""

from __future__ import annotations

import asyncio
import http
import threading
import time
import uuid

import httpx
import pytest
import websockets
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi.testclient import TestClient
from websockets.asyncio.server import serve as ws_serve

from agent_sessions import shares
from agent_sessions.federation import channel as ch
from agent_sessions.federation import grants, noise, remote, service, store


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
    # sharing a session with a peer (#2054)
    ("post", "/api/federation/grants", {"session_key": "codex-api:x", "peer_id": "a" * 32}),
    ("patch", f"/api/federation/grants/{'g' * 22}", {"expires_in": 3600, "revision": 1}),
    ("post", f"/api/federation/grants/{'g' * 22}/revoke", None),
]
REMOTE = f"/api/federation/remote/{'a' * 32}/{'g' * 22}"
READS = [
    "/api/federation",
    "/api/federation/grants",
    "/api/federation/remote",
    f"{REMOTE}/snapshot",
    f"{REMOTE}/events",
    f"{REMOTE}/history?before={uuid.UUID(int=1)}",
    f"{REMOTE}/frame",
]


def _send(c, method, url, body, headers):
    kwargs = {"headers": headers}
    if body is not None:
        kwargs["json"] = body
    return getattr(c, method)(url, **kwargs)


def test_every_route_needs_a_session_and_every_write_csrf_and_origin(auth_cfg):
    c = _client(auth_cfg)
    for url in READS:
        assert c.get(url).status_code == 401, url
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
    # with a session, the new reads answer (nothing is shared either way), uncached
    for url, key in (("/api/federation/grants", "grants"), ("/api/federation/remote", "remote")):
        listed = c.get(url)
        assert listed.status_code == 200 and listed.json()[key] == []
        assert listed.headers["cache-control"] == "no-store"
    for url in READS[3:]:
        assert c.get(url).status_code == 404, url  # not shared with this instance
    # a read is not a write: none of them is reachable by another method
    for url in READS[2:]:
        assert c.post(url.split("?")[0], headers=hdr).status_code == 405, url


def _trusted_peer(c) -> store.Peer:
    key = noise.public_bytes(X25519PrivateKey.generate())
    return c.app.state.federation.store.trust(key, name="GPU box", endpoints=[])


def test_the_operator_grants_changes_and_revokes_through_the_api(auth_cfg, monkeypatch):
    monkeypatch.setattr(shares, "session_state", lambda prov, native: "live")
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    peer = _trusted_peer(c)
    key = f"codex-api:{uuid.uuid4()}"

    made = c.post(
        "/api/federation/grants", json={"session_key": key, "peer_id": peer.peer_id}, headers=hdr
    )
    assert made.status_code == 201 and made.headers["cache-control"] == "no-store"
    grant = made.json()["grant"]
    assert grant["session_key"] == key and grant["peer_id"] == peer.peer_id
    assert grant["peer_name"] == "GPU box" and grant["level"] == "view" and grant["revision"] == 1
    assert grant["state"] == "live" and made.json()["generation"] == 1
    assert 7 * 86400 - 60 < grant["expires_at"] - time.time() < 7 * 86400 + 60  # the default

    listed = c.get("/api/federation/grants").json()
    assert [g["grant_id"] for g in listed["grants"]] == [grant["grant_id"]]
    assert listed["levels"] == ["view", "suggest", "send"]
    assert listed["default_level"] == "view" and listed["max_expires_in"] == 90 * 86400
    assert listed["max_expires_in_by_level"] == {
        "view": 90 * 86400,
        "suggest": 30 * 86400,
        "send": 24 * 3600,
    }
    assert listed["default_expires_in"]["send"] == 3600 and listed["send_risk"]
    assert c.get(f"/api/federation/grants?session={key}").json()["grants"] == listed["grants"]
    other = f"codex-api:{uuid.uuid4()}"
    assert c.get(f"/api/federation/grants?session={other}").json()["grants"] == []

    # The grants to ONE peer, for Settings → Peers: the same rows, each with its session's title.
    second = _trusted_peer(c)
    mine = c.get(f"/api/federation/grants?peer={peer.peer_id}").json()
    assert [g["grant_id"] for g in mine["grants"]] == [grant["grant_id"]]
    # no such session on disk: no title; a listed grant also counts its thread's notes (#2062)
    assert mine["grants"][0] == {**grant, "title": "", "notes": 0}
    monkeypatch.setattr(grants, "titles", lambda keys: {k: "Refactor the importer" for k in keys})
    mine = c.get(f"/api/federation/grants?peer={peer.peer_id}").json()
    assert mine["grants"][0]["title"] == "Refactor the importer"
    assert c.get(f"/api/federation/grants?peer={second.peer_id}").json()["grants"] == []
    assert c.get("/api/federation/grants?peer=..%2Fx").status_code == 404
    assert "title" not in c.get("/api/federation/grants").json()["grants"][0]

    # what the operator API refuses, and how
    def refused(body: dict) -> tuple[int, str]:
        r = c.post("/api/federation/grants", json=body, headers=hdr)
        return r.status_code, r.json()["code"]

    terminal = "claude:11111111-1111-1111-1111-111111111111"
    # a terminal session is shared view only (#2056): any other level is refused, a chat
    # endpoint (nothing to show) is not shareable, and `view` makes a grant of kind `terminal`
    assert refused({"session_key": terminal, "peer_id": peer.peer_id, "level": "send"}) == (
        422,
        "invalid",
    )
    shown = c.post(
        "/api/federation/grants",
        json={"session_key": terminal, "peer_id": peer.peer_id, "level": "view"},
        headers=hdr,
    )
    assert shown.status_code == 201, shown.text
    assert shown.json()["grant"]["kind"] == "terminal" and grant["kind"] == "api"
    assert (
        c.post(
            f"/api/federation/grants/{shown.json()['grant']['grant_id']}/revoke", headers=hdr
        ).json()["grant"]["state"]
        == "revoked"
    )
    assert refused({"session_key": key, "peer_id": peer.peer_id}) == (409, "conflict")
    assert refused({"session_key": other, "peer_id": "b" * 32}) == (404, "not_found")
    assert refused({"session_key": "nope:1", "peer_id": peer.peer_id}) == (404, "not_found")
    assert refused({"session_key": f"peer:{'a' * 32}/{key}", "peer_id": peer.peer_id})[0] == 404
    assert refused({"session_key": other, "peer_id": peer.peer_id, "level": "send"})[0] == 422
    assert refused({"session_key": other, "peer_id": peer.peer_id, "expires_in": 91 * 86400}) == (
        422,
        "invalid",
    )
    assert c.get("/api/federation/grants?session=claude:nope").status_code in (404, 422)
    assert len(c.get("/api/federation/grants").json()["grants"]) == 1

    url = f"/api/federation/grants/{grant['grant_id']}"
    changed = c.patch(url, json={"expires_in": 3600, "revision": 1}, headers=hdr)
    assert changed.status_code == 200 and changed.json()["grant"]["revision"] == 2
    assert changed.json()["grant"]["expires_at"] < grant["expires_at"]
    assert c.patch(url, json={"expires_in": 3600, "revision": 1}, headers=hdr).status_code == 409
    assert c.patch(url, json={"expires_in": 3600}, headers=hdr).status_code == 422
    assert c.patch(url, json={"revision": 2}, headers=hdr).status_code == 422
    missing = f"/api/federation/grants/{'z' * 22}"
    assert (
        c.patch(missing, json={"expires_in": 3600, "revision": 1}, headers=hdr).status_code == 404
    )

    ended = c.post(f"{url}/revoke", headers=hdr)
    assert ended.status_code == 200 and ended.json()["grant"]["state"] == "revoked"
    assert c.post(f"{url}/revoke", headers=hdr).status_code == 200  # idempotent
    assert c.post(f"{missing}/revoke", headers=hdr).status_code == 404
    assert c.post("/api/federation/grants/..%2Fx/revoke", headers=hdr).status_code in (404, 405)
    assert c.get("/api/federation/grants").json()["grants"] == []
    events = c.app.state.federation.store.grant_events(grant["grant_id"])
    assert [e["event"] for e in events] == ["granted", "changed", "revoked"]


def test_the_remote_routes_answer_offline_not_shared_and_invalid(auth_cfg, monkeypatch):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    fed = c.app.state.federation
    peer = _trusted_peer(c)
    grant_id = store.new_grant_id()
    native = str(uuid.uuid4())
    row = {
        "grant_id": grant_id,
        "engine": "codex-api",
        "native_id": native,
        "title": "shared with us",
        "kind": "api",
        "level": "view",
        "expires_at": time.time() + 3600,
        "revision": 4,
    }
    assert fed.store.replace_remote(peer.peer_id, [row])
    (listed,) = c.get("/api/federation/remote").json()["remote"]
    assert listed["key"] == f"peer:{peer.peer_id}/codex-api:{native}"
    assert listed["peer_name"] == "GPU box" and listed["connected"] is False
    assert listed["title"] == "shared with us" and listed["revision"] == 4

    base = f"/api/federation/remote/{peer.peer_id}/{grant_id}"
    turn = str(uuid.uuid4())
    # Mirrored, and its owner is not connected: 503, never an answer from the cache.
    for url in (f"{base}/snapshot", f"{base}/events", f"{base}/history?before={turn}"):
        r = c.get(url)
        assert r.status_code == 503 and r.json()["code"] == "peer_offline", url
    # Not mirrored, or not an id at all: 404, whether or not anything is connected.
    for url in (
        f"/api/federation/remote/{peer.peer_id}/{'z' * 22}/snapshot",
        f"/api/federation/remote/{'b' * 32}/{grant_id}/snapshot",
        f"/api/federation/remote/{peer.peer_id}/..%2F..%2Fx/snapshot",
        f"/api/federation/remote/NOT-A-PEER/{grant_id}/events",
    ):
        r = c.get(url)
        assert r.status_code == 404, url
    for query in ("wait=21", "wait=-1", "wait=nan", "wait=x", "limit=0", "limit=101", "after=-1"):
        assert c.get(f"{base}/events?{query}").status_code == 422, query
    for before in ("", "?before=", "?before=../x", "?before=7"):
        assert c.get(f"{base}/history{before}").status_code == 422, before

    # What the route asks of the channel: the verb, the arguments, and for a held read a timeout
    # of the wait plus ten seconds (the channel's 20 s default would fire before a 20 s wait).
    asked: list[tuple] = []
    answer = {"link": "l", "revision": 9, "next_cursor": 9, "events": []}

    async def read(federation, peer_id, grant, verb, extra, *, check, timeout=ch.CALL_TIMEOUT_S):
        asked.append((peer_id, grant, verb, extra, timeout))
        return check(answer)

    monkeypatch.setattr(remote, "read", read)
    held = c.get(f"{base}/events?after=5&limit=10&wait=20")
    assert held.status_code == 200 and held.json() == answer
    assert held.headers["cache-control"] == "no-store"
    assert c.get(f"{base}/events").status_code == 200
    assert asked == [
        (peer.peer_id, grant_id, "session.events", {"after": 5, "limit": 10, "wait": 20.0}, 30.0),
        (peer.peer_id, grant_id, "session.events", {"after": 0, "limit": 100, "wait": 0.0}, 10.0),
    ]

    # Every refusal a read can end in has a status; a refused grant is 404.
    async def refused(*_a, **_k):
        raise remote.ReadError(code)

    monkeypatch.setattr(remote, "read", refused)
    for code, status in (
        ("no_grant", 404),
        ("peer_offline", 503),
        ("unavailable", 503),
        ("busy", 429),
        ("conflict", 409),
        ("invalid", 422),
        ("timeout", 504),
        ("bad_answer", 502),
        ("too_large", 413),
        ("held", 409),
        ("not_found", 404),
        ("internal", 502),
        ("something-a-peer-made-up", 502),
    ):
        r = c.get(f"{base}/snapshot")
        assert r.status_code == status, code
        assert "something-a-peer-made-up" not in r.text  # a peer's word is never echoed


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
        # `log_config=None, log_level=None` (and no `access_log=False`): uvicorn must not
        # reconfigure the process's loggers. Its defaults set `propagate = False` and a level on
        # `uvicorn.error` / `uvicorn.access` for good, which silently emptied `caplog` for any
        # later test in the same worker
        # (`test_shares.py::test_share_tokens_are_masked_in_uvicorn_logs`).
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                host="127.0.0.1",
                port=0,
                log_config=None,
                log_level=None,
                lifespan="off",
            )
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
