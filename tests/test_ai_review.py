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


def _summary_recap_transport(*, summary=None, recap=None, recap_status=200, calls=None):
    """A transport that answers the tail-summary call and the whole-session recap call (#481)
    DIFFERENTLY — keyed on the recap system prompt. ``recap=None`` makes the recap call return
    non-JSON so its shape guard fails (exercising the fail-soft drop)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        assert request.headers["Authorization"] == f"Bearer {SECRET}"
        payload = json.loads(request.content)
        system = payload["messages"][0]["content"]
        if "returning to a coding-agent session" in system:  # the recap prompt
            content = json.dumps({"recap": recap}) if recap is not None else "not json at all"
            return httpx.Response(
                recap_status, json={"choices": [{"message": {"content": content}}]}
            )
        content = json.dumps(summary if summary is not None else _ok_result())
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

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
    assert "## Terminal screen" in text
    assert "running pytest" in text
    assert "\x1b" not in text  # escapes consumed by the narrow accessor


def test_gather_input_missing_everything_raises(ai_prefs, fake_jsonl):
    with pytest.raises(review.ReviewError):
        review.gather_input("claude:88888888-8888-8888-8888-888888888888", 24000)


def test_gather_input_tail_truncates_and_fingerprint_tracks_content(ai_prefs, fake_jsonl):
    # The `## Session` header (#611) sits outside `max_input_chars` — it carries a last-output
    # age, so folding it into the budget would drift the body's cut point (and its fingerprint)
    # with the clock. The payload stays bounded by cap + SESSION_CONTEXT_MAX.
    budget = 1_000  # prefs.AI_REVIEW_INPUT_CHARS_MIN — below it the screen loses its heading
    text, fp1 = review.gather_input(SID, budget)
    assert len(text) <= budget + review.SESSION_CONTEXT_MAX
    webterm._buffer_append(SID, b"new live bytes change the fingerprint\r\n")
    _, fp2 = review.gather_input(SID, budget)
    assert fp1 != fp2


# ---- engine: pending-draft framing (#560) ------------------------------------------------


def test_gather_input_frames_live_tail_as_pending(ai_prefs, fake_jsonl):
    # The live terminal tail carries the agent's still-being-typed input line; it must be labeled
    # UNSENT/PENDING so the model never reads a queued instruction as already completed.
    webterm._buffer_append(LIVE_ONLY_SID, b"do the deploy step\r\n")
    text, _ = review.gather_input(LIVE_ONLY_SID, 24000)
    assert "## Terminal screen" in text
    assert "rendered snapshot" in text
    assert "NOT been submitted" in text and "PENDING" in text


def test_gather_input_includes_unsent_compose_draft(ai_prefs, fake_jsonl):
    # Phase 2: the app's compose-box draft (#477 SessionMeta.draft) is folded in, clearly marked
    # not-yet-sent — reusing the existing metadata contract, no new write path. Attachment NAMES
    # only, never the stored path/blob.
    metadata.patch(
        SID, draft={"text": "do this", "attachments": [{"name": "shot.png", "path": "/uploads/x"}]}
    )
    text, _ = review.gather_input(SID, 24000)
    assert "## Pending draft (UNSENT" in text
    assert "do this" in text
    assert "shot.png" in text
    assert "/uploads/x" not in text  # the sanitized path is not leaked into the review input


def test_compose_draft_change_moves_the_fingerprint(ai_prefs, fake_jsonl):
    # The draft is deliberately part of the review input (#560), so editing it re-triggers a
    # scheduled review — the one intentional exception to "metadata-only writes don't move the fp".
    _, fp0 = review.gather_input(SID, 24000)
    metadata.patch(SID, draft={"text": "do this", "attachments": []})
    _, fp1 = review.gather_input(SID, 24000)
    assert fp0 != fp1
    metadata.patch(SID, draft={"text": "do this differently", "attachments": []})
    _, fp2 = review.gather_input(SID, 24000)
    assert fp1 != fp2


def test_empty_draft_adds_no_section_and_does_not_move_fingerprint(ai_prefs, fake_jsonl):
    _, fp0 = review.gather_input(SID, 24000)
    metadata.patch(SID, draft={"text": "   ", "attachments": []})  # whitespace-only = empty
    text, fp1 = review.gather_input(SID, 24000)
    assert "Pending draft" not in text
    assert fp0 == fp1


def test_gather_recap_frames_pending_and_includes_draft(ai_prefs, fake_jsonl):
    # Recap parity: the whole-session recap also gets the pending framing + draft, so it never
    # claims an unsent instruction as completed history.
    webterm._buffer_append(SID, b"partial typed command\r\n")
    metadata.patch(SID, draft={"text": "do this", "attachments": []})
    text, _ = review.gather_recap_input(SID, 24000)
    assert "## Terminal screen" in text and "NOT been submitted" in text
    assert "## Pending draft (UNSENT" in text and "do this" in text


def test_draft_alone_is_not_reviewable(ai_prefs, fake_jsonl):
    # A draft with no transcript and no live output is supplementary, not a session to review.
    sid = "claude:77777777-7777-7777-7777-777777777777"
    metadata.patch(sid, draft={"text": "do this", "attachments": []})
    with pytest.raises(review.ReviewError):
        review.gather_input(sid, 24000)


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


def test_list_models_error_carries_the_gateway_text(ai_prefs, monkeypatch):
    """#382: a failed validation probe surfaces the gateway's own error message
    (OpenAI/LiteLLM ``error.message`` shape) so Settings can show WHY, verbatim."""
    gw = "Authentication Error - LiteLLM Virtual Key expected. Received=hx7Kp."
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        httpx.MockTransport(
            lambda _r: httpx.Response(401, json={"error": {"message": gw, "code": "401"}})
        ),
    )
    with pytest.raises(review.ReviewError) as exc:
        asyncio.run(review.list_models())
    assert str(exc.value) == f"model listing returned HTTP 401: {gw}"


def test_list_models_error_text_is_bounded_and_key_redacted(ai_prefs, monkeypatch):
    """The extract is whitespace-collapsed, capped, and never echoes the API key even
    when a hostile/echoing gateway reflects the Authorization header back."""
    noisy = ("x" * 1000) + f"  leak? {SECRET}  \n\n tail"
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        httpx.MockTransport(lambda _r: httpx.Response(500, text=noisy)),
    )
    with pytest.raises(review.ReviewError) as exc:
        asyncio.run(review.list_models())
    msg = str(exc.value)
    assert SECRET not in msg
    assert len(msg) <= len("model listing returned HTTP 500: ") + review.GATEWAY_ERROR_MAX


def test_list_models_error_without_body_stays_status_only(ai_prefs, monkeypatch):
    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(lambda _r: httpx.Response(503)))
    with pytest.raises(review.ReviewError) as exc:
        asyncio.run(review.list_models())
    assert str(exc.value) == "model listing returned HTTP 503"


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


def test_review_timeout_env_tunable(monkeypatch):
    # #391: 30s aborted every real review on slow local models; the timeout is now
    # env-tunable with a sane default and a floor against typos.
    monkeypatch.setenv("AGENT_SESSIONS_AI_REVIEW_TIMEOUT", "300")
    assert review._timeout_env("AGENT_SESSIONS_AI_REVIEW_TIMEOUT", 120.0) == 300.0
    monkeypatch.setenv("AGENT_SESSIONS_AI_REVIEW_TIMEOUT", "0")
    assert review._timeout_env("AGENT_SESSIONS_AI_REVIEW_TIMEOUT", 120.0) == 10.0
    monkeypatch.setenv("AGENT_SESSIONS_AI_REVIEW_TIMEOUT", "garbage")
    assert review._timeout_env("AGENT_SESSIONS_AI_REVIEW_TIMEOUT", 120.0) == 120.0


def test_request_timeout_precedence_pref_env_default(ai_prefs, monkeypatch):
    # #391 follow-up (Settings field): pref > env > 120 default, floored at 10 everywhere,
    # resolved per call so a Settings change applies to the next review without restart.
    monkeypatch.delenv("AGENT_SESSIONS_AI_REVIEW_TIMEOUT", raising=False)
    assert review.request_timeout() == 120.0  # nothing set → default

    monkeypatch.setenv("AGENT_SESSIONS_AI_REVIEW_TIMEOUT", "300")
    assert review.request_timeout() == 300.0  # env beats default

    prefs.set_ai_review({"request_timeout": 240})
    assert review.request_timeout() == 240.0  # pref beats env

    prefs.set_ai_review({"request_timeout": None})  # explicit unset → back to env
    assert review.request_timeout() == 300.0

    # The floor holds even against a raw cfg dict (defense in depth — prefs validation
    # already rejects < 10 over the API).
    assert review.request_timeout({"request_timeout": 3}) == 10.0
    monkeypatch.setenv("AGENT_SESSIONS_AI_REVIEW_TIMEOUT", "2")
    assert review.request_timeout({"request_timeout": None}) == 10.0


def test_run_review_uses_pref_timeout_per_call(ai_prefs, fake_jsonl, monkeypatch):
    # The completion call must pick up the Settings value at call time.
    seen: list[float] = []
    real_client = review._client

    def spy(timeout: float):
        seen.append(timeout)
        return real_client(timeout)

    monkeypatch.setattr(review, "_client", spy)
    monkeypatch.setattr(review, "_TRANSPORT", _chat_transport(_ok_result()))
    prefs.set_ai_review({"request_timeout": 333})
    asyncio.run(review.run_review(SID))
    # run_review now makes TWO completion calls — the tail summary then the whole-session
    # recap (#481) — and each must pick up the per-call Settings timeout.
    assert seen == [333.0, 333.0]


# ---- engine: chronological recap (#481) --------------------------------------------------


def test_head_tail_sample_keeps_bookends_and_bounds():
    text = "HEAD" + ("x" * 1000) + "TAIL"
    out = review._head_tail_sample(text, 80)
    assert out.startswith("HEAD") and out.endswith("TAIL")
    assert "elided" in out
    assert len(out) == 80
    # Under the cap the text is returned verbatim.
    assert review._head_tail_sample("short", 80) == "short"


def test_gather_recap_input_uses_whole_transcript(ai_prefs, fake_jsonl):
    text, fp = review.gather_recap_input(SID, 24000)
    assert "## Transcript (full)" in text
    assert "first message on repo-a" in text
    assert len(fp) == 64


def test_gather_recap_input_bounds_over_cap(ai_prefs, fake_jsonl):
    # The recap head+tail-samples the TRANSCRIPT (not the joined body — eliding the middle of
    # the joined text would drop the screen's and the draft's headings). So the elision marker
    # appears when the transcript itself is over budget.
    big = ai_prefs / ".claude" / "projects" / "-home-user-claude-repo-a"
    (big / "11111111-1111-1111-1111-111111111111.jsonl").write_text(
        "".join(
            json.dumps({"type": "user", "message": {"content": f"msg {i} " + "y" * 200}}) + "\n"
            for i in range(60)
        )
    )
    webterm._buffer_append(SID, b"screen line\r\n")
    text, _ = review.gather_recap_input(SID, 2_000)
    assert len(text) <= 2_000 + review.SESSION_CONTEXT_MAX
    assert "elided" in text
    assert "## Transcript (full)" in text and "## Terminal screen" in text


def test_run_review_generates_recap(ai_prefs, fake_jsonl, monkeypatch):
    monkeypatch.setattr(
        review, "_TRANSPORT", _summary_recap_transport(recap="Cloned repo.\nFixed the bug.")
    )
    out = asyncio.run(review.run_review(SID))
    assert out["ai_recap"] == "Cloned repo.\nFixed the bug."
    assert out["recap_fingerprint"]
    m = metadata.get(SID)
    assert m.ai_recap == "Cloned repo.\nFixed the bug."
    assert m.recap_fingerprint


# ---- #744: the recap is rendered as a numbered <ol>, so the SERVER strips any ordinal the
# model prepends — otherwise the brief reads "1. 1. Cloned the repo."


@pytest.mark.parametrize(
    "raw, want",
    [
        ("1. Cloned the repo.\n2. Fixed the bug.", "Cloned the repo.\nFixed the bug."),
        ("1) Cloned the repo.\n2) Fixed the bug.", "Cloned the repo.\nFixed the bug."),
        ("(1) Cloned the repo.", "Cloned the repo."),
        ("- Cloned the repo.\n* Fixed the bug.", "Cloned the repo.\nFixed the bug."),
        ("• Cloned the repo.\n▪ Fixed the bug.", "Cloned the repo.\nFixed the bug."),
        ("+ Cloned the repo.", "Cloned the repo."),
    ],
)
def test_recap_shape_guard_strips_leading_ordinals(raw, want):
    assert review._recap_shape_guard({"recap": raw}) == want


@pytest.mark.parametrize(
    "raw",
    [
        "Made it 3.5x faster.",  # a decimal mid-sentence is not an ordinal
        "-Wall was already set.",  # a bullet glyph with no following space is not a bullet
        "Ran 2 tests.",
        "1.Cloned the repo.",  # no space → not the numbered-list shape the prompt forbids
    ],
)
def test_recap_shape_guard_leaves_prose_alone(raw):
    assert review._recap_shape_guard({"recap": raw}) == raw


def test_recap_shape_guard_keeps_the_inline_emphasis_subset():
    # `**bold**` + backticks are what the client's inlineMarkup renders; the guard must not eat
    # them while collapsing whitespace.
    raw = "**Root-caused**  the   race in `auth/refresh.ts`."
    assert review._recap_shape_guard({"recap": raw}) == (
        "**Root-caused** the race in `auth/refresh.ts`."
    )


def test_recap_shape_guard_strip_can_never_blank_a_recap():
    # The strip runs AFTER whitespace collapse, and the marker pattern needs a space after the
    # glyph — so a content-free "- " has already become "-" and survives. That ordering is what
    # keeps the guard from writing "" over a previously good recap.
    assert review._recap_shape_guard({"recap": "- \n* \n1. "}) == "-\n*\n1."


def test_run_review_recap_failure_is_fail_soft(ai_prefs, fake_jsonl, monkeypatch):
    # Seed a prior good recap, then make ONLY the recap call fail (the summary still succeeds).
    rk = metadata.resolve_key(SID)
    metadata.patch(rk, ai_recap="PRIOR RECAP", recap_fingerprint="prior-fp")
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _summary_recap_transport(summary=_ok_result(summary="Fresh summary"), recap=None),
    )
    out = asyncio.run(review.run_review(SID))  # must NOT raise
    # The summary/intervention write landed; the recap write is INDEPENDENT and left the last
    # good recap untouched — no partial rollback couples the two.
    assert out["ai_summary"] == "Fresh summary"
    m = metadata.get(SID)
    assert m.ai_summary == "Fresh summary"
    assert m.ai_recap == "PRIOR RECAP"


def test_run_review_recap_skips_when_unchanged(ai_prefs, fake_jsonl, monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        review, "_TRANSPORT", _summary_recap_transport(recap="One step.", calls=calls)
    )
    asyncio.run(review.run_review(SID))
    calls.clear()
    # Second pass over the SAME whole-session content → recap fingerprint unchanged → the recap
    # call is skipped (only the tail summary call fires).
    asyncio.run(review.run_review(SID))
    recap_calls = [
        c
        for c in calls
        if "returning to a coding-agent session" in json.loads(c.content)["messages"][0]["content"]
    ]
    assert recap_calls == []


# ---- #611: the reviewer's input --------------------------------------------------------


def _codex_rollout(home, real_uuid: str, text: str) -> None:
    """Lay down a codex rollout JSONL the transcript adapter can find."""
    d = home / ".codex" / "sessions" / "2026" / "07" / "10"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"rollout-2026-07-10T07-29-08-{real_uuid}.jsonl").write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": [{"text": text}]},
            }
        )
        + "\n"
    )


def test_reconciled_codex_session_reviews_its_real_transcript(ai_prefs, fake_jsonl):
    """The bug: a codex/opencode/antigravity session created in the app keeps its
    `new-<uuid>` placeholder as its physical key forever. `parse_key` rejects that shape, so
    `_plain_transcript` swallowed the error and the reviewer saw an EMPTY transcript for the
    session's whole life — reviewing nothing but terminal bytes."""
    real = "019f4a49-57a9-76c0-b3d4-6476f4aceef5"
    placeholder = "codex:new-d63b0fd4-6043-4f59-8b4b-54b904ce7414"
    _codex_rollout(ai_prefs, real, "audit the dependencies")
    metadata.set_alias(placeholder, f"codex:{real}")

    text, _ = review.gather_input(placeholder, 24000)
    assert "audit the dependencies" in text
    assert "## Transcript (tail)" in text
    # The recap must resolve identically — Hermes asked for this in P1's acceptance, not P8.
    recap, _ = review.gather_recap_input(placeholder, 24000)
    assert "audit the dependencies" in recap


def test_unreconciled_placeholder_still_reviews_live_only(ai_prefs, fake_jsonl):
    # No alias yet (the session just launched): no transcript, but the live screen still is one.
    placeholder = "codex:new-aaaaaaaa-1111-2222-3333-444444444444"
    webterm._buffer_append(placeholder, b"booting codex\r\n")
    text, _ = review.gather_input(placeholder, 24000)
    assert "booting codex" in text
    assert "## Transcript" not in text


def test_session_context_names_the_engine_and_grounds_idleness(ai_prefs, fake_jsonl):
    webterm._buffer_append(LIVE_ONLY_SID, b"compiling\r\n")
    text, _ = review.gather_input(LIVE_ONLY_SID, 24000)
    assert text.startswith("## Session")
    assert "- agent: claude" in text
    assert "RUNNING" in text  # bytes just landed — the model is told so explicitly


def test_session_context_is_bounded_and_outside_the_fingerprint(ai_prefs, fake_jsonl):
    """The header carries a last-output AGE. Hashing it would mark every session changed on
    every sweep and re-review the whole registry forever."""
    webterm._buffer_append(LIVE_ONLY_SID, b"work\r\n")
    text, fp1 = review.gather_input(LIVE_ONLY_SID, 24000)
    header = text.split("\n\n", 1)[0]
    assert len(header) <= review.SESSION_CONTEXT_MAX

    # Advance only the clock: the header's rendered age changes, the fingerprint must not.
    real_time = review.time.time
    try:
        review.time.time = lambda: real_time() + 3600  # noqa: ARG005
        text2, fp2 = review.gather_input(LIVE_ONLY_SID, 24000)
    finally:
        review.time.time = real_time
    assert fp1 == fp2
    assert text != text2  # the model still sees the fresh age


def test_fingerprint_ignores_cursor_only_repaints_but_tracks_visible_text(ai_prefs, fake_jsonl):
    sid = "claude:55555555-5555-5555-5555-555555555555"
    webterm.scrollback._LAST_COLS[sid] = 80
    webterm.scrollback._LAST_ROWS[sid] = 24
    webterm._buffer_append(sid, b"\x1b[1;1Hbuilding\x1b[0m")
    _, fp1 = review.gather_input(sid, 24000)

    # Pure chrome: colour + cursor parking. Nothing a human would see changes.
    webterm._buffer_append(sid, b"\x1b[0m\x1b[?25l\x1b[1;1H\x1b[39m")
    _, fp2 = review.gather_input(sid, 24000)
    assert fp1 == fp2

    # A permission prompt appearing on screen MUST move it — that's what the ⚠ badge is for.
    webterm._buffer_append(sid, b"\x1b[3;1HAllow this command? (y/n)")
    _, fp3 = review.gather_input(sid, 24000)
    assert fp3 != fp1


def test_recap_budget_is_never_narrower_than_the_review_budget():
    assert review.recap_input_chars({"max_input_chars": 40_000}) == 40_000
    assert review.recap_input_chars({"max_input_chars": 1_000}) == review.RECAP_INPUT_CHARS
    assert review.recap_input_chars({}) == review.RECAP_INPUT_CHARS


def test_over_budget_transcript_keeps_its_heading(ai_prefs, fake_jsonl):
    """Truncating the JOINED body sheared off `## Transcript (tail)` on any session over
    budget, so the model got an unlabeled wall of text it could not tell apart from the
    terminal screen. Trim the transcript's own tail instead."""
    sid = "claude:11111111-1111-1111-1111-111111111111"
    webterm._buffer_append(sid, b"live screen line\r\n" + b"noise\r\n" * 500)
    budget = 1_000  # prefs.AI_REVIEW_INPUT_CHARS_MIN — the tightest a user can set
    text, _ = review.gather_input(sid, budget)
    body = text.split("\n\n", 1)[1]
    assert body.startswith("## Transcript (tail)")
    assert "## Terminal screen" in text
    assert len(body) <= budget


def test_screen_never_crowds_out_the_transcript():
    # The screen SECTION (heading included) never exceeds half the payload, so the transcript
    # always keeps a share. Below the pref floor the section cannot carry its own heading and is
    # dropped whole — degenerate, unreachable through Settings, and never unlabeled.
    assert review._screen_budget(24_000) == review.LIVE_TAIL_CHARS
    hdr = len(review._LIVE_TAIL_SECTION)
    assert review._screen_budget(1_000) == 1_000 // 2 - hdr
    assert hdr + review._screen_budget(1_000) <= 1_000 // 2
    assert review._screen_budget(2 * hdr) == 0
    assert review._screen_budget(0) == 0


# ---- Hermes on PR #618: a long draft must never strip the section labels ------------------


def test_long_draft_cannot_crowd_out_or_unlabel_any_section(ai_prefs, fake_jsonl):
    """A 2k pending draft against the 1 000-char pref floor filled the body; tail-truncating the
    joined text then sheared off EVERY heading — including the draft's own "UNSENT" label. Unsent
    text arriving unlabeled is precisely what #560's label exists to prevent."""
    sid = "claude:11111111-1111-1111-1111-111111111111"
    webterm._buffer_append(sid, b"terminal output line\r\n")
    metadata.patch(sid, draft={"text": "D" * 2000, "attachments": []})
    budget = 1_000  # prefs.AI_REVIEW_INPUT_CHARS_MIN

    text, _ = review.gather_input(sid, budget)
    body = text.split("\n\n", 1)[1]
    assert len(body) <= budget
    assert "## Pending draft (UNSENT" in body
    assert "## Terminal screen" in body
    assert "## Transcript (tail)" in body
    # The draft's content is trimmed, never its label, and it never swallows the whole body.
    assert body.index("## Transcript (tail)") < body.index("## Pending draft (UNSENT")


def test_recap_keeps_every_heading_under_a_tight_budget(ai_prefs, fake_jsonl):
    sid = "claude:11111111-1111-1111-1111-111111111111"
    webterm._buffer_append(sid, b"screen line\r\n")
    metadata.patch(sid, draft={"text": "D" * 2000, "attachments": []})
    text, _ = review.gather_recap_input(sid, 1_000)
    body = text.split("\n\n", 1)[1]
    assert len(body) <= 1_000
    assert "## Terminal screen" in body and "## Pending draft (UNSENT" in body


def test_draft_section_is_dropped_whole_rather_than_unlabeled(ai_prefs, fake_jsonl):
    # Budget too small to carry the draft's own heading → no draft section at all, never a
    # naked fragment of the user's unsent text.
    assert review._draft_budget(100) == 0
    assert review._pending_draft_section("claude:nope", 0) == ""


# ---- #636 shell "terminal as agent": reviewed on the live screen, no transcript --------------

SHELL_SID = "shell:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_gather_input_shell_reviews_on_screen_only(ai_prefs, fake_jsonl):
    # The core claim (#636): a shell registers NO transcript adapter, so _plain_transcript is
    # empty and the reviewer runs on the live SCREEN alone — and the Session header names the
    # engine so the model knows it is reviewing a plain terminal.
    webterm._buffer_append(SHELL_SID, b"$ pytest -q\r\n42 passed\r\n")
    text, fp = review.gather_input(SHELL_SID, 24000)
    assert "## Terminal screen" in text
    assert "42 passed" in text
    assert "## Transcript (tail)" not in text  # shell has no transcript adapter
    assert "- agent: shell" in text
    assert len(fp) == 64


def test_gather_input_shell_with_no_screen_still_raises(ai_prefs, fake_jsonl):
    # The screen-only path must not regress into "always reviewable": no transcript AND no screen
    # still raises, exactly like any other engine.
    with pytest.raises(review.ReviewError):
        review.gather_input("shell:11111111-2222-3333-4444-555555555555", 24000)
