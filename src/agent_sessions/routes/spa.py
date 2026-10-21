"""SPA static-serve routes (agent-sessions#265): the React shell at ``/`` and the
deep-link history fallback ``/{spa_path:path}``. Moved verbatim from ``main.create_app``.

The catch-all MUST be registered LAST of all routes (FastAPI matches in order) so it
never shadows the API / ws / auth routes. ``web_dist`` / ``spa_reserved`` are passed from
``create_app`` (kept defined in ``main`` so tests can monkeypatch ``main._WEB_DIST``).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response


def register(app: FastAPI, *, web_dist: Path, spa_reserved: tuple[str, ...]) -> None:
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        # React SPA: serve the shell; the app handles auth via /api 401 (login at /login).
        # (The forced-password-change gate is enforced for all routes by middleware below.)
        return FileResponse(web_dist / "index.html")

    # SPA history fallback (registered LAST so it never shadows the API/ws/auth routes
    # above). A real built file (sw.js, manifest.webmanifest, favicon…) is served as-is;
    # anything else (client routes like /s/claude/<id>) → index.html.
    @app.get("/{spa_path:path}", response_class=HTMLResponse)
    async def spa_fallback(spa_path: str) -> Response:
        if spa_path.split("/", 1)[0] in spa_reserved:
            raise HTTPException(status_code=404, detail="not found")
        candidate = (web_dist / spa_path).resolve()
        if spa_path and candidate.is_file() and web_dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(web_dist / "index.html")
