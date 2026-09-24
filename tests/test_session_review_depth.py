"""Session review depth (#1086 Phase 2): the `session_review` prefs block, and what the standalone
decision pass reads because of it — the recap's CURRENT-STATE line, the review's reason, the
screen's prompt and menu, and (Deep) a bounded transcript tail and earlier outcomes."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agent_sessions import orchestrator, prefs, review
from agent_sessions.main import create_app

NOW = 1_800_000_000.0

# ---- the prefs block ---------------------------------------------------------------------------


def test_defaults_are_recognise_on_and_standard_context(tmp_path):
    assert prefs.get_session_review(tmp_path / "none.json") == {
        "recognise_prompts": True,
        "decision_context": "standard",
    }


def test_writes_are_strict_and_unknown_keys_refused():
    ok = prefs.validate_session_review_patch
    assert ok({"recognise_prompts": False}) is None
    assert ok({"decision_context": "deep"}) is None
    assert ok({"recognise_prompts": "yes"}) is not None
    assert ok({"recognise_prompts": 1}) is not None
    assert ok({"decision_context": "extreme"}) is not None
    assert ok({"bogus": True}) is not None
    assert ok(["decision_context"]) is not None


def test_reads_are_lenient_and_a_partial_write_keeps_the_other_field(tmp_path):
    path = tmp_path / "prefs.json"
    path.write_text(
        json.dumps({"session_review": {"recognise_prompts": "x", "decision_context": 3}})
    )
    assert prefs.get_session_review(path) == {
        "recognise_prompts": True,
        "decision_context": "standard",
    }
    prefs.set_session_review({"decision_context": "deep"}, path)
    prefs.set_session_review({"recognise_prompts": False}, path)
    assert prefs.get_session_review(path) == {
        "recognise_prompts": False,
        "decision_context": "deep",
    }


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
    return {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": cfg.origin}


def test_the_block_rides_config_and_prefs(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    assert c.get("/api/config").json()["session_review"]["decision_context"] == "standard"
    r = c.post("/api/prefs", json={"session_review": {"decision_context": "deep"}}, headers=hdr)
    assert r.status_code == 200 and r.json()["session_review"]["decision_context"] == "deep"
    assert c.get("/api/config").json()["session_review"]["decision_context"] == "deep"
    bad = c.post("/api/prefs", json={"session_review": {"decision_context": "x"}}, headers=hdr)
    assert bad.status_code == 422


# ---- the digest --------------------------------------------------------------------------------


def _card(
    sid, *, flagged=False, recap="Started the refactor\nNow waiting on your review", reason=None
):
    return {
        "id": sid,
        "engine": "claude",
        "title": f"title {sid}",
        "project": {"name": "Alpha"},
        "state": "needs_you" if flagged else "idle",
        "intervention_required": flagged,
        "intervention_reason": reason,
        "ai_summary": "one-line summary",
        "_ai_recap": recap,
        "last_activity": NOW - 3600,
    }


def test_current_state_is_the_recaps_LAST_line_not_its_opening():
    """#1018's measured bug: the digest sent the recap's first 300 characters, while the recap
    prompt puts the current state on its LAST line — so the pass read how the session started."""
    entry = orchestrator._digest_entry(_card("claude:a"), NOW)
    assert entry["current_state"] == "Now waiting on your review"
    assert entry["summary"] == "one-line summary"


def test_the_reason_rides_only_when_the_session_is_flagged():
    flagged = orchestrator._digest_entry(
        _card("claude:a", flagged=True, reason="Asked for a key"), NOW
    )
    assert flagged["needs_user_reason"] == "Asked for a key"
    quiet = orchestrator._digest_entry(_card("claude:b", reason="stale reason"), NOW)
    assert "needs_user_reason" not in quiet


def test_empty_extras_add_no_fields():
    entry = orchestrator._digest_entry(
        _card("claude:a"), NOW, {"prompt": "", "menu": None, "prior_outcomes": []}
    )
    assert not {"prompt", "menu", "prior_outcomes"} & set(entry)


MENU = {
    "engine": "claude",
    "question": "How should I proceed?",
    "options": [
        {"n": 1, "label": "Keep it", "selected": True},
        {"n": 2, "label": "Delete it", "selected": False},
    ],
}


@pytest.fixture
def screens(monkeypatch):
    reads: list[str] = []

    def observed(key):
        reads.append(key)
        if key.endswith("boom"):
            raise OSError("gone")
        return (
            {"prompt_class": "choice", "menu": MENU}
            if key.endswith("menu")
            else {
                "prompt_class": "open",
                "menu": None,
            }
        )

    monkeypatch.setattr(orchestrator, "observed_prompt_for", observed)
    return reads


def test_recognition_ON_adds_the_prompt_and_a_trimmed_menu(screens):
    extras = orchestrator._digest_extras(
        [_card("claude:menu"), _card("claude:plain"), _card("claude:boom")],
        NOW,
        {"recognise_prompts": True, "decision_context": "standard"},
    )
    assert extras["claude:menu"]["prompt"] == "choice"
    assert extras["claude:menu"]["menu"] == {
        "question": "How should I proceed?",
        "options": [{"n": 1, "label": "Keep it"}, {"n": 2, "label": "Delete it"}],
    }
    assert extras["claude:plain"] == {"prompt": "open"}
    assert extras["claude:boom"] == {}  # an unreadable screen is absent, never an error


def test_recognition_OFF_reads_no_screen_at_all(screens):
    extras = orchestrator._digest_extras(
        [_card("claude:menu")], NOW, {"recognise_prompts": False, "decision_context": "standard"}
    )
    assert extras == {"claude:menu": {}}
    assert screens == []


@pytest.fixture
def sources(monkeypatch):
    """The SOURCE readers under `review.transcript_tail`, not the function itself: the tail must
    be proven transcript-only against the real code path (review 5180)."""
    calls = {"transcript": [], "screen": []}

    def plain(key, aliases=None):
        calls["transcript"].append(key)
        return "x" * 5000 + " the conversation END"

    def live(key, n):
        calls["screen"].append(key)
        return "SCREEN-ONLY-MARKER"

    monkeypatch.setattr(review, "_plain_transcript", plain)
    monkeypatch.setattr(review.scrollback, "live_tail_text", live)
    monkeypatch.setattr(orchestrator.scrollback, "live_tail_text", live)
    return calls


def test_deep_is_capped_flagged_first_and_bounded(monkeypatch, screens, sources):
    monkeypatch.setattr(orchestrator, "DEEP_SESSIONS_MAX", 2)
    monkeypatch.setattr(orchestrator.ledger, "latest_by_id", lambda *a, **k: {})
    cards = [_card("claude:quiet1"), _card("claude:flagged", flagged=True), _card("claude:menu")]
    extras = orchestrator._digest_extras(
        cards, NOW, {"recognise_prompts": True, "decision_context": "deep"}
    )
    # Flagged first, then the one stopped at a prompt; the quiet one gets no Deep read.
    assert sources["transcript"] == ["claude:flagged", "claude:menu"]
    assert "transcript_tail" not in extras["claude:quiet1"]
    tail = extras["claude:flagged"]["transcript_tail"]
    assert len(tail) <= orchestrator.DEEP_TRANSCRIPT_CHARS and tail.endswith("END")


@pytest.mark.parametrize("recognise", [True, False])
def test_the_deep_tail_is_the_TRANSCRIPT_and_never_the_screen(sources, recognise, monkeypatch):
    """Review 5180 finding 1: the Deep read used `gather_input`, which assembles the live screen
    and the compose draft with the transcript — so recognition OFF + Deep still read the screen,
    and passed it off as the conversation. The tail is now transcript-only, in both settings."""
    monkeypatch.setattr(orchestrator.ledger, "latest_by_id", lambda *a, **k: {})
    extras = orchestrator._digest_extras(
        [_card("claude:a", flagged=True)],
        NOW,
        {"recognise_prompts": recognise, "decision_context": "deep"},
    )
    tail = extras["claude:a"]["transcript_tail"]
    assert "SCREEN-ONLY-MARKER" not in tail and tail.endswith("the conversation END")
    if not recognise:
        assert sources["screen"] == []  # OFF means no screen read, Deep or not


def test_deep_carries_this_sessions_last_outcomes_newest_first(monkeypatch, screens, sources):
    rows = {
        "a1": {
            "session_id": "claude:a",
            "state": "delivered",
            "verb": "continue",
            "ts": NOW - 7200,
        },
        "a2": {
            "session_id": "claude:a",
            "state": "rejected",
            "verb": "answer",
            "ts": NOW - 3600,
            "rationale": "asked to push",
        },
        "a3": {"session_id": "claude:a", "state": "proposed", "verb": "continue", "ts": NOW - 60},
        "b1": {"session_id": "claude:b", "state": "rejected", "verb": "choose", "ts": NOW - 60},
    }
    monkeypatch.setattr(orchestrator.ledger, "latest_by_id", lambda *a, **k: rows)
    extras = orchestrator._digest_extras(
        [_card("claude:a", flagged=True)],
        NOW,
        {"recognise_prompts": False, "decision_context": "deep"},
    )
    assert extras["claude:a"]["prior_outcomes"] == [
        {"verb": "answer", "outcome": "rejected", "age_hours": 1.0, "rationale": "asked to push"},
        {"verb": "continue", "outcome": "delivered", "age_hours": 2.0, "rationale": ""},
    ]


def test_standard_context_reads_no_transcript(monkeypatch, screens, sources):
    orchestrator._digest_extras(
        [_card("claude:a", flagged=True)],
        NOW,
        {"recognise_prompts": True, "decision_context": "standard"},
    )
    assert sources["transcript"] == []


def test_build_digest_follows_the_stored_setting(monkeypatch, screens, tmp_path):
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))
    prefs.set_session_review({"recognise_prompts": False})
    payload = orchestrator._build_digest([_card("claude:menu")], NOW)
    assert "prompt" not in payload["sessions"][0] and screens == []
    prefs.set_session_review({"recognise_prompts": True})
    payload = orchestrator._build_digest([_card("claude:menu")], NOW)
    assert payload["sessions"][0]["prompt"] == "choice"


# ---- the scheduled sweep sees the settings (review 5180 finding 2) ---------------------------


@pytest.fixture
def sweep_env(monkeypatch):
    from agent_sessions import aitasks, orchestrator_loop

    orchestrator_loop.reset_state()
    aitasks.reset()
    prefs.set_ai_review(
        {"enabled": True, "base_url": "https://ai.test/v1", "api_key": "sk-t", "model": "m"}
    )
    prefs.set_orchestrator({"enabled": True})
    cards = [{"id": "claude:x", "state": "idle", "intervention_required": False}]
    monkeypatch.setattr(orchestrator, "eligible_cards", lambda **k: (cards, {}))
    screen = {"prompt_class": "open", "menu": None}
    monkeypatch.setattr(orchestrator, "observed_prompt_for", lambda key: dict(screen))
    ran: list[int] = []

    async def fake_pass(**kw):
        ran.append(1)
        return {"actions": []}

    monkeypatch.setattr(orchestrator, "run_pass", fake_pass)
    yield orchestrator_loop, ran, screen
    aitasks.reset()


@pytest.mark.parametrize(
    "patch",
    [{"decision_context": "deep"}, {"recognise_prompts": False}],
    ids=["standard-to-deep", "recognition-off"],
)
def test_changing_a_session_review_setting_forces_the_next_pass(sweep_env, patch):
    import asyncio

    loop, ran, _screen = sweep_env
    assert asyncio.run(loop.sweep()).get("ran") is True
    assert asyncio.run(loop.sweep())["skipped"] == "unchanged"
    prefs.set_session_review(patch)
    assert asyncio.run(loop.sweep()).get("ran") is True  # the new evidence is read NOW
    assert len(ran) == 2


def test_a_session_reaching_a_menu_forces_the_next_pass(sweep_env):
    """With recognition on, the screen's prompt and menu are digest inputs, so a session that
    stops at a numbered menu is a change — by the prompt's IDENTITY, never the raw screen."""
    import asyncio

    loop, ran, screen = sweep_env
    assert asyncio.run(loop.sweep()).get("ran") is True
    assert asyncio.run(loop.sweep())["skipped"] == "unchanged"
    screen.update({"prompt_class": "choice", "menu": MENU})
    assert asyncio.run(loop.sweep()).get("ran") is True
    assert asyncio.run(loop.sweep())["skipped"] == "unchanged"  # same menu: no churn
    assert len(ran) == 2
