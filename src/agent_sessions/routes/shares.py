"""Public share links (#1404 P1): the operator's create/list/revoke routes, the public viewer
entry ``/p/<token>`` and the unlock that trades a token (+ password) for a share cookie.

Two cookies, never interchangeable:

* the operator's admin cookie (``auth.py``) — read only by the operator routes here, exactly as
  everywhere else;
* the **share cookie** — its own name and itsdangerous salt, ``Path=/api/public`` (one prefix
  for HTTP and the terminal WebSocket, so it is never sent to the operator API),
  ``SameSite=Strict``, carrying ``{share_id, csrf}``. :func:`require_share` is the only reader, and
  it re-reads the share row on every request, so a revoked or expired link stops at once.

Everything public answers 404 in ``AUTH_MODE=none``: that mode hands an admin cookie to any
request that has none, so a public visitor would be the operator. The rules are pinned in
``docs/invariants/public-shares.md``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import math
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .. import auth, engines, metadata, shares
from ..auth import AuthConfig, enforce_origin

SHARE_COOKIE = "agent_sessions_share"
COOKIE_PATH = "/api/public"
# A visitor re-unlocks at least this often, even on a link that never expires: the cookie is
# a convenience over the token, not a second long-lived credential.
COOKIE_MAX_TTL = 12 * 3600
NONE_MODE_REASON = (
    "Public links are off while BattleLab runs without a login (AUTH_MODE=none), which is also how"
    " Home Free stream mode runs: anyone reaching it would be signed in as you."
)
# The public prefixes: the headers middleware in `main` stamps `no-referrer` + `no-store` on
# both, and the SPA catch-all / service worker leave both alone.
PUBLIC_PREFIXES = ("/p/", "/api/public/")
NAME_RULE = (
    f"a name is 1–{shares.MAX_VISITOR_NAME} characters on one line, letters, numbers, spaces and"
    " punctuation only"
)
PLACEHOLDER_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>Shared BattleLab session</title>
<style>
body{margin:0;font:15px/1.5 system-ui,sans-serif;background:#0d0e10;color:#e8e9ec;
display:flex;min-height:100vh;align-items:center;justify-content:center;padding:24px}
main{max-width:420px;text-align:center}
h1{font-size:17px;margin:0 0 8px}p{color:#a7abb3;margin:0}
</style></head>
<body><main><h1>Shared BattleLab session</h1>
<p>This BattleLab can't show shared sessions yet. Ask the person who shared the link to update
BattleLab, then open it again.</p></main></body></html>
"""
# The runtimes a link can name. A session of any other runtime is refused at create.
_KIND_BY_RUNTIME = {"api": "api", "pty": "terminal"}


def _serializer(cfg: AuthConfig) -> URLSafeTimedSerializer:
    # Its own salt: a share cookie can never load as an admin session, nor the reverse.
    return URLSafeTimedSerializer(cfg.secret_key, salt="agent-sessions:share")


def issue_share_cookie(
    cfg: AuthConfig,
    response: Response,
    share: shares.Share,
    csrf: str,
    name: str | None = None,
    *,
    issued_at: int | None = None,
) -> None:
    """Set the share cookie carrying ``{share_id, csrf, name, iat}``.

    ``iat`` is when the visitor last presented the token (and password). A re-issue — setting a
    name — keeps the original ``iat``, so the cookie can never be stretched past
    ``COOKIE_MAX_TTL`` from the last real unlock (#1414 independent review); it also never
    outlives the link's own expiry."""
    now = int(time.time())
    iat = issued_at if issued_at is not None else now
    payload: dict = {"sid": share.share_id, "csrf": csrf, "iat": iat}
    if name:
        payload["name"] = name
    token = _serializer(cfg).dumps(payload)
    ttl = COOKIE_MAX_TTL - (now - iat)
    if share.expires_at is not None:
        ttl = min(ttl, int(share.expires_at - now))
    response.set_cookie(
        SHARE_COOKIE,
        token,
        max_age=max(1, ttl),
        httponly=True,
        secure=True,
        samesite="strict",
        path=COOKIE_PATH,
    )


def decode_share_cookie(cfg: AuthConfig, conn) -> dict | None:
    """``{sid, csrf}`` from a valid share cookie on a Request or WebSocket, else None. Only the
    signature and age are checked here — the caller must still ask the store whether the share
    is live."""
    raw = conn.cookies.get(SHARE_COOKIE)
    if not raw:
        return None
    try:
        data = _serializer(cfg).loads(raw, max_age=COOKIE_MAX_TTL)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("sid"), str):
        return None
    iat = data.get("iat")
    # The signature's own age resets on every re-sign; `iat` is the last real unlock.
    if (
        not isinstance(iat, int)
        or isinstance(iat, bool)
        or not 0 <= time.time() - iat < COOKIE_MAX_TTL
    ):
        return None
    return data


@dataclass(frozen=True)
class Visitor:
    """Who is asking, as far as a public route may know: the live share, and the name the visitor
    gave (None until they give one). The name is self-chosen — attribution, not identity."""

    share: shares.Share
    name: str | None


def visitor_view(share: shares.Share, name: str | None = None) -> dict:
    """What a visitor learns about the link. Not the session key, the share id or open counts.
    ``name_required`` tells the viewer to ask for a name before it enables the composer."""
    return {
        "kind": share.kind,
        "mode": share.mode,
        "title": share.title,
        "expires_at": share.expires_at,
        "name": name,
        "name_required": share.mode == "send" and not name,
    }


def _check_csrf(cfg: AuthConfig, request: Request, data: dict) -> None:
    enforce_origin(cfg, request)
    sent = request.headers.get("x-csrf-token", "")
    if not sent or not secrets.compare_digest(sent, str(data.get("csrf", ""))):
        raise HTTPException(status_code=403, detail="bad csrf")


def require_share(cfg: AuthConfig, store: shares.ShareStore, *, send: bool = False):
    """FastAPI dependency for every public data route (P2/P3 build on it).

    * ``AUTH_MODE=none`` → 404 (public sharing is off).
    * No / bad share cookie → 401 (the viewer unlocks again).
    * Cookie for a revoked, expired or unknown share → 404 "link unavailable".
    * ``send=True``: the share must be a ``send`` link (else 403), the request must carry the
      cookie's CSRF token in ``X-CSRF-Token`` and a matching Origin, and the visitor must have
      given a name (else 403 "enter your name first") — nobody types into a session anonymously.

    Returns a :class:`Visitor`. The admin cookie is never consulted, so being signed in as the
    operator grants nothing here. A send route must still re-check under
    :meth:`ShareStore.fence` at its mutation seam."""

    async def _dep(request: Request) -> Visitor:
        if cfg.auth_mode == "none":
            raise HTTPException(status_code=404, detail="not found")
        data = decode_share_cookie(cfg, request)
        if data is None:
            raise HTTPException(status_code=401, detail="unlock this link first")
        share = await asyncio.to_thread(store.active, data["sid"])
        if share is None:
            raise HTTPException(status_code=404, detail="link unavailable")
        # Re-cleaned on every read: the cookie is signed, but the rule may have tightened since.
        name = shares.clean_visitor_name(data.get("name")) if data.get("name") else None
        if send:
            if share.mode != "send":
                raise HTTPException(status_code=403, detail="this link is view only")
            _check_csrf(cfg, request, data)
            if not name:
                raise HTTPException(status_code=403, detail="enter your name first")
        return Visitor(share=share, name=name)

    return _dep


def _client_ip(request) -> str:
    """The client address the unlock throttle buckets on.

    The "peer" here is what uvicorn reports AFTER its own proxy-header handling: with
    ``--proxy-headers`` (uvicorn's default, trusting ``--forwarded-allow-ips``) it has already
    replaced the socket peer with the rightmost UNTRUSTED ``X-Forwarded-For`` hop, which behind a
    proxy using ``$proxy_add_x_forwarded_for`` is the real client. This code never parses
    ``X-Forwarded-For`` itself (its leftmost entries are the client's own claim). On top of that,
    ``X-Real-IP`` is trusted only when the peer is still loopback — the local reverse proxy, which
    sets it to ``$remote_addr`` (overwriting any inbound value; see ``deploy/nginx.example.conf``).
    A proxy that sets neither puts every visitor in one bucket, which only tightens the per-client
    budget; the per-share budget (20) bounds guessing regardless of bucketing."""
    peer = request.client.host if request.client else ""
    try:
        loopback = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        loopback = False
    if loopback:
        real = request.headers.get("x-real-ip", "").strip()
        try:
            return str(ipaddress.ip_address(real))
        except ValueError:
            pass
    return peer


def _session_state(prov, native: str) -> str:
    """``live`` / ``archived`` / ``unknown`` / ``unreadable`` for a session a link would name.

    Stricter than the archive path's own precedence, because this is an authorization boundary
    (Hermes on #1414): the PROVIDER'S fresh scan must affirmatively list the session — a sidecar
    row alone proves nothing (the history may be gone) — and it is ``archived`` if EITHER the
    provider or the sidecar says so. A stale ``archived: false`` override cannot make a
    provider-archived session shareable (e.g. a move that landed while its stamp failed). An
    unreadable sidecar, or a scan that raises, decides nothing and refuses."""
    try:
        rows = [s for s in prov.scan() if getattr(s, "uuid", None) == native]
    except Exception:  # noqa: BLE001 — an unreadable engine store must not grant anything
        return "unreadable"
    if not rows:
        return "unknown"
    if any(getattr(s, "archived", False) for s in rows):
        return "archived"
    key = f"{prov.engine_id}:{native}"
    for k in dict.fromkeys((key, metadata.resolve_key(key))):
        state = metadata.archive_override_under_lock(k)
        if state == "unreadable":
            return "unreadable"
        if state == "archived":
            return "archived"
    return "live"


def _create_checked(store: shares.ShareStore, *, key: str, password: str | None, **kw):
    """Create a link only for a session that exists and is not archived, under the session's
    lock — the lock `shares.archiving` holds across an archive's revoke and move, so a link can't
    land in between. The slow password hash runs BEFORE the lock."""
    pw_hash = shares.hash_share_password(password)
    prov, native = engines.parse_key(key)
    lock = store.session_lock(key)
    if not lock.acquire(timeout=shares.SESSION_LOCK_TIMEOUT_S):
        raise HTTPException(status_code=503, detail="this session is busy — try again")
    try:
        state = _session_state(prov, native)
        if state == "unknown":
            raise HTTPException(status_code=404, detail="unknown session")
        if state == "archived":
            raise HTTPException(status_code=409, detail="an archived session can't be shared")
        if state == "unreadable":
            raise HTTPException(
                status_code=503, detail="this session's state can't be read right now — try again"
            )
        return store.create(session_key=key, password_hash=pw_hash, **kw)
    finally:
        lock.release()


def _kind_for(session_key: str) -> tuple[str, str]:
    """``(canonical_key, kind)`` for a session the operator wants to share, or 422/404."""
    try:
        prov, native = engines.parse_key(session_key)
    except engines.EngineError:
        raise HTTPException(status_code=404, detail="unknown session") from None
    runtime = getattr(getattr(prov, "manifest", None), "runtime", None)
    kind = _KIND_BY_RUNTIME.get(runtime or "")
    if kind is None:
        raise HTTPException(status_code=422, detail="this session can't be shared publicly")
    return f"{prov.engine_id}:{native}", kind


def register(
    app: FastAPI,
    *,
    cfg: AuthConfig,
    logged_in,
    csrf_guard,
    web_dist: Path,
    store: shares.ShareStore | None = None,
    throttle: shares.UnlockThrottle | None = None,
) -> None:
    store = store or shares.store()

    @app.exception_handler(shares.SharesUnavailable)
    async def _shares_unavailable(_request: Request, exc: shares.SharesUnavailable) -> Response:
        # The store is missing or unusable: refuse, never treat it as "no links" (fail closed).
        return _no_store(JSONResponse({"detail": str(exc)}, status_code=503))

    throttle = throttle or shares.UnlockThrottle()
    app.state.share_store = store
    app.state.require_share = lambda *, send=False: require_share(cfg, store, send=send)
    none_mode = cfg.auth_mode == "none"
    _visitor = require_share(cfg, store)

    def _unavailable() -> None:
        if none_mode:
            raise HTTPException(status_code=404, detail="public sharing unavailable")

    def _no_store(resp: Response) -> Response:
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # -- operator ----------------------------------------------------------------------

    @app.get("/api/shares")
    async def list_shares(
        request: Request, session: str = "", _user: str = Depends(logged_in)
    ) -> Response:
        key = None
        if session:
            key, _kind = _kind_for(session)
        rows = await asyncio.to_thread(store.list_active, key)
        # In `none` mode nothing new can be created, but links minted before the switch are still
        # listed so they can be revoked.
        return _no_store(
            JSONResponse(
                {
                    "available": not none_mode,
                    "reason": NONE_MODE_REASON if none_mode else None,
                    "shares": [s.public() for s in rows],
                }
            )
        )

    @app.post("/api/shares")
    async def create_share(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> Response:
        _unavailable()
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid JSON") from None
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="expected an object")
        session = body.get("session")
        mode = body.get("mode", "view")
        password = body.get("password")
        expires_at = body.get("expires_at")
        title = body.get("title", "")
        if not isinstance(session, str) or not session:
            raise HTTPException(status_code=422, detail="session is required")
        if password is not None and not isinstance(password, str):
            raise HTTPException(status_code=422, detail="password must be a string")
        if expires_at is not None:
            try:
                if isinstance(expires_at, bool) or not isinstance(expires_at, int | float):
                    raise ValueError
                expires_at = float(expires_at)  # OverflowError on an absurd integer
                if not math.isfinite(expires_at):  # the JSON parser accepts NaN / Infinity
                    raise ValueError
            except (ValueError, OverflowError):
                raise HTTPException(
                    status_code=422, detail="expires_at must be epoch seconds"
                ) from None
        if not isinstance(title, str):
            raise HTTPException(status_code=422, detail="title must be a string")
        key, kind = _kind_for(session)
        if kind == "terminal" and mode != "view":
            raise HTTPException(
                status_code=422, detail="terminal sessions can only be shared view only"
            )
        try:
            # PBKDF2 and the engine's scan: off the loop.
            share, token = await asyncio.to_thread(
                _create_checked,
                store,
                key=key,
                password=password,
                kind=kind,
                mode=mode,
                title=title,
                expires_at=expires_at,
            )
        except shares.ShareError as e:
            raise HTTPException(status_code=422, detail=str(e)) from None
        # The ONLY response that ever carries the token.
        return _no_store(
            JSONResponse({"share": share.public(), "url": f"{cfg.origin}/p/{token}"}, 201)
        )

    @app.delete("/api/shares/{share_id}")
    async def revoke_share(
        share_id: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> Response:
        # Revoking stays available in `none` mode, so links minted before a switch to it can still
        # be ended — they would otherwise come back to life with single-user mode.
        if await asyncio.to_thread(store.get, share_id) is None:
            raise HTTPException(status_code=404, detail="unknown link")
        ended = await asyncio.to_thread(store.revoke, share_id)
        return _no_store(JSONResponse({"revoked": ended}))

    @app.post("/api/shares/revoke-all")
    async def revoke_all_shares(
        _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> Response:
        n = await asyncio.to_thread(store.revoke_all)
        return _no_store(JSONResponse({"revoked": n}))

    # -- public ------------------------------------------------------------------------

    @app.get("/p/{token}")
    async def share_viewer(token: str) -> Response:
        """The viewer page. The token is NOT checked here — the page's own unlock call does that,
        so this route answers the same for any token and tells a prober nothing."""
        _unavailable()
        page = web_dist / "share.html"
        if not page.is_file():
            # A build without the viewer bundle still answers the URL it hands out — with a plain,
            # script-free page rather than a 404 that reads like a dead link.
            return HTMLResponse(PLACEHOLDER_PAGE)
        return FileResponse(page)

    @app.post("/api/public/unlock")
    async def unlock(request: Request) -> Response:
        _unavailable()
        enforce_origin(cfg, request)  # only the viewer page itself may unlock
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid JSON") from None
        token = body.get("token") if isinstance(body, dict) else None
        password = body.get("password") if isinstance(body, dict) else None
        raw_name = body.get("name") if isinstance(body, dict) else None
        name = None
        if raw_name not in (None, ""):
            name = shares.clean_visitor_name(raw_name)
            if name is None:
                raise HTTPException(status_code=422, detail=NAME_RULE)
        if not isinstance(token, str):
            raise HTTPException(status_code=404, detail="link unavailable")
        found = await asyncio.to_thread(store.resolve_token, token)
        if found is None:
            raise HTTPException(status_code=404, detail="link unavailable")
        share, pw_hash = found
        ip = _client_ip(request)
        if pw_hash:
            if not isinstance(password, str) or password == "":
                return JSONResponse(
                    {"detail": "password required", "password_required": True}, status_code=401
                )
            # Reserved BEFORE the slow check, atomically with the budget test, so concurrent
            # guesses can't all pass while PBKDF2 runs.
            stamp = throttle.reserve(share.share_id, ip)
            if stamp is None:
                return JSONResponse(
                    {
                        "detail": "too many wrong passwords — try again later",
                        "retry_after": shares.PAUSE_S,
                    },
                    status_code=429,
                    headers={"Retry-After": str(shares.PAUSE_S)},
                )
            ok = await asyncio.to_thread(auth.verify_password, password, pw_hash)
            if not ok:
                return JSONResponse(
                    {
                        "detail": "wrong password",
                        "password_required": True,
                        "tries_left": throttle.tries_left(share.share_id, ip),
                    },
                    status_code=401,
                )
            throttle.succeed(share.share_id, ip, stamp)
        await asyncio.to_thread(store.note_open, share.share_id)
        csrf = secrets.token_urlsafe(32)
        resp = JSONResponse({"share": visitor_view(share, name), "csrf": csrf})
        issue_share_cookie(cfg, resp, share, csrf, name)
        return _no_store(resp)

    @app.get("/api/public/share")
    async def public_share(
        request: Request,
        visitor: Visitor = Depends(_visitor),  # noqa: B008 — a closure-local dependency
    ) -> Response:
        """The visitor's link, for a reload after the token left the address bar."""
        data = decode_share_cookie(cfg, request) or {}
        return _no_store(
            JSONResponse(
                {"share": visitor_view(visitor.share, visitor.name), "csrf": data.get("csrf")}
            )
        )

    @app.post("/api/public/name")
    async def public_name(
        request: Request,
        visitor: Visitor = Depends(_visitor),  # noqa: B008 — a closure-local dependency
    ) -> Response:
        """Set (or change) the visitor's name — required on a `send` link before the first send.
        Re-issues the share cookie with the same CSRF token, so an open page keeps working."""
        data = decode_share_cookie(cfg, request) or {}
        _check_csrf(cfg, request, data)
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid JSON") from None
        name = shares.clean_visitor_name(body.get("name") if isinstance(body, dict) else None)
        if name is None:
            raise HTTPException(status_code=422, detail=NAME_RULE)
        resp = JSONResponse({"share": visitor_view(visitor.share, name)})
        issue_share_cookie(
            cfg, resp, visitor.share, str(data["csrf"]), name, issued_at=int(data["iat"])
        )
        return _no_store(resp)
