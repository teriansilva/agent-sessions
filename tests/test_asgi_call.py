"""`asgi_call`: HQ's confirmed operations run the UI's own route, in-process, as the operator.

The route's guards must hold exactly as for the browser: with the operator's cookie, CSRF token and
Origin the route runs; without the token, or from another Origin, it refuses — and nothing is
written. Nothing leaves the process (no HTTP client is imported here; see the outbound inventory).
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from agent_sessions import asgi_call, metadata
from agent_sessions.main import create_app

KEY = "claude:00000000-0000-4000-8000-0000000000a1"


def _login(c: TestClient, cfg) -> str:
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return c.get("/api/config").json()["csrf"]


@pytest.fixture
def operator(auth_cfg):
    app = create_app(auth_cfg)
    c = TestClient(app, base_url="https://testserver")
    csrf = _login(c, auth_cfg)
    cookie = "; ".join(f"{k}={v}" for k, v in c.cookies.items())

    def make(override: dict[bytes, bytes | None] | None = None) -> asgi_call.Operator:
        headers = {
            b"cookie": cookie.encode(),
            b"x-csrf-token": csrf.encode(),
            b"origin": auth_cfg.origin.encode(),
            b"host": b"testserver",
        }
        headers.update(override or {})
        return asgi_call.Operator.from_scope(
            {
                "scheme": "https",
                "server": ("testserver", 443),
                "client": ("127.0.0.1", 5000),
                "headers": [(k, v) for k, v in headers.items() if v is not None],
            }
        )

    return app, make


def _run(app, op, method, path, body):
    return asyncio.run(asgi_call.call(app, op, method, path, body))


def test_the_operators_headers_run_the_route(operator):
    app, make = operator
    r = _run(app, make(), "POST", f"/api/sessions/{KEY}/rename", {"title": "from HQ"})
    assert r.status == 200, r.body
    assert r.body == {"id": KEY, "title": "from HQ"}
    assert metadata.get(KEY).title == "from HQ"


def test_without_the_csrf_token_the_route_refuses_and_writes_nothing(operator):
    app, make = operator
    r = _run(
        app, make({b"x-csrf-token": None}), "POST", f"/api/sessions/{KEY}/rename", {"title": "x"}
    )
    assert r.status == 403
    assert metadata.get(KEY).title in (None, "")


def test_another_origin_is_refused(operator):
    app, make = operator
    r = _run(
        app,
        make({b"origin": b"https://evil.example"}),
        "POST",
        f"/api/sessions/{KEY}/rename",
        {"title": "x"},
    )
    assert r.status == 403
    assert metadata.get(KEY).title in (None, "")


def test_without_a_login_the_route_refuses(operator):
    app, make = operator
    r = _run(app, make({b"cookie": None}), "POST", f"/api/sessions/{KEY}/rename", {"title": "x"})
    assert r.status in (401, 403)


def test_only_the_three_guard_headers_and_host_are_forwarded():
    op = asgi_call.Operator.from_scope(
        {
            "headers": [
                (b"Cookie", b"s=1"),
                (b"X-CSRF-Token", b"t"),
                (b"Origin", b"https://o"),
                (b"Host", b"h"),
                (b"Authorization", b"Bearer secret"),
                (b"X-Forwarded-For", b"1.2.3.4"),
            ]
        }
    )
    assert {k for k, _ in op.headers} == {b"cookie", b"x-csrf-token", b"origin", b"host"}


@pytest.mark.parametrize("path", ["api/x", "/api/x?y=1", "/api/x#f"])
def test_a_path_with_a_query_or_no_leading_slash_is_refused(operator, path):
    app, make = operator
    with pytest.raises(ValueError):
        _run(app, make(), "POST", path, None)


def test_a_route_detail_is_read_back(operator):
    app, make = operator
    r = _run(app, make(), "POST", f"/api/sessions/{KEY}/rename", {"title": ""})
    assert r.status == 422
    assert r.detail == "title required"


def test_a_response_past_the_cap_says_it_was_cut_not_that_it_had_no_body(operator, monkeypatch):
    app, make = operator
    monkeypatch.setattr(asgi_call, "RESPONSE_MAX", 8)
    r = _run(app, make(), "POST", f"/api/sessions/{KEY}/rename", {"title": ""})
    assert r.truncated is True and r.body is None and r.status == 422
    monkeypatch.setattr(asgi_call, "RESPONSE_MAX", 1 << 20)
    r = _run(app, make(), "POST", f"/api/sessions/{KEY}/rename", {"title": ""})
    assert r.truncated is False and r.detail == "title required"
