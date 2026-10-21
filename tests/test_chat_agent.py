"""The API agent (#853 P9a-2, #1209): endpoint config, the turn lifecycle, the store and the routes.

The endpoint is an `httpx.MockTransport` behind `review._TRANSPORT`, so every test can count the
outbound requests it caused — several of the guarantees below are "and nothing was sent".
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_sessions import (
    agent_usage,
    chat_config,
    chat_runtime,
    chat_store,
    engines,
    prefs,
    prompts,
    review,
    template_secrets,
    transcript,
)
from agent_sessions.engines.base import EngineError
from agent_sessions.main import create_app

ENGINE = "apichat"
URL = "https://llm.example.test/v1"
KEY = "sk-test-key-0123456789"


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Endpoint:
    """A scripted OpenAI-compatible endpoint that records every request."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.replies: list = []  # each: httpx.Response | Exception | callable(request)
        self.gate: asyncio.Event | None = None

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.gate is not None:
            await self.gate.wait()
        nxt = self.replies.pop(0) if self.replies else ok("hello")
        if isinstance(nxt, Exception):
            raise nxt
        return nxt(request) if callable(nxt) else nxt

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.url.path.endswith("/completions")]


def ok(text: str, *, finish: str = "stop", usage: dict | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"role": "assistant", "content": text}, "finish_reason": finish}
            ],
            "usage": usage or {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        },
    )


@pytest.fixture
def endpoint(monkeypatch, tmp_path):
    # Each test its own conversation store: the sandbox pin is per SESSION, not per test.
    monkeypatch.setenv("AGENT_SESSIONS_CHAT_DIR", str(tmp_path / "chat-store"))
    ep = Endpoint()
    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(ep.handler))
    yield ep
    chat_runtime._TASKS.clear()
    chat_runtime._LOCKS.clear()


def configure(**extra) -> dict:
    return chat_config.set_config(
        ENGINE, {"base_url": URL, "api_key": KEY, "model": "test-model", **extra}
    )


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["create", "send"])
async def test_disable_after_early_admission_cannot_persist_new_work(
    endpoint, tmp_path, monkeypatch, operation
):
    from agent_sessions.plugins import manager

    configure()
    sid = await chat_runtime.new_session(ENGINE, str(tmp_path)) if operation == "send" else None
    admit = chat_runtime._admit_provider

    def revoke(prov):
        admit(prov)
        manager.deactivate(str(uuid.uuid4()), ENGINE)

    monkeypatch.setattr(chat_runtime, "_admit_provider", revoke)
    with pytest.raises(chat_runtime.ChatError) as error:
        if operation == "create":
            await chat_runtime.new_session(ENGINE, str(tmp_path))
        else:
            await chat_runtime.send(ENGINE, sid, str(uuid.uuid4()), "hello")
    assert error.value.status == 409
    logs = list((tmp_path / "chat-store").glob("*.jsonl"))
    assert len(logs) == (1 if sid else 0)
    if sid:
        assert chat_store.read(tmp_path / "chat-store", sid).turns == []
    assert not endpoint.requests


@pytest.mark.anyio
async def test_disable_during_payload_build_stops_actual_outbound_request(
    endpoint, tmp_path, monkeypatch
):
    from agent_sessions.plugins import manager

    configure()
    sid = await chat_runtime.new_session(ENGINE, str(tmp_path))
    redact = template_secrets.redact_messages

    def revoke(messages):
        result = redact(messages)
        manager.deactivate(str(uuid.uuid4()), ENGINE)
        return result

    monkeypatch.setattr(template_secrets, "redact_messages", revoke)
    await chat_runtime.send(ENGINE, sid, str(uuid.uuid4()), "hello")
    await chat_runtime.running_task(ENGINE, sid)
    assert not endpoint.requests
    assert chat_store.read(tmp_path / "chat-store", sid).turns[-1].status == "failed"


@pytest.mark.anyio
async def test_disable_during_response_stops_transport_fallback(endpoint, tmp_path):
    from agent_sessions.plugins import manager

    configure()
    sid = await chat_runtime.new_session(ENGINE, str(tmp_path))

    def rejected(_request):
        # The entire request body is already sent, so disable must not wait for the reply.
        manager.deactivate(str(uuid.uuid4()), ENGINE)
        return httpx.Response(400, json={"error": "thinking is unsupported"})

    endpoint.replies = [rejected, ok("must not retry after revocation")]
    await chat_runtime.send(ENGINE, sid, str(uuid.uuid4()), "hello")
    await chat_runtime.running_task(ENGINE, sid)
    assert len(endpoint.requests) == 1
    assert chat_store.read(tmp_path / "chat-store", sid).turns[-1].status == "failed"


@pytest.mark.anyio
async def test_response_wait_does_not_hold_agent_revocation(endpoint, tmp_path):
    from agent_sessions.plugins import manager

    configure()
    sid = await chat_runtime.new_session(ENGINE, str(tmp_path))
    endpoint.gate = asyncio.Event()
    await chat_runtime.send(ENGINE, sid, str(uuid.uuid4()), "hello")
    task = chat_runtime.running_task(ENGINE, sid)
    try:
        async with asyncio.timeout(30):
            while not endpoint.requests:
                await asyncio.sleep(0.01)
        await asyncio.to_thread(manager.deactivate, str(uuid.uuid4()), ENGINE)
        assert not task.done()
    finally:
        endpoint.gate.set()
        await task
    # The request already sent before disable may finish; no new work is admitted by it.
    assert len(endpoint.requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("cancel", [False, True])
async def test_request_body_handoff_keeps_admission_until_sent_or_cancelled(
    endpoint, tmp_path, monkeypatch, cancel
):
    from agent_sessions.plugins import admission, storage

    entered, finish, sent = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class PausedTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            async for chunk in request.stream:
                assert json.loads(chunk)["model"] == "test-model"
                entered.set()
                await finish.wait()
            sent.set()
            return ok("done")

    configure()
    monkeypatch.setattr(review, "_TRANSPORT", PausedTransport())
    sid = await chat_runtime.new_session(ENGINE, str(tmp_path))
    await chat_runtime.send(ENGINE, sid, str(uuid.uuid4()), "hello")
    task = chat_runtime.running_task(ENGINE, sid)
    try:
        await asyncio.wait_for(entered.wait(), 30)
        # A second worker cannot publish withdrawal halfway through the outgoing body.
        with pytest.raises(storage.StateError, match="busy"):
            with storage.locked(admission.LOCK, wait=0):
                pass
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            finish.set()
            await task
        with storage.locked(admission.LOCK, wait=0):
            pass
        assert sent.is_set() is not cancel
    finally:
        finish.set()
        if not task.done():
            task.cancel()
            await task


async def settle(sid: str) -> None:
    task = chat_runtime.running_task(ENGINE, sid)
    if task is not None:
        await task


async def new_chat(tmp_path) -> str:
    return await chat_runtime.new_session(ENGINE, str(tmp_path))


def tid() -> str:
    return str(uuid.uuid4())


def prefs_text() -> str:
    return prefs._default_path().read_text()


# ---- configuration ------------------------------------------------------------------------------


def test_the_key_is_encrypted_at_rest_and_never_in_a_public_view():
    view = configure()
    assert view["api_key_set"] and view["configured"]
    assert KEY not in json.dumps(view)
    assert KEY not in prefs_text()  # only the AES-GCM envelope is stored
    assert chat_config.snapshot(ENGINE)["api_key"] == KEY


def test_the_key_is_bound_to_its_origin():
    configure()
    before = prefs_text()
    with pytest.raises(prefs.KeyOriginError):
        chat_config.set_config(ENGINE, {"base_url": "https://attacker.example/v1"})
    with pytest.raises(prefs.KeyOriginError):
        chat_config.set_config(ENGINE, {"base_url": "https://attacker.example/v1", "api_key": ""})
    assert prefs_text() == before  # refused before anything was written
    view = chat_config.set_config(ENGINE, {"base_url": URL + "/other"})  # same origin: fine
    assert view["configured"]
    view = chat_config.set_config(
        ENGINE, {"base_url": "https://b.example/v1", "api_key": "sk-b-123"}
    )
    assert chat_config.snapshot(ENGINE)["api_key"] == "sk-b-123"


def test_a_mask_keeps_the_key_and_null_clears_it():
    configure()
    chat_config.set_config(ENGINE, {"api_key": prefs.AI_REVIEW_KEY_MASK})
    assert chat_config.snapshot(ENGINE)["api_key"] == KEY
    chat_config.set_config(ENGINE, {"api_key": None})
    assert not chat_config.is_configured(ENGINE) and chat_config.snapshot(ENGINE) is None


def test_a_config_that_leaves_no_room_for_history_is_refused():
    with pytest.raises(chat_config.ChatConfigError, match="no room"):
        configure(context_window=4096, max_output_tokens=4000)


def test_the_prompt_is_registered():
    assert chat_config.PROMPT_ID in {p.id for p in prompts.REGISTRY}
    assert prompts.effective(chat_config.PROMPT_ID) in prompts.effective_set()


# ---- the turn lifecycle -------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_message_gets_a_reply_through_the_one_transport(tmp_path, endpoint):
    configure()
    sid = await new_chat(tmp_path)
    t = tid()
    out = await chat_runtime.send(ENGINE, sid, t, "What does _post_chat retry?")
    assert out["turn"]["status"] == "pending"
    await settle(sid)
    view = await chat_runtime.get_session(ENGINE, sid)
    turn = view["turns"][0]
    assert (turn["status"], turn["reply"], turn["usage"]["completion_tokens"]) == (
        "done",
        "hello",
        7,
    )
    [req] = endpoint.requests
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert str(req.url) == URL + "/chat/completions"
    body = endpoint.bodies()[0]
    assert body["model"] == "test-model" and body["stream"] is False and body["max_tokens"] == 4096
    assert body["messages"][0] == {"role": "system", "content": prompts.effective("chat_agent")}
    assert [m["role"] for m in body["messages"][1:]] == ["user"]


@pytest.mark.anyio
async def test_template_secrets_are_redacted_on_the_way_out(tmp_path, endpoint):
    configure()
    template_secrets.register_typed(["hunter2-very-secret"])
    sid = await new_chat(tmp_path)
    await chat_runtime.send(ENGINE, sid, tid(), "my password is hunter2-very-secret")
    await settle(sid)
    sent = endpoint.bodies()[0]["messages"][-1]["content"]
    assert "hunter2-very-secret" not in sent and template_secrets.REDACTED in sent


@pytest.mark.anyio
async def test_a_repeated_send_never_appends_and_a_reused_id_cannot_replace(tmp_path, endpoint):
    configure()
    sid = await new_chat(tmp_path)
    t = tid()
    await chat_runtime.send(ENGINE, sid, t, "one")
    await settle(sid)
    again = await chat_runtime.send(ENGINE, sid, t, "one")  # e.g. a retried POST after a lost reply
    assert again["turn"]["status"] == "done"
    with pytest.raises(chat_runtime.ChatError) as e:
        await chat_runtime.send(ENGINE, sid, t, "something else")
    assert e.value.status == 409 and "different message" in e.value.detail
    view = await chat_runtime.get_session(ENGINE, sid)
    assert len(view["turns"]) == 1 and view["turns"][0]["text"] == "one"
    assert len(endpoint.requests) == 1


@pytest.mark.anyio
async def test_one_operation_at_a_time_and_a_disconnect_does_not_cancel_it(tmp_path, endpoint):
    configure()
    endpoint.gate = asyncio.Event()
    sid = await new_chat(tmp_path)
    t = tid()
    await chat_runtime.send(ENGINE, sid, t, "slow one")
    for turn_id, text in ((t, "slow one"), (tid(), "another")):
        with pytest.raises(chat_runtime.ChatError) as e:
            await chat_runtime.send(ENGINE, sid, turn_id, text)
        assert e.value.status == 409
    # the client "reloads": reading shows it in flight, and does not settle it as interrupted
    view = await chat_runtime.get_session(ENGINE, sid)
    assert view["in_flight"] == t and view["turns"][0]["status"] == "pending"
    endpoint.gate.set()
    await settle(sid)
    assert (await chat_runtime.get_session(ENGINE, sid))["turns"][0]["status"] == "done"
    assert len(endpoint.requests) == 1


@pytest.mark.anyio
async def test_retry_resends_the_same_turn_once_and_counts_usage_once(tmp_path, endpoint):
    configure()
    endpoint.replies = [httpx.Response(500), ok("second time lucky")]
    sid = await new_chat(tmp_path)
    t = tid()
    await chat_runtime.send(ENGINE, sid, t, "please")
    await settle(sid)
    assert (await chat_runtime.get_session(ENGINE, sid))["turns"][0]["status"] == "failed"
    await chat_runtime.retry(ENGINE, sid, t)
    await settle(sid)
    view = await chat_runtime.get_session(ENGINE, sid)
    assert len(view["turns"]) == 1
    assert (view["turns"][0]["status"], view["turns"][0]["reply"]) == ("done", "second time lucky")
    assert len(endpoint.requests) == 2
    with pytest.raises(chat_runtime.ChatError):
        await chat_runtime.retry(ENGINE, sid, t)  # a done turn is not retryable
    root = engines.get(ENGINE).store_root()
    assert chat_store.usage_since(root, 0)["out"] == 7  # once, not per attempt


@pytest.mark.anyio
async def test_two_simultaneous_retries_start_one_operation(tmp_path, endpoint):
    configure()
    endpoint.replies = [httpx.Response(503)]
    sid = await new_chat(tmp_path)
    t = tid()
    await chat_runtime.send(ENGINE, sid, t, "x")
    await settle(sid)
    endpoint.gate = asyncio.Event()
    results = await asyncio.gather(
        chat_runtime.retry(ENGINE, sid, t),
        chat_runtime.retry(ENGINE, sid, t),
        return_exceptions=True,
    )
    refused = [r for r in results if isinstance(r, chat_runtime.ChatError)]
    assert len(refused) == 1 and refused[0].status == 409
    endpoint.gate.set()
    await settle(sid)
    assert len(endpoint.requests) == 2  # the failed send + exactly one retry


@pytest.mark.anyio
async def test_a_turn_left_pending_by_a_restart_settles_as_interrupted(tmp_path, endpoint):
    configure()
    sid = await new_chat(tmp_path)
    t = tid()
    root = engines.get(ENGINE).store_root()
    chat_store.append(
        root,
        sid,
        {"type": "user", "turn_id": t, "text": "hi", "ts": 1.0},
        {"type": "status", "turn_id": t, "status": "pending", "ts": 1.0},
    )  # what a crash mid-request leaves behind: no task in this process
    view = await chat_runtime.get_session(ENGINE, sid)
    assert view["turns"][0]["status"] == "failed" and "interrupted" in view["turns"][0]["reason"]
    assert endpoint.requests == []


@pytest.mark.anyio
async def test_a_timeout_says_the_provider_may_have_billed_it(tmp_path, endpoint):
    configure()
    endpoint.replies = [httpx.ReadTimeout("slow")]
    sid = await new_chat(tmp_path)
    await chat_runtime.send(ENGINE, sid, tid(), "hi")
    await settle(sid)
    reason = (await chat_runtime.get_session(ENGINE, sid))["turns"][0]["reason"]
    assert "may already have processed" in reason and "Retry" in reason


@pytest.mark.anyio
async def test_a_context_rejection_fails_recoverably_and_shrinks_the_next_budget(
    tmp_path, endpoint
):
    configure()
    # _post_chat retries a 400 once (it drops its thinking opt-out first), so a real endpoint's
    # context rejection arrives twice.
    too_long = httpx.Response(
        400, json={"error": {"message": "This model's maximum context length is 8192"}}
    )
    endpoint.replies = [too_long, too_long]
    sid = await new_chat(tmp_path)
    await chat_runtime.send(ENGINE, sid, tid(), "hi")
    await settle(sid)
    view = await chat_runtime.get_session(ENGINE, sid)
    assert view["turns"][0]["status"] == "failed" and "too long" in view["turns"][0]["reason"]
    log = chat_store.read(engines.get(ENGINE).store_root(), sid)
    assert log.budget_factor == pytest.approx(chat_runtime.BUDGET_CUT)


@pytest.mark.anyio
async def test_an_output_limit_keeps_the_reply_and_marks_it_cut_off(tmp_path, endpoint):
    configure()
    endpoint.replies = [ok("a partial answ", finish="length")]
    sid = await new_chat(tmp_path)
    await chat_runtime.send(ENGINE, sid, tid(), "hi")
    await settle(sid)
    turn = (await chat_runtime.get_session(ENGINE, sid))["turns"][0]
    assert (turn["status"], turn["reply"], turn["truncated"]) == ("done", "a partial answ", True)
    log = chat_store.read(engines.get(ENGINE).store_root(), sid)
    assert log.budget_factor == 1.0  # NOT a context rejection


@pytest.mark.anyio
async def test_an_oversized_message_is_refused_before_anything_is_stored_or_sent(
    tmp_path, endpoint
):
    configure(context_window=4096, max_output_tokens=512)
    sid = await new_chat(tmp_path)
    with pytest.raises(chat_runtime.ChatError) as e:
        await chat_runtime.send(ENGINE, sid, tid(), "x" * 20_000)
    assert e.value.status == 413
    assert (await chat_runtime.get_session(ENGINE, sid))["turns"] == []
    assert endpoint.requests == []


@pytest.mark.anyio
async def test_history_is_trimmed_oldest_first_to_the_budget(tmp_path, endpoint):
    configure(context_window=4096, max_output_tokens=512)
    sid = await new_chat(tmp_path)
    for i in range(8):
        endpoint.replies = [ok(f"reply {i} " + "r" * 1500)]
        await chat_runtime.send(ENGINE, sid, tid(), f"question {i} " + "q" * 1500)
        await settle(sid)
    body = endpoint.bodies()[-1]
    sent = [m["content"] for m in body["messages"][1:]]
    assert sent[-1].startswith("question 7")
    assert not any(c.startswith("question 0") for c in sent)  # the oldest went first
    assert {m["role"] for m in body["messages"][1:]} <= {"user", "assistant"}
    last = (await chat_runtime.get_session(ENGINE, sid))["turns"][-1]
    assert last["dropped"] > 0


@pytest.mark.anyio
async def test_the_budget_is_re_evaluated_at_send_time(tmp_path, endpoint, monkeypatch):
    configure(context_window=4096, max_output_tokens=512)
    sid = await new_chat(tmp_path)
    text = "y" * 8000  # fits the budget with the default prompt
    real = prompts.effective
    monkeypatch.setattr(
        prompts,
        "effective",
        lambda pid, *a, **k: real(pid) + ("z" * 9000 if pid == "chat_agent" else ""),
    )
    with pytest.raises(chat_runtime.ChatError) as e:
        await chat_runtime.send(ENGINE, sid, tid(), text)
    assert e.value.status == 413


@pytest.mark.anyio
async def test_clearing_the_endpoint_keeps_conversations_readable(tmp_path, endpoint):
    configure()
    sid = await new_chat(tmp_path)
    await chat_runtime.send(ENGINE, sid, tid(), "keep me")
    await settle(sid)
    chat_config.set_config(ENGINE, {"api_key": None})
    view = await chat_runtime.get_session(ENGINE, sid)
    assert view["turns"][0]["reply"] == "hello"
    with pytest.raises(chat_runtime.ChatError) as e:
        await chat_runtime.send(ENGINE, sid, tid(), "more")
    assert e.value.status == 409
    assert [r.uuid for r in engines.get(ENGINE).scan()] == [sid]  # still listed


@pytest.mark.anyio
async def test_transcript_and_usage_read_the_agents_own_store(tmp_path, endpoint):
    configure()
    sid = await new_chat(tmp_path)
    await chat_runtime.send(ENGINE, sid, tid(), "hi")
    await settle(sid)
    turns = transcript.adapter_for(ENGINE)(sid, tmp_path)
    assert [(t.role, t.text) for t in turns] == [("user", "hi"), ("assistant", "hello")]
    report = agent_usage._reporter_for(ENGINE, agent_usage.read_chat_tokens)()
    assert report.tokens["in"] == 11 and report.tokens["out"] == 7


def test_no_terminal_consumer_accepts_a_chat_engine():
    prov = engines.get(ENGINE)
    with pytest.raises(EngineError):
        engines.registry.require_pty(prov)
    with pytest.raises(EngineError, match="runs no process"):
        prov.launch_argv(str(uuid.uuid4()), cwd="/tmp", bypass=False)
    assert not (
        prov.supports_seed_start or prov.supports_orchestrator_input or prov.expects_raw_tty
    )


def test_unconfigured_it_cannot_start_configured_it_can():
    prov = engines.get(ENGINE)
    assert not engines.registry.can_start(prov)
    configure()
    assert engines.registry.can_start(prov)


# ---- routes -------------------------------------------------------------------------------------


@pytest.fixture
def client(auth_cfg, fake_jsonl, endpoint):
    # A CONTEXT-MANAGED client: one event loop for the whole test, so the server-owned request
    # task outlives the POST that started it — as it does in the running app.
    with TestClient(create_app(auth_cfg), base_url="https://testserver") as c:
        yield from _logged_in(c, auth_cfg)


def _logged_in(c, auth_cfg):
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code in (302, 303)
    csrf = c.get("/api/config").json()["csrf"]
    c.headers.update({"X-CSRF-Token": csrf, "Origin": auth_cfg.origin})
    yield c


def test_routes_bootstrap_an_unconfigured_agent_and_never_return_the_key(client, endpoint):
    assert ENGINE not in client.get("/api/config").json()["new_session_engines"]
    row = next(e for e in client.get("/api/engines").json()["engines"] if e["id"] == ENGINE)
    assert (row["present"], row["supports_new"], row["runtime"]) == (False, False, "chat")
    r = client.patch(
        f"/api/agents/{ENGINE}/endpoint", json={"base_url": URL, "api_key": KEY, "model": "m"}
    )
    assert r.status_code == 200 and r.json()["configured"] and KEY not in r.text
    assert KEY not in client.get(f"/api/agents/{ENGINE}/endpoint").text
    assert ENGINE in client.get("/api/config").json()["new_session_engines"]
    detail = client.get(f"/api/engines/{ENGINE}")
    assert detail.status_code == 200 and KEY not in detail.text
    assert endpoint.requests == []


def test_a_cross_origin_patch_or_test_without_a_new_key_sends_nothing(client, endpoint):
    client.patch(
        f"/api/agents/{ENGINE}/endpoint", json={"base_url": URL, "api_key": KEY, "model": "m"}
    )
    r = client.patch(f"/api/agents/{ENGINE}/endpoint", json={"base_url": "https://evil.example/v1"})
    assert r.status_code == 422
    r = client.post(
        f"/api/agents/{ENGINE}/endpoint/test", json={"base_url": "https://evil.example/v1"}
    )
    assert r.status_code == 422 and "enter the API key" in r.json()["detail"]
    assert endpoint.requests == []
    endpoint.replies = [httpx.Response(200, json={"data": [{"id": "m"}]})]
    r = client.post(f"/api/agents/{ENGINE}/endpoint/test", json={"base_url": URL})
    assert r.status_code == 200 and r.json()["models"] == ["m"]
    assert [q.url.host for q in endpoint.requests] == ["llm.example.test"]
    assert chat_config.public(ENGINE)["base_url"] == URL  # the test saved nothing


def test_the_chat_routes_run_a_conversation_and_are_csrf_guarded(client, endpoint, tmp_path):
    client.patch(
        f"/api/agents/{ENGINE}/endpoint", json={"base_url": URL, "api_key": KEY, "model": "m"}
    )
    r = client.post("/api/chat/new", json={"engine": ENGINE, "cwd": str(tmp_path)})
    assert r.status_code == 201
    sid = r.json()["id"]
    assert sid.startswith(f"{ENGINE}:")
    t = tid()
    r = client.post(f"/api/chat/{sid}/messages", json={"turn_id": t, "text": "hello there"})
    assert r.status_code in (200, 202)
    import time

    for _ in range(100):
        view = client.get(f"/api/chat/{sid}").json()
        if view["turns"][0]["status"] != "pending":
            break
        time.sleep(0.05)
    assert view["turns"][0]["reply"] == "hello"
    bare = TestClient(client.app, base_url="https://testserver", cookies=client.cookies)
    r = bare.post(f"/api/chat/{sid}/messages", json={"turn_id": tid(), "text": "x"})
    assert r.status_code == 403  # no CSRF token / origin
    assert (
        client.post(
            "/api/chat/claude:" + str(uuid.uuid4()) + "/messages",
            json={"turn_id": tid(), "text": "x"},
        ).status_code
        == 409
    )


# ---- Hermes on #1216: four integrity findings, each reproduced here ------------------------------


@pytest.mark.anyio
async def test_a_poll_that_saw_pending_never_marks_a_settled_reply_interrupted(
    tmp_path, endpoint, monkeypatch
):
    """The read sees `pending`; the request then finishes while the read is still deciding. The
    read must not mark it interrupted — that would hide the paid reply and invite a second call."""
    configure()
    endpoint.gate = asyncio.Event()
    sid = await new_chat(tmp_path)
    await chat_runtime.send(ENGINE, sid, tid(), "hi")
    real_read = chat_runtime._read

    async def read_then_let_it_finish(root, session_id):
        log = await real_read(root, session_id)
        if log.pending() is not None:
            endpoint.gate.set()  # the reply arrives now, mid-read
            await asyncio.sleep(0.2)  # time enough for the worker to try to settle
        return log

    monkeypatch.setattr(chat_runtime, "_read", read_then_let_it_finish)
    await chat_runtime.get_session(ENGINE, sid)
    monkeypatch.setattr(chat_runtime, "_read", real_read)
    await settle(sid)
    turn = (await chat_runtime.get_session(ENGINE, sid))["turns"][0]
    assert (turn["status"], turn["reply"]) == ("done", "hello")
    assert chat_store.usage_since(engines.get(ENGINE).store_root(), 0)["out"] == 7
    assert len(endpoint.requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("sep", ["\u0085", " ", " "])
async def test_unicode_line_separators_survive_in_messages_and_replies(tmp_path, endpoint, sep):
    configure()
    endpoint.replies = [ok(f"reply{sep}continues")]
    sid = await new_chat(tmp_path)
    await chat_runtime.send(ENGINE, sid, tid(), f"before{sep}after")
    await settle(sid)
    turn = (await chat_runtime.get_session(ENGINE, sid))["turns"][0]
    assert (turn["text"], turn["reply"]) == (f"before{sep}after", f"reply{sep}continues")
    assert len(endpoint.requests) == 1  # the send was actually made


@pytest.mark.anyio
@pytest.mark.parametrize(
    "torn", [b'{"type": "user", "turn_id": "', b"\xe2\x82", b'{"type": "status"\n\xe2']
)
async def test_a_torn_tail_keeps_history_and_the_next_write(tmp_path, endpoint, torn):
    configure()
    sid = await new_chat(tmp_path)
    await chat_runtime.send(ENGINE, sid, tid(), "before the crash")
    await settle(sid)
    root = engines.get(ENGINE).store_root()
    with open(root / f"{sid}.jsonl", "ab") as fh:
        fh.write(torn)  # what a crash mid-write leaves
    view = await chat_runtime.get_session(ENGINE, sid)
    assert [t["text"] for t in view["turns"]] == ["before the crash"]
    await chat_runtime.send(ENGINE, sid, tid(), "after the restart")
    await settle(sid)
    view = await chat_runtime.get_session(ENGINE, sid)
    assert [(t["text"], t["status"]) for t in view["turns"]] == [
        ("before the crash", "done"),
        ("after the restart", "done"),
    ]


def test_a_short_write_is_completed(tmp_path, monkeypatch):
    root = tmp_path / "store"
    sid = str(uuid.uuid4())
    chat_store.create(root, sid, cwd=str(tmp_path))
    real_write = chat_store.os.write
    monkeypatch.setattr(chat_store.os, "write", lambda fd, data: real_write(fd, bytes(data[:1])))
    t = tid()
    chat_store.append(root, sid, {"type": "user", "turn_id": t, "text": "é" * 50, "ts": 1.0})
    monkeypatch.setattr(chat_store.os, "write", real_write)
    assert chat_store.read(root, sid).turns[0].text == "é" * 50


@pytest.mark.anyio
async def test_a_long_reply_is_kept_whole_and_a_cut_is_recorded(tmp_path, endpoint, monkeypatch):
    configure(max_output_tokens=100_000, context_window=400_000)
    endpoint.replies = [ok("x" * 200_001)]
    sid = await new_chat(tmp_path)
    await chat_runtime.send(ENGINE, sid, tid(), "long one")
    await settle(sid)
    turn = (await chat_runtime.get_session(ENGINE, sid))["turns"][0]
    assert len(turn["reply"]) == 200_001 and turn["truncated"] is False
    monkeypatch.setattr(chat_store, "REPLY_MAX", 1_000)
    endpoint.replies = [ok("y" * 1_001)]
    await chat_runtime.send(ENGINE, sid, tid(), "over the cap")
    await settle(sid)
    turn = (await chat_runtime.get_session(ENGINE, sid))["turns"][1]
    assert len(turn["reply"]) == 1_000 and turn["truncated"] is True  # cut, and SAID so


@pytest.mark.anyio
async def test_old_request_snapshot_cannot_send_after_agent_removal(
    tmp_path, endpoint, monkeypatch
):
    configure()
    sid = await new_chat(tmp_path)
    with engines.registry.snapshot_scope():
        monkeypatch.setattr(
            engines.registry,
            "_BY_ID",
            {k: v for k, v in engines.registry._BY_ID.items() if k != ENGINE},
        )
        with pytest.raises(chat_runtime.ChatError, match="removed"):
            await chat_runtime.send(ENGINE, sid, tid(), "hello")
    assert endpoint.requests == []
    assert chat_store.read(tmp_path / "chat-store", sid).turns == []


@pytest.mark.anyio
async def test_queued_chat_turn_rechecks_live_roster_before_model_call(
    tmp_path, endpoint, monkeypatch
):
    configure()
    sid = await new_chat(tmp_path)
    t = tid()
    root = tmp_path / "chat-store"
    chat_store.append(
        root,
        sid,
        {"type": "user", "turn_id": t, "text": "hello", "ts": 1},
        chat_runtime._status(t, "pending"),
    )
    with engines.registry.snapshot_scope():
        monkeypatch.setattr(
            engines.registry,
            "_BY_ID",
            {k: v for k, v in engines.registry._BY_ID.items() if k != ENGINE},
        )
        records = await chat_runtime._run_once(ENGINE, root, sid, t)
    assert records[-1]["status"] == "failed" and "removed" in records[-1]["reason"]
    assert endpoint.requests == []
