"""Pairing, trust and the dialer (#1246 §2–§4), with two real services joined by an in-memory
transport. The store and the identity key are real files under the test's tmp directory."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import stat
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from agent_sessions import engines
from agent_sessions.federation import channel as ch
from agent_sessions.federation import identity, noise, service, store
from agent_sessions.plugins import FIRST_PARTY_DIR as FIRST_PARTY
from agent_sessions.plugins import kinds, manifest
from federation_helpers import pipe, until


def run(coro):
    return asyncio.run(coro)


class Net:
    """A tiny network: every service is reachable at ``wss://<name>.example/ws/fed``."""

    def __init__(self) -> None:
        self.nodes: dict[str, service.Federation] = {}
        self.served: list = []  # the server-side end of every connection, for wire assertions
        self.down: set[str] = set()

    def url(self, name: str) -> str:
        return f"wss://{name}.example/ws/fed"

    def add(self, name: str, tmp_path, monkeypatch=None) -> service.Federation:
        fed = _Node(
            name,
            tmp_path,
            addresses=[{"t": "direct", "url": self.url(name)}],
            name=name,
            dial=self.dial,
            static=X25519PrivateKey.generate(),
        )
        self.nodes[name] = fed
        return fed

    async def dial(self, endpoint: dict):
        name = endpoint["url"].split("//")[1].split(".")[0]
        if name in self.down or name not in self.nodes:
            raise service.DialError("unreachable")
        near, far = pipe()
        self.served.append(far)
        target = self.nodes[name]
        asyncio.get_running_loop().create_task(target.serve(far))
        return near


class _Node(service.Federation):
    """A service with its OWN directory, so two instances can live in one test process."""

    def __init__(self, node: str, tmp_path, **kwargs) -> None:
        self.dir = tmp_path / node / "federation"
        super().__init__(directory=self.dir, **kwargs)


def peers_of(fed) -> dict[str, dict]:
    return {p["peer_id"]: p for p in fed.snapshot()["peers"]}


def pairing_of(fed, pairing_id) -> dict:
    return next(p for p in fed.snapshot()["pairings"] if p["id"] == pairing_id)


async def _pair(net, a, b):
    """A invites, B accepts, both confirm. Returns (a's pairing id, b's pairing id)."""
    invite = a.create_invite()
    theirs = b.accept_invite(invite["invite"])
    await until(lambda: pairing_of(b, theirs["id"])["state"] == "confirm")
    await until(lambda: any(p["state"] == "confirm" for p in a.snapshot()["pairings"]))
    mine = invite["id"]
    return mine, theirs["id"]


async def _stop(*feds):
    for fed in feds:
        await fed.stop()


# ── pairing ───────────────────────────────────────────────────────────


def test_two_instances_pair_only_after_both_confirm_the_same_code(tmp_path):
    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        mine, theirs = await _pair(net, a, b)
        code_a, code_b = pairing_of(a, mine)["code"], pairing_of(b, theirs)["code"]
        assert code_a == code_b and len(code_a) == 6 and code_a.isdigit()
        assert pairing_of(a, mine)["peer_id"] == b.peer_id
        assert pairing_of(b, theirs)["peer_id"] == a.peer_id
        await until(lambda: pairing_of(a, mine)["name"] == "beta")  # the name suggestion crossed
        await until(lambda: pairing_of(b, theirs)["name"] == "alpha")
        assert peers_of(a) == {} and peers_of(b) == {}  # nothing stored yet

        await a.confirm(mine)
        assert peers_of(a) == {} and peers_of(b) == {}  # one confirmation stores nothing
        assert pairing_of(b, theirs)["remote_confirmed"] is True

        await b.confirm(theirs)
        await until(lambda: pairing_of(a, mine)["state"] == "done")
        assert pairing_of(b, theirs)["state"] == "done"
        assert peers_of(a)[b.peer_id]["state"] == "trusted"
        assert peers_of(b)[a.peer_id]["state"] == "trusted"
        assert peers_of(a)[b.peer_id]["name"] == "beta"
        assert peers_of(a)[b.peer_id]["reachable_by"] == ["direct"]
        await _stop(a, b)

    run(go())


def test_the_invite_and_snapshot_never_expose_secrets(tmp_path):
    async def go():
        net = Net()
        a = net.add("alpha", tmp_path)
        invite = a.create_invite()
        _, key, secret, addresses = service.decode_invite(invite["invite"])
        assert key == a.public_key and addresses == a.addresses
        snap = repr(a.snapshot())
        assert service._b64(secret) not in snap and invite["invite"] not in snap
        assert a.snapshot()["invites"] == [{"id": invite["id"], "expires_at": invite["expires_at"]}]
        await _stop(a)

    run(go())


def test_an_invite_is_used_exactly_once(tmp_path):
    async def go():
        net = Net()
        a, b, c = (net.add(n, tmp_path) for n in ("alpha", "beta", "gamma"))
        invite = a.create_invite()
        first = b.accept_invite(invite["invite"])
        await until(lambda: pairing_of(b, first["id"])["state"] == "confirm")
        assert a.snapshot()["invites"] == []  # consumed by the first completed handshake
        second = c.accept_invite(invite["invite"])
        await until(lambda: pairing_of(c, second["id"])["state"] == "failed")
        assert "refused" in pairing_of(c, second["id"])["error"]
        assert pairing_of(b, first["id"])["state"] == "confirm"  # the first is unaffected
        await _stop(a, b, c)

    run(go())


def test_an_expired_invite_is_refused(tmp_path, monkeypatch):
    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        invite = a.create_invite()
        a._invites[invite["id"]].expires_at = time.time() - 1
        theirs = b.accept_invite(invite["invite"])
        await until(lambda: pairing_of(b, theirs["id"])["state"] == "failed")
        assert a.snapshot()["pairings"] == [] and peers_of(a) == {}
        await _stop(a, b)

    run(go())


def test_a_wrong_secret_fails_and_five_attempts_end_the_invite(tmp_path):
    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        invite = a.create_invite()
        invite_id, key, _secret, addresses = service.decode_invite(invite["invite"])
        forged = service.encode_invite(
            invite_id=invite_id, public_key=key, secret=b"\x07" * 32, addresses=addresses
        )
        for _ in range(service.INVITE_ATTEMPTS_MAX):
            attempt = b.accept_invite(forged)
            await until(lambda a_id=attempt["id"]: pairing_of(b, a_id)["state"] == "failed")
        assert a.snapshot()["pairings"] == []
        # the real secret no longer works either: the invite is spent
        late = b.accept_invite(invite["invite"])
        await until(lambda: pairing_of(b, late["id"])["state"] == "failed")
        assert a.snapshot()["invites"] == []
        await _stop(a, b)

    run(go())


def test_an_invite_naming_another_key_is_refused_by_the_acceptor(tmp_path):
    """The acceptor pins the key in the invite: a different instance answering at that address
    (or a relay swapping the target) is refused before the acceptor sends its own key."""

    async def go():
        net = Net()
        a, b, c = (net.add(n, tmp_path) for n in ("alpha", "beta", "gamma"))
        invite = a.create_invite()
        invite_id, _key, secret, addresses = service.decode_invite(invite["invite"])
        forged = service.encode_invite(
            invite_id=invite_id, public_key=c.public_key, secret=secret, addresses=addresses
        )
        theirs = b.accept_invite(forged)
        await until(lambda: pairing_of(b, theirs["id"])["state"] == "failed")
        assert peers_of(a) == {} and peers_of(b) == {}
        await _stop(a, b, c)

    run(go())


def test_a_denial_stores_nothing_on_either_side(tmp_path):
    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        mine, theirs = await _pair(net, a, b)
        await b.confirm(theirs)
        await a.deny(mine)
        await until(lambda: pairing_of(b, theirs)["state"] == "denied")
        assert pairing_of(a, mine)["state"] == "denied"
        assert peers_of(a) == {} and peers_of(b) == {}
        with pytest.raises(service.FederationError):
            await b.confirm(theirs)
        await _stop(a, b)

    run(go())


def test_a_dropped_channel_before_both_confirm_stores_nothing(tmp_path):
    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        mine, theirs = await _pair(net, a, b)
        await a.confirm(mine)
        b._pairings[theirs].channel.close("network")
        await until(lambda: pairing_of(a, mine)["state"] == "failed")
        await until(lambda: pairing_of(b, theirs)["state"] == "failed")
        assert peers_of(a) == {} and peers_of(b) == {}
        with pytest.raises(service.FederationError) as exc:
            await b.confirm(theirs)
        assert exc.value.status == 409
        await _stop(a, b)

    run(go())


def test_a_pairing_times_out(tmp_path):
    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        mine, theirs = await _pair(net, a, b)
        a._pairings[mine].expires_at = time.time() - 1
        assert pairing_of(a, mine)["state"] == "expired"
        await until(lambda: pairing_of(b, theirs)["state"] == "failed")
        assert peers_of(a) == {} and peers_of(b) == {}
        await _stop(a, b)

    run(go())


@pytest.mark.parametrize(
    "text",
    ["", "hello", "blfed1:", "blfed1:!!!!", "blfed1:" + "A" * 5000, None, 7, "blfed2:abcd"],
)
def test_a_malformed_invite_is_refused(tmp_path, text):
    async def go():
        net = Net()
        a = net.add("alpha", tmp_path)
        with pytest.raises(service.FederationError) as exc:
            a.accept_invite(text)
        assert exc.value.code == "bad_invite"
        await _stop(a)

    run(go())


def test_an_instance_cannot_pair_with_itself(tmp_path):
    async def go():
        net = Net()
        a = net.add("alpha", tmp_path)
        with pytest.raises(service.FederationError) as exc:
            a.accept_invite(a.create_invite()["invite"])
        assert exc.value.code == "self"
        await _stop(a)

    run(go())


def test_open_invites_are_bounded_and_need_an_address(tmp_path):
    async def go():
        net = Net()
        a = net.add("alpha", tmp_path)
        for _ in range(service.INVITES_MAX):
            a.create_invite()
        with pytest.raises(service.FederationError) as exc:
            a.create_invite()
        assert exc.value.status == 409
        hidden = _Node("hidden", tmp_path, origin="http://127.0.0.1:8000", name="hidden")
        assert hidden.addresses == []  # a loopback origin is not advertised
        with pytest.raises(service.FederationError) as exc:
            hidden.create_invite()
        assert exc.value.code == "no_address"
        await _stop(a, hidden)

    run(go())


# ── trust at the door ─────────────────────────────────────────────────


def test_the_door_is_shut_while_there_is_nobody_to_talk_to(tmp_path):
    async def go():
        net = Net()
        a = net.add("alpha", tmp_path)
        assert a.accepting() is False
        near, far = pipe()
        await near.send(ch.preamble(ch.MODE_PEER))
        await a.serve(far)
        assert far.sent == [] and far.closed  # not a byte answered
        a.create_invite()
        assert a.accepting() is True
        await _stop(a)

    run(go())


async def _same_channel(a, b) -> None:
    """Wait until both services hold the two ends of the SAME channel."""

    def same() -> bool:
        mine, theirs = a.channel_for(b.peer_id), b.channel_for(a.peer_id)
        return (
            mine is not None
            and theirs is not None
            and mine.handshake_hash == theirs.handshake_hash
            and mine.initiator != theirs.initiator
        )

    await until(same)
    # Both sides dial, so a second handshake may still be in flight and about to replace this
    # channel. Wait until no dial is pending on either side, then until they agree again.
    await until(lambda: not a._dialing and not b._dialing)
    await asyncio.sleep(0.1)
    await until(same)


async def _trusted_pair(net, a, b):
    mine, theirs = await _pair(net, a, b)
    await a.confirm(mine)
    await b.confirm(theirs)
    await until(lambda: pairing_of(a, mine)["state"] == "done")


def test_an_unknown_key_gets_handshake_frames_and_nothing_else(tmp_path):
    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        await _trusted_pair(net, a, b)  # so alpha's door is open
        stranger = X25519PrivateKey.generate()
        near = await net.dial({"url": net.url("alpha")})
        far = net.served[-1]
        est = await ch.initiate(near, stranger, mode=ch.MODE_PEER, expect_static=a.public_key)
        channel = ch.Channel(near, est, verbs={})
        runner = asyncio.create_task(channel.run())
        with pytest.raises(ch.CallError):
            await channel.call("peer.ping", timeout=2)
        await runner
        assert len(far.sent) == 1  # the one handshake message; no application frame, ever
        await _stop(a, b)

    run(go())


def test_peers_connect_and_stay_connected(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "DIAL_TICK_S", 0.05)

    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        a.start()
        b.start()
        await _trusted_pair(net, a, b)
        await _same_channel(a, b)
        assert await a.channel_for(b.peer_id).call("peer.ping") == {}
        assert await b.channel_for(a.peer_id).call("peer.ping") == {}
        await until(lambda: peers_of(a)[b.peer_id]["last_seen_at"] is not None)
        # Both sides dial. Whatever the interleaving, they settle on ONE channel and it stays:
        # no second channel replaces it and nobody keeps redialing.
        settled = a.channel_for(b.peer_id)
        dials = len(net.served)
        await asyncio.sleep(0.4)
        assert a.channel_for(b.peer_id) is settled
        assert len(net.served) == dials
        assert peers_of(a)[b.peer_id]["connected"] and peers_of(b)[a.peer_id]["connected"]
        await _stop(a, b)

    run(go())


def test_revoke_ends_trust_first_and_the_key_is_refused_afterwards(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "DIAL_TICK_S", 0.05)

    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        a.start()
        b.start()
        await _trusted_pair(net, a, b)
        await _same_channel(a, b)

        await a.revoke(b.peer_id)
        assert peers_of(a)[b.peer_id]["state"] == "revoked"
        assert a.channel_for(b.peer_id) is None
        # beta was told over the authenticated channel, shows it, and stops dialing
        await until(lambda: peers_of(b)[a.peer_id]["refused"])
        await until(lambda: not peers_of(b)[a.peer_id]["connected"])

        # beta's key is refused at the next handshake, whatever beta believes
        b._refused.clear()
        before = len(net.served)
        await until(lambda: len(net.served) > before)
        await asyncio.sleep(0.2)
        assert not peers_of(a)[b.peer_id]["connected"]
        assert all(len(end.sent) <= 1 for end in net.served[before:])
        assert await a.revoke(b.peer_id)  # idempotent
        await _stop(a, b)

    run(go())


def test_the_dialer_backs_off_and_recovers(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "DIAL_TICK_S", 0.02)
    monkeypatch.setattr(service, "BACKOFF_MIN_S", 0.05)

    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        await _trusted_pair(net, a, b)
        net.down.add("beta")
        a.start()
        await until(lambda: a._backoff.get(b.peer_id, (0, 0))[0] >= 0.1)
        assert not peers_of(a)[b.peer_id]["connected"]
        net.down.clear()
        await until(lambda: peers_of(a)[b.peer_id]["connected"], timeout=10)
        await until(lambda: b.peer_id not in a._backoff)  # a good hello resets it
        await _stop(a, b)

    run(go())


def test_only_one_process_per_store_serves_federation_and_the_rest_fail_closed(tmp_path):
    async def go():
        net = Net()
        a = net.add("alpha", tmp_path)
        a.create_invite()  # alpha takes ownership on first use
        twin = _Node("alpha", tmp_path, addresses=a.addresses, name="alpha", static=a._static)
        assert a.owner is True and twin.owner is False
        assert twin.snapshot()["owner"] is False
        assert twin.accepting() is False  # its /ws/fed refuses the upgrade
        near, far = pipe()
        await near.send(ch.preamble(ch.MODE_PEER))
        await twin.serve(far)
        assert far.sent == []
        for refused in (
            twin.create_invite,
            lambda: twin.accept_invite("blfed1:x"),
            lambda: twin.cancel_invite("x"),
        ):
            with pytest.raises(service.FederationError) as exc:
                refused()
            assert exc.value.code == "not_owner" and exc.value.status == 409
        for refused in (twin.confirm, twin.deny):
            with pytest.raises(service.FederationError) as exc:
                await refused("x")
            assert exc.value.code == "not_owner"
        # the owner going away frees it for the next process that asks
        await a.stop()
        assert twin.owner is True
        await twin.stop()

    run(go())


def test_a_process_that_is_not_the_owner_reports_connected_as_unknown(tmp_path, monkeypatch):
    """Channels live in the owner process. A twin over the same store lists the peer but cannot
    know whether it is connected: ``connected`` is ``None`` there, never ``False``."""
    monkeypatch.setattr(service, "DIAL_TICK_S", 0.05)

    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        await _trusted_pair(net, a, b)
        a.start()
        b.start()
        await until(lambda: peers_of(a)[b.peer_id]["connected"] is True)
        twin = _Node("alpha", tmp_path, addresses=a.addresses, name="alpha", static=a._static)
        snap = twin.snapshot()
        assert snap["owner"] is False
        assert [(p["peer_id"], p["state"], p["connected"]) for p in snap["peers"]] == [
            (b.peer_id, "trusted", None)
        ]
        # the owner still knows
        assert a.snapshot()["owner"] is True
        assert peers_of(a)[b.peer_id]["connected"] is True
        await twin.stop()
        await _stop(a, b)

    run(go())


@pytest.mark.parametrize("stalled", [False, True])
def test_a_revoke_written_by_another_process_ends_the_owners_channel(
    tmp_path, monkeypatch, stalled
):
    """The documented bound: the owner closes the channel on its next dialer round. That holds
    even while the channel's reader is parked in byte backpressure."""
    monkeypatch.setattr(service, "DIAL_TICK_S", 0.05)

    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        a.start()
        b.start()
        await _trusted_pair(net, a, b)
        await _same_channel(a, b)
        channel = a.channel_for(b.peer_id)
        if stalled:
            # Spend alpha's whole read allowance and overdraw it by an hour, then make it read.
            channel._bytes_in._tokens = -channel._bytes_in._cap * 60
            ping = asyncio.create_task(b.channel_for(a.peer_id).call("peer.ping", timeout=30))
            await asyncio.sleep(0.2)
            assert not ping.done()  # alpha is waiting, not reading
        twin = _Node("alpha", tmp_path, addresses=a.addresses, name="alpha", static=a._static)
        assert twin.owner is False
        loop = asyncio.get_running_loop()
        started = loop.time()
        await twin.revoke(b.peer_id)  # the one write any process may make
        await until(lambda: channel.closed, timeout=2)
        assert loop.time() - started < 1.0  # a few rounds of 0.05 s, not the backpressure wait
        assert a.channel_for(b.peer_id) is None
        assert peers_of(a)[b.peer_id]["state"] == "revoked"
        if stalled:
            with pytest.raises(ch.CallError):
                await ping  # the stalled call was never served
        await asyncio.sleep(0.2)
        assert a.channel_for(b.peer_id) is None  # and beta's redials are refused
        await _stop(a, b, twin)

    run(go())


def test_one_invite_accepted_twice_at_once_makes_one_pairing(tmp_path):
    async def go():
        net = Net()
        a, b, c = (net.add(n, tmp_path) for n in ("alpha", "beta", "gamma"))
        invite = a.create_invite()
        first, second = b.accept_invite(invite["invite"]), c.accept_invite(invite["invite"])

        def settled():
            states = {pairing_of(b, first["id"])["state"], pairing_of(c, second["id"])["state"]}
            return states == {"confirm", "failed"}

        await until(settled)
        mine = [p for p in a.snapshot()["pairings"] if p["state"] == "confirm"]
        assert len(mine) == 1 and len(a.snapshot()["pairings"]) == 1
        await _stop(a, b, c)

    run(go())


def test_pending_confirmations_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "PAIRINGS_MAX", 2)

    async def go():
        net = Net()
        a = net.add("alpha", tmp_path)
        others = [net.add(f"n{i}", tmp_path) for i in range(3)]
        invites = [a.create_invite() for _ in range(3)]

        def waiting() -> int:
            return sum(1 for p in a.snapshot()["pairings"] if p["state"] == "confirm")

        for other, invite in zip(others[:2], invites[:2], strict=True):
            other.accept_invite(invite["invite"])
        await until(lambda: waiting() == 2)
        with pytest.raises(service.FederationError) as exc:
            a.create_invite()  # two confirmations are already waiting on the operator
        assert exc.value.code == "too_many"
        # an invite minted earlier cannot add a third either: its handshake is refused
        got = others[2].accept_invite(invites[2]["invite"])
        await until(lambda: pairing_of(others[2], got["id"])["state"] == "failed")
        assert waiting() == 2 and len(a.snapshot()["pairings"]) == 2
        await _stop(a, *others)

    run(go())


def test_an_install_that_never_paired_has_no_key_and_no_store(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "DIAL_TICK_S", 0.02)

    async def go():
        fed = _Node("fresh", tmp_path, origin="https://fresh.example")
        fed.start()
        await asyncio.sleep(0.15)
        assert fed.accepting() is False
        await fed.stop()
        assert sorted(p.name for p in fed.dir.iterdir()) == [service.LOCK_FILE]
        assert stat.S_IMODE(fed.dir.stat().st_mode) == 0o700

    run(go())


# ── operator edits ────────────────────────────────────────────────────


def test_rename_is_a_local_label_and_is_validated(tmp_path):
    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        await _trusted_pair(net, a, b)
        a.rename(b.peer_id, "  GPU\tbox ‮ ")
        assert peers_of(a)[b.peer_id]["name"] == "GPU box"
        assert peers_of(b)[a.peer_id]["name"] == "alpha"  # the other side is untouched
        for bad in ("", "   ", "x" * 65, 5, None):
            with pytest.raises(service.FederationError):
                a.rename(b.peer_id, bad)
        with pytest.raises(service.FederationError) as exc:
            a.rename("a" * 32, "x")
        assert exc.value.status == 404
        with pytest.raises(service.FederationError) as exc:
            a.rename("../../etc/passwd", "x")
        assert exc.value.status == 404
        await _stop(a, b)

    run(go())


# ── identity and store ────────────────────────────────────────────────


def test_the_identity_key_is_created_once_private_and_stable(tmp_path):
    directory = tmp_path / "fed"
    first = identity.load_or_create(directory)
    second = identity.load_or_create(directory)
    assert identity.public_key(first) == identity.public_key(second)
    path = directory / identity.KEY_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    pid = identity.peer_id(identity.public_key(first))
    assert identity.PEER_ID_RE.fullmatch(pid)


def test_a_damaged_or_exposed_key_is_refused_and_never_replaced(tmp_path):
    directory = tmp_path / "fed"
    identity.load_or_create(directory)
    path = directory / identity.KEY_FILE
    original = path.read_bytes()
    os.chmod(path, 0o644)
    with pytest.raises(identity.IdentityError):
        identity.load_or_create(directory)
    os.chmod(path, 0o600)
    path.write_bytes(b"short")
    with pytest.raises(identity.IdentityError):
        identity.load_or_create(directory)
    assert path.read_bytes() == b"short" and original != b"short"  # left for the operator


def test_the_store_is_private_and_refuses_a_newer_schema(tmp_path):
    peers = store.Store(tmp_path / "fed")
    key = noise.public_bytes(X25519PrivateKey.generate())
    peer = peers.trust(key, name="box", endpoints=[])
    assert peer.trusted and peer.revision == 1
    assert stat.S_IMODE(peers.db_path.stat().st_mode) == 0o600
    assert peers.trusted_key(key).peer_id == peer.peer_id
    assert peers.revoke(peer.peer_id).state == "revoked"
    assert peers.trusted_key(key) is None  # THE handshake check says no
    assert peers.by_key(key).state == "revoked"  # the row stays
    assert peers.trust(key, name="box", endpoints=[]).revision == 3  # pairing again re-trusts

    con = sqlite3.connect(peers.db_path)
    con.execute(f"PRAGMA user_version={store.SCHEMA_VERSION + 1}")
    con.close()
    before = peers.db_path.read_bytes()
    with pytest.raises(store.StoreUnsupported):
        store.Store(tmp_path / "fed").list_peers()
    assert peers.db_path.read_bytes() == before


def test_the_default_store_follows_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_FEDERATION_DIR", str(tmp_path / "elsewhere"))
    assert store.Store().db_path == tmp_path / "elsewhere" / store.DB_FILE
    assert identity.state_dir() == tmp_path / "elsewhere"


# ── the `peer:` namespace ─────────────────────────────────────────────


def test_peer_is_never_a_local_session_key_and_no_plugin_can_claim_it():
    for key in ("peer:abc", "peer:abcdefgh/claude:1234", f"peer:{'a' * 32}/codex-api:x"):
        with pytest.raises(engines.EngineError):
            engines.parse_key(key)
    assert "peer" in kinds.NAMESPACE_RESERVED_IDS
    assert "peer" not in engines.engine_ids()
    source = (FIRST_PARTY / "codex" / "plugin.toml").read_text()
    renamed = source.replace('id = "codex"', 'id = "peer"', 1)
    assert renamed != source
    with pytest.raises(manifest.ManifestError, match="shared by another instance"):
        manifest.load_bytes(renamed.encode(), name="plugin.toml", source="test")
    # and the unchanged manifest loads, so the refusal above is about the id
    assert manifest.load_bytes(source.encode(), name="plugin.toml", source="test").id == "codex"


def test_simultaneous_dials_keep_the_channel_the_lower_peer_id_opened(tmp_path):
    """The tie-break itself, without timing: each side already holds the channel it dialed when
    the other side's arrives."""

    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        low, high = (a, b) if a.peer_id < b.peer_id else (b, a)

        class Fake:
            def __init__(self, initiator):
                self.initiator, self.closed, self.reason = initiator, False, ""

            def close(self, reason=""):
                self.closed, self.reason = True, reason

        # on the LOWER side: its own dial wins over the inbound one, in either arrival order
        mine, inbound = Fake(True), Fake(False)
        assert low._adopt(high.peer_id, mine) is True
        assert low._adopt(high.peer_id, inbound) is False and not mine.closed
        low._channels.clear()
        assert low._adopt(high.peer_id, inbound) is True
        assert low._adopt(high.peer_id, mine) is True and inbound.reason == "replaced"
        # on the HIGHER side: the inbound one (opened by the lower id) wins, in either order
        mine, inbound = Fake(True), Fake(False)
        assert high._adopt(low.peer_id, mine) is True
        assert high._adopt(low.peer_id, inbound) is True and mine.reason == "replaced"
        assert high._adopt(low.peer_id, Fake(True)) is False and not inbound.closed
        # a fresh channel from the SAME side replaces a stale one
        again = Fake(False)
        assert high._adopt(low.peer_id, again) is True and inbound.reason == "replaced"
        await _stop(a, b)

    run(go())


# ── pairing races: a decision made once stays made (#2049 review) ─────


def _hold_trust(fed):
    """Make ``fed``'s trust write wait in its worker thread. Returns (entered, release)."""
    import threading

    entered, release = threading.Event(), threading.Event()
    real = fed.store.trust

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return real(*args, **kwargs)

    fed.store.trust = held
    return entered, release


def _hold_handshakes(monkeypatch):
    """Every inbound handshake completes on the wire and then waits before the service sees the
    result: the window in which an invite can be withdrawn, expire, or be raced."""
    gate = asyncio.Event()
    real = ch.respond

    async def held(*args, **kwargs):
        est = await real(*args, **kwargs)
        await gate.wait()
        return est

    monkeypatch.setattr(service.ch, "respond", held)
    return gate


def test_a_denial_during_the_trust_write_reports_what_was_stored(tmp_path):
    """Both confirmed and the write is under way: the denial is too late and says so. A pairing
    is never `denied` with a trusted peer behind it."""

    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        mine, theirs = await _pair(net, a, b)
        entered, release = _hold_trust(a)
        await a.confirm(mine)
        confirming = asyncio.create_task(b.confirm(theirs))
        await until(entered.is_set)
        assert a._pairings[mine].state == "committing"
        assert pairing_of(a, mine)["state"] == "confirm"  # shown as the state it came from
        denying = asyncio.create_task(a.deny(mine))
        await asyncio.sleep(0.05)
        assert not denying.done()  # it waits for the write instead of answering "denied"
        # nothing else ends the pairing during the write either
        a._end_pairing(a._pairings[mine], "failed", "x")
        a._pairings[mine].expires_at = time.time() - 1
        a._sweep()
        assert a._pairings[mine].state == "committing"
        release.set()
        assert (await denying)["state"] == "done"
        await confirming
        assert peers_of(a)[b.peer_id]["state"] == "trusted"
        assert pairing_of(a, mine)["state"] == "done"
        await _stop(a, b)

    run(go())


def test_a_denial_before_the_last_confirmation_wins_and_nothing_is_stored(tmp_path):
    """The other order: the denial is made first, then the remote confirmation arrives. The
    state changed before anything was awaited, so the confirmation finds the pairing closed."""

    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        mine, theirs = await _pair(net, a, b)
        entered, _release = _hold_trust(a)
        await a.confirm(mine)
        denying = asyncio.create_task(a.deny(mine))
        confirming = asyncio.create_task(b.confirm(theirs))
        assert (await denying)["state"] == "denied"
        with pytest.raises(service.FederationError):
            await confirming
        await until(lambda: not b._pairings[theirs].open)
        assert not entered.is_set()  # the trust write never started
        assert a.store.list_peers() == [] and b.store.list_peers() == []
        assert pairing_of(a, mine)["state"] == "denied"
        await _stop(a, b)

    run(go())


@pytest.mark.parametrize("how", ["withdrawn", "expired"])
def test_an_invite_that_ends_during_its_handshake_makes_no_pairing(tmp_path, monkeypatch, how):
    async def go():
        gate = _hold_handshakes(monkeypatch)
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        invite = a.create_invite()
        theirs = b.accept_invite(invite["invite"])
        await until(lambda: a._pair_handshakes == 1)
        await asyncio.sleep(0.05)  # the handshake is finished on the wire and held
        if how == "withdrawn":
            a.cancel_invite(invite["id"])
        else:
            a._invites[invite["id"]].expires_at = time.time() - 1
        gate.set()
        await until(lambda: not b._pairings[theirs["id"]].open)
        assert a._pairings == {} and a._pair_handshakes == 0
        assert b._pairings[theirs["id"]].state == "failed"
        assert a.store.list_peers() == [] and b.store.list_peers() == []
        await _stop(a, b)

    run(go())


def test_denying_a_pairing_that_is_still_dialing_is_final(tmp_path):
    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        gate, dialed = asyncio.Event(), []
        real = net.dial

        async def slow(endpoint):
            await gate.wait()
            dialed.append(endpoint)
            return await real(endpoint)

        b._dial = slow
        invite = a.create_invite()
        theirs = b.accept_invite(invite["invite"])
        assert theirs["state"] == "dialing"
        assert (await b.deny(theirs["id"]))["state"] == "denied"
        gate.set()
        await asyncio.sleep(0.1)
        assert pairing_of(b, theirs["id"])["state"] == "denied"
        assert dialed == [] and a._pairings == {}  # the dial was cancelled, nothing connected
        with pytest.raises(service.FederationError):
            await b.confirm(theirs["id"])
        await _stop(a, b)

    run(go())


def test_a_dial_that_completes_after_the_pairing_ended_does_not_reopen_it(tmp_path):
    """Belt and braces for the case the cancel does not reach the dial: the pairing ended while
    the handshake ran, and the channel that comes back is closed instead of adopted."""

    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        gate = asyncio.Event()
        real = net.dial

        async def slow(endpoint):
            transport = await real(endpoint)
            await asyncio.shield(gate.wait())
            return transport

        b._dial = slow
        invite = a.create_invite()
        theirs = b.accept_invite(invite["invite"])
        pairing = b._pairings[theirs["id"]]
        pairing.task = None  # so ending it cannot cancel the dial
        b._end_pairing(pairing, "denied")
        gate.set()
        await asyncio.sleep(0.2)
        assert pairing.state == "denied" and pairing.channel is None
        assert all(p["state"] != "confirm" for p in b.snapshot()["pairings"])
        await _stop(a, b)

    run(go())


def test_a_remote_confirmation_after_the_deadline_stores_nothing_without_any_polling(tmp_path):
    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        mine, theirs = await _pair(net, a, b)
        await a.confirm(mine)
        # From here nobody asks A for anything (no snapshot, no operator call): only B's
        # confirmation arrives, after A's deadline.
        a._pairings[mine].timer.cancel()
        a._pairings[mine].expires_at = time.time() - 1
        with pytest.raises(service.FederationError):
            await b.confirm(theirs)
        assert a._pairings[mine].state == "expired"
        assert a.store.list_peers() == [] and b.store.list_peers() == []
        await _stop(a, b)

    run(go())


def test_an_idle_pairing_expires_by_itself(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "INVITE_TTL_S", 0.6)

    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        invite = a.create_invite()
        theirs = b.accept_invite(invite["invite"])
        # no snapshot and no operator call on either side: read the records directly
        await until(lambda: invite["id"] in a._pairings)
        await until(lambda: a._pairings[invite["id"]].state == "expired", timeout=5)
        await until(lambda: not b._pairings[theirs["id"]].open, timeout=5)
        assert a.store.list_peers() == [] and b.store.list_peers() == []
        await _stop(a, b)

    run(go())


def test_parallel_handshakes_on_distinct_invites_cannot_exceed_the_pairing_cap(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(service, "PAIRINGS_MAX", 2)

    async def go():
        gate = _hold_handshakes(monkeypatch)
        net = Net()
        a = net.add("alpha", tmp_path)
        others = [net.add(f"n{i}", tmp_path) for i in range(3)]
        invites = [a.create_invite() for _ in range(3)]
        got = [o.accept_invite(i["invite"]) for o, i in zip(others, invites, strict=True)]
        # all three are in flight at once, none registered yet: two hold a slot, one is refused
        await until(lambda: a._pair_handshakes == 2)
        await until(
            lambda: sum(
                o._pairings[g["id"]].state == "failed" for o, g in zip(others, got, strict=True)
            )
            == 1
        )
        assert a._pairings == {}
        with pytest.raises(service.FederationError) as exc:
            a.create_invite()  # the held handshakes count for the operator's actions too
        assert exc.value.code == "too_many"
        gate.set()
        await until(lambda: len(a._pairings) == 2)
        await asyncio.sleep(0.1)
        assert sum(1 for p in a._pairings.values() if p.open) == 2 and len(a._pairings) == 2
        assert a._pair_handshakes == 0
        await _stop(a, *others)

    run(go())


async def _confirmed_up_to_the_write(net, a, b):
    """Pair A and B until both confirmed and A's trust write is held in its worker. Returns
    (a's pairing id, b's pairing id, release, b's confirm task)."""
    mine, theirs = await _pair(net, a, b)
    entered, release = _hold_trust(a)
    await a.confirm(mine)
    confirming = asyncio.create_task(b.confirm(theirs))
    await until(entered.is_set)
    return mine, theirs, release, confirming


@pytest.mark.parametrize("by", ["owner", "another process"])
@pytest.mark.parametrize("start", ["trusted", "revoked"])
def test_a_revoke_is_never_undone_by_a_pairing_confirmed_before_it(tmp_path, by, start):
    """Re-pairing a known peer, both confirmed, the write held; then the peer is revoked. The
    revoke succeeded, so the stale confirmation writes nothing: the row moved under it. That
    holds from a row that was ALREADY revoked as well (the usual start of a re-pairing): every
    successful revoke moves the revision the confirmation is bound to."""

    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        first_a, first_b = await _pair(net, a, b)
        await a.confirm(first_a)
        await b.confirm(first_b)
        await until(lambda: pairing_of(a, first_a)["state"] == "done")
        assert peers_of(a)[b.peer_id]["state"] == "trusted"
        if start == "revoked":
            await a.revoke(b.peer_id)
            assert peers_of(a)[b.peer_id]["state"] == "revoked"

        mine, theirs, release, confirming = await _confirmed_up_to_the_write(net, a, b)
        twin = _Node("alpha", tmp_path, addresses=a.addresses, name="alpha", static=a._static)
        revoker = a if by == "owner" else twin
        snapshot = await revoker.revoke(b.peer_id)
        assert next(p for p in snapshot["peers"] if p["peer_id"] == b.peer_id)["state"] == "revoked"
        release.set()
        await until(lambda: a._pairings[mine].settled.is_set())
        with contextlib.suppress(service.FederationError):
            await confirming
        assert a._pairings[mine].state == "failed"
        assert "pair again" in a._pairings[mine].error
        assert peers_of(a)[b.peer_id]["state"] == "revoked"  # and it stays revoked
        assert a.store.trusted_key(b.public_key) is None
        await _stop(a, b, twin)

    run(go())


def test_a_first_pairing_is_fenced_too_and_an_unchanged_row_commits(tmp_path):
    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        # absent at confirmation, then the key appears (trusted and revoked) before the write
        mine, theirs, release, confirming = await _confirmed_up_to_the_write(net, a, b)
        assert a._pairings[mine].fence == 0
        store.Store(a.dir).trust(b.public_key, name="beta", endpoints=[])
        store.Store(a.dir).revoke(b.peer_id)
        release.set()
        await until(lambda: a._pairings[mine].settled.is_set())
        with contextlib.suppress(service.FederationError):
            await confirming
        assert a._pairings[mine].state == "failed"
        assert peers_of(a)[b.peer_id]["state"] == "revoked"
        # the store's own contract
        st = store.Store(a.dir)
        with pytest.raises(store.Conflict):
            st.trust(b.public_key, name="beta", endpoints=[], expect=0)
        fence = st.fence(b.public_key)
        assert st.trust(b.public_key, name="beta", endpoints=[], expect=fence).trusted
        with pytest.raises(store.Conflict):
            st.trust(b.public_key, name="beta", endpoints=[], expect=fence)
        await _stop(a, b)

    run(go())


@pytest.mark.parametrize("how", ["channel closed", "handler cancelled", "shutdown"])
def test_a_trust_write_under_way_settles_the_pairing_from_what_was_stored(tmp_path, how):
    """Whatever happens to the caller of the write (its channel closes, its handler is
    cancelled, the service stops), the pairing is never `failed` with a trusted peer."""

    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        mine, theirs, release, confirming = await _confirmed_up_to_the_write(net, a, b)
        pairing = a._pairings[mine]
        stopping = None
        if how == "channel closed":
            pairing.channel.close("network")
            await until(lambda: pairing.channel.closed)
        elif how == "handler cancelled":
            handlers = [t for t in asyncio.all_tasks() if "Channel._serve" in repr(t.get_coro())]
            assert handlers  # A's `pair.confirm` handler, waiting for the write
            for task in handlers:
                task.cancel()
        else:
            stopping = asyncio.create_task(a.stop())
        await asyncio.sleep(0.1)
        assert pairing.state == "committing" and not pairing.settled.is_set()
        assert a.store.list_peers() == []
        release.set()
        await until(lambda: pairing.settled.is_set())
        assert pairing.state == "done"
        assert a.store.trusted_key(b.public_key) is not None
        if stopping is not None:
            await stopping
        with contextlib.suppress(service.FederationError, ch.CallError):
            await confirming
        await _stop(a, b)

    run(go())


def test_the_peer_cap_counts_a_revoked_peer_that_is_trusted_again(tmp_path):
    st = store.Store(tmp_path / "federation")
    keys = [X25519PrivateKey.generate().public_key().public_bytes_raw() for _ in range(65)]
    first = st.trust(keys[0], name="first", endpoints=[])
    st.revoke(first.peer_id)
    for i, key in enumerate(keys[1:], 1):
        st.trust(key, name=f"p{i}", endpoints=[])
    assert sum(p.trusted for p in st.list_peers()) == store.PEERS_MAX == 64
    with pytest.raises(store.StoreError) as exc:
        st.trust(keys[0], name="first", endpoints=[])  # revoked → trusted is a move INTO trust
    assert exc.value.status == 409
    assert sum(p.trusted for p in st.list_peers()) == 64
    st.trust(keys[1], name="renamed", endpoints=[])  # an already trusted peer still updates
    st.revoke(identity.peer_id(keys[2]))
    assert st.trust(keys[0], name="first", endpoints=[]).trusted  # room again


# ── a lost answer to the last confirmation ────────────────────


def _lose_the_answer(fed, *, store_first: bool):
    """``fed`` handles ``pair.confirm`` and its channel drops before the answer leaves: after
    the trust write (``store_first``), or before anything was stored."""
    original = fed._maybe_finish

    async def lossy(pairing):
        if store_first:
            await original(pairing)
        pairing.channel.close("network")

    fed._maybe_finish = lossy


async def _accepted_then_inviter_confirms(net, a, b, *, store_first: bool, down=()):
    """The order of the 2026-11-17 failure: the ACCEPTOR (b) confirms first, then the inviter
    (a), whose call is the one that loses its answer. ``down``: instances no NEW dial reaches
    from then on (the pairing channel is already open)."""
    mine, theirs = await _pair(net, a, b)
    await b.confirm(theirs)
    net.down.update(down)
    _lose_the_answer(b, store_first=store_first)
    answer = await a.confirm(mine)
    return mine, theirs, answer


@pytest.mark.parametrize("proof", ["the other side dials in", "this side probes"])
def test_a_lost_confirmation_answer_still_pairs_both_sides(tmp_path, proof):
    """The other side stored the peer and its answer was lost. This side does not fail (it once
    did, leaving the other side trusting an instance that refuses it): it waits, shown as
    waiting, and stores the peer on proof the other side did. Either direction is proof, since
    an instance may be reachable only one way."""

    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        # "dials in": a's probe cannot reach b, so only b's dial can prove it. "probes": b
        # cannot reach a, so only a's probe can.
        down = ("beta",) if proof == "the other side dials in" else ("alpha",)
        mine, theirs, answer = await _accepted_then_inviter_confirms(
            net, a, b, store_first=True, down=down
        )
        assert answer["state"] == "confirm" and answer["local_confirmed"] is True  # "waiting"
        assert a._pairings[mine].state == "settling"
        assert peers_of(b)[a.peer_id]["state"] == "trusted"
        assert peers_of(a) == {}  # nothing stored before the proof
        b.start()  # b's dialer reaches out to its new peer
        await until(lambda: pairing_of(a, mine)["state"] == "done")
        assert peers_of(a)[b.peer_id]["state"] == "trusted"
        assert peers_of(a)[b.peer_id]["name"] == "beta"
        assert a._pairings[mine].probe.done()
        await _stop(a, b)

    run(go())


def test_a_lost_answer_without_proof_fails_and_stores_nothing(tmp_path, monkeypatch):
    """The answer was lost and the other side stored nothing: no proof can come, the probe
    finds no channel, and the pairing fails at the deadline saying what to check."""
    monkeypatch.setattr(service, "SETTLING_S", 0.6)
    monkeypatch.setattr(service, "PROBE_EVERY_S", 0.05)

    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        mine, theirs, answer = await _accepted_then_inviter_confirms(net, a, b, store_first=False)
        assert a._pairings[mine].state == "settling"
        await until(lambda: pairing_of(a, mine)["state"] == "failed")
        assert "revoke it there" in pairing_of(a, mine)["error"]
        assert a.store.list_peers() == [] and b.store.list_peers() == []
        assert not a.accepting()  # the door the settling pairing held open is shut again
        await _stop(a, b)

    run(go())


def test_a_denial_while_settling_is_final(tmp_path):
    """The operator declines while the pairing waits: a proof arriving afterwards stores
    nothing here."""

    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        mine, theirs, _ = await _accepted_then_inviter_confirms(
            net, a, b, store_first=True, down=("alpha", "beta")
        )
        assert (await a.deny(mine))["state"] == "denied"
        net.down.clear()
        b.start()
        await asyncio.sleep(0.3)
        assert pairing_of(a, mine)["state"] == "denied"
        assert a.store.trusted_key(b.public_key) is None
        await _stop(a, b)

    run(go())


def test_a_failed_trust_write_tells_the_caller_not_to_store(tmp_path, monkeypatch):
    """The side answering the last confirmation could not store the peer. Its answer says so,
    so the caller fails at once and stores nothing (it used to store on a plain ``ok``)."""

    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        mine, theirs = await _pair(net, a, b)
        await b.confirm(theirs)

        def refuse(*args, **kwargs):
            raise store.Conflict()

        monkeypatch.setattr(b.store, "trust", refuse)
        with pytest.raises(service.FederationError) as err:
            await a.confirm(mine)
        assert err.value.code == "peer_gone"
        assert pairing_of(a, mine)["state"] == "failed"
        assert a.store.list_peers() == [] and b.store.list_peers() == []
        await _stop(a, b)

    run(go())


def test_confirmations_that_cross_both_store_and_neither_reports_an_error(tmp_path):
    """Both operators confirm at once: each side's call may find the other side already done.
    That is an ``ok``, not an error shown next to a stored peer."""

    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        mine, theirs = await _pair(net, a, b)
        mine_answer, theirs_answer = await asyncio.gather(a.confirm(mine), b.confirm(theirs))
        await until(lambda: pairing_of(a, mine)["state"] == "done")
        await until(lambda: pairing_of(b, theirs)["state"] == "done")
        assert peers_of(a)[b.peer_id]["state"] == "trusted"
        assert peers_of(b)[a.peer_id]["state"] == "trusted"
        await _stop(a, b)

    run(go())


def test_a_confirmation_that_times_out_settles_like_a_closed_channel(tmp_path):
    """The other kind of lost answer: the channel stays up, the other side stored the peer and
    never answers in time. ``timeout`` is "no answer" too, so this side settles and pairs on
    the proof."""

    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        mine, theirs = await _pair(net, a, b)
        await b.confirm(theirs)
        original = b._maybe_finish

        async def slow(pairing):
            await original(pairing)  # stored on b
            await asyncio.sleep(5)  # and the answer comes too late

        b._maybe_finish = slow
        channel = a._pairings[mine].channel
        call = channel.call
        channel.call = lambda verb, args=None, **kw: call(verb, args, timeout=0.3)
        net.down.add("beta")  # only b's dial can prove it
        answer = await a.confirm(mine)
        assert answer["state"] == "confirm" and a._pairings[mine].state == "settling"
        b.start()
        await until(lambda: pairing_of(a, mine)["state"] == "done")
        assert peers_of(a)[b.peer_id]["state"] == "trusted"
        await _stop(a, b)

    run(go())


def test_a_revoke_while_settling_is_never_undone_by_the_proof(tmp_path):
    """The settling pairing stores through the same fenced write: a revoke of the key made
    while it waits (here by another process over the same store) makes the proof store
    nothing, and the row stays revoked."""

    async def go():
        net = Net()
        a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
        mine, theirs, _ = await _accepted_then_inviter_confirms(
            net, a, b, store_first=True, down=("beta",)
        )
        assert a._pairings[mine].state == "settling" and a._pairings[mine].fence == 0
        other = store.Store(a.dir)
        other.trust(b.public_key, name="beta", endpoints=[])
        other.revoke(b.peer_id)
        b.start()
        await until(lambda: pairing_of(a, mine)["state"] == "failed")
        assert peers_of(a)[b.peer_id]["state"] == "revoked"
        assert a.store.trusted_key(b.public_key) is None
        await _stop(a, b)

    run(go())
