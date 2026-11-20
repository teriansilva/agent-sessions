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
request, so a revoke or the link's expiry ends it on the next one; the link follows the latest
revision. In ``AUTH_MODE=none`` every public route is a 404 and sharing is refused
(`public-shares.md`'s rule).

**A password link** (#2217) answers nothing of itself before the password: ``~meta`` is a 401
naming only whether the header is shown, and ``~/`` a 404. ``~unlock`` checks the password
(attempts reserved before the slow check, the session links' `shares.UnlockThrottle`) and answers
an unlock token, signed under its own salt and naming the link. The content is then served under
``~u/<token>/``, in the path for the same reason as the view token: the sandboxed frame sends no
cookie.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import re
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .. import artifact_relay, artifact_store, auth, shares
from ..auth import AuthConfig, enforce_origin, session_uid

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


#: A password link's unlock token: its own salt (never a view token, session or share cookie), the
#: link's public id inside, the session links' unlock lifetime. The link's own expiry and revoke are
#: checked on every request beside it, so the token never outlives the link.
UNLOCK_TTL_S = 12 * 3600
_UNLOCK_SALT = "agent-sessions:artifact-public-unlock"


def unlock_token(cfg: AuthConfig, public_id: str) -> str:
    return URLSafeTimedSerializer(cfg.secret_key, salt=_UNLOCK_SALT).dumps({"p": public_id})


def _read_unlock(cfg: AuthConfig, token: str, public_id: str) -> bool:
    try:
        data = URLSafeTimedSerializer(cfg.secret_key, salt=_UNLOCK_SALT).loads(
            token, max_age=UNLOCK_TTL_S
        )
    except (BadSignature, SignatureExpired):
        return False
    return isinstance(data, dict) and data.get("p") == public_id


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


#: Markdown opened as a page gets a rendered document (#2210). Chosen by the file NAME: the type,
#: ``text/plain``, is also ``.txt``, ``.csv`` and ``.log``, which keep their bytes.
_MARKDOWN_SUFFIXES = (".md",)
#: Above this the page stays the server's text (no renderer is sent): rendering megabytes of
#: Markdown in the browser is slow, and the text is still there with Download.
MARKDOWN_RENDER_MAX = 1024 * 1024
#: The renderer, built by ``web/vite.config.artifact-md.ts`` beside the app's assets. One CLASSIC
#: script and one stylesheet: the page is an opaque origin, so a module script (a CORS request,
#: never answered) would not load. Set by `register` from the app's web build.
_RENDERER_DIR: Path | None = None
_RENDERER_FILES = ("artifact-md.js", "artifact-md.css")
_renderer_cache: dict[str, tuple[tuple[int, int], str]] = {}

#: The page's own layout: inline, so the text, the header and Download read right even when the
#: renderer is missing or fails. Tokens only, from the device-theme sheet the page links.
_MARKDOWN_CSS = (
    "*{box-sizing:border-box}[hidden]{display:none!important}"
    "html,body{margin:0;background:var(--bg-0);color:var(--text-1)}"
    "body{font:var(--fs-body,14px)/1.5 ui-sans-serif,system-ui,sans-serif}"
    ".md-head{position:sticky;top:0;z-index:1;display:flex;flex-wrap:wrap;align-items:center;"
    "gap:8px;min-height:44px;padding:6px 8px 6px 14px;background:var(--bg-1);"
    "border-bottom:1px solid var(--line-strong)}"
    ".md-name{flex:1 1 auto;min-width:0;font:600 12px var(--font-mono);overflow-wrap:anywhere}"
    ".md-name small{margin-left:8px;font-weight:400;letter-spacing:.1em;color:var(--text-3)}"
    ".md-seg{display:inline-flex;border:1px solid var(--line-strong)}"
    ".md-seg button,.md-dl{display:inline-flex;align-items:center;justify-content:center;gap:6px;"
    "min-height:32px;padding:0 12px;background:transparent;border:0;color:var(--text-2);"
    "font:600 11px var(--font-mono);letter-spacing:.1em;text-transform:uppercase;"
    "text-decoration:none;cursor:pointer}"
    ".md-dl{border:1px solid var(--line-strong)}"
    ".md-seg button[aria-pressed=true]{background:var(--hud-tint);color:var(--hud-text);"
    "box-shadow:inset 0 -2px 0 var(--hud)}"
    ".md-seg button:focus-visible,.md-dl:focus-visible{outline:2px solid var(--accent-text);"
    "outline-offset:2px}"
    ".md-note{display:none;margin:12px 16px 0;padding:8px 12px;border:1px solid var(--line-strong);"
    "border-left:2px solid var(--status-degraded);color:var(--text-2)}"
    "body[data-md=big] #md-note-big,body[data-md=failed] #md-note-failed{display:block}"
    "#md-source{margin:0;padding:16px 20px;font:12.5px/1.55 var(--font-mono);white-space:pre-wrap;"
    "overflow-wrap:anywhere;color:var(--text-1)}"
    "body[data-md=rendered] #md-source,body:not([data-md=rendered]) #md-doc{display:none}"
    "@media (max-width:800px){.md-head{padding:6px 8px 8px 12px}"
    ".md-name{flex-basis:100%;padding:4px 0}.md-seg{flex:1}.md-seg button{flex:1}"
    ".md-seg button,.md-dl{min-height:44px}}"
)


def _is_markdown(name: str) -> bool:
    return name.lower().endswith(_MARKDOWN_SUFFIXES)


def _renderer_url(fname: str) -> str | None:
    """``/assets/<fname>?v=<content hash>``, or ``None`` without a web build. The hash is cached by
    the file's mtime and size, so a new release's renderer is never served under a stale URL."""
    if _RENDERER_DIR is None:
        return None
    path = _RENDERER_DIR / fname
    try:
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size)
        hit = _renderer_cache.get(fname)
        if hit is None or hit[0] != key:
            hit = (key, hashlib.sha256(path.read_bytes()).hexdigest()[:16])
            _renderer_cache[fname] = hit
    except OSError:
        return None
    return f"/assets/{fname}?v={hit[1]}"


def _size_label(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def markdown_page(name: str, rel: str, data: bytes, renderer: tuple[str, str] | None) -> str:
    """A Markdown file opened AS A PAGE (#2210): the file's text in a ``<pre>``, HTML-escaped, under
    a header with Download, plus the renderer that turns it into a document in place.

    The ``<pre>`` is the whole fallback: no build, a failed script or a file over
    `MARKDOWN_RENDER_MAX` still reads as text. It is also the renderer's input (``textContent``
    undoes the escaping) and the Source view, so the text is inlined once. ``html.escape`` covers
    ``&``, ``<``, ``>`` and quotes, so no ``</pre>``, ``</script>`` or ``<!--`` in the source can
    end the element. The renderer is a classic script (no ``type=module``, no ``crossorigin``):
    the page is an opaque origin, and a CORS request from it is never answered (no
    ``Access-Control-Allow-Origin``, ever). Download is ``?download=1`` on the same path, which
    holds under the stable link, a revision pin, a view token and a public ``~/`` alike.

    ``renderer`` is the (script, stylesheet) URL pair, or ``None`` without a web build. A pure
    function: ``web/e2e/fixtures/artifact-md/`` is its output, pinned against drift by
    ``tests/test_artifacts.py``."""
    text = _decode_text(data)
    big = len(data) > MARKDOWN_RENDER_MAX
    js, css = (None, None) if big or renderer is None else renderer
    title = html.escape(name)
    head = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{title}</title>",
        '<link rel="stylesheet" href="/static/nightjar/tokens.css">',
        f"<style>{_MARKDOWN_CSS}</style>",
    ]
    if js and css:
        head.append(f'<link rel="stylesheet" href="{html.escape(css)}">')
    state = "big" if big else "text"
    body = [
        f'</head><body data-md="{state}">',
        '<header class="md-head">',
        f'<span class="md-name">{title}<small>MARKDOWN · {_size_label(len(data))}</small></span>',
        '<span id="md-switch" class="md-seg" role="group" aria-label="View" hidden>'
        '<button type="button" data-view="rendered" aria-pressed="true">Rendered</button>'
        '<button type="button" data-view="source" aria-pressed="false">Source</button></span>',
        '<a class="md-dl" href="?download=1" download data-md-download="">'
        '<span aria-hidden="true">↓</span> Download</a>',
        "</header>",
        f'<p class="md-note" id="md-note-big">This file is {_size_label(len(data))}: shown as'
        " text. Download it to read it in an editor.</p>",
        '<p class="md-note" id="md-note-failed">The renderer didn’t load, so the file is shown'
        " as text.</p>",
        '<main id="md-doc"></main>',
        # The newline after <pre> is one the HTML parser drops, so the text's own first line
        # survives even when it is blank.
        f'<pre id="md-source" data-path="{html.escape(rel)}">\n{html.escape(text)}</pre>',
    ]
    if js and css:
        body.append(
            f'<script src="{html.escape(js)}" '
            "onerror=\"document.body.dataset.md='failed'\"></script>"
        )
    body.append("</body></html>")
    return "".join(head + body)


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
    if _is_markdown(served.name):
        # Every representation of a Markdown file varies on the destination (#2210).
        headers["Vary"] = "Sec-Fetch-Dest"
        if request.query_params.get("download") == "1":
            headers["Content-Disposition"] = _download_name(served.name)
        elif request.headers.get("sec-fetch-dest") in _PAGE_DESTS:
            data = await asyncio.to_thread(served.blob.read_bytes)
            js, css = (_renderer_url(f) for f in _RENDERER_FILES)
            renderer = (js, css) if js and css else None
            page = markdown_page(served.name, rel or served.name, data, renderer)
            return HTMLResponse(page, headers=headers)
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
#: A password link's files live under ``~u/<unlock token>/`` instead (#2217).
UNLOCKED_PREFIX = "~u/"
#: The words for a relay-hosted share asked for options it does not have yet (#2217).
RELAY_NO_OPTIONS = (
    "A password, an expiry and hiding the header are not available yet for a public copy hosted"
    " on the BattleLab relay (Home Free). Share without them, or turn sign-in on to host the link"
    " on this box."
)


def _share_options(body: dict) -> tuple[str | None, float | None, bool]:
    """``{password?, expires_at?, header?}`` → (password, expires_at, header). Raises
    `ValueError` with words the dialog can show."""
    unknown = set(body) - {"password", "expires_at", "header"}
    if unknown:
        raise ValueError(f"unknown option: {sorted(unknown)[0]}")
    header = body.get("header", True)
    if not isinstance(header, bool):
        raise ValueError("header must be true or false")
    password = body.get("password")
    if password is not None and not isinstance(password, str):
        raise ValueError("the password must be text")
    if password and len(password) > artifact_store.PUBLIC_PASSWORD_MAX:
        raise ValueError("the password is too long")
    if password:
        artifact_store.check_password_text(password)
    return password or None, artifact_store.check_expiry(body.get("expires_at")), header


async def _public_link(public_id: str) -> artifact_store.PublicLink | None:
    try:
        return await asyncio.to_thread(artifact_store.public_link, public_id)
    except artifact_store.ArtifactsUnavailable:
        return None


def _meta(link: artifact_store.PublicLink) -> dict:
    return {
        "title": link.title,
        "kind": link.kind,
        "updated_at": link.updated_at,
        "header": link.header,
    }


def register(
    app: FastAPI, *, cfg: AuthConfig, logged_in, csrf_guard, web_dist: Path | None = None
) -> None:
    artifact_store.PUBLIC_AVAILABLE = cfg.auth_mode != "none"
    # Wrong passwords on public artifact links, counted like session links' (`public-shares.md`):
    # 5 per (link, client), 20 per link, then 15 minutes' pause.
    throttle = shares.UnlockThrottle()
    global _RENDERER_DIR
    _RENDERER_DIR = (web_dist / "assets") if web_dist is not None else None
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
        aid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """Mint (or return) the artifact's public link. In ``none`` mode (Home Free) the public copy
        is hosted on the BattleLab relay instead (#2190); 409 with the reason when it can't be.

        An optional body ``{password?, expires_at?, header?}`` (#2217) sets a box link's options;
        they apply only when a link is minted (a live link keeps its own). A relay-hosted copy
        has none of them yet: asking for one there is refused, never silently dropped."""
        options: dict = {}
        if await request.body():
            body = await _json_body(request)
            if body is None:
                return JSONResponse(
                    {"detail": "a JSON object is required"}, status_code=422, headers=_NO_STORE
                )
            try:
                password, expires_at, header = _share_options(body)
            except ValueError as e:
                return _fail(e)
            if not artifact_store.PUBLIC_AVAILABLE and (password or expires_at or not header):
                return JSONResponse(
                    {"detail": RELAY_NO_OPTIONS}, status_code=422, headers=_NO_STORE
                )
            # The slow PBKDF2 runs before the store's write transaction.
            try:
                pw_hash = await asyncio.to_thread(artifact_store.hash_public_password, password)
            except ValueError as e:
                return _fail(e)
            options = {"password_hash": pw_hash, "expires_at": expires_at, "header": header}
        if not artifact_store.PUBLIC_AVAILABLE:
            try:
                out = await artifact_relay.share(aid)
            except artifact_relay.RelayRefused as e:
                return _relay_refused(e, status=409)
            except (artifact_store.ArtifactRefused, artifact_store.ArtifactsUnavailable) as e:
                return _fail(e)
            return JSONResponse(out, headers=_NO_STORE)
        try:
            out = await asyncio.to_thread(lambda: artifact_store.share(aid, **options))
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
        """The viewer page's first fetch (#2068): `{title, kind, updated_at, header}` of a live
        link, else 404 (the same answer for unknown, revoked, expired and `none` mode). A password
        link answers 401 `{password_required: true, header}`: before the password, nothing of the
        artifact (no title, kind or time), only whether the page wears the header around its
        password card."""
        link = await _public_link(public_id)
        if link is None:
            return JSONResponse({"detail": "not found"}, status_code=404, headers=_VIEWER_HEADERS)
        if link.password_hash:
            return JSONResponse(
                {"detail": "password required", "password_required": True, "header": link.header},
                status_code=401,
                headers=_VIEWER_HEADERS,
            )
        return JSONResponse(_meta(link), headers=_VIEWER_HEADERS)

    @app.post("/artifacts/p/{public_id}/~unlock")
    async def hosted_public_unlock(public_id: str, request: Request) -> Response:
        """Trade a password link's password for its meta and an unlock token (#2217). Only the
        viewer page itself may ask (Origin). Each attempt is reserved from the throttle before the
        slow check; a correct one hands it back. Unknown, revoked, expired and password-less links
        are all the one 404."""
        if not artifact_store.PUBLIC_AVAILABLE:
            return JSONResponse({"detail": "not found"}, status_code=404, headers=_VIEWER_HEADERS)
        enforce_origin(cfg, request)
        body = await _json_body(request)
        if body is None:
            return JSONResponse(
                {"detail": "a JSON object is required"}, status_code=422, headers=_VIEWER_HEADERS
            )
        password = body.get("password")
        link = await _public_link(public_id)
        if link is None or not link.password_hash:
            return JSONResponse({"detail": "not found"}, status_code=404, headers=_VIEWER_HEADERS)
        if not isinstance(password, str) or password == "":
            return JSONResponse(
                {"detail": "password required", "password_required": True},
                status_code=401,
                headers=_VIEWER_HEADERS,
            )
        from .shares import _client_ip

        ip = _client_ip(request)
        bucket = f"artifact:{public_id}"
        stamp = throttle.reserve(bucket, ip)
        if stamp is None:
            return JSONResponse(
                {
                    "detail": "too many wrong passwords — try again later",
                    "retry_after": shares.PAUSE_S,
                },
                status_code=429,
                headers={**_VIEWER_HEADERS, "Retry-After": str(shares.PAUSE_S)},
            )
        ok = len(password) <= artifact_store.PUBLIC_PASSWORD_MAX and await asyncio.to_thread(
            auth.verify_password, password, link.password_hash
        )
        if not ok:
            return JSONResponse(
                {
                    "detail": "wrong password",
                    "password_required": True,
                    "tries_left": throttle.tries_left(bucket, ip),
                },
                status_code=401,
                headers=_VIEWER_HEADERS,
            )
        throttle.succeed(bucket, ip, stamp)
        return JSONResponse(
            {**_meta(link), "token": unlock_token(cfg, public_id)}, headers=_VIEWER_HEADERS
        )

    @app.get("/artifacts/p/{public_id}/~u/{token}/{rest:path}")
    async def hosted_public_unlocked(
        public_id: str, token: str, rest: str, request: Request
    ) -> Response:
        """A password link's files, for a holder of its unlock token. The row is re-read now: a
        revoke or the link's expiry ends the token on the next request."""
        link = await _public_link(public_id)
        if link is None or not link.password_hash or not _read_unlock(cfg, token, public_id):
            return _hosted_error(404, "not found")
        resp = await _serve(link.artifact_id, rest, None, request)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/artifacts/p/{public_id}/~u/{token}")
    async def hosted_public_unlocked_bare(public_id: str, token: str) -> Response:
        # The same one 404 as the content route for a bad, foreign or ended token.
        link = await _public_link(public_id)
        if link is None or not link.password_hash or not _read_unlock(cfg, token, public_id):
            return _hosted_error(404, "not found")
        return RedirectResponse(
            f"/artifacts/p/{quote(public_id)}/{UNLOCKED_PREFIX}{quote(token)}/",
            status_code=308,
            headers=SANDBOX_HEADERS,
        )

    @app.get("/artifacts/p/{public_id}/~/{rest:path}")
    async def hosted_public(public_id: str, rest: str, request: Request) -> Response:
        """A public link's own files: no account, the row re-read now, the latest revision, under
        the sandbox headers. The viewer page frames ``~/``; relative links stay beneath it. A
        password link's files are never here: only under ``~u/<token>/``."""
        link = await _public_link(public_id)
        if link is None or link.password_hash:
            return _hosted_error(404, "not found")
        resp = await _serve(link.artifact_id, rest, None, request)
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
