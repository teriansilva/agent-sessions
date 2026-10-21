"""#956 Phase 2 — the endpoint test route and the key-origin policy.

The stored API key is only ever sent to the ORIGIN it was saved for. Two doors can move it:

* ``POST /api/ai-review/endpoint/test`` checks a draft connection without saving it. It reuses the
  stored key only for the stored origin, and a refused request makes no outbound call at all.
* ``POST /api/prefs`` saves one. It refuses a patch that moves a stored key to another origin
  unless the same patch supplies a new key or clears the key — checked inside the prefs lock, so
  a save that races another save cannot pair one host with the other's key.

"A new key" means what the merge already treats as a replacement: blank, whitespace and the mask
preserve the stored key, so none of them may authorize a host change.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_sessions import prefs, review
from agent_sessions.main import create_app

STORED_BASE = "https://ai.example.io/v1"
STORED_KEY = "sk-stored-secret-0001"
OTHER_BASE = "https://other.example.org/v1"
TEST_URL = "/api/ai-review/endpoint/test"

PRESERVING_KEYS = [
    pytest.param({}, id="omitted"),
    pytest.param({"api_key": ""}, id="empty"),
    pytest.param({"api_key": "   "}, id="whitespace"),
    pytest.param({"api_key": prefs.AI_REVIEW_KEY_MASK}, id="mask"),
]


def _client(cfg):
    return TestClient(create_app(cfg), base_url="https://testserver")


def _login(c, cfg) -> dict:
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": cfg.origin}


@pytest.fixture
def upstream(monkeypatch):
    """Every request the review client sends, plus a switchable reply."""
    calls: list[httpx.Request] = []
    reply: dict = {"status": 200, "json": {"data": [{"id": "m-b"}, {"id": "m-a"}]}}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(reply["status"], json=reply["json"])

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
    review._models_cache.clear()
    return calls, reply


@pytest.fixture
def stored():
    prefs.set_ai_review({"base_url": STORED_BASE, "api_key": STORED_KEY, "model": "m-a"})


def _prefs_bytes() -> bytes:
    return prefs._default_path().read_bytes()


# ---- the test route -------------------------------------------------------------------


def test_a_test_never_persists_anything(auth_cfg, fake_jsonl, stored, upstream):
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    before = _prefs_bytes()
    r = c.post(TEST_URL, json={"base_url": OTHER_BASE, "api_key": "sk-new"}, headers=hdr)
    assert r.status_code == 200
    assert r.json() == {"models": ["m-a", "m-b"], "listing": "ok"}
    assert _prefs_bytes() == before
    # ...and a draft neither reads nor fills the saved-connection models cache.
    assert review._models_cache == {}


@pytest.mark.parametrize("key", [*PRESERVING_KEYS, pytest.param({"api_key": None}, id="null")])
def test_another_origin_without_a_new_key_is_refused_before_any_request(
    auth_cfg, fake_jsonl, stored, upstream, key
):
    calls, _ = upstream
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post(TEST_URL, json={"base_url": OTHER_BASE, **key}, headers=hdr)
    assert r.status_code == 422
    assert calls == []  # the stored key never left the server
    assert STORED_KEY not in r.text


def test_a_fresh_install_needs_the_key_in_the_request(auth_cfg, fake_jsonl, upstream):
    calls, _ = upstream
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post(TEST_URL, json={"base_url": STORED_BASE}, headers=hdr)
    assert r.status_code == 422
    assert "API key is required" in r.json()["detail"]
    assert calls == []
    ok = c.post(TEST_URL, json={"base_url": STORED_BASE, "api_key": "sk-first"}, headers=hdr)
    assert ok.status_code == 200
    assert calls[-1].headers["authorization"] == "Bearer sk-first"


def test_a_new_key_is_sent_instead_of_the_stored_one(auth_cfg, fake_jsonl, stored, upstream):
    calls, _ = upstream
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post(TEST_URL, json={"base_url": STORED_BASE, "api_key": " sk-new "}, headers=hdr)
    assert r.status_code == 200
    assert calls[-1].headers["authorization"] == "Bearer sk-new"


@pytest.mark.parametrize(
    "same_origin",
    ["https://ai.example.io/v1", "https://AI.Example.IO/v2", "https://ai.example.io:443/other"],
)
def test_the_same_origin_reuses_the_stored_key(auth_cfg, fake_jsonl, stored, upstream, same_origin):
    calls, _ = upstream
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post(TEST_URL, json={"base_url": same_origin}, headers=hdr)
    assert r.status_code == 200, r.text
    assert calls[-1].headers["authorization"] == f"Bearer {STORED_KEY}"


@pytest.mark.parametrize(
    "other_origin",
    [
        "http://ai.example.io/v1",
        "https://ai.example.io:8443/v1",
        "https://ai.example.io.evil.test/v1",
    ],
)
def test_a_changed_scheme_port_or_host_is_another_origin(
    auth_cfg, fake_jsonl, stored, upstream, other_origin
):
    calls, _ = upstream
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post(TEST_URL, json={"base_url": other_origin}, headers=hdr)
    assert r.status_code == 422
    assert calls == []


@pytest.mark.parametrize("status", [404, 405])
def test_an_endpoint_that_cannot_list_models_is_unsupported_not_rejected(
    auth_cfg, fake_jsonl, stored, upstream, status
):
    _, reply = upstream
    reply["status"], reply["json"] = status, {"error": "no such route"}
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post(TEST_URL, json={"base_url": STORED_BASE}, headers=hdr)
    assert r.status_code == 200
    assert r.json() == {"models": [], "listing": "unsupported"}


def test_a_rejection_is_a_502_carrying_the_gateway_reason_without_the_key(
    auth_cfg, fake_jsonl, stored, upstream
):
    _, reply = upstream
    reply["status"] = 401
    reply["json"] = {"error": {"message": f"Invalid API key: {STORED_KEY}"}}
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post(TEST_URL, json={"base_url": STORED_BASE}, headers=hdr)
    assert r.status_code == 502
    assert "Invalid API key" in r.json()["detail"]
    assert STORED_KEY not in r.text


def test_the_route_needs_a_session_and_a_csrf_token(auth_cfg, fake_jsonl, stored, upstream):
    calls, _ = upstream
    c = _client(auth_cfg)
    anon = c.post(TEST_URL, json={"base_url": STORED_BASE}, headers={"Origin": auth_cfg.origin})
    assert anon.status_code in (401, 403)
    _login(c, auth_cfg)
    no_csrf = c.post(TEST_URL, json={"base_url": STORED_BASE}, headers={"Origin": auth_cfg.origin})
    assert no_csrf.status_code in (401, 403)
    assert calls == []


# ---- the save path --------------------------------------------------------------------


@pytest.mark.parametrize("key", PRESERVING_KEYS)
def test_prefs_refuses_moving_a_stored_key_to_another_origin(auth_cfg, fake_jsonl, stored, key):
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    before = _prefs_bytes()
    r = c.post("/api/prefs", json={"ai_review": {"base_url": OTHER_BASE, **key}}, headers=hdr)
    assert r.status_code == 422
    assert "enter the API key" in r.json()["detail"]
    assert _prefs_bytes() == before


def test_prefs_accepts_a_new_origin_with_its_own_key(auth_cfg, fake_jsonl, stored):
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post(
        "/api/prefs",
        json={"ai_review": {"base_url": OTHER_BASE, "api_key": "sk-other"}},
        headers=hdr,
    )
    assert r.status_code == 200
    got = prefs.get_ai_review()
    assert (got["base_url"], got["api_key"]) == (OTHER_BASE, "sk-other")


def test_prefs_accepts_a_new_origin_that_clears_the_key(auth_cfg, fake_jsonl, stored):
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post(
        "/api/prefs", json={"ai_review": {"base_url": OTHER_BASE, "api_key": None}}, headers=hdr
    )
    assert r.status_code == 200
    got = prefs.get_ai_review()
    assert (got["base_url"], got["api_key"]) == (OTHER_BASE, "")


def test_with_no_stored_key_any_origin_is_accepted(auth_cfg, fake_jsonl):
    prefs.set_ai_review({"base_url": STORED_BASE})
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post("/api/prefs", json={"ai_review": {"base_url": OTHER_BASE}}, headers=hdr)
    assert r.status_code == 200


def test_staying_on_the_origin_keeps_the_key(auth_cfg, fake_jsonl, stored):
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post(
        "/api/prefs", json={"ai_review": {"base_url": "https://ai.example.io/v2"}}, headers=hdr
    )
    assert r.status_code == 200
    assert prefs.get_ai_review()["api_key"] == STORED_KEY


@pytest.mark.parametrize("key", PRESERVING_KEYS)
def test_clearing_the_url_while_the_key_stays_is_refused(auth_cfg, fake_jsonl, stored, key):
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    before = _prefs_bytes()
    r = c.post("/api/prefs", json={"ai_review": {"base_url": "", **key}}, headers=hdr)
    assert r.status_code == 422
    assert "clear the API key too" in r.json()["detail"]
    assert _prefs_bytes() == before


def test_clearing_the_url_together_with_the_key_is_allowed(auth_cfg, fake_jsonl, stored):
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post("/api/prefs", json={"ai_review": {"base_url": "", "api_key": None}}, headers=hdr)
    assert r.status_code == 200
    got = prefs.get_ai_review()
    assert (got["base_url"], got["api_key"]) == ("", "")


def test_clear_then_rebind_cannot_carry_the_key_to_another_host(
    auth_cfg, fake_jsonl, stored, upstream
):
    """Hermes on #960: clear the URL (key kept), then a URL-only patch for host B, then a draft test
    for B — the old key must never reach B, at any step."""
    calls, _ = upstream
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    cleared = c.post("/api/prefs", json={"ai_review": {"base_url": ""}}, headers=hdr)
    rebound = c.post("/api/prefs", json={"ai_review": {"base_url": OTHER_BASE}}, headers=hdr)
    tested = c.post(TEST_URL, json={"base_url": OTHER_BASE}, headers=hdr)
    assert (cleared.status_code, rebound.status_code, tested.status_code) == (422, 422, 422)
    assert calls == []
    got = prefs.get_ai_review()
    assert (got["base_url"], got["api_key"]) == (STORED_BASE, STORED_KEY)


def test_a_key_stored_under_an_unparseable_url_stays_bound(auth_cfg, fake_jsonl, upstream):
    calls, _ = upstream
    path = prefs._default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"ai_review": {"base_url": "https://ai.example.io:bad/v1", "api_key": "sk-x"}})
    )
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post("/api/prefs", json={"ai_review": {"base_url": OTHER_BASE}}, headers=hdr)
    assert r.status_code == 422
    t = c.post(TEST_URL, json={"base_url": OTHER_BASE}, headers=hdr)
    assert t.status_code == 422
    assert calls == []


def test_a_key_stored_without_any_url_binds_to_the_first_url(auth_cfg, fake_jsonl):
    prefs.set_ai_review({"api_key": "sk-unbound"})
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post("/api/prefs", json={"ai_review": {"base_url": OTHER_BASE}}, headers=hdr)
    assert r.status_code == 200
    # ...and from then on it is bound to that origin.
    again = c.post("/api/prefs", json={"ai_review": {"base_url": STORED_BASE}}, headers=hdr)
    assert again.status_code == 422


def test_an_in_lock_refusal_leaves_every_other_block_of_the_patch_unwritten(
    auth_cfg, fake_jsonl, stored, monkeypatch
):
    """Hermes on #960: the pre-check passes, a competing save lands before the merge, the merge
    refuses — and a theme change in the same patch must not have been written already."""
    prefs.set_theme("light")
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    real = prefs.key_origin_violation
    calls = {"n": 0}

    def racing(cur, patch):
        calls["n"] += 1
        verdict = real(cur, patch)
        if calls["n"] == 1:  # the route's pre-check: passes, then another save lands
            prefs.set_ai_review({"base_url": "https://b.example.net/v1", "api_key": "sk-b"})
        return verdict

    monkeypatch.setattr(prefs, "key_origin_violation", racing)
    r = c.post(
        "/api/prefs",
        json={"theme": "dark", "ai_review": {"base_url": "https://ai.example.io/v9"}},
        headers=hdr,
    )
    assert r.status_code == 422
    assert prefs.get_theme() == "light"
    got = prefs.get_ai_review()
    assert (got["base_url"], got["api_key"]) == ("https://b.example.net/v1", "sk-b")


@pytest.mark.parametrize(
    "bad",
    [
        "https://ai.example.io:notaport/v1",
        "https://ai.example.io:99999/v1",
        "https:///v1",
        "https://:443/v1",
        "https://exa mple.org/v1",
    ],
)
def test_a_malformed_url_is_a_422_before_any_request(auth_cfg, fake_jsonl, stored, upstream, bad):
    calls, _ = upstream
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post(TEST_URL, json={"base_url": bad, "api_key": "sk-x"}, headers=hdr)
    assert r.status_code == 422
    saved = c.post(
        "/api/prefs", json={"ai_review": {"base_url": bad, "api_key": "sk-x"}}, headers=hdr
    )
    assert saved.status_code == 422
    assert calls == []


def test_a_url_the_transport_cannot_parse_is_a_review_error_not_a_crash(upstream):
    with pytest.raises(review.ReviewError):
        asyncio.run(
            review.list_models(
                force=True, cfg={"base_url": "https://ai.example.io:notaport/v1", "api_key": "k"}
            )
        )


def test_the_in_lock_check_wins_the_race_a_pre_check_would_lose(stored):
    """Validate against origin A, lose a race to a save of origin B with key B, then merge: the
    stale patch must be refused, or it would pair URL A with key B."""
    stale = {"base_url": "https://ai.example.io/v9"}  # same origin as the stored A
    assert prefs.validate_ai_review_patch(stale) is None
    assert prefs.key_origin_violation(prefs.get_ai_review(), stale) is None  # passes pre-lock

    prefs.set_ai_review({"base_url": "https://b.example.net/v1", "api_key": "sk-b"})

    with pytest.raises(prefs.KeyOriginError):
        prefs.set_ai_review(stale)
    got = prefs.get_ai_review()
    assert (got["base_url"], got["api_key"]) == ("https://b.example.net/v1", "sk-b")


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://AI.example.io/v1", "https://ai.example.io:443"),
        ("https://ai.example.io:443/x", "https://ai.example.io:443"),
        ("http://ai.example.io", "http://ai.example.io:80"),
        ("http://127.0.0.1:8080/v1", "http://127.0.0.1:8080"),
        ("", None),
        ("ftp://ai.example.io", None),
        ("https://ai.example.io:notaport/v1", None),
    ],
)
def test_origin_normalization(url, origin):
    assert prefs.endpoint_origin(url) == origin


def test_the_saved_connection_listing_reports_unsupported_as_an_empty_list(
    auth_cfg, fake_jsonl, stored, upstream
):
    """So the Settings status LED can trust a 502 to mean "rejected", never "cannot list"."""
    _, reply = upstream
    reply["status"], reply["json"] = 404, {"error": "no such route"}
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/ai-review/models?refresh=1")
    assert r.status_code == 200
    assert r.json() == {"models": []}
