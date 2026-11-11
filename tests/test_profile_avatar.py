"""The operator's avatar (#1931): the store, the operator routes and the visitor's read.

What is pinned: only PNG/JPEG/WebP by magic bytes (SVG, GIF, HTML, empty refused); a hard size
cap; 0700/0600 modes; an atomic replace that leaves no temp; a symlink never followed; the version
on `/api/config` and the share projection; CSRF+Origin on mutations; the public read behind an
API link's cookie only; and the headers every avatar response carries.
"""

from __future__ import annotations

import contextlib
import os
import stat

import httpx
import pytest

from agent_sessions import profile_avatar
from agent_sessions.main import create_app

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 64
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
GIF = b"GIF89a" + b"\x00" * 64
HTML = b"<!doctype html><script>alert(1)</script>"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def profile_dir(tmp_path, monkeypatch):
    d = tmp_path / "profile"
    monkeypatch.setenv(profile_avatar.ROOT_ENV, str(d))
    return d


# ---- the store ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "mime"), [(PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp")]
)
def test_png_jpeg_and_webp_are_stored_and_read_back_as_sniffed(profile_dir, data, mime):
    v = profile_avatar.write(data)
    got = profile_avatar.read()
    assert got is not None and got.data == data and got.mime == mime and got.version == v
    assert profile_avatar.version() == v


@pytest.mark.parametrize("data", [SVG, GIF, HTML, b"RIFF\x00\x00\x00\x00AVI ", b"\xff\xd8"])
def test_anything_else_is_refused_by_its_bytes(profile_dir, data):
    with pytest.raises(profile_avatar.AvatarError) as e:
        profile_avatar.write(data)
    assert e.value.status == 415
    assert profile_avatar.read() is None


def test_empty_and_oversized_are_refused(profile_dir):
    with pytest.raises(profile_avatar.AvatarError) as e:
        profile_avatar.write(b"")
    assert e.value.status == 422
    with pytest.raises(profile_avatar.AvatarError) as e:
        profile_avatar.write(PNG + b"\x00" * profile_avatar.MAX_BYTES)
    assert e.value.status == 413
    assert profile_avatar.read() is None


def test_modes_are_private_and_a_replace_leaves_no_temp(profile_dir):
    profile_avatar.write(PNG)
    v2 = profile_avatar.write(JPEG)
    assert stat.S_IMODE(os.stat(profile_dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(profile_dir / "avatar").st_mode) == 0o600
    assert sorted(os.listdir(profile_dir)) == ["avatar"]
    assert profile_avatar.version() == v2 != profile_avatar.version_of(PNG)


def test_a_loose_existing_folder_is_tightened(profile_dir):
    profile_dir.mkdir(mode=0o755)
    os.chmod(profile_dir, 0o755)
    profile_avatar.write(PNG)
    assert stat.S_IMODE(os.stat(profile_dir).st_mode) == 0o700


def test_remove_is_idempotent(profile_dir):
    profile_avatar.remove()  # nothing there, no folder: fine
    profile_avatar.write(PNG)
    profile_avatar.remove()
    profile_avatar.remove()
    assert profile_avatar.read() is None and profile_avatar.version() is None


def test_a_symlinked_avatar_is_never_followed(profile_dir, tmp_path):
    target = tmp_path / "elsewhere.png"
    target.write_bytes(PNG)
    profile_dir.mkdir(mode=0o700)
    os.symlink(target, profile_dir / "avatar")
    assert profile_avatar.read() is None


def test_a_symlinked_folder_is_refused(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (real / "avatar").write_bytes(PNG)
    link = tmp_path / "profile"
    os.symlink(real, link)
    monkeypatch.setenv(profile_avatar.ROOT_ENV, str(link))
    # A read never raises — it feeds /api/config — it just finds no avatar; a write refuses.
    assert profile_avatar.read() is None and profile_avatar.version() is None
    with pytest.raises(profile_avatar.AvatarError):
        profile_avatar.write(PNG)


def test_the_cached_read_follows_a_replace_and_an_in_place_rewrite(profile_dir):
    profile_avatar.write(PNG)
    assert profile_avatar.read().data == PNG
    profile_avatar.write(JPEG)  # a replace: a new inode
    assert profile_avatar.read().data == JPEG
    path = profile_dir / "avatar"
    path.write_bytes(WEBP)  # rewritten in place: same inode, new size and mtime
    os.utime(path, ns=(1, 1))
    assert profile_avatar.read().data == WEBP


def test_a_file_that_is_no_longer_an_image_reads_as_none(profile_dir):
    profile_avatar.write(PNG)
    (profile_dir / "avatar").write_bytes(SVG)
    assert profile_avatar.read() is None


# ---- the routes -----------------------------------------------------------------------------


def _client(app, cfg) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
        headers={"Origin": cfg.origin},
    )


async def _operator(app, cfg) -> httpx.AsyncClient:
    op = _client(app, cfg)
    r = await op.post("/login", data={"username": "marcus", "password": "hunter2"})
    assert r.status_code in (200, 303), r.text
    op.headers["X-CSRF-Token"] = (await op.get("/api/config")).json()["csrf"]
    return op


def _assert_avatar_headers(r: httpx.Response, mime: str) -> None:
    assert r.headers["content-type"] == mime
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert "sandbox" in r.headers["content-security-policy"]
    assert "default-src 'none'" in r.headers["content-security-policy"]


@pytest.mark.anyio
async def test_the_operator_uploads_reads_and_removes_and_config_follows(auth_cfg, profile_dir):
    app = create_app(auth_cfg)
    op = await _operator(app, auth_cfg)
    async with contextlib.aclosing(op):
        assert (await op.get("/api/config")).json()["avatar"] is None
        assert (await op.get("/api/profile/avatar")).status_code == 404
        r = await op.put(
            "/api/profile/avatar", content=WEBP, headers={"Content-Type": "image/webp"}
        )
        assert r.status_code == 200, r.text
        version = r.json()["version"]
        assert version == profile_avatar.version_of(WEBP)
        assert (await op.get("/api/config")).json()["avatar"] == version
        got = await op.get(f"/api/profile/avatar?v={version}")
        assert got.status_code == 200 and got.content == WEBP
        _assert_avatar_headers(got, "image/webp")
        # The client's Content-Type is ignored: the bytes decide.
        r = await op.put("/api/profile/avatar", content=SVG, headers={"Content-Type": "image/png"})
        assert r.status_code == 415
        assert (await op.get("/api/profile/avatar")).content == WEBP
        r = await op.delete("/api/profile/avatar")
        assert r.status_code == 200 and r.json() == {"version": None}
        assert (await op.get("/api/config")).json()["avatar"] is None


@pytest.mark.anyio
async def test_a_refused_profile_folder_never_takes_config_or_the_share_view_down(
    auth_cfg, tmp_path, monkeypatch
):
    """A symlinked profile folder (a restore, a hand-made link) reads as "no avatar": the SPA's
    bootstrap still answers, the GET is a 404, and only an upload says what is wrong."""
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (real / "avatar").write_bytes(PNG)
    link = tmp_path / "linked-profile"
    os.symlink(real, link)
    monkeypatch.setenv(profile_avatar.ROOT_ENV, str(link))
    app = create_app(auth_cfg)
    op = await _operator(app, auth_cfg)
    async with contextlib.aclosing(op):
        cfg = await op.get("/api/config")
        assert cfg.status_code == 200 and cfg.json()["avatar"] is None
        assert (await op.get("/api/profile/avatar")).status_code == 404
        assert (await op.put("/api/profile/avatar", content=PNG)).status_code == 500
    v, _ = await _visitor(app, auth_cfg)
    async with contextlib.aclosing(v):
        assert (await v.get("/api/public/avatar")).status_code == 404


@pytest.mark.anyio
async def test_an_oversized_body_is_refused_before_it_is_stored(auth_cfg, profile_dir):
    app = create_app(auth_cfg)
    op = await _operator(app, auth_cfg)
    async with contextlib.aclosing(op):

        async def body():
            yield PNG
            for _ in range(10):
                yield b"\x00" * (128 * 1024)

        r = await op.put("/api/profile/avatar", content=body())
        assert r.status_code == 413
    assert profile_avatar.read() is None


@pytest.mark.anyio
async def test_mutations_need_the_login_csrf_and_origin(auth_cfg, profile_dir):
    app = create_app(auth_cfg)
    async with _client(app, auth_cfg) as anon:
        assert (await anon.get("/api/profile/avatar")).status_code == 401
        assert (await anon.put("/api/profile/avatar", content=PNG)).status_code in (401, 403)
        assert (await anon.delete("/api/profile/avatar")).status_code in (401, 403)
    op = await _operator(app, auth_cfg)
    async with contextlib.aclosing(op):
        no_csrf = {k: v for k, v in op.headers.items() if k.lower() != "x-csrf-token"}
        r = await op.put("/api/profile/avatar", content=PNG, headers={"X-CSRF-Token": "nope"})
        assert r.status_code == 403
        r = await op.put(
            "/api/profile/avatar", content=PNG, headers={"Origin": "https://evil.example"}
        )
        assert r.status_code == 403
        del no_csrf
        r = await op.delete("/api/profile/avatar", headers={"X-CSRF-Token": "nope"})
        assert r.status_code == 403
    assert profile_avatar.read() is None


async def _visitor(app, cfg, *, kind="api"):
    st = app.state.share_store
    share, token = st.create(session_key="codex-api:x", kind=kind, mode="view")
    v = _client(app, cfg)
    r = await v.post("/api/public/unlock", json={"token": token})
    assert r.status_code == 200, r.text
    return v, share


@pytest.mark.anyio
async def test_an_api_links_visitor_reads_the_avatar_and_nothing_else_does(auth_cfg, profile_dir):
    app = create_app(auth_cfg)
    async with _client(app, auth_cfg) as nobody:
        assert (await nobody.get("/api/public/avatar")).status_code == 401
    v, share = await _visitor(app, auth_cfg)
    async with contextlib.aclosing(v):
        assert (await v.get("/api/public/avatar")).status_code == 404  # none set
        profile_avatar.write(JPEG)
        r = await v.get("/api/public/avatar")
        assert r.status_code == 200 and r.content == JPEG
        _assert_avatar_headers(r, "image/jpeg")
        app.state.share_store.revoke(share.share_id)
        assert (await v.get("/api/public/avatar")).status_code == 404
    t, _ = await _visitor(app, auth_cfg, kind="terminal")
    async with contextlib.aclosing(t):
        assert (await t.get("/api/public/avatar")).status_code == 404


@pytest.mark.anyio
async def test_the_operator_cookie_grants_nothing_on_the_public_route(auth_cfg, profile_dir):
    app = create_app(auth_cfg)
    profile_avatar.write(PNG)
    op = await _operator(app, auth_cfg)
    async with contextlib.aclosing(op):
        assert (await op.get("/api/public/avatar")).status_code == 401
