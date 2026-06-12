"""AI review engine + routes (#356 Phase 1) against a MOCKED OpenAI-compatible endpoint
(httpx.MockTransport — no real network in CI): success, malformed JSON, timeout,
live-only / transcript-only / missing-PTY input assembly, staleness semantics (a failed
review never masquerades as fresh), the /models proxy, manual Review-now, and the
per-session exclude toggle."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_sessions import metadata, prefs, review, webterm
from agent_sessions.main import create_app

SECRET = "sk-review-secret-31337"  # noqa: S105 — test fixture value
BASE = "https://ai.test/v1"
SID = "claude:11111111-1111-1111-1111-111111111111"  # exists in fake_jsonl
LIVE_ONLY_SID = "claude:99999999-9999-9999-9999-999999999999"  # no jsonl on disk


@pytest.fixture(autouse=True)
def _reset_review(monkeypatch):
    monkeypatch.setattr(review, "_TRANSPORT", None)
    review._models_cache.clear()
    yield
    review._models_cache.clear()


@pytest.fixture
def ai_prefs(tmp_home, monkeypatch):
    """Point prefs at tmp and store a configured ai_review block."""
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_home / "prefs.json"))
    prefs.set_ai_review({"base_url": BASE, "api_key": SECRET, "model": "test-model"})
    return tmp_home


def _ok_result(summary="Editing tests", title="Fix the tests", required=False, reason=""):
    return {
        "summary": summary,
        "title": title,
        "intervention_required": required,
        "reason": reason,
    }


def _chat_transport(content: object, *, status=200, calls: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        assert request.headers["Authorization"] == f"Bearer {SECRET}"
        body = content if isinstance(content, str) else json.dumps(content)
        return httpx.Response(
            status,
            json={"choices": [{"message": {"content": body}}]},
        )

    return httpx.MockTransport(handler)


# ---- engine: input assembly ------------------------------------------------------------


def test_gather_input_transcript_only(ai_prefs, fake_jsonl):
    text, fp = review.gather_input(SID, 24000)
    assert "first message on repo-a" in text
    assert "## Transcript (tail)" in text
    assert "## Live terminal" not in text  # no PTY output observed → transcript-only
    assert len(fp) == 64


def test_gather_input_live_only(ai_prefs, fake_jsonl):
    webterm._buffer_append(LIVE_ONLY_SID, b"\x1b[1mrunning pytest\x1b[0m\r\n$ done\r\n")
    text, _ = review.gather_input(LIVE_ONLY_SID, 24000)
    assert "## Live terminal (tail)" in text
    assert "running pytest" in text
    assert "\x1b" not in text  # ANSI stripped by the narrow accessor


def test_gather_input_missing_everything_raises(ai_prefs, fake_jsonl):
    with pytest.raises(review.ReviewError):
        review.gather_input("claude:88888888-8888-8888-8888-888888888888", 24000)


def test_gather_input_tail_truncates_and_fingerprint_tracks_content(ai_prefs, fake_jsonl):
    text, fp1 = review.gather_input(SID, 64)
    assert len(text) <= 64
    webterm._buffer_append(SID, b"new live bytes change the fingerprint\r\n")
    _, fp2 = review.gather_input(SID, 64)
    assert fp1 != fp2


# ---- engine: review round trip -----------------------------------------------------------


def test_run_review_success_persists_metadata(ai_prefs, fake_jsonl, monkeypatch):
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _chat_transport(_ok_result(required=True, reason="waiting on permission prompt")),
    )
    out = asyncio.run(review.run_review(SID))
    assert out["ai_summary"] == "Editing tests"
    m = metadata.get(SID)
    assert m.ai_title == "Fix the tests"
    assert m.intervention_required is True
    assert m.intervention_reason == "waiting on permission prompt"
    assert m.reviewed_at is not None
    assert m.review_fingerprint


def test_clean_review_clears_badge_and_reason(ai_prefs, fake_jsonl, monkeypatch):
    metadata.patch(SID, intervention_required=True, intervention_reason="stuck")
    monkeypatch.setattr(
        review, "_TRANSPORT", _chat_transport(_ok_result(required=False, reason="leftover"))
    )
    asyncio.run(review.run_review(SID))
    m = metadata.get(SID)
    assert m.intervention_required is False
    assert m.intervention_reason == ""  # a clean review clears the reason too


def test_run_review_tolerates_fenced_json(ai_prefs, fake_jsonl, monkeypatch):
    fenced = "```json\n" + json.dumps(_ok_result(summary="From fence")) + "\n```"
    monkeypatch.setattr(review, "_TRANSPORT", _chat_transport(fenced))
    out = asyncio.run(review.run_review(SID))
    assert out["ai_summary"] == "From fence"


def test_failed_review_keeps_last_good_result(ai_prefs, fake_jsonl, monkeypatch):
    """Staleness semantics: a failure persists NOTHING — reviewed_at / summary stay at the
    last good review, so the result is visibly stale rather than wrongly fresh."""
    monkeypatch.setattr(review, "_TRANSPORT", _chat_transport(_ok_result()))
    asyncio.run(review.run_review(SID))
    before = metadata.get(SID)

    # 1) malformed JSON content
    monkeypatch.setattr(review, "_TRANSPORT", _chat_transport("zero JSON here"))
    with pytest.raises(review.ReviewError):
        asyncio.run(review.run_review(SID))

    # 2) endpoint timeout
    def boom(_request):
        raise httpx.ConnectTimeout("connect timeout")

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(boom))
    with pytest.raises(review.ReviewError) as exc:
        asyncio.run(review.run_review(SID))
    assert SECRET not in str(exc.value)
    # 3) HTTP 500
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        httpx.MockTransport(lambda _r: httpx.Response(500, text=f"leak? {SECRET}")),
    )
    with pytest.raises(review.ReviewError) as exc:
        asyncio.run(review.run_review(SID))
    assert SECRET not in str(exc.value)  # status only — never the upstream body

    after = metadata.get(SID)
    assert after.reviewed_at == before.reviewed_at
    assert after.ai_summary == before.ai_summary


def test_shape_guard_rejects_missing_fields(ai_prefs, fake_jsonl, monkeypatch):
    monkeypatch.setattr(
        review, "_TRANSPORT", _chat_transport({"summary": "", "intervention_required": False})
    )
    with pytest.raises(review.ReviewError):
        asyncio.run(review.run_review(SID))
    monkeypatch.setattr(
        review, "_TRANSPORT", _chat_transport({"summary": "ok", "intervention_required": "yes"})
    )
    with pytest.raises(review.ReviewError):
        asyncio.run(review.run_review(SID))


def test_run_review_unconfigured_raises_not_configured(tmp_home, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_home / "prefs.json"))
    with pytest.raises(review.NotConfiguredError):
        asyncio.run(review.run_review(SID))


# ---- engine: /models proxy ---------------------------------------------------------------


def test_list_models_parses_caches_and_refreshes(ai_prefs, monkeypatch):
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["Authorization"] == f"Bearer {SECRET}"
        assert request.url.path.endswith("/v1/models")
        return httpx.Response(200, json={"data": [{"id": "m-b"}, {"id": "m-a"}, {"id": "m-a"}]})

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
    assert asyncio.run(review.list_models()) == ["m-a", "m-b"]
    assert asyncio.run(review.list_models()) == ["m-a", "m-b"]
    assert len(calls) == 1  # second hit served from the 60s cache
    asyncio.run(review.list_models(force=True))
    assert len(calls) == 2  # the refresh button bypasses the cache


def test_list_models_endpoint_without_models_route(ai_prefs, monkeypatch):
    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(lambda _r: httpx.Response(404)))
    with pytest.raises(review.ReviewError):
        asyncio.run(review.list_models())


# ---- routes ---------------------------------------------------------------------------


def _client(cfg):
    return TestClient(create_app(cfg), base_url="https://testserver")


def _login(c, cfg):
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return c.get("/api/config").json()["csrf"]


def test_models_route(auth_cfg, ai_prefs, monkeypatch):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        httpx.MockTransport(lambda _r: httpx.Response(200, json={"data": [{"id": "m1"}]})),
    )
    r = c.get("/api/ai-review/models")
    assert r.status_code == 200
    assert r.json() == {"models": ["m1"]}
    # Upstream without /models → 502 (the UI falls back to free-text entry).
    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(lambda _r: httpx.Response(404)))
    assert c.get("/api/ai-review/models?refresh=1").status_code == 502


def test_models_route_unconfigured_is_400(auth_cfg, tmp_home, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_home / "prefs.json"))
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/ai-review/models").status_code == 400


def test_manual_review_route_and_row_surface(auth_cfg, ai_prefs, fake_jsonl, monkeypatch):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    headers = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _chat_transport(_ok_result(title="Refit repo-a", required=True, reason="blocked")),
    )

    r = c.post(f"/api/sessions/{SID}/review", headers=headers)
    assert r.status_code == 200
    d = r.json()
    assert d["ai_summary"] == "Editing tests"
    assert d["title"] == "Refit repo-a"  # no user title → ai_title is the display title

    rows = {s["id"]: s for s in c.get("/api/sessions?limit=50").json()["sessions"]}
    row = rows[SID]
    assert row["title"] == "Refit repo-a"  # precedence: ai_title over first message
    assert row["ai_summary"] == "Editing tests"
    assert row["intervention_required"] is True
    assert row["intervention_reason"] == "blocked"
    assert row["reviewed_at"] is not None
    assert row["review_excluded"] is False
    # Search matches the DISPLAYED title (the ai_title) per the issue's search semantics.
    hits = c.get("/api/sessions?q=refit").json()["sessions"]
    assert [s["id"] for s in hits] == [SID]

    # A manual rename always wins over the AI title.
    r = c.post(f"/api/sessions/{SID}/rename", json={"title": "My name"}, headers=headers)
    assert r.status_code == 200
    rows = {s["id"]: s for s in c.get("/api/sessions?limit=50").json()["sessions"]}
    assert rows[SID]["title"] == "My name"


def test_manual_review_409_when_unconfigured(auth_cfg, fake_jsonl, tmp_home, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_home / "prefs.json"))
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        f"/api/sessions/{SID}/review",
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 409


def test_manual_review_unknown_session_404(auth_cfg, ai_prefs):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/sessions/bogus:nope/review",
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 404


def test_review_with_no_reviewable_content_is_502(auth_cfg, ai_prefs, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/sessions/claude:88888888-8888-8888-8888-888888888888/review",
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 502


def test_review_exclude_toggle(auth_cfg, ai_prefs, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    headers = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    # No body → toggle on.
    r = c.post(f"/api/sessions/{SID}/review-exclude", headers=headers)
    assert r.status_code == 200 and r.json()["review_excluded"] is True
    rows = {s["id"]: s for s in c.get("/api/sessions?limit=50").json()["sessions"]}
    assert rows[SID]["review_excluded"] is True
    # Explicit body → set.
    r = c.post(f"/api/sessions/{SID}/review-exclude", json={"excluded": False}, headers=headers)
    assert r.json()["review_excluded"] is False


def test_review_writes_follow_reconciled_alias_sidecar(
    auth_cfg, ai_prefs, fake_jsonl, opencode_db, monkeypatch
):
    """Regression (Hermes review on #367): for a reconciled opencode session whose
    title/sticky live under the PLACEHOLDER physical key (#127), Review-now and the
    exclude toggle must patch THAT sidecar — not mint a sparse logical-key entry that
    shadows it in the list read path (`get(key) or get(phys)`)."""
    from agent_sessions import webterm

    placeholder = "opencode:new-22222222-2222-2222-2222-222222222222"
    real = "opencode:ses_aaaaaaaaaaaaaaaaaaaaaaaa"  # OC_TOP seeded by opencode_db
    # Rename + pin happened while the session was still on its placeholder; reconcile
    # then recorded the alias placeholder→real.
    metadata.patch(placeholder, title="named-before-converge", sticky=True)
    metadata.set_alias(placeholder, real)

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    headers = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}

    # Exclude via the LOGICAL real id — the write must land on the placeholder sidecar.
    r = c.post(f"/api/sessions/{real}/review-exclude", json={"excluded": True}, headers=headers)
    assert r.status_code == 200 and r.json()["review_excluded"] is True
    rows = {s["id"]: s for s in c.get("/api/sessions?engine=opencode&limit=50").json()["sessions"]}
    assert rows[real]["review_excluded"] is True
    assert rows[real]["title"] == "named-before-converge"  # NOT shadowed by a sparse entry
    assert rows[real]["sticky"] is True

    # Review-now via the real id: the live tail lives under the placeholder (physical)
    # key, and the persisted ai_* fields must land on the same sidecar as the title.
    webterm._buffer_append(placeholder, b"opencode is doing things\r\n")
    monkeypatch.setattr(review, "_TRANSPORT", _chat_transport(_ok_result(title="AI title")))
    r = c.post(f"/api/sessions/{real}/review", headers=headers)
    assert r.status_code == 200
    assert r.json()["title"] == "named-before-converge"  # user title still wins

    rows = {s["id"]: s for s in c.get("/api/sessions?engine=opencode&limit=50").json()["sessions"]}
    assert rows[real]["ai_summary"] == "Editing tests"
    assert rows[real]["title"] == "named-before-converge"
    assert rows[real]["sticky"] is True
    # Single source of truth: everything stayed on the placeholder sidecar — no
    # logical-key entry was created at all.
    idx = metadata.load()
    assert real not in idx
    assert idx[placeholder].review_excluded is True
    assert idx[placeholder].ai_summary == "Editing tests"
