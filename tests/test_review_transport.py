"""The chat-completions transport's request shape and failure taxonomy (#841).

Covers the thinking opt-out and its ORDERED degrade, the refusal memo (including what it must
NOT conclude), truncation detection, and the timeout-vs-unreachable split — on BOTH response
paths, because `run_review` and `complete_json` classify independently and `complete_json`
serves nine call sites (the orchestrator pass, Pulse/Ask, handoff, autosort, the recap).

Everything runs against httpx.MockTransport; CI never touches the network.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agent_sessions import prefs, review

SECRET = "sk-transport-secret-4242"  # noqa: S105 — test fixture value
BASE = "https://ai.test/v1"
SID = "claude:11111111-1111-1111-1111-111111111111"  # exists in fake_jsonl

# A payload larger than the ~63-token review object — the shape the orchestrator pass,
# Pulse/Ask and handoff actually return, so the transport is not only exercised against the
# smallest contract in the app.
BIG_CONTRACT = {
    "assessment": "three sessions idle, one blocked on a permission prompt",
    "actions": [{"id": f"a{i}", "verb": "continue", "confidence": 0.8} for i in range(6)],
    "matches": [{"id": f"claude:{i}", "why": "recent work on the same repo"} for i in range(4)],
}


@pytest.fixture(autouse=True)
def _reset_transport(monkeypatch):
    monkeypatch.setattr(review, "_TRANSPORT", None)
    review._thinking_refused.clear()
    yield
    review._thinking_refused.clear()


@pytest.fixture
def ai_prefs(tmp_home, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_home / "prefs.json"))
    prefs.set_ai_review({"base_url": BASE, "api_key": SECRET, "model": "test-model"})
    return tmp_home


def _json_response(content: object, *, finish="stop", status=200) -> httpx.Response:
    body = content if isinstance(content, str) else json.dumps(content)
    return httpx.Response(
        status,
        json={
            "choices": [
                {"message": {"content": body}},
            ]
        }
        if finish is None
        else {"choices": [{"message": {"content": body}, "finish_reason": finish}]},
    )


def _recording(handler, sent: list[dict]):
    def wrapped(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return handler(request, len(sent))

    return httpx.MockTransport(wrapped)


def _thinking_off(payload: dict) -> bool:
    return (payload.get("chat_template_kwargs") or {}).get("enable_thinking") is False


async def _complete(messages=None, **kw):
    return await review.complete_json(messages or [{"role": "user", "content": "hi"}], **kw)


# ---- the opt-out is actually sent -------------------------------------------------------


def test_complete_json_sends_the_thinking_opt_out(ai_prefs, monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(
        review, "_TRANSPORT", _recording(lambda _r, _n: _json_response(BIG_CONTRACT), sent)
    )
    assert asyncio.run(_complete()) == BIG_CONTRACT
    assert len(sent) == 1, "a cooperating endpoint must cost exactly one call"
    assert _thinking_off(sent[0])
    # The opt-out is a request field only — it must not disturb the messages array, which is
    # what the prompt-registry guarantee stands on.
    assert sent[0]["messages"] == [{"role": "user", "content": "hi"}]


def test_run_review_sends_the_thinking_opt_out(ai_prefs, fake_jsonl, monkeypatch):
    sent: list[dict] = []
    ok = {"summary": "s", "title": "t", "intervention_required": False, "reason": ""}
    monkeypatch.setattr(review, "_TRANSPORT", _recording(lambda _r, _n: _json_response(ok), sent))
    asyncio.run(review.run_review(SID))
    assert sent and _thinking_off(sent[0])


# ---- the ordered degrade ----------------------------------------------------------------


def test_degrade_drops_thinking_before_json_mode(ai_prefs, monkeypatch):
    """A 400 names no field, so the ladder must isolate one at a time — thinking first."""
    sent: list[dict] = []

    def handler(_request, n):
        return _json_response(BIG_CONTRACT) if n >= 2 else httpx.Response(400, text="bad field")

    monkeypatch.setattr(review, "_TRANSPORT", _recording(handler, sent))
    assert asyncio.run(_complete()) == BIG_CONTRACT
    assert len(sent) == 2
    assert _thinking_off(sent[0]) and "response_format" in sent[0]
    # Attempt 2 drops ONLY the thinking option; json mode is still being asked for.
    assert "chat_template_kwargs" not in sent[1]
    assert "response_format" in sent[1], "json mode must not be dropped in the same step"
    # Having been isolated, the refusal is now remembered.
    assert len(review._thinking_refused) == 1


def test_json_mode_dropped_last_and_thinking_refusal_not_inferred(ai_prefs, monkeypatch):
    """Both fields refused: the third attempt drops json mode — and the memo learns NOTHING
    about the thinking option, because it was absent from the request that failed."""
    sent: list[dict] = []

    def handler(_request, n):
        return _json_response(BIG_CONTRACT) if n >= 3 else httpx.Response(400, text="nope")

    monkeypatch.setattr(review, "_TRANSPORT", _recording(handler, sent))
    assert asyncio.run(_complete()) == BIG_CONTRACT
    assert len(sent) == 3
    assert _thinking_off(sent[0])
    assert "chat_template_kwargs" not in sent[1] and "response_format" in sent[1]
    assert "chat_template_kwargs" not in sent[2] and "response_format" not in sent[2]
    assert review._thinking_refused == set(), (
        "a refusal of a request that did NOT carry the thinking option proves nothing about "
        "it — recording one would disable the opt-out against an endpoint that never objected"
    )


def test_a_silently_ignoring_endpoint_still_completes(ai_prefs, monkeypatch):
    """The fallback this design exists to protect: a 200 that ignores the field. Nothing is
    inferred from it, so the next call still carries the opt-out and still works."""
    sent: list[dict] = []
    monkeypatch.setattr(
        review, "_TRANSPORT", _recording(lambda _r, _n: _json_response(BIG_CONTRACT), sent)
    )
    assert asyncio.run(_complete()) == BIG_CONTRACT
    assert asyncio.run(_complete()) == BIG_CONTRACT
    assert review._thinking_refused == set()
    assert all(_thinking_off(p) for p in sent), "a 200 must not be read as acceptance either"


# ---- the memo ---------------------------------------------------------------------------


def test_memo_suppresses_reprobing(ai_prefs, monkeypatch):
    sent: list[dict] = []

    def handler(request, n):
        payload = json.loads(request.content)
        if _thinking_off(payload):
            return httpx.Response(400, text="unknown field chat_template_kwargs")
        return _json_response(BIG_CONTRACT)

    monkeypatch.setattr(review, "_TRANSPORT", _recording(handler, sent))
    asyncio.run(_complete())
    assert len(sent) == 2, "first call probes"
    asyncio.run(_complete())
    assert len(sent) == 3, "second call must go straight through, without re-probing"
    assert not _thinking_off(sent[2])


@pytest.mark.parametrize(
    "patch",
    [
        pytest.param({"model": "other-model"}, id="model"),
        # Another host needs its own key: the stored one is only sent where it was saved (#956).
        pytest.param(
            {"base_url": "https://elsewhere.test/v1", "api_key": "sk-elsewhere"}, id="base_url"
        ),
    ],
)
def test_memo_is_invalidated_by_config_change(ai_prefs, monkeypatch, patch):
    sent: list[dict] = []

    def handler(request, n):
        payload = json.loads(request.content)
        if _thinking_off(payload):
            return httpx.Response(400, text="unknown field")
        return _json_response(BIG_CONTRACT)

    monkeypatch.setattr(review, "_TRANSPORT", _recording(handler, sent))
    asyncio.run(_complete())
    assert not _thinking_off(sent[-1])
    prefs.set_ai_review(patch)
    asyncio.run(_complete())
    changed = next(iter(patch))
    assert _thinking_off(sent[2]), f"a changed {changed} must re-probe, not inherit the verdict"


def test_model_override_does_not_poison_the_configured_model(ai_prefs, monkeypatch):
    """`complete_json(model=...)` addresses a different backend, so its refusal must not
    suppress the field for the configured model — nor read that model's entry."""
    sent: list[dict] = []

    def handler(request, n):
        payload = json.loads(request.content)
        if payload["model"] == "picky-model" and _thinking_off(payload):
            return httpx.Response(400, text="unknown field")
        return _json_response(BIG_CONTRACT)

    monkeypatch.setattr(review, "_TRANSPORT", _recording(handler, sent))
    asyncio.run(_complete(model="picky-model"))
    assert not _thinking_off(sent[-1])
    asyncio.run(_complete())  # the CONFIGURED model — untouched by the override's refusal
    assert sent[-1]["model"] == "test-model"
    assert _thinking_off(sent[-1])


# ---- truncation -------------------------------------------------------------------------


def test_truncation_raises_even_when_the_partial_parses(ai_prefs, monkeypatch):
    """The dangerous case: a cap stops the generation on a boundary that still yields valid
    JSON. Checking only for empty content would persist that partial as a real answer."""
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        httpx.MockTransport(lambda _r: _json_response({"answer": "partial"}, finish="length")),
    )
    with pytest.raises(review.ReviewError, match="truncated"):
        asyncio.run(_complete())


def test_a_truncated_review_preserves_complete_core_fields(ai_prefs, fake_jsonl, monkeypatch):
    """#1020 salvages the preview contract, never a partial assessment or an extra retry."""
    ok = {"summary": "s", "title": "t", "intervention_required": False, "reason": ""}
    sent: list[dict] = []
    monkeypatch.setattr(
        review, "_TRANSPORT", _recording(lambda _r, _n: _json_response(ok, finish="length"), sent)
    )
    result = asyncio.run(review.run_review(SID))
    assert result["ai_summary"] == "s" and result["ai_title"] == "t"
    assert result["intervention_required"] is False
    assert result["assessment"]["status"] == "missing"
    assert len(sent) == 2, "one review and one independent recap attempt; no repair retry"


def test_a_truncated_review_with_incomplete_core_still_raises(ai_prefs, fake_jsonl, monkeypatch):
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        httpx.MockTransport(lambda _r: _json_response('{"summary": "cut', finish="length")),
    )
    with pytest.raises(review.ReviewError, match="truncated"):
        asyncio.run(review.run_review(SID))


def test_a_normal_finish_reason_is_untouched(ai_prefs, monkeypatch):
    monkeypatch.setattr(
        review, "_TRANSPORT", httpx.MockTransport(lambda _r: _json_response(BIG_CONTRACT))
    )
    assert asyncio.run(_complete()) == BIG_CONTRACT


# ---- the failure taxonomy ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (httpx.ReadTimeout("read"), "did not answer within"),
        (httpx.WriteTimeout("write"), "did not answer within"),
        (httpx.PoolTimeout("pool"), "did not answer within"),
        (httpx.ConnectTimeout("connect"), "did not answer within"),
        (httpx.ConnectError("refused"), "unreachable"),
    ],
)
def test_timeout_is_not_reported_as_unreachable(ai_prefs, monkeypatch, exc, expected):
    """`endpoint unreachable (ReadTimeout)` is what this module used to say while the endpoint
    was answering every request it was given (#841). ConnectTimeout counts as a timeout too —
    it is an httpx.TimeoutException, and matching the base class is the point."""

    def boom(_request):
        raise exc

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(boom))
    with pytest.raises(review.ReviewError) as err:
        asyncio.run(_complete())
    assert expected in str(err.value)
    assert SECRET not in str(err.value)


def test_review_path_shares_the_taxonomy(ai_prefs, fake_jsonl, monkeypatch):
    def boom(_request):
        raise httpx.ReadTimeout("read")

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(boom))
    with pytest.raises(review.ReviewError) as err:
        asyncio.run(review.run_review(SID))
    assert "did not answer within" in str(err.value)
    assert "unreachable" not in str(err.value)
    assert SECRET not in str(err.value)
