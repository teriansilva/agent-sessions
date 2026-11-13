"""#1996 — a spoken turn is asked without extended thinking, and says where its time went.

The Commander is waiting on the line. HQ's endpoint may be set to think (the operator's own
setting, for typed turns): measured 2026-11-13, a short answer's first content came after 2.3 s
with thinking and 0.2 s without. So every model request of a CALL turn (each round, the call-end
judgement, the spoken-confirmation judge) carries the provider's opt-out, whatever the level;
a typed turn keeps the level as set.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

import pytest

from agent_sessions import ai_providers, ask_orchestrator, prefs, review, voice_calls, voice_judge
from test_ask_orchestrator import BASE, KEY, Script, ai, answer, app, call  # noqa: F401
from test_voice_confirm import Rig, run

OPT_OUT = {"enable_thinking": False}


def _thinking(level: str) -> None:
    prefs.set_ai_review(
        {"egress_acknowledged": True, "enabled": True, "base_url": BASE, "api_key": KEY,
         "model": "m", "thinking": level}
    )  # fmt: skip


def _turn(cid: str, text: str, **kw):
    async def go():
        task, _ = await ask_orchestrator.begin(
            cid, str(uuid.uuid4()), text, working_keys=set(), **kw
        )
        return await task

    return run(go())


def test_inside_a_spoken_turn_every_level_is_sent_as_off_and_the_setting_is_untouched():
    cfg = {"provider": "custom", "thinking": "high"}
    assert ai_providers.thinking_fields(cfg, interactive=True) == {}  # high: the provider's own
    with ai_providers.spoken_turn():
        assert ai_providers.in_spoken_turn()
        fields = ai_providers.thinking_fields(cfg, interactive=True)
        assert fields == {"chat_template_kwargs": OPT_OUT}
    assert not ai_providers.in_spoken_turn()
    assert cfg == {"provider": "custom", "thinking": "high"}
    assert ai_providers.thinking_fields(cfg, interactive=True) == {}


def test_each_provider_gets_its_own_opt_out_in_a_spoken_turn():
    with ai_providers.spoken_turn():
        openai = ai_providers.thinking_fields(
            {"provider": "openai", "thinking": "medium"}, interactive=True
        )
        router = ai_providers.thinking_fields(
            {"provider": "openrouter", "thinking": "high"}, interactive=True
        )
        none = ai_providers.thinking_fields(
            {"provider": "mistral", "thinking": "high"}, interactive=True
        )
    assert set(openai) == {"reasoning_effort"} and openai["reasoning_effort"] != "medium"
    assert router == {"reasoning": {"enabled": False}}
    assert none == {}  # a provider without a thinking setting is sent nothing, as ever


def test_a_task_started_inside_a_spoken_turn_is_inside_it_and_one_outside_is_not():
    async def go():
        async def probe():
            return ai_providers.in_spoken_turn()

        with ai_providers.spoken_turn():
            inside = asyncio.create_task(probe())
            in_thread = await asyncio.to_thread(ai_providers.in_spoken_turn)
        outside = asyncio.create_task(probe())
        return await inside, in_thread, await outside

    assert run(go()) == (True, True, False)


def test_a_spoken_turn_is_asked_without_thinking_whatever_the_level(app, ai):  # noqa: F811
    script = ai(answer("All quiet, Commander."), answer("no"))
    _thinking("medium")
    view = _turn(app.new(), "What's up?", via="call")
    assert view["status"] == "done"
    # the answer's round AND the call-end judgement of this short utterance
    assert len(script.bodies) == 2
    assert all(b.get("chat_template_kwargs") == OPT_OUT for b in script.bodies)


def test_every_round_of_a_spoken_tool_turn_is_asked_without_thinking(app, ai):  # noqa: F811
    script = ai(call("fleet_status"), answer("Three sessions are running and none needs you."))
    _thinking("high")
    _turn(app.new(), "Give me the state of the fleet and tell me what is running", via="call")
    assert len(script.bodies) == 2  # two rounds; no judgement: a tool was used
    assert all(b.get("chat_template_kwargs") == OPT_OUT for b in script.bodies)


def test_a_typed_turn_keeps_the_level_the_operator_set(app, ai):  # noqa: F811
    script = ai(answer("All quiet."))
    _thinking("medium")
    _turn(app.new(), "What's up?")
    (body,) = script.bodies
    assert "chat_template_kwargs" not in body  # medium: the provider's own thinking, as set


def test_a_spoken_turn_s_tools_refused_fallback_is_asked_without_thinking(app, ai, monkeypatch):  # noqa: F811
    """Hermes on #1996: where the endpoint refuses tools the answer comes from the two-stage
    find, which reads the configuration itself. It is inside the spoken turn all the same."""
    import httpx

    from agent_sessions import pulse_chat

    ai()
    _thinking("medium")
    bodies: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "tools" in body:
            return httpx.Response(400, json={"error": {"message": "tools are not supported"}})
        msg = {"role": "assistant", "content": "no"}
        return httpx.Response(200, json={"choices": [{"message": msg, "finish_reason": "stop"}]})

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
    seen: list[bool] = []

    async def fake_ask(query, history=None, **kw):
        # What the real find would be sent with: its requests read the context, not a config.
        seen.append(ai_providers.in_spoken_turn())
        fields = ai_providers.thinking_fields(review._require_config(), interactive=True)
        seen.append(fields == {"chat_template_kwargs": OPT_OUT})
        return {"answer": "Nothing found.", "matches": [], "mission_matches": [],
                "stage": "catalog", "configured": True}  # fmt: skip

    monkeypatch.setattr(pulse_chat, "ask", fake_ask)
    view = _turn(app.new(), "bye for now", via="call")
    assert view["status"] == "done" and seen == [True, True]
    # The tools request is sent with the opt-out. Refused, the transport asks once more without
    # its optional field (a refusal names none): that retry is the only request without it.
    assert "tools" in bodies[0] and bodies[0].get("chat_template_kwargs") == OPT_OUT
    judged = [b for b in bodies if "tools" not in b]
    assert judged and all(b.get("chat_template_kwargs") == OPT_OUT for b in judged)


def test_the_same_fallback_on_a_typed_turn_keeps_the_level(app, ai, monkeypatch):  # noqa: F811
    from agent_sessions import pulse_chat

    ai()
    _thinking("medium")
    seen: list[bool] = []

    async def fake_ask(query, history=None, **kw):
        seen.append(ai_providers.in_spoken_turn())
        return {"answer": "Nothing found.", "matches": [], "mission_matches": [],
                "stage": "catalog", "configured": True}  # fmt: skip

    import httpx

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "tools are not supported"}})

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(pulse_chat, "ask", fake_ask)
    _turn(app.new(), "find the docs session")
    assert seen == [False]


def test_the_spoken_confirmation_judge_is_asked_without_thinking(monkeypatch):
    seen: list[tuple] = []

    class _Resp:
        status_code = 200

        def json(self):
            msg = {"role": "assistant", "content": "confirm"}
            return {"choices": [{"finish_reason": "stop", "message": msg}]}

    async def post(cfg, body, **kw):
        fields = ai_providers.thinking_fields(cfg, interactive=True)
        seen.append((dict(cfg), fields))
        return _Resp()

    stored = {"model": "m", "base_url": "x", "thinking": "medium", "provider": "custom"}
    monkeypatch.setattr(review, "_require_config", lambda: stored)
    monkeypatch.setattr(review, "post_chat_response", post)
    assert asyncio.run(voice_judge.judge_cards("Start a session.", 1, "ja")) == ("confirm", 0)
    ((cfg, fields),) = seen
    assert fields == {"chat_template_kwargs": OPT_OUT}
    assert cfg["thinking"] == "medium" and stored["thinking"] == "medium"  # the setting, as set
    assert not ai_providers.in_spoken_turn()


# --- the timing line ---------------------------------------------------------------------------


def test_an_answer_logs_where_its_time_went_and_no_words(monkeypatch, caplog):
    r = Rig(monkeypatch, {"answer": "Twenty-five sessions are running, Commander."})
    clock = {"now": 5000.0}
    monkeypatch.setattr(voice_calls.time, "monotonic", lambda: clock["now"])

    async def go():
        r.call.user_stopped_at = clock["now"] - 0.5
        await r.ask("How many sessions are running right now?")
        assert r.call.answer_clock is not None
        clock["now"] += 0.25
        await r.ev(t="bot_started")
        await r.ev(t="bot_started")  # the next sentence: nothing more to log

    with caplog.at_level(logging.INFO, logger="agent_sessions.voice_calls"):
        run(go())
    lines = [rec.getMessage() for rec in caplog.records if " answer: " in rec.getMessage()]
    assert len(lines) == 1
    assert "speaking 0.25s later" in lines[0] and r.call.id in lines[0]
    assert "sessions" not in lines[0] and "Commander" not in lines[0]
    assert r.call.answer_clock is None


def test_an_empty_answer_logs_nothing(monkeypatch, caplog):
    r = Rig(monkeypatch, {"answer": ""})

    async def go():
        await r.ask("Hm")
        assert r.call.answer_clock is None
        await r.ev(t="bot_started")

    with caplog.at_level(logging.INFO, logger="agent_sessions.voice_calls"):
        run(go())
    assert not [rec for rec in caplog.records if " answer: " in rec.getMessage()]


# --- the worker's turn wait, and when the Commander stopped --------------------------------------


def test_the_worker_is_told_how_long_to_wait_on_an_unfinished_sentence():
    from test_voice_noise import _SETTINGS

    cfg = voice_calls.worker_config(_SETTINGS, "aria")
    assert cfg["turn_wait"] == voice_calls.WORKER_TURN_WAIT_S
    # under the shortest commit pause that drafts at all, so the draft starts before it commits
    assert voice_calls.WORKER_STOP_S < cfg["turn_wait"] <= voice_calls.COMMIT_PAUSE_S["natural"]


def test_the_stop_is_when_the_voice_detector_ended_the_last_segment():
    from agent_sessions.voice_turns import stop_instant

    # the turn model waited a second for more: the Commander stopped when the detector said so
    assert stop_instant(100.0, 101.0) == 100.0
    assert stop_instant(None, 101.0) == 101.0  # no segment seen
    assert stop_instant(102.0, 101.0) == 101.0  # a clock that makes no sense


# --- the call's own thinking level and model (Settings → HQ calls) ------------------------------


def _calls(**patch) -> None:
    from agent_sessions import hq_calls_settings

    hq_calls_settings.save(patch)


def test_calls_think_off_on_hq_s_model_unless_set_otherwise():
    from agent_sessions import hq_calls_settings

    block = hq_calls_settings.get()
    assert block["thinking"] == "off" and block["hq_model"] == ""
    assert hq_calls_settings.spoken(block) == {"thinking": "off", "model": None}
    public = hq_calls_settings.public()
    assert public["thinking"] == "off" and public["hq_model"] == ""


def test_the_call_s_thinking_level_is_what_a_spoken_turn_is_sent_with(app, ai):  # noqa: F811
    from agent_sessions import ai_providers as ap

    script = ai(answer("All quiet, Commander."), answer("no"))
    prefs.set_ai_review(
        {"egress_acknowledged": True, "enabled": True, "base_url": "https://api.openai.com/v1",
         "api_key": "sk-" + "a" * 40, "model": "m", "provider": "openai", "thinking": "low"}
    )  # fmt: skip
    _calls(thinking="high")
    _turn(app.new(), "What's up?", via="call")
    assert all(b.get("reasoning_effort") == "high" for b in script.bodies)
    assert ap.thinking_fields(review._require_config(), interactive=True) != {
        "reasoning_effort": "high"
    }  # outside the turn: the typed level, untouched


def test_same_as_hq_sends_a_spoken_turn_exactly_as_a_typed_one(app, ai):  # noqa: F811
    script = ai(answer("All quiet, Commander."), answer("no"))
    _thinking("medium")
    _calls(thinking="hq")
    _turn(app.new(), "What's up?", via="call")
    assert all("chat_template_kwargs" not in b for b in script.bodies)


def test_the_call_s_model_takes_every_request_of_a_spoken_turn_and_no_typed_one(app, ai):  # noqa: F811
    script = ai(call("fleet_status"), answer("Three sessions are running, none of them needs you."))
    _thinking("medium")
    _calls(hq_model="fast-model")
    cid = app.new()
    _turn(cid, "Give me the state of the fleet and tell me what is running", via="call")
    assert [b["model"] for b in script.bodies] == ["fast-model", "fast-model"]
    script.bodies.clear()
    _turn(cid, "And typed?")
    assert script.bodies and {b["model"] for b in script.bodies} == {"m"}


def test_a_short_spoken_turn_s_judgement_goes_to_the_call_s_model_too(app, ai):  # noqa: F811
    script = ai(answer("Glad to help."), answer("no"))
    _thinking("medium")
    _calls(hq_model="fast-model")
    _turn(app.new(), "Thanks", via="call")
    assert [b["model"] for b in script.bodies] == ["fast-model", "fast-model"]


@pytest.mark.parametrize(
    "patch",
    [{"thinking": "maximum"}, {"thinking": None}, {"hq_model": 5}, {"hq_model": "a\nb"},
     {"hq_model": "x" * 500}],
)  # fmt: skip
def test_a_bad_call_setting_is_refused(patch):
    from agent_sessions import hq_calls_settings

    with pytest.raises(hq_calls_settings.SettingsError):
        hq_calls_settings.save(patch)


def test_a_call_model_is_stored_trimmed_and_can_be_cleared():
    from agent_sessions import hq_calls_settings

    _calls(hq_model="  fast-model ")
    assert hq_calls_settings.get()["hq_model"] == "fast-model"
    _calls(hq_model="")
    assert hq_calls_settings.spoken() == {"thinking": "off", "model": None}


# --- the nested search, through the real find (Hermes 8103) --------------------------------------


def _searching_endpoint(monkeypatch, bodies: list[dict]) -> None:
    """HQ's first round calls `find_sessions`; the REAL two-stage find then asks the endpoint
    itself (it reads the configuration on its own), and HQ answers from what it found."""
    import httpx

    from agent_sessions import pulse_chat

    card = {
        "id": "claude:11111111-1111-4111-8111-111111111111",
        "title": "docs rebase",
        "project": {"name": "docs"},
        "cwd": "/work/docs",
        "ai_summary": "rebasing the docs branch",
        "state": "idle",
        "live": False,
    }
    monkeypatch.setattr(pulse_chat, "build_catalog", lambda **kw: [dict(card)])

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "tools" in body:
            rounds = sum(1 for b in bodies if "tools" in b)
            msg = call("find_sessions", {"query": "docs"}) if rounds == 1 else answer("Found it.")
        else:  # the find's own request (or a judgement): plain JSON back
            msg = {"role": "assistant", "content": json.dumps({"answer": "none", "matches": []})}
        return httpx.Response(200, json={"choices": [{"message": msg, "finish_reason": "stop"}]})

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))


def test_a_spoken_search_is_asked_with_the_call_s_level_and_model(app, ai, monkeypatch):  # noqa: F811
    ai()
    _thinking("medium")
    _calls(hq_model="fast-model")
    bodies: list[dict] = []
    _searching_endpoint(monkeypatch, bodies)
    view = _turn(app.new(), "Find the session that rebases the docs branch for me", via="call")
    assert view["status"] == "done"
    nested = [b for b in bodies if "tools" not in b]
    assert nested, "the find never asked the endpoint: this test would pin nothing"
    assert all(b.get("chat_template_kwargs") == OPT_OUT for b in bodies)
    assert {b["model"] for b in bodies} == {"fast-model"}


def test_the_same_search_typed_keeps_hq_s_level_and_model(app, ai, monkeypatch):  # noqa: F811
    ai()
    _thinking("medium")
    _calls(hq_model="fast-model")
    bodies: list[dict] = []
    _searching_endpoint(monkeypatch, bodies)
    _turn(app.new(), "Find the session that rebases the docs branch for me")
    assert [b for b in bodies if "tools" not in b]
    assert {b["model"] for b in bodies} == {"m"}
    # HQ's own rounds are sent as set (medium: nothing); the find is a background-style pass
    # and keeps its own rule, whatever a call would do.
    assert all("chat_template_kwargs" not in b for b in bodies if "tools" in b)
