"""Device-link routes for QR cross-device sign-in (#650).

Flow (WhatsApp-Web direction — the NEW client shows the QR, the signed-in phone approves):

  1. New client (unauth) ``POST /link/start`` -> a ``challenge_id`` (public) + ``claim_token``
     (private) + expiry. It renders the QR via ``GET /link/qr?c=<challenge_id>`` and polls
     ``GET /link/status?t=<claim_token>``.
  2. The QR encodes the absolute ``<origin>/link/approve?c=<challenge_id>`` — pinned to THIS
     instance's origin, which is what makes the grant per-instance (a different install signs
     with a different secret and serves a different origin).
  3. The signed-in phone opens ``GET /link/approve`` (authed page) and POSTs approve / deny —
     session + Origin guarded, mirroring the other server-rendered form posts (``/login``,
     ``/change-password``): the SameSite=Lax cookie + Origin check is the CSRF defense for a
     form that can't set the ``X-CSRF-Token`` header.
  4. The waiting client's next ``/link/status`` mints a full session cookie exactly once.

Disabled in ``none`` auth mode (there is no login to reuse). The minted session is an ordinary
session, so the forced first-login password-change gate in ``main`` still applies to the new
client — QR sign-in never routes around it, and (because the gate redirects the phone's
``/link/approve`` to ``/change-password``) no approval can happen while a change is pending.
"""

from __future__ import annotations

import io
import time
from urllib.parse import quote

import segno
from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..auth import AuthConfig, enforce_origin, issue_session, session_uid
from ..devicelink import DeviceLinkStore

# Per-IP throttle on the UNAUTHENTICATED start, mirroring the TOTP login throttle in
# routes/auth.py: a rolling 60s window so the login page can't be scripted into a challenge
# factory. The store's outstanding-pending cap is the second, global backstop.
_START_MAX_PER_MIN = 12


def register(
    app: FastAPI, *, cfg: AuthConfig, store: DeviceLinkStore, templates: Jinja2Templates
) -> None:
    _starts: dict[str, list[float]] = {}

    def _rate_ok(ip: str) -> bool:
        now = time.time()
        # Opportunistically drop IPs whose windows have fully aged out, so the map can't grow
        # unbounded from spoofed X-Forwarded-For values.
        for k in [k for k, ts in _starts.items() if not any(now - t < 60 for t in ts)]:
            _starts.pop(k, None)
        window = [t for t in _starts.get(ip, []) if now - t < 60]
        if len(window) >= _START_MAX_PER_MIN:
            _starts[ip] = window
            return False
        window.append(now)
        _starts[ip] = window
        return True

    def _unavailable() -> None:
        # QR sign-in exists only in single-user mode — in `none` mode there is no login to
        # reuse, so the whole surface 404s (same posture as the 2FA routes).
        if cfg.auth_mode == "none":
            raise HTTPException(status_code=404, detail="device link unavailable")

    def _client_ip(request: Request) -> str:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
        return request.client.host if request.client else ""

    @app.post("/link/start")
    async def link_start(request: Request) -> Response:
        _unavailable()
        enforce_origin(cfg, request)  # same-origin only (the login page's own fetch)
        if not _rate_ok(_client_ip(request)):
            raise HTTPException(status_code=429, detail="too many requests")
        ch = store.start(ip=_client_ip(request), ua=request.headers.get("user-agent", "")[:200])
        if ch is None:
            raise HTTPException(status_code=429, detail="too many pending sign-in requests")
        return JSONResponse(
            {
                "challenge_id": ch.challenge_id,
                "claim_token": ch.claim_token,
                "expires_at": int(ch.expires_at),
                "qr_path": f"/link/qr?c={ch.challenge_id}",
            }
        )

    @app.get("/link/qr")
    async def link_qr(c: str = "") -> Response:
        _unavailable()
        # The QR encodes the ABSOLUTE, per-instance approve URL so the phone opens the right
        # BattleLab. Only render for a live challenge — never echo an arbitrary caller value.
        if store.get(c) is None:
            raise HTTPException(status_code=404, detail="unknown challenge")
        url = f"{cfg.origin}/link/approve?c={c}"
        buf = io.BytesIO()
        segno.make(url, error="m").save(buf, kind="svg", scale=6, border=2)
        return Response(
            buf.getvalue(),
            media_type="image/svg+xml",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/link/status")
    async def link_status(t: str = "") -> Response:
        _unavailable()
        state, minted = store.claim(t)
        resp = JSONResponse({"state": "approved" if minted else state})
        if minted:
            # The new client goes unauth -> authed here: mint the real session cookie. The
            # forced-change gate applies on its NEXT request like any other session.
            issue_session(cfg, resp)
        return resp

    def _render(request: Request, mode: str, ch=None, status: int = 200) -> Response:
        return templates.TemplateResponse(
            request,
            "link_approve.html",
            {
                "mode": mode,  # confirm | approved | denied | invalid
                "challenge_id": ch.challenge_id if ch else "",
                "requester_ip": ch.requester_ip if ch else "",
                "requester_ua": ch.requester_ua if ch else "",
            },
            status_code=status,
        )

    @app.get("/link/approve", response_class=HTMLResponse)
    async def link_approve_page(request: Request, c: str = "") -> Response:
        _unavailable()
        if session_uid(cfg, request) is None:
            # Not signed in on this device yet — send through login, then back to approve.
            nxt = quote(f"/link/approve?c={c}", safe="")
            return RedirectResponse(f"/login?next={nxt}", status_code=303)
        ch = store.get(c)
        return _render(request, "confirm" if ch else "invalid", ch)

    @app.post("/link/approve")
    async def link_approve(request: Request, challenge_id: str = Form(...)) -> Response:
        _unavailable()
        enforce_origin(cfg, request)
        if session_uid(cfg, request) is None:
            raise HTTPException(status_code=401, detail="no session")
        return _render(request, "approved" if store.approve(challenge_id) else "invalid")

    @app.post("/link/deny")
    async def link_deny(request: Request, challenge_id: str = Form(...)) -> Response:
        _unavailable()
        enforce_origin(cfg, request)
        if session_uid(cfg, request) is None:
            raise HTTPException(status_code=401, detail="no session")
        store.deny(challenge_id)
        return _render(request, "denied")
