"""Artifact routes (#1932 P1): the operator API and the sandboxed hosting routes.

**The operator API** (``/api/artifacts…``) is ``logged_in``; every mutation also takes
``csrf_guard`` (CSRF + Origin). Store work runs off the event loop.

**Hosting** (``/artifacts/…``) serves agent-written HTML and JS, so it never runs with the app's
authority. Every response — a page, an asset, an error — carries `SANDBOX_HEADERS`: a
``Content-Security-Policy: sandbox`` WITHOUT ``allow-same-origin``, so the page is an opaque
origin and can neither read BattleLab's cookies nor read any same-origin ``/api`` answer;
``nosniff`` and ``no-referrer`` beside it. The served type comes
from the store's fixed extension table (`artifact_store.TYPES`); anything else downloads. There
is deliberately NO ``Cross-Origin-Resource-Policy``: an opaque-origin page's own assets would be
blocked by it. A request
path is looked up in the revision's manifest (`artifact_store.resolve`): the filesystem is never
joined with a client path.

**Private viewing is a short-lived, artifact-scoped view token in the path.** A sandboxed page is
an opaque origin, so the browser does not attach the operator's ``SameSite=Lax`` cookie to the
page's own asset requests: cookie-authenticated assets would all fail. So the operator's stable
link ``/artifacts/<id>/`` (cookie required, else the sign-in page) redirects to
``/artifacts/v/<token>/``, where ``token`` is signed with the app's secret under its own salt,
names exactly one artifact (and optionally one revision) and expires after `VIEW_TTL_S`. Relative
links stay under the token's prefix. The token reads that one artifact and nothing else.

**Public links** (``/artifacts/p/<public_id>/``, P3) need no account. The row is re-read on every
request, so a revoke ends the link on the next one; the link follows the latest revision. In
``AUTH_MODE=none`` every public route is a 404 and sharing is refused (`public-shares.md`'s rule).
"""

from __future__ import annotations

import asyncio
import html
import re
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .. import artifact_relay, artifact_store
from ..auth import AuthConfig, session_uid

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
VIEW_TTL_S = 12 * 3600
_VIEW_SALT = "agent-sessions:artifact-view"

#: Every hosting response carries these. NO `allow-same-origin`: the page is an opaque origin.
SANDBOX_CSP = (
    "sandbox allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox"
    " allow-downloads allow-modals; frame-ancestors 'self'"
)
SANDBOX_HEADERS: dict[str, str] = {
    "Content-Security-Policy": SANDBOX_CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # NO `Cross-Origin-Resource-Policy`: the page is an opaque origin, so its own asset requests
    # are never same-origin with the asset, and `same-origin` blocked every stylesheet, image and
    # script a hosted site loads (Hermes on PR #1933, proven in a real browser and pinned by
    # web/e2e/appmode-artifacts.spec.ts). The sandbox, not CORP, keeps the page away from BattleLab.
    "X-Frame-Options": "SAMEORIGIN",
    "Cache-Control": "private, no-cache",
}
_REF = re.compile(r"^(?P<id>[1-9][0-9]{3}(?:-[1-9][0-9]{3}){3})(?:@(?P<rev>[1-9][0-9]{0,6}))?$")
_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _serializer(cfg: AuthConfig) -> URLSafeTimedSerializer:
    # Its own salt: a view token never loads as a session or share cookie, nor the reverse.
    return URLSafeTimedSerializer(cfg.secret_key, salt=_VIEW_SALT)


def view_token(cfg: AuthConfig, aid: str, revision: int | None) -> str:
    return _serializer(cfg).dumps({"a": aid, "r": revision})


def _read_token(cfg: AuthConfig, token: str) -> tuple[str, int | None] | None:
    try:
        data = _serializer(cfg).loads(token, max_age=VIEW_TTL_S)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    aid, rev = data.get("a"), data.get("r")
    if not isinstance(aid, str) or not artifact_store.ID_RE.fullmatch(aid):
        return None
    if rev is not None and (not isinstance(rev, int) or isinstance(rev, bool) or rev < 1):
        return None
    return aid, rev


def _hosted_error(status: int, text: str) -> Response:
    return Response(
        text + "\n", status_code=status, media_type="text/plain; charset=utf-8",
        headers=SANDBOX_HEADERS,
    )  # fmt: skip


def _download_name(name: str) -> str:
    """RFC 6266: an ASCII fallback plus the UTF-8 ``filename*``."""
    ascii_name = _FILENAME_SAFE.sub("_", name)[:120] or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name[:200], safe='')}"


def _listing_page(aid: str, files: list[dict]) -> Response:
    rows = "".join(
        f'<li><a href="{html.escape(quote(f["path"]))}">{html.escape(f["path"])}</a>'
        f" <small>{f['size']} B</small></li>"
        for f in files
    )
    body = (
        '<!doctype html><meta charset="utf-8"><title>Files</title>'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<h1>Files</h1><ul>{rows}</ul>"
    )
    return HTMLResponse(body, headers=SANDBOX_HEADERS)


#: A browser opening a page (top level or in a frame) says so in ``Sec-Fetch-Dest``.
_PAGE_DESTS = frozenset({"document", "iframe", "frame"})


def _media_page(name: str, media: str) -> Response:
    """An audio or video file opened AS A PAGE gets a one-element player, not the bare bytes.

    A bare media file becomes the browser's own media document, and its player re-fetches the file
    from that document's origin. Under ``CSP: sandbox`` that origin is ``null``, and Chrome 157
    makes that re-fetch in CORS mode, so with no ``Access-Control-Allow-Origin`` it is blocked and
    nothing plays. A plain ``<audio>``/``<video>`` element fetches without CORS (as in any hosted
    site), which an opaque origin may do. Answering ``Allow-Origin: null`` instead would open the
    file to every sandboxed page anywhere, so the player page it is. The element's own request is
    not a page (``Sec-Fetch-Dest: audio``/``video``) and gets the bytes."""
    tag = "video" if media.startswith("video/") else "audio"
    src = html.escape(quote(name))
    body = (
        f'<!doctype html><meta charset="utf-8"><title>{html.escape(name)}</title>'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<style>html,body{margin:0;height:100%;background:#000}"
        "body{display:grid;place-items:center}"
        "audio{width:min(640px,92vw)}video{max-width:100%;max-height:100vh}</style>"
        f'<{tag} controls playsinline preload="metadata" src="{src}"></{tag}>'
    )
    return HTMLResponse(body, headers={**SANDBOX_HEADERS, "Vary": "Sec-Fetch-Dest"})


async def _serve(aid: str, rel: str, revision: int | None, request: Request) -> Response:
    try:
        served = await asyncio.to_thread(artifact_store.resolve, aid, rel, revision=revision)
    except artifact_store.Redirect as r:
        if r.location == "index":
            files = await asyncio.to_thread(artifact_store.listing, aid, revision=revision)
            return _listing_page(aid, files)
        return RedirectResponse(r.location, status_code=308, headers=SANDBOX_HEADERS)
    except (artifact_store.NotServed, artifact_store.ArtifactsUnavailable):
        return _hosted_error(404, "not found")
    headers = dict(SANDBOX_HEADERS)
    if served.type is None:
        headers["Content-Disposition"] = _download_name(served.name)
        media = "application/octet-stream"
    else:
        media = served.type
        if media.startswith(("audio/", "video/")):
            if request.headers.get("sec-fetch-dest") in _PAGE_DESTS:
                return _media_page(served.name, media)
            headers["Vary"] = "Sec-Fetch-Dest"
    return FileResponse(served.blob, media_type=media, headers=headers)


def _fail(e: Exception) -> JSONResponse:
    if isinstance(e, artifact_store.ArtifactRefused):
        return JSONResponse({"detail": e.code}, status_code=e.status, headers=_NO_STORE)
    if isinstance(e, artifact_store.ArtifactsUnavailable):
        return JSONResponse(
            {"detail": "the artifact store is unavailable"}, status_code=503, headers=_NO_STORE
        )
    return JSONResponse({"detail": str(e)}, status_code=422, headers=_NO_STORE)


def _relay_refused(e: artifact_relay.RelayRefused, *, status: int) -> JSONResponse:
    """The relay could not host or remove a public copy (#2190): ``public_off`` on a share (the
    UI's existing path), ``relay_failed`` otherwise; the reason in the operator's words."""
    detail = "public_off" if status == 409 else "relay_failed"
    return JSONResponse(
        {"detail": detail, "code": e.code, "reason": e.reason},
        status_code=status,
        headers=_NO_STORE,
    )


async def _json_body(request: Request) -> dict | None:
    try:
        body = await request.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


#: The public viewer page (#2068) is the app's own page, not agent content: no sandbox CSP (the
#: app's normal CSP applies), but never cached and never a Referer.
_VIEWER_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
#: The prefix a public artifact's own files live under, beneath the viewer page (#2068).
CONTENT_PREFIX = "~/"


def register(
    app: FastAPI, *, cfg: AuthConfig, logged_in, csrf_guard, web_dist: Path | None = None
) -> None:
    artifact_store.PUBLIC_AVAILABLE = cfg.auth_mode != "none"
    # ---- the operator API -----------------------------------------------------------------

    @app.get("/api/artifacts")
    async def artifacts_list(request: Request, _user: str = Depends(logged_in)) -> JSONResponse:
        q = request.query_params
        try:
            since = float(q["since"]) if q.get("since") else None
            out = await asyncio.to_thread(
                artifact_store.list_artifacts,
                project=q.get("project") or None,
                session=q.get("session") or None,
                mission=q.get("mission") or None,
                kind=q.get("kind") or None,
                visibility=q.get("visibility") or None,
                q=q.get("q") or None,
                since=since,
                cursor=q.get("cursor") or None,
            )
        except (ValueError, artifact_store.ArtifactsUnavailable) as e:
            return _fail(e)
        return JSONResponse(out, headers=_NO_STORE)

    @app.get("/api/artifacts/settings")
    async def artifacts_settings(_user: str = Depends(logged_in)) -> JSONResponse:
        try:
            usage = await asyncio.to_thread(artifact_store.usage)
        except artifact_store.ArtifactsUnavailable as e:
            return _fail(e)
        return JSONResponse({"enabled": artifact_store.enabled(), **usage}, headers=_NO_STORE)

    @app.put("/api/artifacts/settings")
    async def artifacts_settings_put(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        body = await _json_body(request)
        if body is None or not isinstance(body.get("enabled"), bool):
            return JSONResponse(
                {"detail": "{enabled: bool} is required"}, status_code=422, headers=_NO_STORE
            )
        from .. import artifact_broker, prefs

        await asyncio.to_thread(prefs.set_artifacts_enabled, body["enabled"])
        if not body["enabled"]:
            await asyncio.to_thread(artifact_broker.revoke_all)
        return JSONResponse({"enabled": artifact_store.enabled()}, headers=_NO_STORE)

    @app.get("/api/artifacts/activation")
    async def activation_get(request: Request, _user: str = Depends(logged_in)) -> JSONResponse:
        """``?scope=&id=`` → the mode set there (``null`` = inherits). For a session, also
        ``?cwd=`` → what its next launch gets and where that comes from."""
        q = request.query_params
        scope, sid = q.get("scope") or "", q.get("id") or ""
        try:
            mode = await asyncio.to_thread(artifact_store.get_activation, scope, sid)
            out: dict = {
                "scope": scope,
                "id": sid,
                "mode": mode,
                "enabled": artifact_store.enabled(),
            }
            if scope == "session":
                eff = await asyncio.to_thread(
                    artifact_store.effective_mode, [sid], cwd=q.get("cwd") or None
                )
                out["effective"] = {"mode": eff.mode, "source": eff.source}
        except (ValueError, artifact_store.ArtifactsUnavailable) as e:
            return _fail(e)
        return JSONResponse(out, headers=_NO_STORE)

    @app.put("/api/artifacts/activation")
    async def activation_put(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        """``{scope, id, mode: off|private|public|null}``. Reaches the NEXT launch only."""
        body = await _json_body(request)
        if body is None:
            return JSONResponse(
                {"detail": "a JSON object is required"}, status_code=422, headers=_NO_STORE
            )
        try:
            mode = await asyncio.to_thread(
                artifact_store.set_activation, body.get("scope"), body.get("id"), body.get("mode")
            )
        except (ValueError, artifact_store.ArtifactsUnavailable) as e:
            return _fail(e)
        return JSONResponse(
            {"scope": body.get("scope"), "id": body.get("id"), "mode": mode}, headers=_NO_STORE
        )

    @app.get("/api/artifacts/{aid}")
    async def artifact_get(aid: str, _user: str = Depends(logged_in)) -> JSONResponse:
        try:
            out = await asyncio.to_thread(artifact_store.get, aid)
        except (artifact_store.ArtifactRefused, artifact_store.ArtifactsUnavailable) as e:
            return _fail(e)
        if out is None:
            return JSONResponse({"detail": "no such artifact"}, status_code=404, headers=_NO_STORE)
        return JSONResponse(out, headers=_NO_STORE)

    @app.delete("/api/artifacts/{aid}")
    async def artifact_delete(
        aid: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        # A relay-hosted public copy (#2190) goes first, under the artifact's relay lock; if the
        # relay can't confirm, the artifact stays, so a public copy is never left orphaned.
        try:
            gone = await artifact_relay.delete_artifact(aid)
        except artifact_relay.RelayRefused as e:
            return _relay_refused(e, status=502)
        except (artifact_store.ArtifactRefused, artifact_store.ArtifactsUnavailable) as e:
            return _fail(e)
        if not gone:
            return JSONResponse({"detail": "no such artifact"}, status_code=404, headers=_NO_STORE)
        return JSONResponse({"deleted": aid}, headers=_NO_STORE)

    # ---- hosting --------------------------------------------------------------------------
    # Order matters: the token and public prefixes are registered before the operator's
    # `/artifacts/{ref}/…`, so a ref can never shadow them (and `v` / `p` are not valid ids).

    @app.post("/api/artifacts/{aid}/share")
    async def artifact_share(
        aid: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        """Mint (or return) the artifact's public link. In ``none`` mode (Home Free) the public copy
        is hosted on the BattleLab relay instead (#2190); 409 with the reason when it can't be."""
        if not artifact_store.PUBLIC_AVAILABLE:
            try:
                out = await artifact_relay.share(aid)
            except artifact_relay.RelayRefused as e:
                return _relay_refused(e, status=409)
            except (artifact_store.ArtifactRefused, artifact_store.ArtifactsUnavailable) as e:
                return _fail(e)
            return JSONResponse(out, headers=_NO_STORE)
        try:
            out = await asyncio.to_thread(artifact_store.share, aid)
        except artifact_store.ArtifactRefused as e:
            if e.code == "public_off":
                return JSONResponse(
                    {"detail": "public_off", "reason": artifact_store.PUBLIC_OFF_REASON},
                    status_code=409,
                    headers=_NO_STORE,
                )
            return _fail(e)
        except artifact_store.ArtifactsUnavailable as e:
            return _fail(e)
        return JSONResponse(out, headers=_NO_STORE)

    @app.delete("/api/artifacts/{aid}/share")
    async def artifact_revoke(
        aid: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        try:
            # The relay copy first (#2190): kept, with the reason, if the relay can't confirm.
            await artifact_relay.revoke(aid)
        except artifact_relay.RelayRefused as e:
            return _relay_refused(e, status=502)
        except (artifact_store.ArtifactRefused, artifact_store.ArtifactsUnavailable) as e:
            return _fail(e)
        try:
            out = await asyncio.to_thread(artifact_store.revoke, aid)
        except (artifact_store.ArtifactRefused, artifact_store.ArtifactsUnavailable) as e:
            return _fail(e)
        return JSONResponse(out, headers=_NO_STORE)

    @app.get("/artifacts/p/{public_id}/~meta")
    async def hosted_public_meta(public_id: str) -> Response:
        """The viewer page's one fetch (#2068): `{title, kind, updated_at}` of a live link, else 404
        (the same answer for unknown, revoked and `none` mode)."""
        try:
            meta = await asyncio.to_thread(artifact_store.public_meta, public_id)
        except artifact_store.ArtifactsUnavailable:
            meta = None
        if meta is None:
            return JSONResponse({"detail": "not found"}, status_code=404, headers=_VIEWER_HEADERS)
        return JSONResponse(meta, headers=_VIEWER_HEADERS)

    @app.get("/artifacts/p/{public_id}/~/{rest:path}")
    async def hosted_public(public_id: str, rest: str, request: Request) -> Response:
        """A public link's own files: no account, the row re-read now, the latest revision, under
        the sandbox headers. The viewer page frames ``~/``; relative links stay beneath it."""
        try:
            aid = await asyncio.to_thread(artifact_store.by_public_id, public_id)
        except artifact_store.ArtifactsUnavailable:
            aid = None
        if aid is None:
            return _hosted_error(404, "not found")
        resp = await _serve(aid, rest, None, request)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/artifacts/p/{public_id}/{rest:path}")
    async def hosted_public_viewer(public_id: str, rest: str, request: Request) -> Response:
        """The public link itself (#2068): ``/artifacts/p/<id>/`` is the BattleLab viewer page — the
        public header above the artifact in a sandboxed frame. The same page for every well-formed
        id: its own ``~meta`` fetch decides, so this route tells a prober nothing. A deeper path
        from before the viewer existed redirects to the same file under ``~/``."""
        if not artifact_store.PUBLIC_AVAILABLE:
            return _hosted_error(404, "not found")  # every public route is a 404 without a login
        if not artifact_store.PUBLIC_ID_RE.fullmatch(public_id):
            # Not an id's shape at all: the format is public, so this says nothing about any link.
            return _hosted_error(404, "not found")
        if rest:
            query = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(
                f"/artifacts/p/{quote(public_id)}/{CONTENT_PREFIX}{quote(rest)}{query}",
                status_code=308,
                headers=SANDBOX_HEADERS,
            )
        page = (web_dist / "artifact.html") if web_dist is not None else None
        if page is None or not page.is_file():
            # A build without the viewer bundle: the artifact itself, as before the viewer.
            return RedirectResponse(
                f"/artifacts/p/{quote(public_id)}/{CONTENT_PREFIX}",
                status_code=307,
                headers=SANDBOX_HEADERS,
            )
        return FileResponse(page, headers=_VIEWER_HEADERS)

    @app.get("/artifacts/p/{public_id}")
    async def hosted_public_bare(public_id: str) -> Response:
        if not artifact_store.PUBLIC_AVAILABLE:
            return _hosted_error(404, "not found")  # every public route is a 404 without a login
        return RedirectResponse(
            f"/artifacts/p/{quote(public_id)}/", status_code=308, headers=SANDBOX_HEADERS
        )

    @app.get("/artifacts/v/{token}/{rest:path}")
    async def hosted_view(token: str, rest: str, request: Request) -> Response:
        read = _read_token(cfg, token)
        if read is None:
            return _hosted_error(404, "this view link has expired; open the artifact again")
        aid, rev = read
        return await _serve(aid, rest, rev, request)

    @app.get("/artifacts/v/{token}")
    async def hosted_view_bare(token: str) -> Response:
        return RedirectResponse(f"/artifacts/v/{token}/", status_code=308, headers=SANDBOX_HEADERS)

    @app.get("/artifacts/{ref}")
    async def hosted_bare(ref: str) -> Response:
        if not _REF.fullmatch(ref):
            return _hosted_error(404, "not found")
        return RedirectResponse(f"/artifacts/{ref}/", status_code=308, headers=SANDBOX_HEADERS)

    @app.get("/artifacts/{ref}/{rest:path}")
    async def hosted_operator(ref: str, rest: str, request: Request) -> Response:
        """The operator's stable link: signed in → a view token for this artifact, else the
        sign-in page that comes back here."""
        m = _REF.fullmatch(ref)
        if m is None:
            return _hosted_error(404, "not found")
        query = f"?{request.url.query}" if request.url.query else ""
        if session_uid(cfg, request) is None:
            target = quote(request.url.path + query, safe="/@")
            return RedirectResponse(
                f"/login?next={target}", status_code=303, headers=SANDBOX_HEADERS
            )
        aid = m.group("id")
        rev = int(m.group("rev")) if m.group("rev") else None
        try:
            exists = await asyncio.to_thread(artifact_store.get, aid)
        except (artifact_store.ArtifactRefused, artifact_store.ArtifactsUnavailable):
            exists = None
        if exists is None:
            return _hosted_error(404, "not found")
        token = view_token(cfg, aid, rev)
        return RedirectResponse(
            f"/artifacts/v/{token}/{quote(rest)}{query}",
            status_code=303,
            headers={**SANDBOX_HEADERS, "Cache-Control": "no-store"},
        )
