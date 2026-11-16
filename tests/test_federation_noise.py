"""Noise XX for the peer channel (#1246): the published cacophony vectors, byte for byte, plus
the properties the channel relies on (a wrong psk fails, a tampered message fails, the caller
learns the remote static key)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from agent_sessions.federation import noise

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "noise_xx_vectors.json").read_text())[
    "vectors"
]


def _key(hexstr: str) -> X25519PrivateKey:
    return X25519PrivateKey.from_private_bytes(bytes.fromhex(hexstr))


def test_both_published_patterns_are_covered():
    assert sorted(v["protocol_name"] for v in VECTORS) == [
        noise.NAME_XX.decode(),
        noise.NAME_XXPSK3.decode(),
    ]


@pytest.mark.parametrize("vec", VECTORS, ids=lambda v: v["protocol_name"])
def test_published_vector(vec):
    psks = vec.get("init_psks") or []
    psk = bytes.fromhex(psks[0]) if psks else None
    init = noise.HandshakeState(
        initiator=True,
        static=_key(vec["init_static"]),
        ephemeral=_key(vec["init_ephemeral"]),
        prologue=bytes.fromhex(vec["init_prologue"]),
        psk=psk,
    )
    resp = noise.HandshakeState(
        initiator=False,
        static=_key(vec["resp_static"]),
        ephemeral=_key(vec["resp_ephemeral"]),
        prologue=bytes.fromhex(vec["resp_prologue"]),
        psk=psk,
    )
    messages = vec["messages"]
    for i, msg in enumerate(messages[:3]):
        writer, reader = (init, resp) if i % 2 == 0 else (resp, init)
        payload = bytes.fromhex(msg["payload"])
        wire = writer.write_message(payload)
        assert wire.hex() == msg["ciphertext"], f"handshake message {i}"
        assert reader.read_message(wire) == payload
    assert init.complete and resp.complete
    assert init.handshake_hash == resp.handshake_hash
    if vec.get("handshake_hash"):
        assert init.handshake_hash.hex() == vec["handshake_hash"]
    assert init.rs == noise.public_bytes(_key(vec["resp_static"]))
    assert resp.rs == noise.public_bytes(_key(vec["init_static"]))
    i_send, i_recv = init.split()
    r_send, r_recv = resp.split()
    assert len(messages) > 3, "the vector must exercise the transport keys too"
    for i, msg in enumerate(messages[3:], start=3):
        send, recv = (i_send, r_recv) if i % 2 == 0 else (r_send, i_recv)
        payload = bytes.fromhex(msg["payload"])
        wire = send.encrypt(b"", payload)
        assert wire.hex() == msg["ciphertext"], f"transport message {i}"
        assert recv.decrypt(b"", wire) == payload


def _pair(psk_i=None, psk_r=None):
    a, b = X25519PrivateKey.generate(), X25519PrivateKey.generate()
    return (
        noise.HandshakeState(initiator=True, static=a, psk=psk_i, prologue=b"p"),
        noise.HandshakeState(initiator=False, static=b, psk=psk_r, prologue=b"p"),
        a,
        b,
    )


def test_a_wrong_psk_fails_the_last_message():
    init, resp, _, _ = _pair(psk_i=b"\x01" * 32, psk_r=b"\x02" * 32)
    resp.read_message(init.write_message())
    init.read_message(resp.write_message())
    with pytest.raises(noise.NoiseError):
        resp.read_message(init.write_message())
    assert not resp.complete


def test_a_tampered_handshake_message_fails():
    init, resp, _, _ = _pair()
    resp.read_message(init.write_message())
    wire = bytearray(resp.write_message())
    wire[40] ^= 1
    with pytest.raises(noise.NoiseError):
        init.read_message(bytes(wire))


def test_a_different_prologue_fails():
    a, b = X25519PrivateKey.generate(), X25519PrivateKey.generate()
    init = noise.HandshakeState(initiator=True, static=a, prologue=b"one")
    resp = noise.HandshakeState(initiator=False, static=b, prologue=b"two")
    resp.read_message(init.write_message())
    with pytest.raises(noise.NoiseError):
        init.read_message(resp.write_message())


def test_turn_order_is_enforced():
    init, resp, _, _ = _pair()
    with pytest.raises(noise.NoiseError):
        resp.write_message()
    with pytest.raises(noise.NoiseError):
        init.read_message(b"\x00" * 32)
    with pytest.raises(noise.NoiseError):
        init.split()


def test_transport_rejects_replay_and_reorder():
    init, resp, _, _ = _pair()
    resp.read_message(init.write_message())
    init.read_message(resp.write_message())
    resp.read_message(init.write_message())
    send, _ = init.split()
    _, recv = resp.split()
    first, second = send.encrypt(b"", b"one"), send.encrypt(b"", b"two")
    with pytest.raises(noise.NoiseError):
        recv.decrypt(b"", second)  # out of order
    assert recv.decrypt(b"", first) == b"one"
    with pytest.raises(noise.NoiseError):
        recv.decrypt(b"", first)  # replay
    assert recv.decrypt(b"", second) == b"two"


def test_a_low_order_static_key_is_refused():
    init, resp, _, _ = _pair()
    wire = bytearray(init.write_message())
    wire[:32] = bytes(32)  # an all-zero ephemeral gives an all-zero shared secret
    resp.read_message(bytes(wire))
    with pytest.raises(noise.NoiseError):
        resp.write_message()
