"""Web Push — VAPID + aes128gcm, in-repo (#726 Phase 3).

No new dependency. RFC 8291 (message encryption) needs P-256 ECDH, HKDF-SHA256 and
AES-128-GCM; RFC 8292 (VAPID) needs ES256 JWT signing. All four are in the already-pinned
``cryptography`` (Home Free's handshake uses the same primitives), and ``httpx`` already ships
for the POST. Pulling in ``pywebpush`` would add a dependency to re-implement what we can
already do in ~150 lines we can read.

**The payload rule is a security boundary, not a size optimisation.** A push message transits a
third-party push service (FCM, Mozilla autopush). Encryption protects it in transit, but the
operator does not run that infrastructure, and a service that is compromised or subpoenaed
holds whatever we handed it. So a push carries a session title, a project name and a link —
never screen or transcript content. :func:`build_payload` is the only constructor, it takes
exactly those fields, and ``tests/test_webpush.py`` fails if the shape ever widens.

**The private key never leaves the server.** The VAPID keypair is generated on first use and
stored ``0600`` next to ``prefs.json``; only the public key is ever served to a browser. A
subscription's ``endpoint`` is a per-device capability URL — anyone holding it can push to that
device — so it is treated as a secret: never rendered, never logged.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import fcntl
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import utils as asym_utils
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Payload caps. Push services reject large bodies (~4KB is the common ceiling), and our payload
# is three short strings by contract anyway.
TITLE_MAX = 120
BODY_MAX = 240
RECORD_SIZE = 4096
JWT_TTL_S = 12 * 3600  # RFC 8292 caps at 24h; half that leaves room for clock skew.
PUSH_TIMEOUT_S = 10.0

# Test seam, mirroring review._TRANSPORT — CI never touches the network.
_TRANSPORT: httpx.BaseTransport | None = None


class PushError(RuntimeError):
    """A push failed. Never embeds the endpoint URL (it is a per-device capability)."""


class SubscriptionGone(PushError):
    """The push service says this subscription is dead (404/410) — drop it."""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(txt: str) -> bytes:
    pad = "=" * (-len(txt) % 4)
    return base64.urlsafe_b64decode(txt + pad)


def _keys_path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_VAPID_KEYS",
            str(Path.home() / ".config" / "agent-sessions" / "vapid.json"),
        )
    )


def load_or_create_keys(path: Path | None = None) -> tuple[ec.EllipticCurvePrivateKey, str]:
    """The VAPID keypair, minted on first use. Returns ``(private_key, public_key_b64url)``.

    Written ``0600`` at creation rather than chmod-after: a widened-then-narrowed window is
    still a window, and this key authenticates every push we ever send.
    """
    p = path or _keys_path()
    p.parent.mkdir(parents=True, exist_ok=True)

    # Fast path: an existing, valid keypair. Read outside the lock because it is by far the
    # common case and holds no risk — a torn read falls through to the locked path below.
    existing = _read_keys(p)
    if existing is not None:
        return existing

    # Slow path: minting. Serialised across PROCESSES, because this identity must be minted
    # exactly once ever. Without the lock, two first-use requests both generate a keypair,
    # both write the file, and each returns ITS OWN public key — every browser that subscribed
    # against a losing key is permanently unreachable, silently. (Reproduced at 16/16 distinct
    # keys from 16 concurrent callers.)
    lock_path = p.with_name(p.name + ".lock")
    lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        # Re-check under the lock: whoever held it before us may have just minted the pair.
        existing = _read_keys(p)
        if existing is not None:
            return existing
        if p.exists():
            # Present but unreadable. Do NOT silently mint a replacement: rotating this key
            # strands every existing subscription with no error anyone would ever see. A
            # corrupt long-lived identity is an operator problem, and it must say so.
            raise PushError(
                f"the VAPID key file at {p} exists but could not be read; refusing to rotate "
                "the key (that would silently strand every registered device). Move it aside "
                "deliberately to mint a new one."
            )
        return _mint_keys(p)
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _read_keys(p: Path) -> tuple[ec.EllipticCurvePrivateKey, str] | None:
    """The stored pair, or ``None`` when absent/unreadable. Never mints."""
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text())
        priv = serialization.load_pem_private_key(raw["private_pem"].encode(), password=None)
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None
    return (priv, raw["public_key"]) if isinstance(priv, ec.EllipticCurvePrivateKey) else None


def _mint_keys(p: Path) -> tuple[ec.EllipticCurvePrivateKey, str]:
    """Generate and durably store a fresh pair. Caller must hold the mint lock."""
    priv = ec.generate_private_key(ec.SECP256R1())
    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    doc = {
        "private_pem": priv.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        "public_key": _b64e(pub_raw),
    }
    # Atomic: write a private temp, fsync, then rename into place. A direct O_TRUNC write
    # leaves a window where the file exists but is empty or half-written — and a reader that
    # lands there sees "corrupt", which used to mean "mint a new key".
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(doc).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, p)
    return priv, doc["public_key"]


# The push services browsers actually mint subscriptions against. A `PushSubscription`
# endpoint is issued BY the browser's own push service — there is no legitimate case where it
# points somewhere else — so naming them is both precise and complete.
DEFAULT_PUSH_HOSTS: tuple[str, ...] = (
    "android.googleapis.com",  # legacy GCM
    "fcm.googleapis.com",  # Chrome / Chromium
    "updates.push.services.mozilla.com",  # Firefox
    "notify.windows.com",  # Edge / WNS (subdomains)
    "push.apple.com",  # Safari (subdomains, e.g. web.push.apple.com)
)


def allowed_push_hosts() -> tuple[str, ...]:
    """The allowlist, extensible for a self-hosted push service via
    ``AGENT_SESSIONS_PUSH_ALLOWED_HOSTS`` (comma-separated)."""
    extra = os.environ.get("AGENT_SESSIONS_PUSH_ALLOWED_HOSTS", "")
    return DEFAULT_PUSH_HOSTS + tuple(h.strip().lower() for h in extra.split(",") if h.strip())


def assert_allowed_target(endpoint: str) -> None:
    """Raise ``PushError`` unless ``endpoint`` is https at a known push-service host.

    **An allowlist, deliberately, rather than a resolve-and-check.** The endpoint is
    attacker-supplied — a client hands it to the API and the server later POSTs an encrypted
    body plus a VAPID assertion to it — so it is an SSRF sink pointed at whatever this host can
    reach: loopback, a cloud metadata service, the rest of the LAN.

    The obvious control is "resolve the hostname and reject private addresses", and it does not
    work: the resolution you validate is not the resolution the HTTP client then performs.
    An attacker-controlled name answers the check with a public address and the connection
    with `127.0.0.1`. Binding the socket to a pre-validated IP (with an SNI override to keep
    TLS honest) would close that, but it is fiddly and a subtle mistake there is worse than
    the bug.

    A host allowlist has no such window because **no DNS enters the decision**. Nobody can
    re-point `fcm.googleapis.com`, and a browser never issues an endpoint anywhere else.
    """
    parts = urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        raise PushError("endpoint must be an https URL with a hostname")
    for allowed in allowed_push_hosts():
        if host == allowed or host.endswith("." + allowed):
            return
    raise PushError("endpoint is not a known push-service host; refusing to send")


def assert_usable_keys(p256dh: str, auth: str) -> None:
    """Raise ``ValueError`` unless the browser's key material actually decodes.

    Checked at SUBSCRIBE time. Two arbitrary strings satisfy an isinstance check and then blow
    up deep inside the encryption path at send time — outside the transport's exception
    boundary — which used to abort the entire fanout and silence every later device. Failing
    here costs one bad registration instead.
    """
    try:
        pub = _b64d(p256dh)
        secret = _b64d(auth)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"subscription key material is not valid base64url ({e})") from None
    if len(secret) != 16:
        raise ValueError("auth secret must be 16 bytes")
    try:
        # The real check: it has to be a point on P-256, not merely 65 bytes.
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), pub)
    except ValueError as e:
        raise ValueError(f"p256dh is not a valid P-256 public key ({e})") from None


def public_key(path: Path | None = None) -> str:
    """The base64url public key — the ONLY half a browser ever sees."""
    return load_or_create_keys(path)[1]


def _vapid_header(endpoint: str, subject: str, path: Path | None = None) -> dict[str, str]:
    """RFC 8292 ``Authorization: vapid t=<jwt>, k=<pubkey>``.

    The signature must be raw ``r||s`` (64 bytes). ``cryptography`` signs to DER, so it is
    decoded and re-encoded fixed-width — a DER signature here is silently rejected by every
    push service, which is a genuinely miserable thing to debug.
    """
    priv, pub = load_or_create_keys(path)
    parts = urlsplit(endpoint)
    claims = {
        "aud": f"{parts.scheme}://{parts.netloc}",
        "exp": int(time.time()) + JWT_TTL_S,
        "sub": subject,
    }
    header = {"typ": "JWT", "alg": "ES256"}
    seg = lambda obj: _b64e(json.dumps(obj, separators=(",", ":")).encode()).encode()  # noqa: E731
    signing_input = seg(header) + b"." + seg(claims)
    der = priv.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = asym_utils.decode_dss_signature(der)
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    jwt = signing_input.decode() + "." + _b64e(raw_sig)
    return {"Authorization": f"vapid t={jwt}, k={pub}"}


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(payload: bytes, p256dh_b64: str, auth_b64: str) -> bytes:
    """RFC 8291 ``aes128gcm`` body for one subscription.

    Layout: ``salt(16) | rs(4) | idlen(1) | server_public(65) | ciphertext``.
    """
    client_pub_raw = _b64d(p256dh_b64)
    auth_secret = _b64d(auth_b64)
    client_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), client_pub_raw)

    server_priv = ec.generate_private_key(ec.SECP256R1())
    server_pub_raw = server_priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    shared = server_priv.exchange(ec.ECDH(), client_pub)

    # The auth secret salts the first extract; the key_info binds BOTH public keys, so a
    # transcript from one subscription can't be replayed against another.
    key_info = b"WebPush: info\x00" + client_pub_raw + server_pub_raw
    ikm = _hkdf(auth_secret, shared, key_info, 32)

    salt = secrets.token_bytes(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)

    # 0x02 is the last-record padding delimiter (RFC 8188) — we always send exactly one record.
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return (
        salt
        + RECORD_SIZE.to_bytes(4, "big")
        + len(server_pub_raw).to_bytes(1, "big")
        + server_pub_raw
        + ciphertext
    )


def build_payload(*, title: str, project: str, url: str) -> bytes:
    """The ONLY push payload constructor. Three short strings and a link — no screen text, no
    transcript, no rationale.

    Keeping this a named function with a fixed signature is the enforcement point: a caller
    cannot pass "just a bit of the screen" without changing this signature, and the test suite
    asserts the encoded body contains nothing else.
    """
    return json.dumps(
        {
            "title": str(title)[:TITLE_MAX],
            "body": str(project)[:BODY_MAX],
            "url": str(url)[:500],
        },
        separators=(",", ":"),
    ).encode()


def send(subscription: dict, payload: bytes, *, subject: str = "mailto:admin@localhost") -> None:
    """POST one encrypted push. Raises :class:`SubscriptionGone` on 404/410 (caller prunes),
    :class:`PushError` otherwise.

    Errors never embed the endpoint: it is a per-device capability URL, so leaking it into a
    log is handing out the ability to push to that device.
    """
    endpoint = subscription.get("endpoint") or ""
    keys = subscription.get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        raise PushError("subscription is missing endpoint or keys")

    body = encrypt(payload, keys["p256dh"], keys["auth"])
    headers = {
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": "86400",
        "Urgency": "normal",
        **_vapid_header(endpoint, subject),
    }
    # trust_env=False: the VAPID assertion and the endpoint must not be handed to an ambient
    # proxy the operator didn't configure — same posture as review.py.
    client_kwargs: dict = {"timeout": PUSH_TIMEOUT_S, "trust_env": False}
    # Enforced on EVERY path, including the test transport. The check is a static host
    # comparison with no DNS and no I/O, so there is no reason to exempt tests — and a control
    # that tests bypass is a control nobody exercises.
    assert_allowed_target(subscription.get("endpoint", ""))
    if _TRANSPORT is not None:
        client_kwargs["transport"] = _TRANSPORT
    try:
        with httpx.Client(**client_kwargs) as client:
            resp = client.post(endpoint, content=body, headers=headers)
    except httpx.HTTPError as e:
        raise PushError(f"push request failed: {e.__class__.__name__}") from None
    if resp.status_code in (404, 410):
        raise SubscriptionGone("subscription is no longer valid")
    if resp.status_code >= 400:
        raise PushError(f"push service returned {resp.status_code}")


__all__ = [
    "PushError",
    "SubscriptionGone",
    "build_payload",
    "encrypt",
    "public_key",
    "send",
]
