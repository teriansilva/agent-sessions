"""Who is connected to an API session (#1931): the store and what each side reads."""

from __future__ import annotations

import contextlib

import httpx
import pytest

from agent_sessions import presence
from agent_sessions.main import create_app


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_viewers_count_until_the_ttl_and_the_operator_is_one():
    clock = _Clock()
    p = presence.Presence(clock=clock)
    p.touch("k", presence.OPERATOR)
    p.touch("k", presence.OPERATOR)  # a second tab: still one operator
    p.touch("k", "v:a", name="Jana")
    p.touch("k", "v:b", name=None)
    p.touch("other", "v:c", name="X")
    assert p.count("k") == 3
    assert p.view("k") == {"count": 3, "visitors": [{"name": "Jana"}, {"name": None}]}
    clock.t += presence.TTL_S - 1
    p.touch("k", "v:a", name="Jana")
    clock.t += 2  # the operator and v:b are past the window, v:a was just seen
    assert p.view("k") == {"count": 1, "visitors": [{"name": "Jana"}]}
    clock.t += presence.TTL_S + 1
    assert p.count("k") == 0 and p.count("other") == 0


def test_viewers_per_session_are_bounded_and_never_evict_the_operator():
    clock = _Clock()
    p = presence.Presence(clock=clock)
    p.touch("k", presence.OPERATOR)  # the stalest entry from here on
    for i in range(presence.MAX_PER_SESSION + 50):
        clock.t += 0.01
        p.touch("k", f"v:{i}")
    assert p.count("k") == presence.MAX_PER_SESSION
    assert presence.OPERATOR in p._by_session["k"]


def test_sessions_are_bounded(monkeypatch):
    monkeypatch.setattr(presence, "MAX_SESSIONS", 5)
    clock = _Clock()
    p = presence.Presence(clock=clock)
    for i in range(8):
        clock.t += 1
        p.touch(f"k{i}", "v:a")
    assert len(p._by_session) == 5
    assert p.count("k7") == 1 and p.count("k0") == 0


def test_the_visitor_id_is_per_browser_and_never_the_token():
    a = presence.visitor_id("share1", "csrf-token-a")
    assert a == presence.visitor_id("share1", "csrf-token-a")
    assert a != presence.visitor_id("share1", "csrf-token-b")
    assert a != presence.visitor_id("share2", "csrf-token-a")
    assert "csrf-token-a" not in a and a.startswith("v:")


# ---- through the public route ----------------------------------------------------------------


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fresh(monkeypatch):
    p = presence.Presence()
    monkeypatch.setattr(presence, "_DEFAULT", p)
    return p


def _client(app, cfg) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
        headers={"Origin": cfg.origin},
    )


async def _visitor(app, cfg, key, name=None):
    share, token = app.state.share_store.create(session_key=key, kind="api", mode="view")
    v = _client(app, cfg)
    body = {"token": token, "name": name} if name else {"token": token}
    r = await v.post("/api/public/unlock", json=body)
    assert r.status_code == 200, r.text
    return v


@pytest.mark.anyio
async def test_each_visitor_browser_polling_events_is_counted_once(auth_cfg, fresh):
    app = create_app(auth_cfg)
    key = "codex-api:presence"
    a = await _visitor(app, auth_cfg, key, name="Jana")
    b = await _visitor(app, auth_cfg, key)
    async with contextlib.aclosing(a), contextlib.aclosing(b):
        # The runtime may refuse an unknown session; the touch happens first either way.
        await a.get("/api/public/events?after=0")
        await a.get("/api/public/events?after=0")
        await b.get("/api/public/events?after=0")
    assert fresh.view(key) == {"count": 2, "visitors": [{"name": "Jana"}, {"name": None}]}
