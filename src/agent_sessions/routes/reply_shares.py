"""Reply links (#1956): share one finished reply of an API session, read only, as a sealed snapshot.

Operator routes (cookie + CSRF + Origin) make, list and revoke them. The public side is two
cookie-less GETs that hand out **nothing but ciphertext**: ``/r/{id}`` (the viewer page,
``reply.html``) and ``/api/public/reply/{id}`` (the envelope). The key is in the link's fragment,
which the browser keeps; see ``reply_share.py``.

**Off in ``AUTH_MODE=none``,** like every public route (``docs/invariants/public-shares.md``): that
mode hands any cookie-less request an admin session, and Home Free (which runs in it) is reached
through the relay instead (#1956 phase 3).
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from .. import engines, reply_share, share_api, shares, template_secrets
from .. import structured_runtime as runtime
from ..auth import AuthConfig
from . import shares as shares_routes

#: An id is 16 random bytes in base64url: anything else is refused before any lookup.
_ID_RE = re.compile(r"[A-Za-z0-9_-]{22}")
#: How many pages of history a share may walk to find an older turn.
_MAX_HISTORY_PAGES = 50
_GONE = "link unavailable"


def _no_store(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _api_session(session_key: object) -> tuple[str, str]:
    """``(canonical key, agent label)`` of an API session, else 404 / 422."""
    if not isinstance(session_key, str) or not session_key:
        raise HTTPException(status_code=422, detail="session is required")
    try:
        prov, native = engines.parse_key(session_key)
    except engines.EngineError:
        raise HTTPException(status_code=404, detail="unknown session") from None
    manifest = getattr(prov, "manifest", None)
    if getattr(manifest, "runtime", None) != "api":
        raise HTTPException(status_code=422, detail="only a reply in an API session can be shared")
    label = getattr(getattr(manifest, "identity", None), "label", None) or prov.engine_id
    return f"{prov.engine_id}:{native}", label


async def _find_turn(key: str, turn_id: str) -> dict:
    """The turn as the public projection reads it (settled text, never a streaming draft)."""
    try:
        page = await runtime.snapshot(key)
    except runtime.StructuredError as e:
        raise HTTPException(status_code=e.status, detail=e.detail) from None
    for _ in range(_MAX_HISTORY_PAGES):
        for t in page.get("turns") or []:
            if t.get("turn_id") == turn_id:
                return t
        before = page.get("history_before", page.get("before"))
        if not before:
            break
        try:
            page = await runtime.history(key, before=before)
        except runtime.StructuredError as e:
            raise HTTPException(status_code=e.status, detail=e.detail) from None
    raise HTTPException(status_code=404, detail="no such reply in this session")


def register(
    app: FastAPI,
    *,
    cfg: AuthConfig,
    logged_in,
    csrf_guard,
    web_dist: Path,
    store: shares.ShareStore | None = None,
) -> None:
    store = store or shares.store()
    none_mode = cfg.auth_mode == "none"

    def _unavailable() -> None:
        if none_mode:
            raise HTTPException(status_code=404, detail="public sharing unavailable")

    # -- operator ----------------------------------------------------------------------

    @app.post("/api/reply-shares")
    async def create_reply_share(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> Response:
        if none_mode:
            raise HTTPException(
                status_code=409,
                detail="reply links need a login: this server runs without one (AUTH_MODE=none)",
            )
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid JSON") from None
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="expected an object")
        key, agent = _api_session(body.get("session"))
        turn_id = body.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            raise HTTPException(status_code=422, detail="turn_id is required")
        password = body.get("password")
        if password is not None and not isinstance(password, str):
            raise HTTPException(status_code=422, detail="password must be a string")
        if isinstance(password, str) and len(password) > 256:
            raise HTTPException(status_code=422, detail="password is too long (256 characters)")
        try:
            expires_at = reply_share.expiry(body.get("expires_in", reply_share.DEFAULT_EXPIRY))
        except reply_share.ReplyShareError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from None
        turn = await _find_turn(key, turn_id)
        share_id = reply_share.new_id()

        def build() -> tuple[dict, str]:
            # Off the loop: redaction reads the secret store, and PBKDF2 is deliberately slow.
            snapshot = reply_share.content(turn, agent, share_api.redactor())
            return reply_share.seal(
                snapshot, share_id=share_id, expires_at=expires_at, password=password or None
            )

        try:
            envelope, k = await asyncio.to_thread(build)
        except reply_share.ReplyShareError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from None
        except (template_secrets.RedactionUnavailable, template_secrets.SecretKeyUnavailable):
            # The redactor can't read the secret store: share nothing rather than unredacted.
            raise HTTPException(
                status_code=503, detail="secrets can't be checked right now — try again"
            ) from None
        row = await asyncio.to_thread(
            store.reply_create,
            share_id=share_id,
            session_key=key,
            turn_id=turn_id,
            agent=agent,
            has_password=bool(password),
            expires_at=expires_at,
            envelope=json.dumps(envelope, separators=(",", ":")),
        )
        # The ONLY response that carries the key: the operator sees it once, here.
        return _no_store(
            JSONResponse({"share": row, "key": k, "path": f"/r/{share_id}"}, status_code=201)
        )

    @app.get("/api/reply-shares")
    async def list_reply_shares(session: str | None = None, _user: str = Depends(logged_in)):
        rows = await asyncio.to_thread(store.reply_list, session)
        return _no_store(JSONResponse({"available": not none_mode, "shares": rows}))

    @app.delete("/api/reply-shares/{share_id}")
    async def revoke_reply_share(
        share_id: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> Response:
        ended = await asyncio.to_thread(store.reply_revoke, share_id)
        if not ended:
            raise HTTPException(status_code=404, detail="no live reply link with that id")
        return _no_store(JSONResponse({"revoked": True}))

    @app.post("/api/reply-shares/revoke-all")
    async def revoke_all_reply_shares(
        _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> Response:
        n = await asyncio.to_thread(store.reply_revoke_all)
        return _no_store(JSONResponse({"revoked": n}))

    # -- public (cookie-less, ciphertext only) -----------------------------------------

    @app.get("/api/public/reply/{share_id}")
    async def public_reply(share_id: str) -> Response:
        """The sealed envelope of a live reply link. One 404 for unknown, revoked and expired."""
        _unavailable()
        if not _ID_RE.fullmatch(share_id):
            raise HTTPException(status_code=404, detail=_GONE)
        envelope = await asyncio.to_thread(store.reply_envelope, share_id)
        if envelope is None:
            raise HTTPException(status_code=404, detail=_GONE)
        return _no_store(Response(content=envelope, media_type="application/json"))

    @app.get("/r/")
    @app.get("/r/{share_id}")
    async def reply_viewer(share_id: str = "") -> Response:
        """The viewer page, the same for every id: its own fetch decides, so this route tells a
        prober nothing. The key never reaches here — it is in the fragment."""
        _unavailable()
        page = web_dist / "reply.html"
        if not page.is_file():
            # A build without the viewer bundle: the share viewer's script-free placeholder.
            return HTMLResponse(shares_routes.PLACEHOLDER_PAGE)
        return FileResponse(page)
