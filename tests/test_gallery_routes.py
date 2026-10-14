"""Real auth/CSRF boundaries, anonymous transport, admission fences and filesystem refusals."""

from __future__ import annotations

import asyncio
import copy
import gzip
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_sessions.gallery import contract, gallery_client, library, skills, storage, trust
from agent_sessions.main import create_app
from agent_sessions.playbooks import store
from test_gallery_library import catalog as source_catalog
from test_gallery_library import imported


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    return source_catalog.__wrapped__(tmp_path, monkeypatch)


def test_routes_auth_csrf_origin_strict_payload_and_inert_import(auth_cfg, catalog):
    client = TestClient(create_app(auth_cfg), base_url="https://testserver")
    for path in ("/api/gallery", "/api/library/skills", "/api/gallery/operations/" + "a" * 32):
        response = client.get(path)
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"
    assert (
        client.post(
            "/login",
            data={"username": "marcus", "password": "hunter2"},
            headers={"Origin": auth_cfg.origin},
            follow_redirects=False,
        ).status_code
        == 303
    )
    headers = {"Origin": auth_cfg.origin, "X-CSRF-Token": client.get("/api/config").json()["csrf"]}
    entry = next(e for e in catalog[0]["entries"] if e["content"]["kind"] == "skill")
    payload = dict(
        kind="skill",
        id="routed-skill",
        digest=entry["digest"],
        operation=uuid.uuid4().hex,
        acknowledge=True,
    )
    assert client.post("/api/gallery/import", json=payload).status_code == 403
    assert (
        client.post(
            "/api/gallery/import",
            json=payload,
            headers={**headers, "Origin": "https://invalid.example"},
        ).status_code
        == 403
    )
    for raw in ('{"kind":"skill","kind":"playbook"}', '{"unexpected":1}', '{"x":NaN}'):
        assert client.post("/api/gallery/import", content=raw, headers=headers).status_code == 422
    assert not storage.skills_root().exists()
    response = client.post("/api/gallery/import", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert result["state"] == "complete"
    skill = client.get("/api/library/skills/routed-skill").json()
    assert skill["files"] == entry["content"]["files"]
    changed = {**skill["files"], "SKILL.md": "# My local instructions"}
    assert (
        client.put(
            "/api/library/skills/routed-skill",
            json=dict(revision=skill["revision"], files=changed),
            headers=headers,
        ).status_code
        == 405
    )
    edited = dict(
        new_id="edited-skill",
        revision=skill["revision"],
        origin=skill["gallery_origin"]["pointer"],
        files=changed,
        operation=uuid.uuid4().hex,
    )
    assert client.post("/api/library/skills/routed-skill/copies", json=edited).status_code == 403
    response = client.post("/api/library/skills/routed-skill/copies", json=edited, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["result"]["id"] == "edited-skill"
    assert client.get("/api/library/skills/routed-skill").json() == skill
    assert client.get("/api/library/skills/edited-skill").json()["files"] == changed
    assert (
        client.post("/api/library/skills/routed-skill/copies", json=edited, headers=headers).json()
        == response.json()
    )
    assert (
        client.post(
            "/api/library/skills/routed-skill/copies",
            json={**edited, "operation": uuid.uuid4().hex},
            headers=headers,
        ).status_code
        == 409
    )
    assert not (storage.skills_root().parent / ".claude").exists()


def test_refresh_is_fixed_origin_anonymous_cookie_free_and_bounded(catalog, monkeypatch):
    async def run():
        record = trust.current()
        seen = []

        def serve(request):
            seen.append(request)
            assert request.url.host == "relay.battlelab.superstatus.io"
            assert request.headers["accept-encoding"] == "identity"
            assert not any(
                key in request.headers for key in ("authorization", "cookie", "x-api-key")
            )
            if request.url.path.endswith("/current.json"):
                return httpx.Response(
                    200,
                    stream=httpx.ByteStream(
                        contract.canonical({k: record[k] for k in ("sequence", "digest")})
                    ),
                    headers={"Set-Cookie": "private=secret; Path=/"},
                )
            if request.url.path.endswith(".sig"):
                return httpx.Response(200, stream=httpx.ByteStream(record["signature"].encode()))
            return httpx.Response(
                200,
                stream=httpx.ByteStream(record["data"].encode()),
                headers={"Set-Cookie": "private=secret; Path=/"},
            )

        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
        monkeypatch.setattr(gallery_client, "_TRANSPORT", httpx.MockTransport(serve))
        assert await gallery_client.refresh() == record
        assert len(seen) == 3
        for response in (
            httpx.Response(302, headers={"Location": "https://invalid.example"}),
            httpx.Response(200, stream=httpx.ByteStream(b" " * 1025)),
        ):
            monkeypatch.setattr(
                gallery_client,
                "_TRANSPORT",
                httpx.MockTransport(lambda request, response=response: response),
            )
            with pytest.raises(ValueError):
                await gallery_client.refresh()
            assert trust.current() == record

    asyncio.run(run())


@pytest.mark.parametrize(
    "serve",
    [
        lambda request: httpx.Response(404, text="<html>404 Not Found</html>"),
        lambda request: httpx.Response(302, headers={"Location": "https://invalid.example"}),
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused", request=request)),
    ],
    ids=["not-published", "redirect", "connection-refused"],
)
def test_refresh_route_reports_an_unreachable_gallery_as_503_and_keeps_the_listing(
    auth_cfg, catalog, monkeypatch, serve
):
    """#1440: "can't be reached" is its own answer (503, reason=unreachable), never a 422 that
    reads like refused content; the stored listing is untouched."""
    record = trust.current()
    monkeypatch.setattr(gallery_client, "_TRANSPORT", httpx.MockTransport(serve))
    client = TestClient(create_app(auth_cfg), base_url="https://testserver")
    client.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    headers = {"Origin": auth_cfg.origin, "X-CSRF-Token": client.get("/api/config").json()["csrf"]}
    response = client.post("/api/gallery/refresh", headers=headers)
    assert response.status_code == 503
    assert response.json() == {
        "detail": gallery_client.UNREACHABLE,
        "reason": "unreachable",
    }
    assert response.headers["cache-control"] == "no-store"
    assert trust.current() == record
    assert client.get("/api/gallery").json()["state"] == "ready"


def test_refresh_route_refuses_unverifiable_content_as_422_not_unreachable(
    auth_cfg, catalog, monkeypatch
):
    monkeypatch.setattr(
        gallery_client,
        "_TRANSPORT",
        httpx.MockTransport(
            lambda request: httpx.Response(200, stream=httpx.ByteStream(b'{"sequence":"x"}'))
        ),
    )
    client = TestClient(create_app(auth_cfg), base_url="https://testserver")
    client.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    headers = {"Origin": auth_cfg.origin, "X-CSRF-Token": client.get("/api/config").json()["csrf"]}
    response = client.post("/api/gallery/refresh", headers=headers)
    assert response.status_code == 422
    assert "reason" not in response.json()


@pytest.mark.parametrize("limit", [1024, contract.MAX_BYTES, 16384])
def test_encoded_responses_are_refused_before_reading_or_decoding(limit):
    compressed = gzip.compress(b"x" * (contract.MAX_BYTES * 2))
    reads = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            reads.append(True)
            yield compressed

    async def run():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"Content-Encoding": "gzip"}, stream=Stream()
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ValueError, match="Encoded"):
                await gallery_client._get(client, gallery_client.BASE, limit)
        assert not reads

    asyncio.run(run())


def test_cancelled_worker_drains_before_releasing_its_caller():
    async def run():
        entered, release, finished = Event(), Event(), Event()

        def worker():
            entered.set()
            release.wait(5)
            finished.set()

        task = asyncio.create_task(gallery_client._finish(worker))
        await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()

    asyncio.run(run())


def test_withdrawal_waits_for_admitted_import_then_blocks_next_import(catalog, monkeypatch):
    doc, accept = catalog
    entry = doc["entries"][0]
    entered, release = Event(), Event()
    original = store._publish_new

    def held(*args):
        entered.set()
        assert release.wait(1.5)
        return original(*args)

    monkeypatch.setattr(store, "_publish_new", held)
    withdrawn = copy.deepcopy(doc)
    withdrawn["sequence"] += 1
    withdrawn["entries"][0]["available"] = False
    with ThreadPoolExecutor(max_workers=2) as pool:
        publishing = pool.submit(
            library.import_copy, "playbook", "first-copy", entry["digest"], uuid.uuid4().hex, True
        )
        assert entered.wait(1)
        refreshing = pool.submit(accept, withdrawn)
        assert not refreshing.done()
        release.set()
        assert publishing.result()["state"] == "complete"
        refreshing.result()
    with pytest.raises(ValueError, match="withdrawn"):
        library.import_copy("playbook", "next-copy", entry["digest"], uuid.uuid4().hex, True)


def test_interrupted_floor_only_accepts_exact_cut_or_newer(catalog, monkeypatch):
    doc, accept = catalog
    old = copy.deepcopy(doc)
    doc["sequence"] += 1
    write = storage.write

    def fail_index(path, value):
        if path.name == "index.json":
            raise OSError("interrupted after floor")
        return write(path, value)

    with monkeypatch.context() as m:
        m.setattr(storage, "write", fail_index)
        with pytest.raises(OSError):
            accept(doc)
    with pytest.raises(ValueError):
        trust.current()
    with pytest.raises(ValueError, match="rollback"):
        accept(old)
    accept(doc)
    assert trust.current()["sequence"] == 2


@pytest.mark.parametrize("successor", ["omit", "rewrite", "valid", "exact"])
def test_interrupted_index_preserves_the_retained_floor_history(catalog, monkeypatch, successor):
    doc, accept = catalog
    next_cut = copy.deepcopy(doc)
    next_cut["sequence"] = 2
    added = copy.deepcopy(next(e for e in doc["entries"] if e["content"]["kind"] == "skill"))
    added["content"]["version"] = "1.1.0"
    added["content"]["files"]["SKILL.md"] = "Introduced in sequence two"
    next_cut["entries"].append(added)
    write = storage.write

    def interrupt(path, value):
        if path.name == "index.json":
            raise OSError("interrupted after advancing the floor")
        write(path, value)

    with monkeypatch.context() as m:
        m.setattr(storage, "write", interrupt)
        with pytest.raises(OSError):
            accept(next_cut)
    floor = storage.read(storage.root() / "floor.json")
    assert floor["sequence"] == 2
    candidate = copy.deepcopy(next_cut)
    candidate["sequence"] = 2 if successor == "exact" else 3
    if successor == "omit":
        candidate["entries"].pop()
    elif successor == "rewrite":
        candidate["entries"][-1]["content"]["files"]["SKILL.md"] = "Rewritten history"
    if successor in {"omit", "rewrite"}:
        with pytest.raises(ValueError, match="Retain every published version"):
            accept(candidate)
        assert storage.read(storage.root() / "floor.json") == floor
        assert storage.read(storage.root() / "index.json")["sequence"] == 1
    else:
        accept(candidate)
        assert trust.current()["sequence"] == candidate["sequence"]


@pytest.mark.parametrize("fault", ["missing", "other-cut", "bad-signature"])
def test_interrupted_index_needs_authenticated_floor_evidence(catalog, monkeypatch, fault):
    doc, accept = catalog
    old = trust.current()
    doc["sequence"] = 2
    write = storage.write

    def interrupt(path, value):
        if path.name == "index.json":
            raise OSError("interrupted")
        write(path, value)

    with monkeypatch.context() as m:
        m.setattr(storage, "write", interrupt)
        with pytest.raises(OSError):
            accept(doc)
    floor = storage.read(storage.root() / "floor.json")
    path = storage.root() / ("cut-" + floor["digest"] + ".json")
    if fault == "missing":
        path.rename(path.with_suffix(".retained"))
    elif fault == "other-cut":
        storage.write(path, old)
    else:
        storage.write(path, {**storage.read(path), "signature": "invalid"})
    doc["sequence"] = 3
    with pytest.raises(ValueError):
        accept(doc)
    assert storage.read(storage.root() / "floor.json") == floor
    assert storage.read(storage.root() / "index.json")["sequence"] == 1


@pytest.mark.parametrize("node", ["empty-directory", "symlink", "hardlink", "fifo"])
def test_skill_nodes_never_disappear_during_save(catalog, node):
    imported(catalog, "skill")
    root = storage.skills_root() / "my-copy"
    if node == "empty-directory":
        (root / "empty").mkdir()
    elif node == "symlink":
        (root / "bad").symlink_to(root / "SKILL.md")
    elif node == "hardlink":
        os.link(root / "SKILL.md", root / "bad")
    else:
        os.mkfifo(root / "bad")
    with pytest.raises((OSError, ValueError)):
        skills.get("my-copy")
    assert (root / "SKILL.md").exists()
