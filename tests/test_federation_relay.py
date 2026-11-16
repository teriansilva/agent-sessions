"""Federation over the relay (#1246 P3): two real app instances, each with a real Home Free
agent, joined only by a relay double that records what it forwards.

The Noise channel runs between the two APP processes. The relay and both agents are pipes: these
tests assert they carry ciphertext only, that a flipped byte ends the handshake, and that every
missing piece (no agent, an old agent, a relay without the role) fails closed.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_sessions.federation import channel as ch
from agent_sessions.federation import endpoints as ep
from agent_sessions.federation import service
from agent_sessions.homefree import peerpipe
from agent_sessions.homefree.agent import AgentConfig, HomeFreeAgent
from relay_double import RelayDouble
from test_federation_routes import Instance, _wait

VECTOR = json.loads(
    (Path(__file__).parent / "fixtures" / "relay_peer_signature_vector.json").read_text()
)


# ── the signature the relay verifies ──────────────────────────────────


def test_the_peer_signature_matches_the_vector_shared_with_the_relay():
    nonce = bytes.fromhex(VECTOR["nonce_hex"])
    message = peerpipe.peer_message(nonce, VECTOR["name"], VECTOR["target"])
    assert message.hex() == VECTOR["message_hex"]
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(VECTOR["seed_hex"]))
    assert key.sign(message).hex() == VECTOR["signature_hex"]
    assert key.sign(nonce).hex() == VECTOR["register_signature_hex"]  # the other domain
    assert VECTOR["signature_hex"] != VECTOR["register_signature_hex"]


def test_relay_endpoints_are_validated_like_any_untrusted_input():
    good = {"t": "relay", "url": "wss://relay.example/relay/ws", "name": "Alpha-Box"}
    assert ep.clean([good]) == [
        {"t": "relay", "url": "wss://relay.example/relay/ws", "name": "alpha-box"}
    ]
    for bad in (
        {**good, "name": "a"},
        {**good, "name": "has space"},
        {**good, "name": "../etc"},
        {**good, "url": "https://relay.example/relay/ws"},
        {**good, "url": "wss://user:pw@relay.example/relay/ws"},
        {**good, "url": "wss://relay.example/relay/ws?role=agent"},
        {"t": "relay", "url": good["url"]},
        {"t": "teleport", "url": good["url"], "name": "alpha-box"},
    ):
        assert ep.clean([bad]) == [], bad
    mixed = [good, {"t": "direct", "url": "wss://box.example/ws/fed"}]
    assert [e["t"] for e in ep.ordered(ep.clean(mixed))] == ["direct", "relay"]


# ── boxes ─────────────────────────────────────────────────────────────


class Box:
    """One instance: the real app under uvicorn, plus its real Home Free agent, both running in
    the app's event loop thread."""

    def __init__(self, cfg, tmp_path, name: str, relay_url: str, *, direct: bool, agent=True):
        self.name = name
        self.app = Instance(cfg, tmp_path / name / "federation")
        self.fed = self.app.fed
        if not direct:
            self.fed.addresses = []
        # A short directory: an AF_UNIX path is limited to ~108 bytes.
        self.home = Path(tempfile.mkdtemp(prefix="hf"))
        self.fed.relay_socket = self.home / peerpipe.SOCKET_NAME
        self.loop = self.app.server.servers[0].get_loop()
        self.agent = None
        self._tasks: list = []
        if agent:
            self.agent = HomeFreeAgent(
                AgentConfig(
                    relay_url=relay_url,
                    console_name=name,
                    access_key="k" * 32,
                    identity_path=self.home / "identity",
                    app_port=self.app.port,
                )
            )
            asyncio.run_coroutine_threadsafe(self._start(), self.loop).result(10)

    async def _start(self) -> None:
        async def control() -> None:
            while True:
                try:
                    await self.agent.run_once()
                except Exception:  # noqa: BLE001 — reconnect, as the real supervisor does
                    pass
                await asyncio.sleep(0.05)

        self._tasks = [
            asyncio.create_task(control()),
            asyncio.create_task(self.agent.peer.serve()),
        ]

    def stop(self) -> None:
        async def cancel() -> None:
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)

        if self._tasks:
            asyncio.run_coroutine_threadsafe(cancel(), self.loop).result(10)
        self.app.stop()
        shutil.rmtree(self.home, ignore_errors=True)

    def reachable(self) -> list[str]:
        return self.app.state()["self"]["reachable_by"]


class World:
    """A relay double in its own thread, and the boxes around it."""

    def __init__(self, cfg, tmp_path, **relay_kwargs) -> None:
        import threading

        self.cfg, self.tmp_path = cfg, tmp_path
        self.relay = RelayDouble(**relay_kwargs)
        self.boxes: list[Box] = []
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(target=lambda: asyncio.run(self._run()), daemon=True)
        self._thread.start()
        assert self._ready.wait(10)

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        async with self.relay:
            self._ready.set()
            await self._stop.wait()

    def box(self, name: str, *, direct: bool = False, agent: bool = True) -> Box:
        box = Box(self.cfg, self.tmp_path, name, self.relay.url, direct=direct, agent=agent)
        self.boxes.append(box)
        return box

    def close(self) -> None:
        for box in self.boxes:
            box.stop()
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=10)


@pytest.fixture
def world(auth_cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(service, "DIAL_TICK_S", 0.1)
    made: list[World] = []

    def make(**relay_kwargs) -> World:
        made.append(World(auth_cfg, tmp_path, **relay_kwargs))
        return made[-1]

    yield make
    for w in made:
        w.close()


def _pairing(box: Box, pairing_id: str) -> dict | None:
    found = [p for p in box.app.state()["pairings"] if p["id"] == pairing_id]
    return found[0] if found else None


def _pair(inviter: Box, acceptor: Box) -> None:
    invite = inviter.app.post("/invites").json()
    accepted = acceptor.app.post("/pairings", {"invite": invite["invite"]})
    assert accepted.status_code == 200, accepted.text
    theirs = accepted.json()["id"]
    on_acceptor = _wait(lambda: (p := _pairing(acceptor, theirs)) and p["state"] == "confirm" and p)
    on_inviter = _wait(
        lambda: (p := _pairing(inviter, invite["id"])) and p["state"] == "confirm" and p
    )
    assert on_inviter["code"] == on_acceptor["code"]
    assert inviter.app.post(f"/pairings/{invite['id']}/confirm").status_code == 200
    assert acceptor.app.post(f"/pairings/{theirs}/confirm").status_code == 200


def _connected(box: Box) -> dict | None:
    peers = box.app.state()["peers"]
    return peers[0] if peers and peers[0]["connected"] and peers[0]["last_seen_at"] else None


def _ping(box: Box) -> dict:
    peer = box.app.state()["peers"][0]
    channel = box.fed.channel_for(peer["peer_id"])
    return asyncio.run_coroutine_threadsafe(channel.call("peer.ping"), box.loop).result(10)


PLAINTEXT_MARKERS = (b"peer.hello", b"peer.ping", b"pair.hello", b"pair.confirm", b'"verb"')


def _assert_blind(relay: RelayDouble) -> None:
    assert relay.forwarded, "the relay carried nothing: the test did not use it"
    blob = b"".join(relay.forwarded)
    for marker in PLAINTEXT_MARKERS:
        assert marker not in blob, marker
    # the only clear-text frame is the handshake preamble, which names no verb and no secret
    # A frame is clear text when it PARSES as a JSON object, not when it merely starts with
    # "{": one ciphertext frame in 256 starts with that byte.
    clear = [doc for doc in map(_json_object, relay.forwarded) if doc is not None]
    assert clear and all(doc.keys() <= {"v", "mode", "invite"} for doc in clear)


def _json_object(frame: bytes) -> dict | None:
    try:
        doc = json.loads(frame)
    except ValueError:  # includes UnicodeDecodeError
        return None
    return doc if isinstance(doc, dict) else None


def test_the_blindness_check_takes_a_ciphertext_frame_that_starts_with_a_brace():
    relay = RelayDouble.__new__(RelayDouble)
    relay.forwarded = [b'{"v":1,"mode":"pair","invite":"ab"}', b"{P^\xb7G\x93\xc3\xae<WO", b"[1]"]
    _assert_blind(relay)


# ── both ways ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("inviter", ["alpha", "beta"])
def test_two_relay_only_instances_pair_and_stay_connected(world, inviter):
    w = world()
    a, b = w.box("alpha-box"), w.box("beta-box")
    _wait(lambda: a.reachable() == ["relay"] and b.reachable() == ["relay"])
    first, second = (a, b) if inviter == "alpha" else (b, a)
    _pair(first, second)
    peer_on_a, peer_on_b = _wait(lambda: _connected(a)), _wait(lambda: _connected(b))
    assert peer_on_a["reachable_by"] == ["relay"] and peer_on_b["reachable_by"] == ["relay"]
    assert _ping(a) == {} and _ping(b) == {}
    # the pairing call went from the acceptor to the inviter, through the relay
    assert w.relay.peer_calls[0] == (second.name, first.name)
    _assert_blind(w.relay)


@pytest.mark.parametrize("inviter", ["direct", "relay"])
def test_a_direct_and_a_relay_only_instance_pair_in_either_direction(world, inviter):
    w = world()
    direct, hidden = w.box("alpha-box", direct=True), w.box("beta-box")
    _wait(lambda: direct.reachable() == ["direct", "relay"] and hidden.reachable() == ["relay"])
    if inviter == "direct":
        _pair(direct, hidden)  # the relay-only box dials the direct address, never the relay
        assert ("beta-box", "alpha-box") not in w.relay.peer_calls
    else:
        _pair(hidden, direct)  # the direct box must go through the relay to reach it
        assert w.relay.peer_calls[0] == ("alpha-box", "beta-box")
    _wait(lambda: _connected(direct))
    _wait(lambda: _connected(hidden))
    assert _ping(direct) == {} and _ping(hidden) == {}
    assert _connected(hidden)["reachable_by"] == ["direct", "relay"]


def test_the_channel_comes_back_through_the_relay_after_it_drops(world):
    w = world()
    a, b = w.box("alpha-box"), w.box("beta-box")
    _wait(lambda: a.reachable() == ["relay"] and b.reachable() == ["relay"])
    _pair(a, b)
    _wait(lambda: _connected(a) and _connected(b))
    calls = len(w.relay.peer_calls)
    peer = a.app.state()["peers"][0]["peer_id"]
    a.loop.call_soon_threadsafe(a.fed.channel_for(peer).close, "test drop")
    _wait(lambda: len(w.relay.peer_calls) > calls)
    _wait(lambda: _connected(a) and _connected(b))
    assert _ping(a) == {}


# ── nobody in the middle can read or change it ────────────────────────


def test_a_byte_flipped_by_the_relay_ends_the_pairing_and_stores_nothing(world):
    w = world()
    a, b = w.box("alpha-box"), w.box("beta-box")
    _wait(lambda: a.reachable() == ["relay"] and b.reachable() == ["relay"])
    invite = a.app.post("/invites").json()
    w.relay.flip_next = True
    theirs = b.app.post("/pairings", {"invite": invite["invite"]}).json()["id"]
    failed = _wait(lambda: (p := _pairing(b, theirs)) and p["state"] == "failed" and p)
    assert "refused" in failed["error"]
    assert a.app.state()["peers"] == [] and b.app.state()["peers"] == []
    assert not [p for p in a.app.state()["pairings"] if p["state"] == "confirm"]


# ── every missing piece fails closed ──────────────────────────────────


def test_without_a_home_free_agent_there_is_no_relay_transport(world):
    w = world()
    a = w.box("alpha-box")
    lonely = w.box("beta-box", agent=False)
    _wait(lambda: a.reachable() == ["relay"])
    time.sleep(0.3)
    assert lonely.reachable() == []
    refused = lonely.app.post("/invites")
    assert refused.status_code == 409 and refused.json()["code"] == "no_address"
    invite = a.app.post("/invites").json()
    theirs = lonely.app.post("/pairings", {"invite": invite["invite"]}).json()["id"]
    failed = _wait(lambda: (p := _pairing(lonely, theirs)) and p["state"] == "failed" and p)
    assert "Home Free is not set up" in failed["error"]
    assert w.relay.peer_calls == []


def test_a_relay_without_the_peer_role_fails_closed(world):
    w = world(peer_role=False)
    a, b = w.box("alpha-box"), w.box("beta-box")
    _wait(lambda: a.reachable() == ["relay"] and b.reachable() == ["relay"])
    invite = a.app.post("/invites").json()
    theirs = b.app.post("/pairings", {"invite": invite["invite"]}).json()["id"]
    failed = _wait(lambda: (p := _pairing(b, theirs)) and p["state"] == "failed" and p)
    assert "could not be reached" in failed["error"]
    assert a.app.state()["peers"] == [] and b.app.state()["peers"] == []


def test_an_agent_that_predates_peer_start_fails_closed(world, monkeypatch):
    w = world()
    a, b = w.box("alpha-box"), w.box("beta-box")
    _wait(lambda: a.reachable() == ["relay"] and b.reachable() == ["relay"])

    async def ignore(start):  # what an agent without this module does with an unknown message
        return None

    a.agent.peer.handle_peer_start = ignore
    invite = a.app.post("/invites").json()
    theirs = b.app.post("/pairings", {"invite": invite["invite"]}).json()["id"]
    failed = _wait(lambda: (p := _pairing(b, theirs)) and p["state"] == "failed" and p)
    assert "could not be reached" in failed["error"]


def test_an_instance_on_another_relay_is_not_dialed(world):
    w = world()
    a, b = w.box("alpha-box"), w.box("beta-box")
    _wait(lambda: a.reachable() == ["relay"] and b.reachable() == ["relay"])
    invite = a.app.post("/invites").json()
    invite_id, key, secret, _ = service.decode_invite(invite["invite"])
    elsewhere = service.encode_invite(
        invite_id=invite_id,
        public_key=key,
        secret=secret,
        addresses=[
            {"t": "relay", "url": "wss://other-relay.example/relay/ws", "name": "alpha-box"}
        ],
    )
    theirs = b.app.post("/pairings", {"invite": elsewhere}).json()["id"]
    failed = _wait(lambda: (p := _pairing(b, theirs)) and p["state"] == "failed" and p)
    assert "different relay" in failed["error"]
    assert w.relay.peer_calls == []


def test_a_call_to_an_instance_that_trusts_nobody_gets_a_closed_leg(world):
    """The callee's app refuses the `/ws/fed` upgrade (no peer, no invite): its agent closes the
    relay leg, and the caller sees the transport end before any handshake answer."""
    w = world()
    a, b = w.box("alpha-box"), w.box("beta-box")
    _wait(lambda: a.reachable() == ["relay"] and b.reachable() == ["relay"])

    async def call() -> str:
        transport = await service.dial_relay(
            {"t": "relay", "url": w.relay.url, "name": "beta-box"},
            a.fed.relay_address,
            a.fed.relay_socket,
        )
        await transport.send(ch.preamble(ch.MODE_PEER))
        try:
            await asyncio.wait_for(transport.recv(), 5)
        except ch.ChannelClosed:
            return "closed"
        return "answered"

    assert asyncio.run_coroutine_threadsafe(call(), a.loop).result(15) == "closed"


# ── the local socket ──────────────────────────────────────────────────


def _socket_request(box: Box, request: dict, *, raw: bytes | None = None):
    async def go():
        reader, writer = await asyncio.open_unix_connection(str(box.fed.relay_socket))
        peerpipe.write_frame(writer, raw if raw is not None else json.dumps(request).encode())
        await writer.drain()
        answer = await asyncio.wait_for(peerpipe.read_frame(reader), 10)
        writer.close()
        return json.loads(answer) if answer else None

    return asyncio.run_coroutine_threadsafe(go(), box.loop).result(20)


def test_the_agent_socket_is_private_and_answers_only_its_two_requests(world, monkeypatch):
    w = world()
    a = w.box("alpha-box")
    _wait(lambda: a.reachable() == ["relay"])
    path = a.fed.relay_socket
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert _socket_request(a, {"op": "info"}) == {
        "ok": True,
        "relay_url": w.relay.url,
        "name": "alpha-box",
    }
    assert "key" not in json.dumps(_socket_request(a, {"op": "info"}))
    for bad in (
        {"op": "shell", "cmd": "id"},
        {"op": "call"},
        {"op": "call", "target": "../../etc"},
        {"op": "call", "target": "x"},
        {"op": "call", "target": "beta-box", "relay": "wss://evil.example/relay/ws"},
        {"op": "info", "extra": 1},
    ):
        assert _socket_request(a, bad) == {"ok": False, "code": "bad_request"}, bad
    assert _socket_request(a, {}, raw=b"not json") is None
    assert _socket_request(a, {"op": "call", "target": "alpha-box"})["code"] == "offline"
    assert _socket_request(a, {"op": "call", "target": "ghost-box"})["code"] == "offline"
    # another local user reaching the socket gets not a word
    monkeypatch.setattr(peerpipe, "_peer_uid", lambda writer: os.getuid() + 1)
    assert _socket_request(a, {"op": "info"}) is None
    assert w.relay.peer_calls == []
