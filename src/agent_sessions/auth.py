"""Session-cookie auth + CSRF + Origin/Referer enforcement.

- Single admin user. Username + password hash come from environment.
- Session cookie is a signed ``itsdangerous`` token containing ``{uid, csrf}``,
  ``HttpOnly``, ``Secure``, ``SameSite=Lax``, idle TTL configurable via env.
- Every state-changing request must carry the CSRF token in the
  ``X-CSRF-Token`` header AND have an ``Origin`` (or ``Referer``) that
  matches the expected host.
- ``/api/auth-check`` returns 204 if the cookie is valid, 401 otherwise.
  Used by nginx ``auth_request`` to gate ``/terminal/*``.

Configuration (all required at process start unless dev_mode=True):

    AGENT_SESSIONS_USERNAME       — operator login (single user)
    AGENT_SESSIONS_PASSWORD_HASH  — pbkdf2_sha256 hash; mint via ``hash_password()``
    AGENT_SESSIONS_SECRET_KEY     — 32+ byte random secret (cookie signing)
    AGENT_SESSIONS_ORIGIN         — e.g. ``https://your-domain.example``
    AGENT_SESSIONS_SESSION_TTL    — seconds, default 86400 (24h idle)
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass

from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

_SESSION_COOKIE = "agent_sessions"
# Short-lived pre-auth cookie for the optional 2FA second step (issue #116). It is a
# *distinct* cookie — its own name, its own itsdangerous salt, and a short TTL — so it can
# never be decoded as (or mistaken for) a full session: it does not satisfy
# require_session / require_csrf_and_origin and grants no access to /api/config or any
# authed route. It only carries enough to gate POST /login/totp.
_PREAUTH_COOKIE = "agent_sessions_preauth"
_PREAUTH_TTL = 300  # 5 minutes to enter the code
_PBKDF2_ITERS = 600_000
_PBKDF2_DKLEN = 32


@dataclass(frozen=True)
class AuthConfig:
    username: str
    password_hash: str  # "pbkdf2_sha256$<iters>$<salt_hex>$<key_hex>"
    secret_key: str
    origin: str
    session_ttl: int = 86400
    # "single-user" (default) — env username + password hash, cookie login, forced
    # change. "none" — no login at all (self-host on a trusted/localhost network):
    # the admin session is auto-established so the SPA + CSRF + Origin still work, but
    # the user is never prompted for credentials. Anything but "none" → "single-user".
    auth_mode: str = "single-user"

    @classmethod
    def from_env(cls) -> AuthConfig:
        raw_mode = os.environ.get("AGENT_SESSIONS_AUTH_MODE")
        auth_mode = "none" if raw_mode == "none" else "single-user"
        try:
            return cls(
                # In `none` mode there is no login, so username/password-hash are not
                # required from the env — default to a fixed admin uid + empty hash.
                username=(
                    os.environ["AGENT_SESSIONS_USERNAME"]
                    if auth_mode != "none"
                    else os.environ.get("AGENT_SESSIONS_USERNAME", "admin")
                ),
                password_hash=(
                    os.environ["AGENT_SESSIONS_PASSWORD_HASH"]
                    if auth_mode != "none"
                    else os.environ.get("AGENT_SESSIONS_PASSWORD_HASH", "")
                ),
                secret_key=os.environ["AGENT_SESSIONS_SECRET_KEY"],
                origin=os.environ["AGENT_SESSIONS_ORIGIN"],
                session_ttl=int(os.environ.get("AGENT_SESSIONS_SESSION_TTL", "86400")),
                auth_mode=auth_mode,
            )
        except KeyError as e:
            raise RuntimeError(f"missing required env var: {e.args[0]}") from None


# ---- password hashing ---------------------------------------------------------


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERS, _PBKDF2_DKLEN)
    return f"pbkdf2_sha256${_PBKDF2_ITERS}${salt.hex()}${key.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iters_s, salt_hex, key_hex = encoded.split("$")
    except ValueError:
        return False
    if scheme != "pbkdf2_sha256":
        return False
    try:
        iters = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(key_hex)
    except ValueError:
        return False
    got = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iters, _PBKDF2_DKLEN)
    return hmac.compare_digest(got, expected)


# ---- cookie + CSRF ------------------------------------------------------------


def _serializer(cfg: AuthConfig) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(cfg.secret_key, salt="agent-sessions:cookie")


def issue_session(cfg: AuthConfig, response: Response) -> str:
    """Mint a fresh signed session cookie. Returns the CSRF token."""
    csrf = secrets.token_urlsafe(32)
    token = _serializer(cfg).dumps({"uid": cfg.username, "csrf": csrf})
    response.set_cookie(
        _SESSION_COOKIE,
        token,
        max_age=cfg.session_ttl,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return csrf


def clear_session(response: Response) -> None:
    response.delete_cookie(_SESSION_COOKIE, path="/")


# ---- pre-auth (2FA second step) cookie ----------------------------------------


def _preauth_serializer(cfg: AuthConfig) -> URLSafeTimedSerializer:
    # Different salt from the session serializer → a pre-auth token can never be loaded as
    # a session cookie (and vice-versa), even though both sign with the same secret key.
    return URLSafeTimedSerializer(cfg.secret_key, salt="agent-sessions:preauth")


def issue_preauth(cfg: AuthConfig, response: Response, uid: str) -> None:
    """Set the short-lived pre-auth cookie after a correct password when 2FA is enabled.

    This is NOT a session — the full session cookie is minted only after the TOTP/recovery
    step succeeds.
    """
    token = _preauth_serializer(cfg).dumps({"stage": "totp", "uid": uid})
    response.set_cookie(
        _PREAUTH_COOKIE,
        token,
        max_age=_PREAUTH_TTL,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def decode_preauth(cfg: AuthConfig, request: Request) -> dict | None:
    """The pre-auth payload if a valid, unexpired pre-auth cookie is present, else None."""
    raw = request.cookies.get(_PREAUTH_COOKIE)
    if not raw:
        return None
    try:
        data = _preauth_serializer(cfg).loads(raw, max_age=_PREAUTH_TTL)
    except (BadSignature, SignatureExpired):
        return None
    return data if isinstance(data, dict) and data.get("stage") == "totp" else None


def clear_preauth(response: Response) -> None:
    response.delete_cookie(_PREAUTH_COOKIE, path="/")


def _decode_cookie(cfg: AuthConfig, request: Request) -> dict | None:
    raw = request.cookies.get(_SESSION_COOKIE)
    if not raw:
        return None
    try:
        return _serializer(cfg).loads(raw, max_age=cfg.session_ttl)
    except (BadSignature, SignatureExpired):
        return None


def session_uid(cfg: AuthConfig, conn) -> str | None:
    """uid for a valid session cookie, else None. Works for Request *or* WebSocket
    (both expose ``.cookies``) — the websocket terminal route reuses this so it
    inherits the exact same session gate as the HTTP routes (issue #49)."""
    data = _decode_cookie(cfg, conn)
    return data.get("uid") if data else None


def current_csrf(cfg: AuthConfig, request: Request) -> str | None:
    data = _decode_cookie(cfg, request)
    return data.get("csrf") if data else None


def origin_matches(cfg: AuthConfig, request: Request) -> bool:
    """Fail-closed Origin/Referer check.

    Uses ``Origin`` when present, else derives the scheme://host[:port] from
    ``Referer``. Returns False when neither header is present or neither
    matches ``cfg.origin`` — callers should reject on False.
    """
    origin = request.headers.get("origin")
    if origin is None:
        ref = request.headers.get("referer", "")
        parts = ref.split("/", 3)
        origin = "/".join(parts[:3]) if len(parts) >= 3 else ""
    return origin == cfg.origin


def enforce_origin(cfg: AuthConfig, request: Request) -> None:
    """Raise 403 unless Origin/Referer matches. Fail-closed (no header == reject)."""
    if not origin_matches(cfg, request):
        raise HTTPException(status_code=403, detail="bad origin")


def require_session(cfg: AuthConfig):
    """FastAPI dependency: 401 unless a valid session cookie is present."""

    async def _dep(request: Request) -> str:
        data = _decode_cookie(cfg, request)
        if not data:
            raise HTTPException(status_code=401, detail="no session")
        return data["uid"]

    return _dep


def require_csrf_and_origin(cfg: AuthConfig):
    """FastAPI dependency for state-changing routes.

    Enforces both:
    - ``X-CSRF-Token`` header equals the CSRF token bound to the session cookie
    - ``Origin`` (or ``Referer`` if Origin absent) matches ``cfg.origin``
    """

    async def _dep(request: Request) -> None:
        data = _decode_cookie(cfg, request)
        if not data:
            raise HTTPException(status_code=401, detail="no session")
        sent_csrf = request.headers.get("x-csrf-token", "")
        if not sent_csrf or not hmac.compare_digest(sent_csrf, data.get("csrf", "")):
            raise HTTPException(status_code=403, detail="bad csrf")
        enforce_origin(cfg, request)

    return _dep


__all__ = [
    "AuthConfig",
    "hash_password",
    "verify_password",
    "issue_session",
    "clear_session",
    "issue_preauth",
    "decode_preauth",
    "clear_preauth",
    "current_csrf",
    "origin_matches",
    "enforce_origin",
    "require_session",
    "require_csrf_and_origin",
]
