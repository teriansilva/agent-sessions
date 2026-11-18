"""#2137 P2 — HQ answers with the whole conversation in context.

Pinned:

* every settled exchange is replayed (not the last eight), each as stored, within the history
  budget; over it, the oldest exchanges are left out of that request, never the newest;
* the budget is `HISTORY_SHARE` of the request, or of the model's own window when its endpoint
  lists one (from `review.list_models`, never a call of its own);
* a background summary replaces the oldest exchanges: bound to the exact prefix it read, refused
  if that prefix changed, never truncated, at most `SUMMARIES_MAX`; a failure changes nothing;
* one compaction task per conversation, never on the answer path;
* each answered turn stores its context size (measured from `usage.prompt_tokens`, else
  estimated) and the conversation view says what the meter shows.

Assertions about what the model is sent are made on the OUTBOUND request.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

from agent_sessions import ask_orchestrator, ask_store, review
from test_ask_orchestrator import (  # noqa: F401 — fixtures
    QD,
    _router_bodies,
    ai,
    answer,
    app,
    found,
    no_tools,
    route,
)


def _turn(cid: str, q: str, a: str) -> str:
    tid = str(uuid.uuid4())
    ask_store.append(
        cid,
        {"type": "user", "turn_id": tid, "text": q, "qd": QD},
        {"type": "assistant", "turn_id": tid, "text": a},
        {"type": "status", "turn_id": tid, "status": "done"},
    )
    return tid


def _usage(text: str, prompt_tokens: int | None) -> httpx.Response:
    body: dict = {"choices": [{"message": {"role": "assistant", "content": text},
                               "finish_reason": "stop"}]}  # fmt: skip
    if prompt_tokens is not None:
        body["usage"] = {"prompt_tokens": prompt_tokens}
    return httpx.Response(200, json=body)


def _users(body: dict) -> list[str]:
    return [m["content"] for m in body["messages"] if m["role"] == "user"]


# --- the whole conversation ----------------------------------------------------------------------


def test_every_settled_exchange_is_replayed_not_the_last_eight(app, ai):  # noqa: F811
    script = ai(answer("ok"))
    cid = app.new()
    for i in range(12):
        _turn(cid, f"question {i}", f"answer {i}")
    assert app.ask(cid, "and now?").status_code == 200
    users = _users(script.bodies[0])
    assert users[:12] == [f"question {i}" for i in range(12)]
    assert users[-1] == "and now?"


def test_a_long_answer_is_replayed_as_stored_not_cut_at_two_thousand(app, ai):  # noqa: F811
    script = ai(answer("ok"))
    cid = app.new()
    long = "word " * 700  # 3,500 characters, under the store's TEXT_MAX
    _turn(cid, "tell me everything", long)
    app.ask(cid, "thanks")
    replayed = [m["content"] for m in script.bodies[0]["messages"] if m["role"] == "assistant"]
    assert replayed[0] == long.strip()


def test_over_the_budget_the_oldest_exchanges_are_left_out_never_the_newest():
    records = []
    tids = []
    for i in range(6):
        tid = str(uuid.uuid4())
        tids.append(tid)
        records += [
            {"type": "user", "turn_id": tid, "text": f"q{i} " + "x" * 1000},
            {"type": "assistant", "turn_id": tid, "text": f"a{i} " + "y" * 1000},
            {"type": "status", "turn_id": tid, "status": "done"},
        ]
    now = str(uuid.uuid4())
    history, _ = ask_orchestrator._history(records, now, budget=4_100 * 2)
    users = [m["content"][:2] for m in history if m["role"] == "user"]
    assert users == ["q2", "q3", "q4", "q5"]  # the newest that fit, in order
    assert sum(len(m["content"]) for m in history) <= 4_100 * 2


@pytest.mark.parametrize("path", ["tools", "no_tools"])
def test_over_budget_on_either_path_the_request_carries_the_newest_exchanges(
    path,
    app,  # noqa: F811
    ai,  # noqa: F811
    no_tools,  # noqa: F811
    found,  # noqa: F811
    monkeypatch,
):
    """Hermes on #2137: the fallback holds on BOTH HQ paths, with the window unknown and no
    summary landed (compaction off): the oldest exchanges are left out of the outbound request,
    never the newest, and the question is always there."""
    monkeypatch.setattr(ask_orchestrator, "schedule_compaction", lambda cid: None)
    monkeypatch.setattr(ask_orchestrator, "history_budget", lambda cfg, model=None: 4_100 * 2)
    if path == "tools":
        script = ai(answer("ok"))
    else:
        script = no_tools(route({"search": False, "answer": "ok"}))
    cid = app.new()
    for i in range(6):
        _turn(cid, f"q{i} " + "x" * 1000, f"a{i} " + "y" * 1000)
    assert app.ask(cid, "and now?").status_code == 200
    body = script.bodies[0] if path == "tools" else _router_bodies(script)[0]
    users = [u[:2] for u in _users(body)]
    assert users == ["q2", "q3", "q4", "q5", "an"]


def test_the_budget_follows_the_models_own_window(monkeypatch):
    monkeypatch.setattr(review, "context_window", lambda model: None)
    assert ask_orchestrator.history_budget({"model": "m"}) == int(
        ask_orchestrator.REQUEST_BUDGET_CHARS * ask_orchestrator.HISTORY_SHARE
    )
    monkeypatch.setattr(review, "context_window", lambda model: 32_768)
    small = ask_orchestrator.history_budget({"model": "m"})
    assert small == int((32_768 - ask_orchestrator.OUTPUT_RESERVE_TOKENS) * 4 * 0.5)
    monkeypatch.setattr(review, "context_window", lambda model: 1_000_000)  # larger: the cap
    assert ask_orchestrator.history_budget({"model": "m"}) == int(
        ask_orchestrator.REQUEST_BUDGET_CHARS * ask_orchestrator.HISTORY_SHARE
    )


# --- the window, from the endpoint's own listing ------------------------------------------------


def test_the_window_comes_from_the_model_listing_and_is_read_at_most_once(monkeypatch):
    from agent_sessions import prefs

    prefs.set_ai_review(
        {"egress_acknowledged": True, "enabled": True, "base_url": "https://ai.example/v1",
         "api_key": "k", "model": "m"}
    )  # fmt: skip
    gets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        gets.append(request.url.path)
        return httpx.Response(200, json={"data": [
            {"id": "m", "context_length": 131_072}, {"id": "v", "max_model_len": 32_768},
            {"id": "x"}]})  # fmt: skip

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
    review._models_cache.clear()
    assert review.context_window("m") is None  # never blocks, never calls out
    assert gets == []
    asyncio.run(_real_refresh())
    asyncio.run(_real_refresh())  # within the TTL: not read again
    assert gets == ["/v1/models"]
    assert review.context_window("m") == 131_072
    assert review.context_window("v") == 32_768
    assert review.context_window("x") is None


async def _real_refresh() -> None:
    """`review.refresh_context_windows` as shipped (the conftest stubs the module attribute)."""
    await _REAL_REFRESH()


_REAL_REFRESH = review.refresh_context_windows


# --- the context size on each turn ---------------------------------------------------------------


def test_an_answered_turn_stores_its_measured_size_and_the_view_shows_it(app, ai, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ask_orchestrator, "schedule_compaction", lambda cid: None)
    ai(_usage("ok", 4_321))
    cid = app.new()
    app.ask(cid, "hello")
    body = app.get(cid)
    assert body["turns"][-1]["ctx"] == {"tokens": 4_321, "estimated": False}
    assert body["context"] == {"latest": {"tokens": 4_321, "estimated": False},
                               "compacting": False}  # fmt: skip


def test_without_usage_the_size_is_estimated(app, ai):  # noqa: F811
    ai(_usage("ok", None))
    cid = app.new()
    app.ask(cid, "hello")
    ctx = app.get(cid)["context"]["latest"]
    assert ctx["estimated"] is True and ctx["tokens"] > 0


def test_a_known_window_rides_with_the_size(app, ai, monkeypatch):  # noqa: F811
    monkeypatch.setattr(review, "context_window", lambda model: 128_000)
    ai(_usage("ok", 900))
    cid = app.new()
    app.ask(cid, "hello")
    assert app.get(cid)["context"]["latest"] == {"tokens": 900, "estimated": False,
                                                 "window": 128_000}  # fmt: skip


def test_a_new_conversation_has_no_size_yet(app):  # noqa: F811
    cid = app.new()
    assert app.get(cid)["context"] == {"latest": None, "compacting": False}


def test_the_store_refuses_a_malformed_size():
    for bad in ({"tokens": -1}, {"tokens": "9"}, {"tokens": 1, "window": 0}, {"tokens": 1, "x": 1}):
        with pytest.raises(ValueError):
            ask_store._admit({"type": "status", "turn_id": str(uuid.uuid4()), "status": "done",
                              "ctx": bad}, [])  # fmt: skip


# --- compaction ----------------------------------------------------------------------------------


def _prefix_through(records: list[dict], tid: str) -> int:
    """How many records the conversation holds up to and including turn ``tid``'s last one."""
    return max(i for i, r in enumerate(records) if r.get("turn_id") == tid) + 1


def _long_conversation(cid: str, n: int = 8, size: int = 1_000) -> list[str]:
    return [_turn(cid, f"q{i} " + "x" * size, f"a{i} " + "y" * size) for i in range(n)]


def _summariser(
    monkeypatch,
    reply: str | None = "The Commander planned the TikTok mission.",
    finish: str = "stop",
    status: int = 200,
    delay: float = 0.0,
) -> list[dict]:
    sent: list[dict] = []

    async def post(cfg, body):
        sent.append(body)
        if delay:
            await asyncio.sleep(delay)
        return httpx.Response(status, json={"choices": [{"message": {"role": "assistant",
                              "content": reply}, "finish_reason": finish}]})  # fmt: skip

    monkeypatch.setattr(review, "post_chat_response", post)
    monkeypatch.setattr(review, "_require_config", lambda: {"model": "m"})
    return sent


def test_a_conversation_within_its_budget_is_not_summarised(app, monkeypatch):  # noqa: F811
    sent = _summariser(monkeypatch)
    cid = app.new()
    _long_conversation(cid, 3, 100)
    assert asyncio.run(ask_orchestrator._compact(cid)) == "fits"
    assert sent == []


def test_compaction_summarises_the_oldest_and_keeps_the_newest(app, monkeypatch):  # noqa: F811
    sent = _summariser(monkeypatch)
    monkeypatch.setattr(ask_orchestrator, "compaction_budget", lambda cfg: 10_000)
    cid = app.new()
    tids = _long_conversation(cid)
    assert asyncio.run(ask_orchestrator._compact(cid)) == "done"
    # The summariser was given the oldest exchanges as quoted DATA under its guarded prompt.
    (body,) = sent
    from agent_sessions import prompts

    assert body["messages"][0]["content"] == prompts.effective("hq_conversation_compact")
    assert '"commander": "q0' in body["messages"][1]["content"]
    (summary,) = [r for r in ask_store.read(cid) if r["type"] == "summary"]
    assert summary["upto"] in tids and summary["upto"] != tids[-1]
    # The next request opens with the summary as data, then the exchanges after it.
    history, _ = ask_orchestrator._history(ask_store.read(cid), str(uuid.uuid4()), budget=10_000)
    assert history[0]["content"].startswith(ask_orchestrator.SUMMARY_NOTE)
    assert history[1]["content"] == ask_orchestrator.SUMMARY_ACK
    kept = [m["content"][:2] for m in history[2:] if m["role"] == "user"]
    assert kept[-1] == "q7" and "q0" not in kept


def test_the_request_after_a_summary_opens_with_it_as_data(app, ai, monkeypatch):  # noqa: F811
    """On the OUTBOUND request: the summary as a user message marked as data, then the exchanges
    after it; the exchanges it covers are not sent again, and it is never a system message."""
    monkeypatch.setattr(ask_orchestrator, "schedule_compaction", lambda cid: None)
    script = ai(answer("ok"))
    cid = app.new()
    tids = _long_conversation(cid, 4, 50)
    records = ask_store.read(cid)
    n = _prefix_through(records, tids[1])
    rec = {"upto": tids[1], "n": n, "text": "Earlier: two things.",
           "digest": ask_store.records_digest(records[:n])}  # fmt: skip
    assert ask_store.append_summary(cid, rec)
    app.ask(cid, "what next?")
    msgs = script.bodies[0]["messages"]
    users = [m["content"] for m in msgs if m["role"] == "user"]
    assert users[0] == f"{ask_orchestrator.SUMMARY_NOTE}\n\nEarlier: two things."
    assert [u[:2] for u in users[1:3]] == ["q2", "q3"] and users[-1] == "what next?"
    assert not any(m["role"] == "system" and "Earlier: two things." in m["content"] for m in msgs)


def test_records_appended_while_it_summarises_stay_after_the_summary(app, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ask_orchestrator, "compaction_budget", lambda cfg: 10_000)
    cid = app.new()
    _long_conversation(cid)
    sent: list[dict] = []

    async def post(cfg, body):
        sent.append(body)
        _turn(cid, "a question asked meanwhile", "its answer")  # lands during the summary
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant",
                              "content": "Summary."}, "finish_reason": "stop"}]})  # fmt: skip

    monkeypatch.setattr(review, "post_chat_response", post)
    monkeypatch.setattr(review, "_require_config", lambda: {"model": "m"})
    assert asyncio.run(ask_orchestrator._compact(cid)) == "done"
    types = [r["type"] for r in ask_store.read(cid)]
    assert types[-1] == "summary"  # appended after, never inserted
    history, _ = ask_orchestrator._history(ask_store.read(cid), str(uuid.uuid4()), budget=10_000)
    assert any(m["content"] == "a question asked meanwhile" for m in history)


def test_a_summary_of_a_prefix_that_changed_is_refused(app, monkeypatch):  # noqa: F811
    cid = app.new()
    tids = _long_conversation(cid, 3, 10)
    records = ask_store.read(cid)
    n = _prefix_through(records, tids[1])
    rec = {"upto": tids[1], "n": n, "digest": ask_store.records_digest(records[:n]),
           "text": "Summary."}  # fmt: skip
    stale = {**rec, "digest": "0" * 64}
    assert ask_store.append_summary(cid, stale) is None
    assert ask_store.append_summary(cid, rec) is not None
    assert ask_store.append_summary(cid, rec) is None  # one already covers it


def test_a_summary_for_a_deleted_conversation_writes_nothing(app, monkeypatch):  # noqa: F811
    cid = app.new()
    tids = _long_conversation(cid, 2, 10)
    records = ask_store.read(cid)
    n = _prefix_through(records, tids[0])
    rec = {"upto": tids[0], "n": n, "digest": ask_store.records_digest(records[:n]),
           "text": "Summary."}  # fmt: skip
    ask_store.delete_conversation(cid)
    assert ask_store.append_summary(cid, rec) is None
    assert not ask_store.exists(cid)


def test_at_most_summaries_max_are_kept(app):  # noqa: F811
    cid = app.new()
    tids = _long_conversation(cid, ask_store.SUMMARIES_MAX + 2, 10)
    for k in range(ask_store.SUMMARIES_MAX + 1):
        records = ask_store.read(cid)
        n = _prefix_through(records, tids[k])
        rec = {"upto": tids[k], "n": n, "digest": ask_store.records_digest(records[:n]),
               "text": f"S{k}"}  # fmt: skip
        written = ask_store.append_summary(cid, rec)
        assert (written is not None) == (k < ask_store.SUMMARIES_MAX)


@pytest.mark.parametrize(
    "kw",
    [
        {"reply": "x" * (ask_store.SUMMARY_TEXT_MAX + 1)},  # over the cap: refused, never cut
        {"reply": ""},
        {"finish": "length"},
        {"status": 500},
        {"reply": None},
    ],
)
def test_a_failed_summary_changes_nothing(kw, app, monkeypatch):  # noqa: F811
    _summariser(monkeypatch, **kw)
    monkeypatch.setattr(ask_orchestrator, "compaction_budget", lambda cfg: 10_000)
    cid = app.new()
    _long_conversation(cid)
    before = ask_store.read(cid)
    assert asyncio.run(ask_orchestrator._compact(cid)) == "failed"
    assert ask_store.read(cid) == before


def test_a_summary_that_takes_too_long_changes_nothing(app, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ask_orchestrator, "COMPACT_TIMEOUT_S", 0.05)
    _summariser(monkeypatch, delay=1.0)
    monkeypatch.setattr(ask_orchestrator, "compaction_budget", lambda cfg: 10_000)
    cid = app.new()
    _long_conversation(cid)
    before = ask_store.read(cid)
    assert asyncio.run(ask_orchestrator._compact(cid)) == "failed"
    assert ask_store.read(cid) == before


def test_one_compaction_per_conversation_and_one_more_pass_when_asked_meanwhile(monkeypatch):
    runs: list[str] = []
    gate = asyncio.Event

    async def scenario():
        hold = gate()

        async def fake(cid):
            runs.append(cid)
            await hold.wait()
            return "fits"

        monkeypatch.setattr(ask_orchestrator, "_compact", fake)
        ask_orchestrator.schedule_compaction("c1")
        await asyncio.sleep(0)
        assert ask_orchestrator.compacting("c1")
        ask_orchestrator.schedule_compaction("c1")  # a second settle meanwhile
        ask_orchestrator.schedule_compaction("c1")  # and a third: still ONE more pass
        hold.set()
        while ask_orchestrator.compacting("c1"):
            await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert runs == ["c1", "c1"]
    assert not ask_orchestrator.compacting("c1")


def test_a_settled_turn_schedules_compaction_off_the_answer_path(app, ai, monkeypatch):  # noqa: F811
    ai(answer("ok"))
    scheduled: list[str] = []
    monkeypatch.setattr(ask_orchestrator, "schedule_compaction", scheduled.append)
    cid = app.new()
    assert app.ask(cid, "hello").status_code == 200
    assert scheduled == [cid]


# --- review 8445 (Hermes on #2160) ---------------------------------------------------------------


def _reply_with_usage(content: str, tokens: int) -> httpx.Response:
    choice = {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
    return httpx.Response(200, json={"choices": [choice], "usage": {"prompt_tokens": tokens}})


def test_the_search_branch_gets_the_whole_history_and_its_own_request_is_measured(
    app,  # noqa: F811
    no_tools,  # noqa: F811
    monkeypatch,
):
    """Router says search → the find answers. The find is handed HQ's whole history with HQ's
    limits, and the size stored is the FIND's request, never the router's."""
    import json as _json

    from agent_sessions import pulse_chat

    monkeypatch.setattr(ask_orchestrator, "schedule_compaction", lambda cid: None)
    seen: dict = {}

    async def fake_find(query, history=None, **kw):
        seen.update(history=history, **kw)
        # The find's own request, through the transport like the real one's, kept as an
        # accepted stage does (`pulse_chat._keep`).
        with review.capture_requests() as captured:
            r = await review.post_chat_response(
                review._require_config(),
                {"model": "m", "messages": [{"role": "user", "content": "catalog"}],
                 "stream": False},
            )  # fmt: skip
        assert r.status_code == 200
        pulse_chat._keep(captured)
        return {"answer": "found it", "matches": [], "mission_matches": [], "stage": "catalog"}

    monkeypatch.setattr(pulse_chat, "ask", fake_find)
    no_tools(_reply_with_usage(_json.dumps({"search": True}), 111), _reply_with_usage("x", 777))
    cid = app.new()
    for i in range(12):
        _turn(cid, f"q{i}", f"a{i}")
    body = app.ask(cid, "which session fixed it?").json()
    assert body["turn"]["status"] == "done"
    assert seen["turns_max"] is None
    assert seen["chars_max"] == ask_orchestrator.HISTORY_MESSAGE_CHARS
    assert [m["content"] for m in seen["history"] if m["role"] == "user"][:12] == [
        f"q{i}" for i in range(12)
    ]
    assert app.get(cid)["context"]["latest"] == {"tokens": 777, "estimated": False}


def test_with_routing_off_the_finds_request_is_measured(app, no_tools, monkeypatch):  # noqa: F811
    from agent_sessions import prefs, pulse_chat

    monkeypatch.setattr(ask_orchestrator, "schedule_compaction", lambda cid: None)
    prefs.set_hq_route({"enabled": False})

    async def fake_find(query, history=None, **kw):
        with review.capture_requests() as captured:
            await review.post_chat_response(
                review._require_config(),
                {"model": "m", "messages": [{"role": "user", "content": "catalog"}],
                 "stream": False},
            )  # fmt: skip
        pulse_chat._keep(captured)
        return {"answer": "found it", "matches": [], "mission_matches": [], "stage": "catalog"}

    monkeypatch.setattr(pulse_chat, "ask", fake_find)
    no_tools(_reply_with_usage("x", 555))
    cid = app.new()
    app.ask(cid, "which session?")
    assert app.get(cid)["context"]["latest"] == {"tokens": 555, "estimated": False}


def test_the_typed_override_sets_the_budget_and_the_window(app, ai, monkeypatch):  # noqa: F811
    """Endpoint default `m` (128k), typed override `small` (16k): the request is sent as `small`,
    its history held to `small`'s budget, and the stored window is `small`'s."""
    from agent_sessions import prefs

    monkeypatch.setattr(ask_orchestrator, "schedule_compaction", lambda cid: None)
    windows = {"m": 128_000, "small": 16_384}
    monkeypatch.setattr(review, "context_window", lambda model: windows.get(model))
    prefs.set_hq_models({"typed": {"model": "small"}})
    script = ai(_reply_with_usage("ok", 4_000))
    cid = app.new()
    for i in range(12):
        _turn(cid, f"q{i} " + "x" * 1_000, f"a{i} " + "y" * 1_000)
    app.ask(cid, "and now?")
    body = script.bodies[0]
    assert body["model"] == "small"
    budget = int((16_384 - ask_orchestrator.OUTPUT_RESERVE_TOKENS) * 4 * 0.5)
    history = [m for m in body["messages"] if m["role"] in ("user", "assistant")][:-1]
    assert sum(len(m["content"]) for m in history) <= budget
    assert [m["content"][:3] for m in history if m["role"] == "user"][-1] == "q11"
    assert app.get(cid)["context"]["latest"]["window"] == 16_384


def test_a_calls_model_sets_the_budget():
    from agent_sessions import ai_providers

    windows = {"m": 128_000, "talk": 20_000}
    old = review.context_window
    review.context_window = lambda model: windows.get(model)  # type: ignore[assignment]
    try:
        cfg = {"model": "m"}
        full = ask_orchestrator.history_budget(cfg)
        with ai_providers.spoken_turn(model="talk"):
            spoken = ask_orchestrator.history_budget(cfg)
        assert spoken == int((20_000 - ask_orchestrator.OUTPUT_RESERVE_TOKENS) * 4 * 0.5) < full
    finally:
        review.context_window = old  # type: ignore[assignment]


def test_the_routers_own_model_holds_its_history_to_its_budget(app, no_tools, monkeypatch):  # noqa: F811
    from agent_sessions import prefs

    monkeypatch.setattr(ask_orchestrator, "schedule_compaction", lambda cid: None)
    windows = {"m": 128_000, "tiny": 12_000}
    monkeypatch.setattr(review, "context_window", lambda model: windows.get(model))
    prefs.set_hq_route({"enabled": True, "model": "tiny"})
    script = no_tools(route({"search": False, "answer": "ok"}))
    cid = app.new()
    for i in range(12):
        _turn(cid, f"q{i} " + "x" * 1_000, f"a{i} " + "y" * 1_000)
    app.ask(cid, "and now?")
    body = _router_bodies(script)[0]
    assert body["model"] == "tiny"
    budget = int((12_000 - ask_orchestrator.OUTPUT_RESERVE_TOKENS) * 4 * 0.5)
    history = [m for m in body["messages"] if m["role"] in ("user", "assistant")][:-1]
    assert 0 < sum(len(m["content"]) for m in history) <= budget


def test_compaction_works_to_the_smallest_answering_budget(monkeypatch):
    from agent_sessions import hq_calls_settings, prefs

    windows = {"m": 128_000, "small": 16_384, "talk": 20_000}
    monkeypatch.setattr(review, "context_window", lambda model: windows.get(model))
    prefs.set_hq_models({"typed": {"model": "small"}})
    monkeypatch.setattr(hq_calls_settings, "spoken", lambda: {"model": "talk", "thinking": None})
    assert ask_orchestrator.compaction_budget({"model": "m"}) == ask_orchestrator.history_budget(
        {"model": "m"}, "small"
    )


# --- review 8446 (Hermes on #2160) ---------------------------------------------------------------


def test_a_router_without_its_own_model_is_held_to_the_model_it_is_sent_with(
    app,  # noqa: F811
    no_tools,  # noqa: F811
    monkeypatch,
):
    """Endpoint `m` (12k), typed `large` (128k), router model empty: the router is SENT as `m`
    (an empty router model clears the typed row, #2040), so its history fits `m`'s budget."""
    from agent_sessions import prefs

    monkeypatch.setattr(ask_orchestrator, "schedule_compaction", lambda cid: None)
    windows = {"m": 12_000, "large": 128_000}
    monkeypatch.setattr(review, "context_window", lambda model: windows.get(model))
    prefs.set_hq_models({"typed": {"model": "large"}})
    prefs.set_hq_route({"enabled": True, "model": ""})
    script = no_tools(route({"search": False, "answer": "ok"}))
    cid = app.new()
    for i in range(12):
        _turn(cid, f"q{i} " + "x" * 1_500, f"a{i} " + "y" * 1_500)
    app.ask(cid, "and now?")
    body = _router_bodies(script)[0]
    assert body["model"] == "m"
    budget = int((12_000 - ask_orchestrator.OUTPUT_RESERVE_TOKENS) * 4 * 0.5)
    history = [m for m in body["messages"] if m["role"] in ("user", "assistant")][:-1]
    assert 0 < sum(len(m["content"]) for m in history) <= budget
