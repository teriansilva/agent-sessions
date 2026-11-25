"""The operator's display name (#1942): what share-link visitors see instead of "Operator"."""

from __future__ import annotations

import contextlib

import httpx
import pytest

from agent_sessions import prefs
from agent_sessions.main import create_app


@pytest.fixture
def anyio_backend():
    return "asyncio"


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


@pytest.mark.parametrize(
    ("raw", "kept"),
    [
        ("Marcus", "Marcus"),
        ("  Marcus   Braun ", "Marcus Braun"),
        ("Rene\u0301e", "Ren\u00e9e"),  # NFC
        ("x" * 40, "x" * 40),
        ("line\nbreak", "line break"),  # whitespace of any kind collapses to one space
        ("tab\tx", "tab x"),
    ],
)
def test_clean_names_are_kept_normalised(raw, kept):
    assert prefs.clean_operator_name(raw) == kept


@pytest.mark.parametrize(
    "raw", ["x" * 41, "Mar\u202ecus", "Mar\u200bcus", "Mar\x07cus", 5, ["a"], "   "]
)
def test_disguised_or_oversized_names_are_refused(raw):
    assert prefs.clean_operator_name(raw) is None


@pytest.mark.anyio
async def test_set_clear_and_config_through_the_prefs_route(auth_cfg):
    app = create_app(auth_cfg)
    op = await _operator(app, auth_cfg)
    async with contextlib.aclosing(op):
        assert (await op.get("/api/config")).json()["operator_name"] is None
        r = await op.post("/api/prefs", json={"operator_name": "  Marcus  "})
        assert r.status_code == 200 and r.json() == {"operator_name": "Marcus"}
        assert (await op.get("/api/config")).json()["operator_name"] == "Marcus"
        for empty in ("", None):
            r = await op.post("/api/prefs", json={"operator_name": empty})
            assert r.status_code == 200 and r.json() == {"operator_name": None}
            assert (await op.get("/api/config")).json()["operator_name"] is None
            await op.post("/api/prefs", json={"operator_name": "Marcus"})


@pytest.mark.anyio
async def test_a_refused_name_writes_nothing_not_even_the_other_keys(auth_cfg):
    app = create_app(auth_cfg)
    op = await _operator(app, auth_cfg)
    async with contextlib.aclosing(op):
        await op.post("/api/prefs", json={"operator_name": "Marcus"})
        before = prefs.get_accent()
        r = await op.post("/api/prefs", json={"accent": "#123456", "operator_name": "Mar\u202ecus"})
        assert r.status_code == 422
        assert prefs.get_operator_name() == "Marcus"
        assert prefs.get_accent() == before


@pytest.mark.anyio
async def test_it_needs_the_login_and_csrf(auth_cfg):
    app = create_app(auth_cfg)
    async with _client(app, auth_cfg) as anon:
        r = await anon.post("/api/prefs", json={"operator_name": "Mallory"})
        assert r.status_code in (401, 403)
    op = await _operator(app, auth_cfg)
    async with contextlib.aclosing(op):
        r = await op.post(
            "/api/prefs", json={"operator_name": "Mallory"}, headers={"X-CSRF-Token": "nope"}
        )
        assert r.status_code == 403
    assert prefs.get_operator_name() is None


def test_a_hand_edited_store_is_re_cleaned_on_read(tmp_path):
    path = tmp_path / "prefs.json"
    path.write_text('{"operator_name": "Mar\\u202ecus"}')
    assert prefs.get_operator_name(path) is None
