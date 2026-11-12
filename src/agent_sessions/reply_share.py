"""Reply links (#1956): one finished agent reply of an API session, shared read-only as a SEALED
snapshot.

**A snapshot, never a live read.** The reply is copied when the operator shares it: redacted with
the visitor redactor (``share_api.redactor``, fail closed), its command / tool steps reduced to a
COUNT (never a name, an argument or an output), and nothing else of the session — no prompt, no
other turn, no path, no model. Later edits of the session change nothing a visitor sees.

**Sealed with a key only the link carries.** The snapshot is encrypted (AES-256-GCM) under a key
derived from a random 32-byte ``K`` — and, with a password, from the password too
(PBKDF2-SHA256, 600k). ``K`` goes into the link's URL *fragment* (``#k=…``), which a browser never
sends to a server, and is not stored here. Whoever serves the envelope — this box on any domain,
or (phase 3) the Home Free relay — holds ciphertext only. ``expires_at`` and the id are bound as
AAD, so neither can be swapped onto another envelope.

The format is mirrored by ``web/src/reply/seal.ts`` (WebCrypto). The fixture
``web/src/reply/fixtures/envelopes.json`` is sealed here and opened there, so the two can't drift.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

VERSION = 1
INFO = b"battlelab-reply-v1"
PBKDF2_ITERATIONS = 600_000
#: An envelope is at most this many bytes of JSON: what the relay (phase 2) accepts too.
MAX_ENVELOPE_BYTES = 256 * 1024
#: The expiries the dialog offers, in seconds. ``never`` is a direct install's choice only.
EXPIRY_CHOICES: dict[str, int | None] = {
    "1h": 3600,
    "1d": 86400,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
    "never": None,
}
DEFAULT_EXPIRY = "7d"


class ReplyShareError(Exception):
    """A refused share, its message safe to show the operator."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_id() -> str:
    """128 random bits: an id nobody can guess or enumerate."""
    return b64u(secrets.token_bytes(16))


def expiry(choice: object, now: float | None = None) -> int | None:
    """``expires_at`` (unix seconds) for a dialog choice; refuses anything else."""
    if not isinstance(choice, str) or choice not in EXPIRY_CHOICES:
        raise ReplyShareError(f"expires_in must be one of {', '.join(EXPIRY_CHOICES)}")
    span = EXPIRY_CHOICES[choice]
    return None if span is None else int((now if now is not None else time.time()) + span)


def content(turn: dict, agent: str, redact) -> dict:
    """The one block a visitor reads, from a COMPLETED agent turn of the runtime's snapshot.

    ``redact`` is a ``share_api`` redactor: secrets become ``[secret]`` and a cut edge is trimmed
    and marked, exactly as a public link shows a reply. Tools are counted, never described."""
    if turn.get("state") != "completed":
        raise ReplyShareError("only a finished reply can be shared")
    reply = turn.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        raise ReplyShareError("this turn has no reply to share")
    cut_start = bool(turn.get("reply_truncated") or turn.get("first_message_partial"))
    tools = turn.get("tools") or []
    return {
        "v": VERSION,
        "agent": redact(agent) or "Agent",
        "reply": redact(reply, start=cut_start, end=bool(turn.get("reply_truncated"))),
        "ts": turn.get("reply_ts") or turn.get("ts"),
        "tools_hidden": len(tools),
        "tools_more": bool(turn.get("tools_truncated")),
    }


def _aad(share_id: str, expires_at: int | None) -> bytes:
    return f"{share_id}|{expires_at or 0}".encode()


def _content_key(k: bytes, password: str | None, salt: bytes | None) -> bytes:
    ikm = k
    if password is not None:
        assert salt is not None
        ikm += PBKDF2HMAC(
            algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ITERATIONS
        ).derive(password.encode("utf-8"))
    # An explicit all-zero salt: what RFC 5869 means by "no salt", and what WebCrypto is given.
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=bytes(32), info=INFO).derive(ikm)


def seal(
    body: dict, *, share_id: str, expires_at: int | None, password: str | None = None
) -> tuple[dict, str]:
    """``(envelope, K)``: the envelope a server may hold, and the key only the link carries."""
    if password is not None and not password:
        password = None
    k = secrets.token_bytes(32)
    salt = os.urandom(16) if password is not None else None
    nonce = os.urandom(12)
    plaintext = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ct = AESGCM(_content_key(k, password, salt)).encrypt(
        nonce, plaintext, _aad(share_id, expires_at)
    )
    envelope = {
        "v": VERSION,
        "id": share_id,
        "expires_at": expires_at,
        "password": password is not None,
        "salt": b64u(salt) if salt else None,
        "nonce": b64u(nonce),
        "ct": b64u(ct),
    }
    if len(json.dumps(envelope, separators=(",", ":"))) > MAX_ENVELOPE_BYTES:
        raise ReplyShareError("this reply is too long to share as a link (256 KiB sealed)")
    return envelope, b64u(k)


def open_envelope(envelope: dict, key: str, password: str | None = None) -> dict:
    """The snapshot back — what the visitor's browser does (tests and tools only)."""
    salt = unb64u(envelope["salt"]) if envelope.get("salt") else None
    ck = _content_key(unb64u(key), password if envelope.get("password") else None, salt)
    plaintext = AESGCM(ck).decrypt(
        unb64u(envelope["nonce"]),
        unb64u(envelope["ct"]),
        _aad(envelope["id"], envelope.get("expires_at")),
    )
    return json.loads(plaintext)
