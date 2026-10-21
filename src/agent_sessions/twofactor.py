"""Optional TOTP 2FA store + verification (issue #116).

Single-admin app, so there is exactly one record. The TOTP secret, the recovery-code
hashes, and the anti-replay cursor live in a dedicated ``0600`` secrets file (default
``<env-dir>/2fa.json``, override ``AGENT_SESSIONS_2FA_FILE``) — **not** in ``prefs.json``
(non-secret UI state) and **not** in the metadata sidecar.

Security properties (the point of this module):

- **Secret entropy ≥ 160 bits**: ``pyotp.random_base32()`` returns 32 base32 chars.
- **Two-phase enrollment**: :func:`begin_enrollment` writes a *pending* (not enabled)
  secret; :func:`confirm_enrollment` only flips ``enabled`` after a valid code, so a
  misconfigured authenticator can never lock the admin out. A begin while already enabled
  does not disturb the active secret until confirmed.
- **Recovery codes** are shown exactly once (returned by begin/regenerate), stored only as
  PBKDF2 hashes (the same :func:`auth.hash_password` used for the password), and
  **consumed atomically** — the matched hash is removed under an exclusive lock on use.
- **Anti-replay survives a restart**: the last consumed TOTP step (the 30s timecode) is
  persisted; a login verify rejects any step ≤ the stored cursor, so bouncing the service
  cannot reopen a just-used code window. That rejection reports :data:`TOTP_REPLAYED`, so
  the login page can tell a spent code from a wrong one (#814) — with two tabs open, the
  operator's *correct* code is routinely the one that gets refused.
- **Serialized writes**: every read-modify-write (consume a recovery code, advance the
  replay cursor, confirm enrollment) takes an exclusive ``flock`` on a sidecar lock file,
  then writes the data atomically (``0600`` temp file + ``os.replace`` — the same shape as
  :mod:`envfile`). The lock file has a stable inode so concurrent writers serialize even
  though the data file is replaced.

The secret is **not** encrypted at rest in v1 — the ``0600`` perms are the at-rest control
(``SECRET_KEY``-wrapping is a possible follow-up). The secret/recovery codes are never
logged and never returned by ``/api/config``.
"""

from __future__ import annotations

import contextlib
import fcntl
import hmac
import json
import os
import secrets
import tempfile
import time
from pathlib import Path

import pyotp

from . import discover
from .auth import hash_password, verify_password

# The label shown in authenticator apps. Renamed TermRoyale→BattleLab (#211 Phase 3); this
# only affects NEW enrollments — existing entries keep their stored label and their secret is
# untouched, so codes keep working (release-noted).
ISSUER = "BattleLab"
RECOVERY_COUNT = 10
# Entropy per recovery code, and the display grouping. Generation (_gen_recovery_codes),
# formatting (_format_recovery) and the shape test (_looks_like_recovery) all derive from
# these two, so the minted format and the accepted format cannot drift apart (#815).
RECOVERY_BYTES = 6  # → 12 hex chars, rendered xxxx-xxxx-xxxx
RECOVERY_GROUP = 4  # hex chars per hyphen-separated block
STEP_SECONDS = 30
# ±1 step (30s) skew tolerance, per RFC 6238 guidance.
WINDOW = 1

# Outcomes of a login TOTP check. A *replayed* code is valid-but-spent, which the login page
# has to report differently from a wrong one (#814).
TOTP_OK = "ok"
TOTP_REPLAYED = "replayed"
TOTP_INVALID = "invalid"


class TwoFactorStoreError(Exception):
    """The 2FA store exists but couldn't be read/parsed.

    A *missing* file means "not enrolled" (fine). A *present but unreadable/corrupt* file
    must NOT silently downgrade to "disabled" — that would let a truncated/tampered file
    bypass the second factor. Auth decisions treat this as fail-closed (see is_enabled).
    """


def default_path() -> Path:
    """The 2FA secrets file path: ``AGENT_SESSIONS_2FA_FILE`` override, else ``2fa.json``
    next to the env/credential store (``discover.default_env_path()``'s directory)."""
    override = os.environ.get("AGENT_SESSIONS_2FA_FILE")
    if override:
        return Path(override)
    return discover.default_env_path().parent / "2fa.json"


def _blank() -> dict:
    return {
        "version": 1,
        "enabled": False,
        "secret": None,  # active TOTP secret (base32) once confirmed
        "pending_secret": None,  # secret mid-enrollment, before confirm
        "recovery": [],  # PBKDF2 hashes of unused recovery codes
        "pending_recovery": [],  # recovery hashes staged during enrollment
        "last_step": 0,  # anti-replay cursor: last consumed TOTP step
    }


def _load(path: Path) -> dict:
    """Read the record. A missing file → a blank (disabled) record. A present-but-
    unreadable/corrupt file → :class:`TwoFactorStoreError` (callers fail closed)."""
    try:
        with path.open() as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return _blank()
    except (OSError, json.JSONDecodeError) as e:
        raise TwoFactorStoreError(f"unreadable 2FA store at {path}: {e}") from e
    if not isinstance(raw, dict):
        raise TwoFactorStoreError(f"malformed 2FA store at {path} (not a JSON object)")
    data = _blank()
    data.update({k: raw[k] for k in data if k in raw})
    return data


def _atomic_write(path: Path, data: dict) -> None:
    """Write ``data`` as JSON, mode ``0600``, atomically (temp file + ``os.replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as fh:  # mkstemp already created it 0600
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)  # atomic
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _with_lock(path: Path, fn):
    """Serialize a read-modify-write under an exclusive flock on ``<path>.lock``.

    ``fn(data) -> (result, write?)``: mutate ``data`` in place and return a result plus a
    flag for whether to persist. The lock file's inode is stable across the atomic replace
    of the data file, so concurrent second-factor attempts can't race the cursor / codes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = _load(path)
        result, write = fn(data)
        if write:
            _atomic_write(path, data)
        return result
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ---- recovery codes -----------------------------------------------------------


def _normalize_recovery(code: str) -> str:
    """Case/format-insensitive form for comparison: lowercase, hyphens/space stripped."""
    return "".join((code or "").split()).replace("-", "").lower()


def _looks_like_recovery(candidate: str) -> bool:
    """Whether a *normalized* code could be one this module minted: exactly the hex string
    :func:`_gen_recovery_codes` produces.

    A cheap shape test, so a 6-digit TOTP never reaches the PBKDF2 comparison loop (#815) —
    a miss there costs ``RECOVERY_COUNT`` full key derivations (measured at 5.25s on the
    production host), which is what made the login page look hung after a wrong code. The
    test reveals nothing: the submitter already knows which shape they sent.
    """
    return len(candidate) == RECOVERY_BYTES * 2 and all(c in "0123456789abcdef" for c in candidate)


def _format_recovery(raw: str) -> str:
    """Group a raw hex token into ``RECOVERY_GROUP``-sized blocks: ``xxxx-xxxx-xxxx``.

    Derived from the token it is handed rather than fixed offsets, so the display format
    follows :data:`RECOVERY_BYTES` instead of silently truncating a longer token — which
    would mint codes :func:`_looks_like_recovery` then rejects.
    """
    return "-".join(raw[i : i + RECOVERY_GROUP] for i in range(0, len(raw), RECOVERY_GROUP))


def _gen_recovery_codes() -> tuple[list[str], list[str]]:
    """Return (plaintext codes shown once, their PBKDF2 hashes for storage)."""
    plain = [_format_recovery(secrets.token_hex(RECOVERY_BYTES)) for _ in range(RECOVERY_COUNT)]
    hashes = [hash_password(_normalize_recovery(c)) for c in plain]
    return plain, hashes


# ---- TOTP verification --------------------------------------------------------


def _match_step(secret: str, code: str, now: int | None = None) -> int | None:
    """Return the TOTP step a ``code`` matches within ±WINDOW, else None.

    Constant-time compare; iterates the whole window without an early return so the timing
    doesn't reveal which step matched.
    """
    code = (code or "").strip()
    if not (secret and code.isdigit()):
        return None
    now = int(time.time()) if now is None else now
    totp = pyotp.TOTP(secret)
    current = now // STEP_SECONDS
    matched: int | None = None
    for step in range(current - WINDOW, current + WINDOW + 1):
        candidate = totp.at(step * STEP_SECONDS)
        if hmac.compare_digest(candidate, code):
            matched = step
    return matched


# ---- public API ---------------------------------------------------------------


def is_enabled(path: Path | None = None) -> bool:
    """Whether a second factor is required. **Fail-closed**: a present-but-corrupt store
    is treated as enabled, so a tampered/truncated file demands 2FA at login rather than
    bypassing it (the corrupt store then can't verify any code → login is locked until the
    admin runs ``clear-2fa`` or disables with the password). A *missing* file → disabled."""
    try:
        data = _load(path or default_path())
    except TwoFactorStoreError:
        return True
    return bool(data.get("enabled") and data.get("secret"))


def begin_enrollment(account: str, path: Path | None = None) -> dict:
    """Start (or restart) enrollment: stage a fresh secret + recovery codes (pending, not
    enabled) and return the secret, the ``otpauth://`` URI, and the one-time recovery codes.

    Does not touch the active secret until :func:`confirm_enrollment` succeeds.
    """
    path = path or default_path()
    secret = pyotp.random_base32()  # 32 base32 chars → 160 bits
    plain, hashes = _gen_recovery_codes()

    def _mut(data: dict):
        data["pending_secret"] = secret
        data["pending_recovery"] = hashes
        return None, True

    _with_lock(path, _mut)
    uri = pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=ISSUER)
    return {"secret": secret, "otpauth_uri": uri, "recovery_codes": plain}


def confirm_enrollment(code: str, path: Path | None = None, now: int | None = None) -> bool:
    """Verify ``code`` against the pending secret; on success activate 2FA. 2FA is never
    enabled without a confirmed code. Seeds the replay cursor with the confirm step so the
    just-entered code can't be replayed at login."""
    path = path or default_path()

    def _mut(data: dict):
        pending = data.get("pending_secret")
        if not pending:
            return False, False
        step = _match_step(pending, code, now=now)
        if step is None:
            return False, False
        data["secret"] = pending
        data["recovery"] = list(data.get("pending_recovery") or [])
        data["pending_secret"] = None
        data["pending_recovery"] = []
        data["enabled"] = True
        data["last_step"] = step
        return True, True

    return _with_lock(path, _mut)


def check_totp(code: str, path: Path | None = None, now: int | None = None) -> bool:
    """Non-mutating TOTP check against the active secret (no replay advance). Used as a
    *fresh proof* for disable / regenerate, where consuming a code or advancing the cursor
    would be surprising. A corrupt store can't prove anything → False."""
    try:
        data = _load(path or default_path())
    except TwoFactorStoreError:
        return False
    if not (data.get("enabled") and data.get("secret")):
        return False
    return _match_step(data["secret"], code, now=now) is not None


def login_totp_outcome(code: str, path: Path | None = None, now: int | None = None) -> str:
    """Verify a login TOTP, advancing the persisted replay cursor on success.

    Returns :data:`TOTP_OK` (cursor advanced), :data:`TOTP_REPLAYED` (the code is valid but
    its step was already consumed — the caller must say "already used", not "invalid"), or
    :data:`TOTP_INVALID`. Any step ≤ the stored cursor is refused; that protection survives
    a restart because the cursor is on disk.

    :data:`TOTP_REPLAYED` means *cursor-rejected* (``step <= last_step``), which covers both
    resubmitting the code just consumed and submitting the older of the two codes still
    inside the ±1 window after the newer one was accepted.

    Named for its outcome rather than ``verify_*`` deliberately: it returns a string, so a
    stale ``if verify_...(code):`` call site would read as *truthy* for a failed check — an
    auth-bypass shape. Renaming makes any missed caller raise instead of silently passing.
    """
    path = path or default_path()

    def _mut(data: dict):
        if not (data.get("enabled") and data.get("secret")):
            return TOTP_INVALID, False
        step = _match_step(data["secret"], code, now=now)
        if step is None:
            return TOTP_INVALID, False
        if step <= int(data.get("last_step") or 0):
            return TOTP_REPLAYED, False
        data["last_step"] = step
        return TOTP_OK, True

    try:
        return _with_lock(path, _mut)
    except TwoFactorStoreError:
        return TOTP_INVALID  # corrupt store can't verify → fail closed (login stays blocked)


def verify_recovery_for_login(code: str, path: Path | None = None) -> bool:
    """Consume a one-time recovery code: if ``code`` matches a stored hash, remove that hash
    (atomically, under the lock) and return True. Each code works at most once."""
    path = path or default_path()
    candidate = _normalize_recovery(code)
    if not _looks_like_recovery(candidate):
        return False  # not one of ours — don't pay RECOVERY_COUNT key derivations (#815)

    def _mut(data: dict):
        if not data.get("enabled"):
            return False, False
        hashes = list(data.get("recovery") or [])
        for i, h in enumerate(hashes):
            if verify_password(candidate, h):
                del hashes[i]
                data["recovery"] = hashes
                return True, True
        return False, False

    try:
        return _with_lock(path, _mut)
    except TwoFactorStoreError:
        return False  # corrupt store can't verify → fail closed


def regenerate_recovery(path: Path | None = None) -> list[str] | None:
    """Replace the recovery codes (only when 2FA is enabled); return the new plaintext codes
    once, or None if 2FA isn't enabled."""
    path = path or default_path()
    plain, hashes = _gen_recovery_codes()

    def _mut(data: dict):
        if not (data.get("enabled") and data.get("secret")):
            return None, False
        data["recovery"] = hashes
        return plain, True

    try:
        return _with_lock(path, _mut)
    except TwoFactorStoreError:
        return None


def recovery_remaining(path: Path | None = None) -> int:
    """How many unused recovery codes remain (for the Settings UI). 0 on a corrupt store."""
    try:
        data = _load(path or default_path())
    except TwoFactorStoreError:
        return 0
    return len(data.get("recovery") or [])


def disable(path: Path | None = None) -> None:
    """Turn 2FA off and clear all secrets/codes (back to a blank, disabled record).

    Writes a blank record under the lock **without** reading the old one, so it always
    succeeds — even on a corrupt/unreadable store (disabling with the password is a valid
    recovery path from a tampered file)."""
    path = path or default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _atomic_write(path, _blank())
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def clear(path: Path | None = None) -> bool:
    """Host escape hatch (``agent-sessions clear-2fa``): remove the secrets file entirely.

    Returns True if a file was removed. Also removes the sidecar lock file.
    """
    path = path or default_path()
    removed = False
    with contextlib.suppress(OSError):
        path.unlink()
        removed = True
    with contextlib.suppress(OSError):
        path.with_name(path.name + ".lock").unlink()
    return removed
