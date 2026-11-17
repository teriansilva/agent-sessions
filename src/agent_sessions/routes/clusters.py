"""Session cluster routes (#1997). See ``docs/invariants/session-clusters.md``.

* ``GET    /api/clusters?project=`` — clusters with counts, kept, source, summary, and each
  project's last sweep and its error.
* ``POST   /api/clusters`` ``{project_id, title}`` — create an operator cluster.
* ``PATCH  /api/clusters/{id}`` ``{title?, keep?}`` — ``title`` renames (the title is kept from
  then on); ``keep: true`` keeps the title shown now; ``keep: false`` is Let HQ rename.
* ``POST   /api/clusters/{id}/merge`` ``{into}`` — same project only.
* ``DELETE /api/clusters/{id}`` — dissolve.
* ``PUT    /api/sessions/{sid}/cluster`` ``{cluster_id | null}`` — place a session, or hand it
  back to HQ's choice. Refused across projects.
* ``POST   /api/clusters/recluster`` ``{project_id?}`` — sweep now, ignoring fingerprints.

Every write needs the session cookie and the CSRF token. An unreadable store answers 503 on a
write and ``unreadable`` on the read; it is never rewritten.
"""

from __future__ import annotations

import asyncio

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .. import aitasks, clusters_loop, engines, metadata, prefs, projects, review
from .. import session_clusters as sc

_UNREADABLE = "the clusters cannot be read right now"


def _refused(e: sc.ClustersError) -> JSONResponse:
    return JSONResponse({"detail": str(e)}, status_code=e.status)


def _unreadable() -> JSONResponse:
    return JSONResponse({"detail": _UNREADABLE}, status_code=503)


async def _body(request: Request) -> dict | None:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — a malformed body is a 422, not a 500
        return None
    return body if isinstance(body, dict) else None


def _known_project(project_id: object) -> bool:
    """A live project entity, or the synthetic Default one."""
    if not isinstance(project_id, str) or not project_id:
        return False
    if project_id == projects.DEFAULT_PROJECT_ID:
        return True
    p = projects.load().get(project_id)
    return p is not None and not p.archived


def _list_sync(project: str | None) -> dict:
    cfg = prefs.public_clusters()
    try:
        doc = sc.read_checked()
    except sc.ClustersUnreadable:
        return {"clusters": [], "sweeps": {}, "unreadable": True, "settings": cfg}
    try:
        counts = clusters_loop.counts(doc, clusters_loop.collect().live)
    except Exception:  # noqa: BLE001 — counts are display; the missions store may be down
        counts = None
    rows = [
        sc.describe(doc, cid, counts.get(cid, 0) if counts is not None else 0)
        for cid, c in doc["clusters"].items()
        if project is None or c["project_id"] == project
    ]
    rows.sort(key=lambda r: (r["project_id"], r["title"].casefold(), r["id"]))
    sweeps = {
        pid: sc.sweep_record(doc, pid) for pid in doc["sweeps"] if project is None or pid == project
    }
    out = {"clusters": rows, "sweeps": sweeps, "unreadable": False, "settings": cfg}
    if counts is None:
        out["counts_unavailable"] = True
    return out


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    @app.get("/api/clusters")
    async def list_clusters(
        _: str = Depends(logged_in), project: str | None = Query(None)
    ) -> JSONResponse:
        project_f = (project or "").strip() or None
        return JSONResponse(await asyncio.to_thread(_list_sync, project_f))

    @app.post("/api/clusters")
    async def create_cluster(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        body = await _body(request)
        if body is None or set(body) - {"project_id", "title"}:
            return JSONResponse({"detail": "send {project_id, title}"}, status_code=422)
        if not await asyncio.to_thread(_known_project, body.get("project_id")):
            return JSONResponse({"detail": "that project isn't here"}, status_code=404)
        try:
            out = await asyncio.to_thread(sc.create, body["project_id"], body.get("title"))
        except sc.ClustersError as e:
            return _refused(e)
        except sc.ClustersUnreadable:
            return _unreadable()
        return JSONResponse(out, status_code=201)

    # Before `/api/clusters/{cid}`-shaped POSTs so "recluster" is never read as a cluster id.
    @app.post("/api/clusters/recluster")
    async def recluster(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        body = await _body(request)
        if body is None or set(body) - {"project_id"}:
            return JSONResponse({"detail": "send {} or {project_id}"}, status_code=422)
        project_id = body.get("project_id")
        if project_id is not None and not await asyncio.to_thread(_known_project, project_id):
            return JSONResponse({"detail": "that project isn't here"}, status_code=404)
        cfg = prefs.public_clusters()
        if not cfg["enabled"]:
            return JSONResponse({"detail": "session clusters are turned off"}, status_code=409)
        if not cfg["configured"]:
            return JSONResponse(
                {"detail": "clustering needs an AI endpoint (Settings → AI)"}, status_code=409
            )
        try:
            async with aitasks.track("clusters", "recluster"):
                out = await clusters_loop.run_sweep(project_id=project_id, force=True)
        except review.NotConfiguredError:
            return JSONResponse(
                {"detail": "clustering needs an AI endpoint (Settings → AI)"}, status_code=409
            )
        except Exception:  # noqa: BLE001 — the fleet could not be read (missions store, scan)
            clusters_loop.log.exception("re-cluster failed")
            return JSONResponse(
                {"detail": "the sessions could not be read right now"}, status_code=503
            )
        if out.get("unreadable"):
            return _unreadable()
        return JSONResponse(out)

    @app.patch("/api/clusters/{cid}")
    async def patch_cluster(
        cid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        body = await _body(request)
        if body is None or not body or set(body) - {"title", "keep"}:
            return JSONResponse({"detail": "send {title} or {keep}"}, status_code=422)
        if "title" in body and "keep" in body:
            return JSONResponse({"detail": "send title or keep, not both"}, status_code=422)
        if "keep" in body and not isinstance(body["keep"], bool):
            return JSONResponse({"detail": "keep must be a boolean"}, status_code=422)
        try:
            if "title" in body:
                out = await asyncio.to_thread(sc.rename, cid, body["title"])
            else:
                out = await asyncio.to_thread(sc.set_keep, cid, body["keep"])
        except sc.ClustersError as e:
            return _refused(e)
        except sc.ClustersUnreadable:
            return _unreadable()
        return JSONResponse(out)

    @app.post("/api/clusters/{cid}/merge")
    async def merge_cluster(
        cid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        body = await _body(request)
        into = body.get("into") if body is not None else None
        if not isinstance(into, str) or set(body) - {"into"}:
            return JSONResponse({"detail": "send {into: <cluster id>}"}, status_code=422)
        try:
            out = await asyncio.to_thread(sc.merge, cid, into)
        except sc.ClustersError as e:
            return _refused(e)
        except sc.ClustersUnreadable:
            return _unreadable()
        return JSONResponse(out)

    @app.delete("/api/clusters/{cid}")
    async def dissolve_cluster(
        cid: str,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        try:
            await asyncio.to_thread(sc.dissolve, cid)
        except sc.ClustersError as e:
            return _refused(e)
        except sc.ClustersUnreadable:
            return _unreadable()
        return JSONResponse({"ok": True})

    def _place_sync(key: str, cluster_id: str | None) -> dict:
        """Place ``key`` or hand it back. The session must be one the list shows (same scope as
        the sweep), and its project is read here, never taken from the request."""
        fact = clusters_loop.collect().live.get(key)
        if fact is None:
            raise sc.ClustersError("unknown session", status=404)
        if cluster_id is None:
            aliases = metadata.load_aliases()
            sc.unplace((key, engines.physical_key(key, aliases)))
            return {"id": key, "cluster": None}
        if fact.mission:
            raise sc.ClustersError(
                "a mission holds this session; the mission is its group", status=409
            )
        return {"id": key, "cluster": sc.place(key, cluster_id, fact.project_id)}

    @app.put("/api/sessions/{sid}/cluster")
    async def place_session(
        sid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        try:
            key = engines.canonical_key(sid)
        except engines.EngineError:
            return JSONResponse({"detail": "unknown session"}, status_code=404)
        body = await _body(request)
        if body is None or set(body) != {"cluster_id"}:
            return JSONResponse({"detail": "send {cluster_id} (null to hand it back)"}, 422)
        cluster_id = body["cluster_id"]
        if cluster_id is not None and not isinstance(cluster_id, str):
            return JSONResponse({"detail": "cluster_id must be a cluster id or null"}, 422)
        try:
            out = await asyncio.to_thread(_place_sync, key, cluster_id)
        except sc.ClustersError as e:
            return _refused(e)
        except sc.ClustersUnreadable:
            return _unreadable()
        except Exception:  # noqa: BLE001 — the fleet could not be read (missions store, scan)
            clusters_loop.log.exception("placing a session failed")
            return JSONResponse(
                {"detail": "the sessions could not be read right now"}, status_code=503
            )
        return JSONResponse(out)
