"""End-to-end Noise-NNpsk0-style handshake + AES-256-GCM transport.

The relay never sees any of this — it forwards the opaque frames verbatim. The
wire format is pinned in ``docs/home-free-handshake.md`` so the browser WebCrypto
implementation interoperates byte-for-byte.

Primitives (all WebCrypto-available): X25519, HKDF-SHA256, AES-256-GCM, SHA-256.
"""

from __future__ import annotations

import hashlib

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PROTO = b"battlelab-home-free/v1"
PSK_INFO = b"battlelab-home-free psk v1"
KEYS_INFO = b"bl-hf keys v1"
_ZERO_NONCE = bytes(12)


class HandshakeError(Exception):
    """Raised when a confirmation fails to verify (PSK mismatch / MITM / tamper)."""


def derive_psk(access_key: str) -> bytes:
    """Derive the 32-byte pre-shared key from the installer access key string."""
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"", info=PSK_INFO).derive(
        access_key.encode("utf-8")
    )


def _nonce(counter: int) -> bytes:
    return b"\x00\x00\x00\x00" + counter.to_bytes(8, "big")


def _derive_session_keys(
    dh: bytes, psk: bytes, e_i: bytes, e_r: bytes
) -> tuple[bytes, bytes, bytes]:
    """Return (k_i2r, k_r2i, transcript_hash)."""
    th = hashlib.sha256(PROTO + e_i + e_r).digest()
    okm = HKDF(algorithm=hashes.SHA256(), length=64, salt=psk, info=KEYS_INFO + th).derive(dh)
    return okm[:32], okm[32:], th


class Transport:
    """A bidirectional AES-256-GCM stream with per-direction monotonic counters.

    Counter 0 is reserved for the handshake confirmation; transport starts at 1.
    Each frame carries its counter explicitly so replays / reorders are rejected.
    """

    def __init__(self, send_key: bytes, recv_key: bytes) -> None:
        self._send = AESGCM(send_key)
        self._recv = AESGCM(recv_key)
        self._send_ctr = 0  # incremented to 1 on first send
        self._recv_seen = 0  # last accepted counter

    def encrypt(self, plaintext: bytes) -> bytes:
        self._send_ctr += 1
        ctr = self._send_ctr
        blob = self._send.encrypt(_nonce(ctr), plaintext, None)
        return ctr.to_bytes(8, "big") + blob

    def decrypt(self, frame: bytes) -> bytes:
        if len(frame) < 8:
            raise HandshakeError("short transport frame")
        ctr = int.from_bytes(frame[:8], "big")
        if ctr <= self._recv_seen:
            raise HandshakeError("replayed or out-of-order transport frame")
        plaintext = self._recv.decrypt(_nonce(ctr), frame[8:], None)
        self._recv_seen = ctr
        return plaintext


class Initiator:
    """The browser side. ``start`` -> msg1; ``finish(msg2)`` -> (Transport, msg3)."""

    def __init__(self, psk: bytes, *, _ephemeral: X25519PrivateKey | None = None) -> None:
        # ``_ephemeral`` is a TEST-ONLY hook to pin the ephemeral key so
        # deterministic cross-implementation vectors can be generated. Production
        # callers never pass it — a fresh ephemeral is generated each handshake.
        self._psk = psk
        self._eph = _ephemeral or X25519PrivateKey.generate()
        self._e_i = self._eph.public_key().public_bytes_raw()

    def start(self) -> bytes:
        return self._e_i

    def finish(self, msg2: bytes) -> tuple[Transport, bytes]:
        if len(msg2) != 48:
            raise HandshakeError("bad msg2 length")
        e_r, conf_r = msg2[:32], msg2[32:]
        dh = self._eph.exchange(X25519PublicKey.from_public_bytes(e_r))
        k_i2r, k_r2i, th = _derive_session_keys(dh, self._psk, self._e_i, e_r)
        try:
            AESGCM(k_r2i).decrypt(_ZERO_NONCE, conf_r, th)
        except Exception as exc:  # InvalidTag et al.
            raise HandshakeError("responder confirmation failed (bad PSK?)") from exc
        conf_i = AESGCM(k_i2r).encrypt(_ZERO_NONCE, b"", th)
        # Initiator sends on k_i2r, receives on k_r2i.
        return Transport(send_key=k_i2r, recv_key=k_r2i), conf_i


class Responder:
    """The agent side. ``respond(msg1)`` -> msg2; ``finish(msg3)`` -> Transport."""

    def __init__(self, psk: bytes, *, _ephemeral: X25519PrivateKey | None = None) -> None:
        # ``_ephemeral``: TEST-ONLY deterministic-vector hook (see Initiator).
        self._psk = psk
        self._eph = _ephemeral or X25519PrivateKey.generate()
        self._e_r = self._eph.public_key().public_bytes_raw()
        self._k_i2r: bytes | None = None
        self._k_r2i: bytes | None = None
        self._th: bytes | None = None

    def respond(self, msg1: bytes) -> bytes:
        if len(msg1) != 32:
            raise HandshakeError("bad msg1 length")
        e_i = msg1
        dh = self._eph.exchange(X25519PublicKey.from_public_bytes(e_i))
        self._k_i2r, self._k_r2i, self._th = _derive_session_keys(dh, self._psk, e_i, self._e_r)
        conf_r = AESGCM(self._k_r2i).encrypt(_ZERO_NONCE, b"", self._th)
        return self._e_r + conf_r

    def finish(self, msg3: bytes) -> Transport:
        if self._k_i2r is None:
            raise HandshakeError("finish() before respond()")
        if len(msg3) != 16:
            raise HandshakeError("bad msg3 length")
        try:
            AESGCM(self._k_i2r).decrypt(_ZERO_NONCE, msg3, self._th)
        except Exception as exc:
            raise HandshakeError("initiator confirmation failed (bad PSK?)") from exc
        # Responder sends on k_r2i, receives on k_i2r.
        return Transport(send_key=self._k_r2i, recv_key=self._k_i2r)
