"""Tests for the Home Free end-to-end handshake + transport."""

import pytest
from cryptography.exceptions import InvalidTag

from agent_sessions.homefree.handshake import (
    HandshakeError,
    Initiator,
    Responder,
    Transport,
    derive_psk,
)


def _complete_handshake(psk_i: bytes, psk_r: bytes):
    """Drive a full handshake; return (initiator_transport, responder_transport)."""
    ini = Initiator(psk_i)
    res = Responder(psk_r)
    msg1 = ini.start()
    msg2 = res.respond(msg1)
    t_ini, msg3 = ini.finish(msg2)
    t_res = res.finish(msg3)
    return t_ini, t_res


def test_psk_is_deterministic_and_key_sized():
    a = derive_psk("VIPER-8231-XYZ")
    b = derive_psk("VIPER-8231-XYZ")
    assert a == b
    assert len(a) == 32
    assert derive_psk("other") != a


def test_full_handshake_and_bidirectional_transport():
    psk = derive_psk("shared-access-key")
    t_ini, t_res = _complete_handshake(psk, psk)

    # initiator -> responder
    frame = t_ini.encrypt(b"hello from browser")
    assert t_res.decrypt(frame) == b"hello from browser"
    # responder -> initiator
    frame = t_res.encrypt(b"pty output bytes")
    assert t_ini.decrypt(frame) == b"pty output bytes"


def test_transport_frames_are_ciphertext():
    psk = derive_psk("k")
    t_ini, _ = _complete_handshake(psk, psk)
    marker = b"SUPER_SECRET_MARKER_9021"
    frame = t_ini.encrypt(marker)
    assert marker not in frame  # never appears in cleartext on the wire


def test_psk_mismatch_rejected_at_responder_confirmation():
    ini = Initiator(derive_psk("right"))
    res = Responder(derive_psk("wrong"))
    msg1 = ini.start()
    msg2 = res.respond(msg1)
    with pytest.raises(HandshakeError):
        ini.finish(msg2)  # responder confirmation won't verify under a different PSK


def test_psk_mismatch_rejected_at_initiator_confirmation():
    # Same-PSK up to msg2 so the initiator proceeds, then a forged msg3 fails.
    psk = derive_psk("k")
    ini = Initiator(psk)
    res = Responder(psk)
    res.respond(ini.start())
    with pytest.raises(HandshakeError):
        res.finish(b"\x00" * 16)  # bogus initiator confirmation


def test_tampered_transport_frame_rejected():
    psk = derive_psk("k")
    t_ini, t_res = _complete_handshake(psk, psk)
    frame = bytearray(t_ini.encrypt(b"payload"))
    frame[-1] ^= 0x01  # flip a ciphertext/tag bit
    with pytest.raises(InvalidTag):
        t_res.decrypt(bytes(frame))


def test_replayed_frame_rejected():
    psk = derive_psk("k")
    t_ini, t_res = _complete_handshake(psk, psk)
    frame = t_ini.encrypt(b"once")
    assert t_res.decrypt(frame) == b"once"
    with pytest.raises(HandshakeError):
        t_res.decrypt(frame)  # same counter → replay


def test_out_of_order_frame_rejected():
    psk = derive_psk("k")
    t_ini, t_res = _complete_handshake(psk, psk)
    f1 = t_ini.encrypt(b"first")
    f2 = t_ini.encrypt(b"second")
    assert t_res.decrypt(f2) == b"second"
    with pytest.raises(HandshakeError):
        t_res.decrypt(f1)  # counter went backwards


def test_bad_message_lengths_rejected():
    psk = derive_psk("k")
    with pytest.raises(HandshakeError):
        Responder(psk).respond(b"too-short")
    ini = Initiator(psk)
    ini.start()
    with pytest.raises(HandshakeError):
        ini.finish(b"too-short")


def test_nonce_counter_monotonic():
    t = Transport(b"\x00" * 32, b"\x11" * 32)
    counters = [int.from_bytes(t.encrypt(b"x")[:8], "big") for _ in range(5)]
    assert counters == [1, 2, 3, 4, 5]
