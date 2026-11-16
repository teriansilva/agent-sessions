"""Noise XX for the federation peer channel (#1246).

This is the published Noise Protocol Framework (revision 34), not a protocol of our own: the
patterns ``Noise_XX_25519_AESGCM_SHA256`` and ``Noise_XXpsk3_25519_AESGCM_SHA256``, built on the
``cryptography`` primitives the app already pins. ``tests/test_federation_noise.py`` runs the
published cacophony test vectors for both names through it, byte for byte.

Why XX: both instances hold a long-lived X25519 static key, and each must learn and check the
other's. XX sends both static keys encrypted and authenticates both sides. ``psk3`` mixes the
one-time invite secret into the first pairing, so only the holder of the invite can complete it.

    -> e
    <- e, ee, s, es
    -> s, se            (psk3: then psk)

A :class:`HandshakeState` never decides trust. It reports the remote static key (``rs``) and the
handshake hash; the caller compares ``rs`` with its pinned key before it accepts a single
application frame.
"""

from __future__ import annotations

import hashlib
import hmac

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

NAME_XX = b"Noise_XX_25519_AESGCM_SHA256"
NAME_XXPSK3 = b"Noise_XXpsk3_25519_AESGCM_SHA256"

DHLEN = 32
HASHLEN = 32
TAGLEN = 16
# The Noise spec caps every message at 65535 bytes.
MAX_MESSAGE = 65535
MAX_PLAINTEXT = MAX_MESSAGE - TAGLEN
# A nonce of 2^64-1 is reserved; a cipher that reaches it must stop.
_MAX_NONCE = (1 << 64) - 1


class NoiseError(Exception):
    """The handshake or a transport message failed. Never carries key material."""


def public_bytes(private: X25519PrivateKey) -> bytes:
    return private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _dh(private: X25519PrivateKey, public: bytes) -> bytes:
    try:
        return private.exchange(X25519PublicKey.from_public_bytes(public))
    except ValueError as exc:  # an all-zero shared secret (a low-order point)
        raise NoiseError("bad public key") from exc


def _hkdf(chaining_key: bytes, ikm: bytes, outputs: int) -> list[bytes]:
    """Noise's HKDF (spec §4.3): HMAC-SHA256 with the chaining key as the salt."""
    temp = hmac.new(chaining_key, ikm, hashlib.sha256).digest()
    out: list[bytes] = []
    prev = b""
    for i in range(1, outputs + 1):
        prev = hmac.new(temp, prev + bytes([i]), hashlib.sha256).digest()
        out.append(prev)
    return out


class CipherState:
    """One direction's AEAD key with its counter nonce (spec §5.1)."""

    def __init__(self, key: bytes | None = None) -> None:
        self._key = key
        self._aead = AESGCM(key) if key is not None else None
        self.n = 0

    def has_key(self) -> bool:
        return self._key is not None

    def _nonce(self) -> bytes:
        if self.n >= _MAX_NONCE:
            raise NoiseError("nonce exhausted")
        # AESGCM in Noise: 32 zero bits, then the counter big-endian.
        return b"\x00\x00\x00\x00" + self.n.to_bytes(8, "big")

    def encrypt(self, ad: bytes, plaintext: bytes) -> bytes:
        if self._aead is None:
            return plaintext
        out = self._aead.encrypt(self._nonce(), plaintext, ad)
        self.n += 1
        return out

    def decrypt(self, ad: bytes, ciphertext: bytes) -> bytes:
        if self._aead is None:
            return ciphertext
        try:
            out = self._aead.decrypt(self._nonce(), ciphertext, ad)
        except (InvalidTag, ValueError) as exc:
            raise NoiseError("decrypt failed") from exc
        self.n += 1
        return out


class _SymmetricState:
    def __init__(self, protocol_name: bytes) -> None:
        if len(protocol_name) <= HASHLEN:
            self.h = protocol_name.ljust(HASHLEN, b"\x00")
        else:
            self.h = hashlib.sha256(protocol_name).digest()
        self.ck = self.h
        self.cipher = CipherState()

    def mix_key(self, ikm: bytes) -> None:
        self.ck, temp_k = _hkdf(self.ck, ikm, 2)
        self.cipher = CipherState(temp_k)

    def mix_hash(self, data: bytes) -> None:
        self.h = hashlib.sha256(self.h + data).digest()

    def mix_key_and_hash(self, ikm: bytes) -> None:
        self.ck, temp_h, temp_k = _hkdf(self.ck, ikm, 3)
        self.mix_hash(temp_h)
        self.cipher = CipherState(temp_k)

    def encrypt_and_hash(self, plaintext: bytes) -> bytes:
        ciphertext = self.cipher.encrypt(self.h, plaintext)
        self.mix_hash(ciphertext)
        return ciphertext

    def decrypt_and_hash(self, ciphertext: bytes) -> bytes:
        plaintext = self.cipher.decrypt(self.h, ciphertext)
        self.mix_hash(ciphertext)
        return plaintext

    def split(self) -> tuple[CipherState, CipherState]:
        k1, k2 = _hkdf(self.ck, b"", 2)
        return CipherState(k1), CipherState(k2)


# Each message of XX as its tokens. psk3 appends "psk" to the last one.
_XX = (("e",), ("e", "ee", "s", "es"), ("s", "se"))


class HandshakeState:
    """One side of an XX (or XXpsk3) handshake.

    Call :meth:`write_message` and :meth:`read_message` in turn (the initiator writes first).
    After the third message :attr:`complete` is true; :meth:`split` then returns the transport
    ciphers as ``(send, recv)`` for this side.
    """

    def __init__(
        self,
        *,
        initiator: bool,
        static: X25519PrivateKey,
        prologue: bytes = b"",
        psk: bytes | None = None,
        ephemeral: X25519PrivateKey | None = None,
    ) -> None:
        if psk is not None and len(psk) != 32:
            raise ValueError("a Noise psk is 32 bytes")
        self.initiator = initiator
        self._s = static
        self._e = ephemeral  # only tests pass one; a real handshake generates it
        self._psk = psk
        self.rs: bytes | None = None
        self._re: bytes | None = None
        self._sym = _SymmetricState(NAME_XXPSK3 if psk is not None else NAME_XX)
        self._sym.mix_hash(prologue)
        self._index = 0
        self._patterns = [list(m) for m in _XX]
        if psk is not None:
            self._patterns[2].append("psk")

    @property
    def complete(self) -> bool:
        return self._index >= len(self._patterns)

    @property
    def handshake_hash(self) -> bytes:
        """The transcript hash. Unique per handshake; equal on both sides once complete."""
        return self._sym.h

    def _my_turn(self) -> bool:
        return (self._index % 2 == 0) == self.initiator

    def _mix_dh(self, step: str) -> None:
        # "es" is always initiator-ephemeral x responder-static, "se" the reverse.
        if step == "ee":
            local, remote = self._e, self._re
        elif (step == "es") == self.initiator:
            local, remote = self._e, self.rs
        else:
            local, remote = self._s, self._re
        if local is None or remote is None:
            raise NoiseError("handshake out of order")
        self._sym.mix_key(_dh(local, remote))

    def write_message(self, payload: bytes = b"") -> bytes:
        if self.complete or not self._my_turn():
            raise NoiseError("not this side's turn to write")
        out = bytearray()
        for step in self._patterns[self._index]:
            if step == "e":
                if self._e is None:
                    self._e = X25519PrivateKey.generate()
                e_pub = public_bytes(self._e)
                out += e_pub
                self._sym.mix_hash(e_pub)
                if self._psk is not None:
                    self._sym.mix_key(e_pub)
            elif step == "s":
                out += self._sym.encrypt_and_hash(public_bytes(self._s))
            elif step == "psk":
                self._sym.mix_key_and_hash(self._psk or b"")
            else:
                self._mix_dh(step)
        out += self._sym.encrypt_and_hash(payload)
        if len(out) > MAX_MESSAGE:
            raise NoiseError("handshake message too large")
        self._index += 1
        return bytes(out)

    def read_message(self, message: bytes) -> bytes:
        if self.complete or self._my_turn():
            raise NoiseError("not this side's turn to read")
        if len(message) > MAX_MESSAGE:
            raise NoiseError("handshake message too large")
        view = memoryview(message)
        for step in self._patterns[self._index]:
            if step == "e":
                if len(view) < DHLEN:
                    raise NoiseError("short handshake message")
                self._re = bytes(view[:DHLEN])
                view = view[DHLEN:]
                self._sym.mix_hash(self._re)
                if self._psk is not None:
                    self._sym.mix_key(self._re)
            elif step == "s":
                size = DHLEN + (TAGLEN if self._sym.cipher.has_key() else 0)
                if len(view) < size:
                    raise NoiseError("short handshake message")
                self.rs = self._sym.decrypt_and_hash(bytes(view[:size]))
                view = view[size:]
            elif step == "psk":
                self._sym.mix_key_and_hash(self._psk or b"")
            else:
                self._mix_dh(step)
        payload = self._sym.decrypt_and_hash(bytes(view))
        self._index += 1
        return payload

    def split(self) -> tuple[CipherState, CipherState]:
        """``(send, recv)`` transport ciphers for this side. Only after the handshake."""
        if not self.complete:
            raise NoiseError("handshake not complete")
        c1, c2 = self._sym.split()  # c1: initiator -> responder
        return (c1, c2) if self.initiator else (c2, c1)
