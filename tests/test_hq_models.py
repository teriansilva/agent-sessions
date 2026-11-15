"""HQ rows keep per-field intent on the wire, without changing unrelated work (#2040)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agent_sessions import ai_providers, ask_orchestrator, orchestrator_chat, prefs, review
from test_ask_orchestrator import ai, answer, app  # noqa: F401
from test_voice_fast_turns import _searching_endpoint, _thinking, _turn


def test_defaults_and_partial_patches_preserve_the_other_row(app):  # noqa: F811
    defaults = {
        "typed": {"model": "", "thinking": "hq"},
        "mission": {"model": "", "thinking": "hq"},
    }
    assert app.c.get("/api/config").json()["hq_models"] == defaults
    r = app.c.post(
        "/api/prefs", json={"hq_models": {"typed": {"model": " chosen "}}}, headers=app.hdr
    )
    assert r.status_code == 200
    assert r.json()["hq_models"] == {**defaults, "typed": {"model": "chosen", "thinking": "hq"}}
    prefs.set_hq_models({"mission": {"thinking": "auto"}})
    assert prefs.get_hq_models() == {
        "typed": {"model": "chosen", "thinking": "hq"},
        "mission": {"model": "", "thinking": "auto"},
    }


@pytest.mark.parametrize(
    "patch",
    [
        None,
        {},
        {"other": {}},
        {"typed": {}},
        {"typed": []},
        {"typed": {"thinking": "max"}},
        {"mission": {"model": "a\nb"}},
        {"typed": {"model": "x" * (prefs.AI_REVIEW_MODEL_MAX + 1)}},
        {"mission": {"endpoint": "http://other"}},
    ],
)
def test_invalid_models_patch_does_not_apply_adjacent_changes(app, patch):  # noqa: F811
    r = app.c.post("/api/prefs", json={"hq_reports": True, "hq_models": patch}, headers=app.hdr)
    assert r.status_code == 422
    assert not prefs.get_hq_reports()


def test_models_write_keeps_auth_and_csrf(app):  # noqa: F811
    r = app.c.post("/api/prefs", json={"hq_models": {"typed": {"model": "chosen"}}})
    assert r.status_code == 403
    assert prefs.get_hq_models()["typed"]["model"] == ""


@pytest.fixture
def wire(monkeypatch):
    bodies = []

    async def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
    cfg = {
        "provider": "openai",
        "schema": 2,
        "base_url": "http://127.0.0.1/v1",
        "model": "endpoint",
        "thinking": "medium",
        "api_key": "",
        "model_mode": "specific",
        "max_input_chars": 2000,
    }

    async def send(tag="request"):
        await review._post_chat(
            cfg,
            {"model": cfg["model"], "messages": [{"role": "user", "content": tag}]},
            interactive=True,
        )
        return bodies[-1]

    return cfg, bodies, send


@pytest.mark.anyio
@pytest.mark.parametrize(
    "call_model,call_thinking,own_model,own_thinking,model,thinking",
    [
        (None, None, None, None, "endpoint", "medium"),
        ("voice", "high", None, None, "voice", "high"),
        ("voice", "high", "router", None, "router", "high"),
        ("voice", "high", None, "off", "voice", "minimal"),
        ("voice", "high", "endpoint", "auto", "endpoint", None),
        (None, None, "typed", "low", "typed", "low"),
    ],
)
async def test_each_field_resolves_own_then_call_then_endpoint(
    wire, call_model, call_thinking, own_model, own_thinking, model, thinking
):
    _, _, send = wire
    with ai_providers.spoken_turn(call_thinking, call_model):
        with ai_providers.model_override(model=own_model, thinking=own_thinking):
            assert ai_providers.in_spoken_turn()
            body = await send()
    assert body["model"] == model
    assert body.get("reasoning_effort") == thinking
    assert not ai_providers.in_spoken_turn()


@pytest.mark.anyio
async def test_router_inherit_does_not_take_a_typed_only_choice(wire):
    _, _, send = wire
    with ai_providers.model_override(model="typed", thinking="high"):
        with ai_providers.model_override():
            nested = await send()
        resumed = await send()
    assert (nested["model"], nested["reasoning_effort"]) == ("endpoint", "medium")
    assert (resumed["model"], resumed["reasoning_effort"]) == ("typed", "high")


@pytest.mark.anyio
async def test_explicit_model_equal_to_auto_default_is_still_pinned(wire, monkeypatch):
    cfg, _, send = wire
    cfg["model_mode"] = "auto"

    async def choose(_):
        return "auto-selected"

    monkeypatch.setattr(review, "_auto_model", choose)
    with ai_providers.model_override(model="endpoint"):
        chosen = await send()
    inherited = await send()
    assert chosen["model"] == "endpoint"
    assert inherited["model"] == "auto-selected"


def test_typed_turn_uses_its_row_and_default_call_stays_unchanged(app, ai):  # noqa: F811
    script = ai(
        answer("A detailed answer about the fleet."),
        answer("A detailed spoken answer about the fleet."),
    )
    _thinking("medium")
    prefs.set_hq_models({"typed": {"model": "typed", "thinking": "off"}})
    question = (
        "Give me a detailed overview of every running session "
        "and explain which project each of them belongs to"
    )
    _turn(app.new(), question)
    _turn(app.new(), question, via="call")
    assert [b["model"] for b in script.bodies] == ["typed", "m"]
    assert all(b.get("chat_template_kwargs") == {"enable_thinking": False} for b in script.bodies)


@pytest.mark.anyio
async def test_mission_conversation_applies_its_row_only(wire, monkeypatch):
    _, bodies, send = wire
    prefs.set_hq_models({"mission": {"model": "mission", "thinking": "low"}})
    monkeypatch.setattr(review, "_require_config", lambda: wire[0])

    async def classify(*_):
        await send()
        return "history"

    monkeypatch.setattr(orchestrator_chat, "_classify", classify)
    monkeypatch.setattr(orchestrator_chat, "_history_answer", lambda: {})
    await orchestrator_chat.ask("history", mission_id="a" * 32)
    await orchestrator_chat.ask("history")
    assert [(b["model"], b["reasoning_effort"]) for b in bodies] == [
        ("mission", "low"),
        ("endpoint", "medium"),
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("cancel_parent", [False, True])
async def test_background_review_spawned_inside_a_turn_stays_clean_after_parent_exit(
    wire, monkeypatch, cancel_parent
):
    cfg, bodies, send = wire
    entered, release, parent_ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
    spawned = []
    monkeypatch.setattr(review, "_require_config", lambda: cfg)
    monkeypatch.setattr(review, "gather_review_input", lambda *a, **kw: ("fixture", "fingerprint"))
    monkeypatch.setattr(review, "_note_failure", lambda *a: None)

    async def tail(*_):
        entered.set()
        await release.wait()
        assert not ai_providers.in_spoken_turn()
        await send("background review")
        raise review.ReviewError("fixture stops before persistence")

    monkeypatch.setattr(review, "_tail_review_call", tail)

    async def parent():
        with ai_providers.spoken_turn("high", "voice"):
            with ai_providers.model_override(model="typed", thinking="low"):
                spawned.append(asyncio.create_task(review.run_review("claude:fixture")))
                await entered.wait()
                parent_ready.set()
                if cancel_parent:
                    await asyncio.Future()

    task = asyncio.create_task(parent())
    await asyncio.wait_for(parent_ready.wait(), timeout=5)
    if cancel_parent:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    release.set()
    with pytest.raises(review.ReviewError):
        await spawned[0]
    assert (bodies[0]["model"], bodies[0]["reasoning_effort"]) == ("endpoint", "medium")


@pytest.mark.anyio
async def test_concurrent_contexts_keep_models_thinking_and_speech_identity_separate(wire):
    _, bodies, send = wire
    entered = [asyncio.Event() for _ in range(4)]
    release = asyncio.Event()

    async def worker(index, context, spoken):
        with context:
            entered[index].set()
            await release.wait()
            assert ai_providers.in_spoken_turn() is spoken
            await send(str(index))

    tasks = [
        asyncio.create_task(worker(0, ai_providers.model_override("low", "typed"), False)),
        asyncio.create_task(worker(1, ai_providers.spoken_turn("high", "voice"), True)),
        asyncio.create_task(worker(2, ai_providers.model_override("off", "mission"), False)),
        asyncio.create_task(worker(3, ai_providers.clear_model_context(), False)),
    ]
    await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), 5)
    release.set()
    await asyncio.gather(*tasks)
    by_tag = {b["messages"][0]["content"]: (b["model"], b["reasoning_effort"]) for b in bodies}
    assert by_tag == {
        "0": ("typed", "low"),
        "1": ("voice", "high"),
        "2": ("mission", "minimal"),
        "3": ("endpoint", "medium"),
    }


@pytest.mark.anyio
@pytest.mark.parametrize("cancel", [False, True])
async def test_error_and_cancellation_restore_a_later_request_in_the_same_task(wire, cancel):
    _, bodies, send = wire
    entered = asyncio.Event()

    async def worker():
        try:
            with ai_providers.spoken_turn("high", "voice"):
                with ai_providers.model_override("low", "mission"):
                    await send("before")
                    entered.set()
                    if cancel:
                        await asyncio.Future()
                    raise RuntimeError("failed turn")
        except (asyncio.CancelledError, RuntimeError):
            assert not ai_providers.in_spoken_turn()
            await send("after")

    task = asyncio.create_task(worker())
    await asyncio.wait_for(entered.wait(), 5)
    if cancel:
        task.cancel()
    await task
    assert [(b["model"], b["reasoning_effort"]) for b in bodies] == [
        ("mission", "low"),
        ("endpoint", "medium"),
    ]


def test_typed_foreground_search_keeps_its_row(app, ai, monkeypatch):  # noqa: F811
    ai()
    _thinking("medium")
    prefs.set_hq_models({"typed": {"model": "typed", "thinking": "off"}})
    bodies = []
    _searching_endpoint(monkeypatch, bodies)
    result = _turn(app.new(), "Find the session that rebases the docs branch for me")
    assert result["status"] == "done"
    assert any("tools" not in b for b in bodies), "the foreground search must ask the endpoint"
    assert {b["model"] for b in bodies} == {"typed"}
    assert all(b.get("chat_template_kwargs") == {"enable_thinking": False} for b in bodies)


@pytest.mark.anyio
async def test_real_router_inherits_call_model_but_keeps_own_thinking_and_speech(wire, monkeypatch):
    cfg, bodies, _ = wire
    prefs.set_hq_route({"model": "", "thinking": "off"})
    monkeypatch.setattr(review, "_require_config", lambda: cfg)
    seen = []

    async def handler(request):
        bodies.append(json.loads(request.content))
        seen.append(ai_providers.in_spoken_turn())
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '{"search":false,"answer":"Hello"}',
                        }
                    }
                ]
            },
        )

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
    turn = ask_orchestrator._Turn(
        cid="ask_" + "0" * 32,
        turn_id="b" * 32,
        question="hello",
        working_keys=set(),
        progress=lambda _: None,
    )
    with ai_providers.spoken_turn("high", "voice"):
        assert await ask_orchestrator._route(turn, [], False) == "Hello"
    with ai_providers.model_override("high", "typed"):
        assert await ask_orchestrator._route(turn, [], False) == "Hello"
    assert [(b["model"], b["reasoning_effort"]) for b in bodies] == [
        ("voice", "minimal"),
        ("endpoint", "minimal"),
    ]
    assert seen == [True, False]
