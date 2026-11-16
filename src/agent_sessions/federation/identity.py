"""This instance's federation identity (#1246 §2).

One long-lived X25519 static key, used as the Noise static key of every peer channel. It is
separate from the Home Free relay key (``homefree/agent.py``), so rotating one never breaks the
other. The **peer id** is a fingerprint of the public key; it is what an operator sees and what a
peer pins.

The key file is created once, ``0600``, and never rewritten: a second writer loses the
``O_EXCL`` race and reads the winner's key, so two processes starting together agree.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import re
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

KEY_FILE = "instance.key"
DIR_MODE = 0o700
PEER_ID_RE = re.compile(r"\A[a-z2-7]{32}\Z")
PUBLIC_KEY_LEN = 32


class IdentityError(RuntimeError):
    """The identity key is unreadable or malformed. Federation stays off; nothing is replaced."""


def state_dir() -> Path:
    """Where the key and the store live. Override: ``AGENT_SESSIONS_FEDERATION_DIR``."""
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_FEDERATION_DIR",
            str(Path.home() / ".config" / "agent-sessions" / "federation"),
        )
    )


def peer_id(public_key: bytes) -> str:
    """The fingerprint an operator sees: 20 bytes of SHA-256, base32, lower case."""
    if len(public_key) != PUBLIC_KEY_LEN:
        raise ValueError("a public key is 32 bytes")
    return base64.b32encode(hashlib.sha256(public_key).digest()[:20]).decode("ascii").lower()


def _ensure_dir(directory: Path) -> None:
    directory.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(directory, DIR_MODE)
    except FileExistsError:
        pass


def load_or_create(directory: Path | None = None) -> X25519PrivateKey:
    directory = directory or state_dir()
    path = directory / KEY_FILE
    _ensure_dir(directory)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    except FileExistsError:
        return _read(path)
    key = X25519PrivateKey.generate()
    raw = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        # A half-written key must not be read back as "the" identity by the next start.
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise
    return key


def _read(path: Path) -> X25519PrivateKey:
    try:
        st = os.stat(path)
        raw = path.read_bytes()
    except OSError as exc:
        raise IdentityError(f"federation identity key is unreadable ({exc.strerror})") from exc
    if st.st_mode & 0o077:
        raise IdentityError("federation identity key is readable by others; refusing to use it")
    if len(raw) != 32:
        # A concurrent creator may not have written yet, or the file is damaged. Either way this
        # process must not invent a second identity.
        raise IdentityError("federation identity key is incomplete or damaged")
    return X25519PrivateKey.from_private_bytes(raw)


def public_key(private: X25519PrivateKey) -> bytes:
    return private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
