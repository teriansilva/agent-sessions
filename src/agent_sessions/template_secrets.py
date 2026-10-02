"""Secret template variables (#1090, Phase 2) — encryption at rest, and the redaction set.

A secret variable is a record in the variables store (``template_vars``) with ``kind: "secret"``
that holds ONLY an AES-GCM envelope ``{kid, nonce, ct}`` — never the plaintext. Its value is
decrypted in exactly two places: the server-side template send (``template_send``), which pastes
it into the agent's PTY, and the redaction set below. It never goes back to the browser.

**The key is its own file, not derived from anything else.** ``template-secrets.key`` beside the
store (override ``AGENT_SESSIONS_TEMPLATE_SECRETS_KEY``): 32 random bytes, 0600, created with
``O_EXCL`` on the first secret write and never overwritten. It is deliberately NOT derived from
``AGENT_SESSIONS_SECRET_KEY`` — that is the cookie-signing key, rotated to log everyone out, and
rotating it must not silently strand every stored secret. A copy of the store without the key
file decrypts nothing; that, and nothing more, is what encryption at rest buys here. The agents
this app launches run as the same user and can read the key file — the docs say so plainly.

**Undecryptable is a state, not an error.** ``kid`` is the first 8 hex of ``sha256(key)``. The
AAD is the variable's scope-qualified identity (``global:<name>`` / ``project:<id>:<name>``, store
v3, #1191) as the envelope's ``aad`` marker says — or its bare name for an envelope marked
``legacy`` (written before scopes; re-encrypted on its next write). A missing key file, a
different key (``kid`` mismatch) or a failed authentication all read as *needs re-entry* —
deterministically, never a crash and never a silent empty string — and a send that needs such a
secret is refused before a byte is written.

**Redaction.** Once a secret is pasted into a session it is in that session's transcript and
screen, which recap, review, Ask and missions read and send to the AI endpoint. ``redact_text`` is
applied by ``review._post_chat`` — the one chat transport — to every non-system message, so it
covers every prompt, current and future. It replaces each secret's raw form and its JSON-escaped
and URL-encoded forms, and matches through ANSI escape sequences. It CANNOT catch a secret the
agent's TUI wrapped across screen lines, split with cursor movement, or re-encoded (base64, hex);
that residual is documented and pinned by a test rather than claimed away. Secrets are at least
``SECRET_MIN`` characters so a short value (``test``) cannot mangle every prompt. A value typed
once at send time joins the set in memory for ``TYPED_TTL_S`` and is lost on restart.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.parse
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger(__name__)

KEY_BYTES = 32
NONCE_BYTES = 12
#: Minimum length of any secret, stored or typed once. Below this, replacing every occurrence in
#: an AI payload would redact ordinary words (a secret ``test`` would mangle every prompt).
SECRET_MIN = 8
#: How long a typed-once value stays in the in-memory redaction set after its send.
TYPED_TTL_S = 24 * 3600
#: The ``kid`` a fingerprint uses when no key file exists yet (every secret typed once). A fixed
#: sentinel, so a missing key never fails a send by itself.
NO_KEY_KID = "nokey"
REDACTED = "[secret]"

_ENVELOPE_KEYS = frozenset({"kid", "nonce", "ct"})
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")

_lock = threading.Lock()
_typed: dict[str, float] = {}
_stored_cache: tuple[object, frozenset[str]] | None = None


class RedactionUnavailable(RuntimeError):
    """The set of values to redact could not be established. The AI transport REFUSES to send
    rather than send unredacted (Hermes on #1105: fail closed, not open)."""


class SecretKeyUnavailable(RuntimeError):
    """The key file exists but cannot be used (wrong size, unreadable). Secret writes refuse
    rather than overwrite it: another key would strand every secret already stored."""


def key_path() -> Path:
    override = os.environ.get("AGENT_SESSIONS_TEMPLATE_SECRETS_KEY")
    if override:
        return Path(override)
    from . import template_vars

    return template_vars.store_path().with_name("template-secrets.key")


def _read_key() -> bytes | None:
    """The key, or ``None`` if there is no key file. A key file of the wrong size raises."""
    path = key_path()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise SecretKeyUnavailable(f"template-secrets.key is unreadable ({e.strerror})") from None
    try:
        data = os.read(fd, KEY_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) != KEY_BYTES:
        raise SecretKeyUnavailable("template-secrets.key is not a 32-byte key")
    return data


def _key_for_write() -> bytes:
    """The key, created if there is none. Never overwrites an existing file.

    Written COMPLETE under a temporary name first, fsynced, then published with ``link()`` — which
    is atomic and fails if the name exists. So ``template-secrets.key`` is either absent or a whole
    32-byte key: a crash can no longer leave a 0-byte key that refuses every later secret write
    (independent review of #1105). A writer that loses the race uses the winner's key, never its
    own.
    """
    key = _read_key()
    if key is not None:
        return key
    path = key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = os.urandom(KEY_BYTES)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(6).hex()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            # os.write may write fewer bytes than asked; a short key must never be published —
            # it would be undecryptable yet refuse every replacement (Hermes on #1105).
            view = memoryview(fresh)
            while view:
                n = os.write(fd, view)
                if n <= 0:
                    raise OSError(errno.EIO, "short write creating template-secrets.key")
                view = view[n:]
            os.fsync(fd)
            if os.fstat(fd).st_size != KEY_BYTES:
                raise OSError(errno.EIO, "template-secrets.key was not written in full")
        finally:
            os.close(fd)
        try:
            os.link(tmp, path)
        except FileExistsError:
            key = _read_key()
            if key is None:
                raise SecretKeyUnavailable(
                    "template-secrets.key vanished while being created"
                ) from None
            return key
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    try:
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError as e:  # pragma: no cover - a filesystem without directory fsync
        if e.errno not in (errno.EINVAL, errno.ENOTSUP):
            raise
    return fresh


def _kid(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:8]


def current_kid() -> str:
    """The key's id, ``NO_KEY_KID`` when there is no key file, ``"bad"`` when it is unusable."""
    try:
        key = _read_key()
    except SecretKeyUnavailable:
        return "bad"
    return NO_KEY_KID if key is None else _kid(key)


def encrypt(name: str, plaintext: str) -> dict:
    """A bare-name envelope: the name is the AAD, so an envelope copied onto another name does not
    decrypt there. The variables store no longer writes these (it writes ``encrypt_scoped``, #1191);
    stores that keep one secret per fixed name (the chat agent's key) still do."""
    key = _key_for_write()
    nonce = os.urandom(NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), name.encode("utf-8"))
    return {
        "kid": _kid(key),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ct": base64.b64encode(ct).decode("ascii"),
    }


def valid_envelope(env: object) -> bool:
    """Shape check for a stored envelope (the store re-validates records on read)."""
    if not isinstance(env, dict) or set(env) != _ENVELOPE_KEYS:
        return False
    if not all(isinstance(env[k], str) for k in _ENVELOPE_KEYS):
        return False
    if not re.fullmatch(r"[0-9a-f]{8}", env["kid"]):
        return False
    try:
        nonce = base64.b64decode(env["nonce"], validate=True)
        ct = base64.b64decode(env["ct"], validate=True)
    except ValueError:
        return False
    return len(nonce) == NONCE_BYTES and len(ct) > 16


def decrypt_checked(name: str, env: dict) -> str | None:
    """``decrypt`` for REDACTION: a key file that EXISTS but cannot be read (EACCES, EIO, wrong
    size) RAISES ``SecretKeyUnavailable`` instead of reading as "needs re-entry" — one transient
    failed read must never become an empty inventory that is then cached under the valid key id
    (Hermes on #1105, round 3). A key that is genuinely absent, a different key (``kid``
    mismatch) or a failed authentication still answer ``None``: those plaintexts are unknowable.
    """
    key = _read_key()  # raises SecretKeyUnavailable on anything but "no such file"
    if key is None or env.get("kid") != _kid(key):
        return None
    try:
        raw = AESGCM(key).decrypt(
            base64.b64decode(env["nonce"]), base64.b64decode(env["ct"]), name.encode("utf-8")
        )
    except (InvalidTag, ValueError, KeyError):
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def decrypt(name: str, env: dict) -> str | None:
    """The plaintext, or ``None`` = needs re-entry (no key, another key, tampered, wrong name)."""
    try:
        key = _read_key()
    except SecretKeyUnavailable:
        return None
    if key is None or env.get("kid") != _kid(key):
        return None
    try:
        raw = AESGCM(key).decrypt(
            base64.b64decode(env["nonce"]), base64.b64decode(env["ct"]), name.encode("utf-8")
        )
    except (InvalidTag, ValueError, KeyError):
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


# ---- scoped envelopes (the variables store, v3 — #1191) ----------------------------------------

#: The envelope's AAD marker. ``scoped``: the AAD is the scope-qualified identity
#: (``global:<name>`` / ``project:<id>:<name>``). ``legacy``: the AAD is the bare name — an
#: envelope written before scopes existed, kept byte for byte until that variable's next write.
#: The marker is the ONLY thing that decides which AAD a decryption uses; nothing is ever tried
#: one way and then the other.
AAD_SCOPED = "scoped"
AAD_LEGACY = "legacy"
AAD_MARKERS = frozenset({AAD_SCOPED, AAD_LEGACY})
SCOPE_GLOBAL = "global"
SCOPE_PROJECT = "project"


def scoped_aad(scope: str, project_id: str | None, name: str) -> str:
    """The identity a scoped envelope is bound to. Neither a project id nor a variable name can
    contain ``:`` (the store validates both), so the encoding is unambiguous."""
    if scope == SCOPE_GLOBAL and project_id is None:
        return f"global:{name}"
    if scope == SCOPE_PROJECT and isinstance(project_id, str) and project_id:
        return f"project:{project_id}:{name}"
    raise ValueError("not a variable scope")


def encrypt_scoped(scope: str, project_id: str | None, name: str, plaintext: str) -> dict:
    """The envelope the variables store writes: bound to the scope-qualified identity, so one moved
    to another project, to the global scope or to another name does not decrypt there."""
    aad = scoped_aad(scope, project_id, name)
    key = _key_for_write()
    nonce = os.urandom(NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), aad.encode("utf-8"))
    return {
        "kid": _kid(key),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ct": base64.b64encode(ct).decode("ascii"),
        "aad": AAD_SCOPED,
    }


def valid_store_envelope(env: object) -> bool:
    """Shape check for a variables-store v3 envelope: the bare shape plus an ``aad`` marker."""
    if not isinstance(env, dict):
        return False
    marker = env.get("aad")
    # TYPE first: `[] in frozenset` raises TypeError (unhashable) instead of answering "no".
    if not isinstance(marker, str) or marker not in AAD_MARKERS:
        return False
    return valid_envelope({k: v for k, v in env.items() if k != "aad"})


def _record_aad(scope: str, project_id: str | None, name: str, env: dict) -> bytes | None:
    """The AAD the envelope's OWN marker names, or None when the marker does not fit the record.
    A ``legacy`` envelope is only ever a global one: legacy envelopes predate project scopes, so
    one found on a project record was moved there and is never decrypted."""
    marker = env.get("aad")
    if not isinstance(marker, str):
        return None
    try:
        if marker == AAD_SCOPED:
            return scoped_aad(scope, project_id, name).encode("utf-8")
        if marker == AAD_LEGACY and scope == SCOPE_GLOBAL and project_id is None:
            return name.encode("utf-8")
    except ValueError:
        return None
    return None


def _open(key: bytes, env: dict, aad: bytes) -> str | None:
    if env.get("kid") != _kid(key):
        return None
    try:
        raw = AESGCM(key).decrypt(base64.b64decode(env["nonce"]), base64.b64decode(env["ct"]), aad)
    except (InvalidTag, ValueError, KeyError):
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def decrypt_record(scope: str, project_id: str | None, name: str, env: dict) -> str | None:
    """A store record's plaintext, or ``None`` = needs re-entry. The AAD follows the envelope's
    marker and nothing else."""
    aad = _record_aad(scope, project_id, name, env)
    if aad is None:
        return None
    try:
        key = _read_key()
    except SecretKeyUnavailable:
        return None
    return None if key is None else _open(key, env, aad)


def decrypt_record_checked(scope: str, project_id: str | None, name: str, env: dict) -> str | None:
    """``decrypt_record`` for REDACTION: a key file that exists but cannot be read RAISES
    ``SecretKeyUnavailable``, exactly like ``decrypt_checked``."""
    key = _read_key()  # raises SecretKeyUnavailable on anything but "no such file"
    aad = _record_aad(scope, project_id, name, env)
    if key is None or aad is None:
        return None
    return _open(key, env, aad)


# ---- redaction ---------------------------------------------------------------------------------


def remember(values: list[str]) -> None:
    """Values a send is about to type into a session — stored AND typed-once. Held for the TTL
    whatever happens to the store or the key afterwards (Hermes on #1105: a stored secret that
    was delivered must stay redacted even if the key disappears before the next AI call)."""
    register_typed(values)


def retire(values: list[str]) -> None:
    """A stored value is about to be replaced or deleted: keep redacting it for the TTL. Called by
    the store BEFORE it writes, so this holds even if the redaction set was never computed in this
    process. Same bounded, in-memory lifetime as a typed-once value."""
    register_typed(values)


def register_typed(values: list[str]) -> None:
    """Add values typed once at send time to the redaction set, in memory, for ``TYPED_TTL_S``."""
    until = time.time() + TYPED_TTL_S
    with _lock:
        for v in values:
            if len(v) >= SECRET_MIN:
                _typed[v] = until


def _stored_values() -> frozenset[str]:
    """Every decryptable stored secret, cached against the store file and the key id.

    FAIL-CLOSED (Hermes on #1105). A store that cannot be read COMPLETELY raises
    ``RedactionUnavailable`` — warm cache or cold — and the transport refuses to send: values known
    from an earlier read cannot vouch for a secret stored since. Nothing from a failed read is
    cached, so recovery is picked up on the next call. Only a complete read is ever cached.
    """
    global _stored_cache
    from . import template_vars

    path = template_vars.store_path()
    try:
        try:
            st = path.stat()
            sig: object = (st.st_mtime_ns, st.st_size, st.st_ino, current_kid())
        except FileNotFoundError:
            sig = ("absent", current_kid())
        with _lock:
            if _stored_cache is not None and _stored_cache[0] == sig:
                return _stored_cache[1]
        values = frozenset(v for v in template_vars.secret_values_checked() if len(v) >= SECRET_MIN)
    except Exception as e:  # noqa: BLE001 — any failure to establish the set
        # REFUSE, warm cache or cold (Hermes on #1105, round 2). The known set cannot vouch for a
        # secret stored since it was computed, so "redact what we knew" is still fail-open. The
        # cache is left exactly as it was: nothing from the failed read is kept.
        log.warning(
            "template secrets: the variables store could not be read (%s)", type(e).__name__
        )
        raise RedactionUnavailable(
            f"the secret store could not be read ({type(e).__name__})"
        ) from None
    with _lock:
        # A value that LEAVES the stored set — replaced, deleted, or no longer decryptable — is
        # still in every transcript it was pasted into: it stays redacted for the TTL.
        if _stored_cache is not None:
            until = time.time() + TYPED_TTL_S
            for gone in _stored_cache[1] - values:
                _typed[gone] = max(_typed.get(gone, 0.0), until)
        _stored_cache = (sig, values)
    return values


def redaction_values() -> list[str]:
    """Every value to redact, longest first (so a secret containing another is replaced whole)."""
    # Stored FIRST: recomputing it moves any value that has left the store into the typed set,
    # and the snapshot below must include it on this very call. It RAISES
    # `RedactionUnavailable` when the set cannot be established — callers refuse to send.
    stored = _stored_values()
    now = time.time()
    with _lock:
        for v in [v for v, until in _typed.items() if until <= now]:
            del _typed[v]
        typed = set(_typed)
    return sorted(stored | typed, key=len, reverse=True)


def _variants(secret: str) -> list[str]:
    out = [
        secret,
        json.dumps(secret)[1:-1],
        urllib.parse.quote(secret, safe=""),
        urllib.parse.quote_plus(secret, safe=""),
    ]
    seen: list[str] = []
    for v in out:
        if v and v not in seen:
            seen.append(v)
    return seen


def redact_text(text: str, secrets: list[str] | None = None) -> str:
    """``text`` with every secret (raw, JSON-escaped, URL-encoded) replaced by ``[secret]``.

    A secret interleaved with ANSI escape sequences (a coloured or cursor-positioned echo) is
    matched on the text with the sequences removed — and then only that stripped text is
    returned, since the escapes carry nothing an AI reader needs. Text containing no secret is
    returned unchanged, byte for byte.
    """
    if secrets is None:
        secrets = redaction_values()
    if not secrets or not text:
        return text
    variants = [v for s in secrets for v in _variants(s)]

    def replace(t: str) -> str:
        for v in variants:
            if v in t:
                t = t.replace(v, REDACTED)
        return t

    text = replace(text)
    # Whatever is left may still hide a secret behind escape sequences (a coloured or
    # cursor-positioned echo). Checked AFTER the plain pass, so a plain copy elsewhere in the same
    # text cannot mask an interleaved one.
    if "\x1b" in text:
        stripped = _ANSI_RE.sub("", text)
        if any(v in stripped for v in variants):
            text = replace(stripped)
    return text


def redact_messages(messages: list) -> list:
    """A copy of chat ``messages`` with every NON-system message's text redacted.

    System messages are left exactly as they are: ``review._assert_registered_system_prompts``
    compares them to the prompt registry byte for byte, and they are the operator's own prompt
    text, never session content.
    """
    secrets = redaction_values()
    if not secrets:
        return messages
    out = []
    for m in messages:
        if not isinstance(m, dict) or m.get("role") == "system":
            out.append(m)
            continue
        content = m.get("content")
        if isinstance(content, str):
            m = {**m, "content": redact_text(content, secrets)}
        elif isinstance(content, list):
            m = {
                **m,
                "content": [
                    {**p, "text": redact_text(p["text"], secrets)}
                    if isinstance(p, dict) and isinstance(p.get("text"), str)
                    else p
                    for p in content
                ],
            }
        out.append(m)
    return out


def _reset_for_tests() -> None:
    global _stored_cache
    with _lock:
        _typed.clear()
        _stored_cache = None
