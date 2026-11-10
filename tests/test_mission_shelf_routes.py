"""The mission shelf's routes (#1740 P1a): the manifest, the verdict and the mission-bound read.

The read path takes no path and is STRICTER than `/api/files/raw`: these pin an authenticated
user asking for another mission's item, an item whose file moved under an excluded folder, a file
swapped for a symlink, a deleted file, bytes changed to something that is not their extension,
the PDF download headers, the sandbox on every response, and the Home Free proxy carrying the
route under its raw ceiling.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from agent_sessions import files, mission_shelf, missions, prefs
from agent_sessions.main import create_app
from test_mission_shelf import SESSION, png

SANDBOX = "sandbox; default-src 'none'; img-src 'self' data:; media-src 'self'"


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_FS_ROOT", str(home))
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "missions.db"))
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))
    missions.reset_schema_cache_for_test()
    files.reset_capabilities_for_test()
    files.reset_raw_streams_for_test()
    mission_shelf.reset_cache_for_test()
    monkeypatch.setattr(mission_shelf, "SETTLE_S", -1.0)
    yield
    files.reset_raw_streams_for_test()
    missions.reset_schema_cache_for_test()


@pytest.fixture
def proj(tmp_path):
    p = tmp_path / "home" / "proj"
    p.mkdir()
    return p


@pytest.fixture
def client(auth_cfg):
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    c.headers.update(
        {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": auth_cfg.origin}
    )
    return c


@pytest.fixture
def shelf(proj):
    m = missions.create_mission("assets", cwd=str(proj))["id"]
    missions.adopt(m, SESSION)
    (proj / "poster.png").write_bytes(png())
    (proj / "report.pdf").write_bytes(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n")
    (proj / "notes.md").write_text("# Notes\n\n<script>alert(1)</script>\n")
    (proj / "badge.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>')

    def add(**item):
        return mission_shelf.deliver(m, item, session_key=SESSION, phys_key=SESSION)["id"]

    ids = {
        "image": add(title="Poster", why="w", path=str(proj / "poster.png")),
        "pdf": add(title="Report", why="w", path=str(proj / "report.pdf")),
        "md": add(title="Notes", why="w", path=str(proj / "notes.md")),
        "svg": add(title="Badge", why="w", path=str(proj / "badge.svg")),
        "text": add(title="Finding", why="w", text="plain"),
    }
    return m, ids


def raw(client, m, item, **q):
    qs = "&".join(f"{k}={v}" for k, v in q.items())
    return client.get(f"/api/missions/{m}/shelf/{item}/raw" + (f"?{qs}" if qs else ""))


def assert_sandboxed(r):
    assert r.headers["content-security-policy"] == SANDBOX
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["cross-origin-resource-policy"] == "same-origin"


def test_every_shelf_route_needs_a_login(auth_cfg, shelf):
    m, ids = shelf
    anon = TestClient(create_app(auth_cfg), base_url="https://testserver")
    for path in (f"/api/missions/{m}/shelf", f"/api/missions/{m}/shelf/{ids['image']}/raw"):
        r = anon.get(path)
        assert r.status_code == 401
        assert r.headers["cache-control"] == "no-store"
    r = anon.patch(f"/api/missions/{m}/shelf/{ids['image']}", json={})
    assert r.status_code in (401, 403)


def test_the_manifest_reads_the_whole_shelf(client, shelf):
    m, ids = shelf
    r = client.get(f"/api/missions/{m}/shelf")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["revision"] == 5 and body["counts"]["total"] == 5
    assert [i["id"] for i in body["items"]] == list(ids.values())
    assert client.get(f"/api/missions/{m}/shelf?kind=text").json()["items"][0]["title"] == "Finding"
    assert client.get(f"/api/missions/{m}/shelf?kind=bogus").status_code == 422
    assert client.get(f"/api/missions/msn_{'0' * 32}/shelf").status_code == 404


def test_an_image_is_served_inline_with_its_sniffed_type_under_the_sandbox(client, shelf):
    m, ids = shelf
    r = raw(client, m, ids["image"])
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == png()
    assert r.headers["content-disposition"].startswith("inline;")
    assert_sandboxed(r)
    part = client.get(f"/api/missions/{m}/shelf/{ids['image']}/raw", headers={"Range": "bytes=0-7"})
    assert part.status_code == 206 and part.content == png()[:8]
    assert raw(client, m, ids["image"], max="10").status_code == 413


def test_a_pdf_is_download_only_with_attachment_headers(client, shelf):
    m, ids = shelf
    assert raw(client, m, ids["pdf"]).status_code == 415
    r = raw(client, m, ids["pdf"], download="1")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    cd = r.headers["content-disposition"]
    assert cd.startswith("attachment;") and "filename*=UTF-8''report.pdf" in cd
    assert_sandboxed(r)


def test_a_text_document_is_plain_text_and_an_svg_is_never_markup(client, shelf):
    m, ids = shelf
    r = raw(client, m, ids["md"])
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/plain; charset=utf-8"
    assert "<script>" in r.text  # bytes as text — the client shows them, never renders them
    assert_sandboxed(r)
    assert raw(client, m, ids["svg"]).status_code == 415
    d = raw(client, m, ids["svg"], download="1")
    assert d.status_code == 200 and d.headers["content-type"] == "application/octet-stream"
    # A text item has no bytes to stream: nothing here reads it.
    assert raw(client, m, ids["text"]).status_code == 404


def test_another_missions_item_is_not_this_missions(client, shelf, proj):
    m, ids = shelf
    other = missions.create_mission("other", cwd=str(proj))["id"]
    r = raw(client, other, ids["image"])
    assert r.status_code == 404
    assert_sandboxed(r)
    assert raw(client, m, "shf_" + "0" * 32).status_code == 404
    assert raw(client, m, "../../etc/passwd").status_code == 404


def test_a_file_moved_under_an_excluded_folder_is_refused(client, shelf, proj):
    m, ids = shelf
    prefs.set_folder_exclusions([str(proj)])
    r = raw(client, m, ids["image"])
    assert r.status_code == 403
    assert_sandboxed(r)
    prefs.set_folder_exclusions([])
    assert raw(client, m, ids["image"]).status_code == 200


def test_a_file_swapped_for_a_symlink_is_refused(client, shelf, proj, tmp_path):
    m, ids = shelf
    target = tmp_path / "home" / "secret.png"
    target.write_bytes(png())
    os.unlink(proj / "poster.png")
    os.symlink(target, proj / "poster.png")
    r = raw(client, m, ids["image"])
    assert r.status_code == 422
    assert r.content != png()
    assert_sandboxed(r)


def test_a_deleted_file_is_missing_and_stays_on_the_shelf(client, shelf, proj):
    m, ids = shelf
    os.unlink(proj / "poster.png")
    assert raw(client, m, ids["image"]).status_code == 404
    item = next(
        i for i in client.get(f"/api/missions/{m}/shelf").json()["items"] if i["id"] == ids["image"]
    )
    assert item["file"] == {"state": "missing"}


def test_bytes_changed_to_something_else_are_no_longer_shown_inline(client, shelf, proj):
    m, ids = shelf
    (proj / "poster.png").write_text("<html><script>alert(1)</script></html>")
    assert raw(client, m, ids["image"]).status_code == 415
    assert raw(client, m, ids["image"], download="1").status_code == 200
    item = next(
        i for i in client.get(f"/api/missions/{m}/shelf").json()["items"] if i["id"] == ids["image"]
    )
    assert item["file"]["state"] == "changed"


def test_reads_share_the_viewers_stream_cap_and_never_leak_a_slot(client, shelf, proj):
    m, ids = shelf
    for _ in range(files.RAW_MAX_STREAMS + 2):
        assert raw(client, m, ids["image"]).status_code == 200
    os.unlink(proj / "poster.png")
    for _ in range(files.RAW_MAX_STREAMS + 2):
        assert raw(client, m, ids["image"]).status_code == 404
    deadline = 50
    while files.raw_streams_inflight() and deadline:
        import time

        time.sleep(0.05)
        deadline -= 1
    assert files.raw_streams_inflight() == 0


def test_the_verdict_is_csrf_guarded_and_revision_checked(client, shelf, auth_cfg):
    m, ids = shelf
    url = f"/api/missions/{m}/shelf/{ids['image']}"
    r = client.patch(
        url,
        json={"expected_revision": 5, "verdict": "good", "note": "ok"},
        headers={"X-CSRF-Token": "wrong"},
    )
    assert r.status_code == 403
    r = client.patch(url, json={"expected_revision": 5, "verdict": "needs_work", "note": "redo"})
    assert r.status_code == 200
    assert r.json()["item"]["verdict"] == "needs_work" and r.json()["revision"] == 6
    stale = client.patch(url, json={"expected_revision": 5, "verdict": "good"})
    assert stale.status_code == 409 and stale.json()["revision"] == 6
    assert (
        client.patch(url, content=b"[]", headers={"content-type": "application/json"}).status_code
        == 422
    )


def test_the_home_free_proxy_carries_the_shelf_read_under_its_raw_ceiling():
    from agent_sessions.homefree import appproxy

    good = f"/api/missions/msn_{'a' * 32}/shelf/shf_{'b' * 32}/raw"
    assert appproxy._is_raw(good)
    assert appproxy._is_raw(good + "?download=1&max=100")
    assert appproxy._is_raw("/api/files/raw?path=x")
    assert not appproxy._is_raw(f"/api/missions/msn_{'a' * 32}/shelf")
    assert not appproxy._is_raw(good + "x")
