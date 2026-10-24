"""The NIGHTJAR footer's telemetry (#1531): host CPU/RAM sampling, the HQ token counter, the
authenticated route, and the one place the counter is fed (``review._post_chat``)."""

from __future__ import annotations

import asyncio
import json
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_sessions import hq_contacts, hq_usage, prompts, review, telemetry
from agent_sessions.main import create_app


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


# ---------------------------------------------------------------------------- HQ usage counter


def test_usage_from_reads_the_provider_usage():
    p, t, est = hq_usage.usage_from(
        {"usage": {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500}}, []
    )
    assert (p, t, est) == (1200, 1500, False)


def test_usage_from_derives_total_when_the_provider_omits_it():
    assert hq_usage.usage_from({"usage": {"prompt_tokens": 10, "completion_tokens": 5}}, []) == (
        10,
        15,
        False,
    )


def test_partial_provider_usage_estimates_the_answer_and_says_so():
    """Hermes on #1560: prompt tokens alone were recorded as an EXACT total equal to the prompt,
    understating TODAY and hiding the ≈. The answer is estimated and the count marked."""
    resp = {"usage": {"prompt_tokens": 20}, "choices": [{"message": {"content": "a" * 40}}]}
    assert hq_usage.usage_from(resp, []) == (20, 30, True)


def test_a_tool_only_answer_without_usage_counts_its_tool_traffic():
    """Hermes on #1560: the fallback counted message text only, so HQ's tool schemas and a reply
    that is nothing but a tool call estimated to ~1 token."""
    tools = [{"type": "function", "function": {"name": "find", "parameters": {"q": "x" * 200}}}]
    msgs = [
        {"role": "user", "content": "u" * 8},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"function": {"name": "find", "arguments": '{"q":"' + "p" * 32 + '"}'}}],
        },
    ]
    resp = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {"function": {"name": "open", "arguments": "{" + "k" * 76 + "}"}}
                    ],
                }
            }
        ]
    }
    p, t, est = hq_usage.usage_from(resp, msgs, tools)
    assert est is True
    import json as _json

    sent = (
        8
        + len("find")
        + len('{"q":"' + "p" * 32 + '"}')
        + len(_json.dumps(tools, separators=(",", ":")))
    )
    assert p == -(-sent // 4)
    assert t - p == -(-(len("open") + 78) // 4)  # the tool call's name + arguments, not ~0


def test_usage_from_estimates_without_usage_and_says_so():
    msgs = [{"role": "system", "content": "x" * 40}, {"role": "user", "content": "y" * 8}]
    resp = {"choices": [{"message": {"content": "z" * 20}}]}
    p, t, est = hq_usage.usage_from(resp, msgs)
    assert est is True
    assert p == 12  # 48 chars / 4
    assert t == 17  # + 20 chars / 4


def test_record_accumulates_today_and_keeps_the_last_context(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_HQ_USAGE", str(tmp_path / "hq.json"))
    hq_usage.record(1000, 1200, False)
    hq_usage.record(1800, 2000, True)
    snap = hq_usage.snapshot()
    assert snap["today_tokens"] == 3200
    assert snap["ctx_tokens"] == 1800
    assert snap["ctx_estimated"] is True and snap["today_estimated"] is True
    doc = json.loads((tmp_path / "hq.json").read_text())
    # Numbers only: nothing a prompt, an answer or a session could leak through.
    assert set(doc) == {
        "day",
        "today_tokens",
        "today_estimated",
        "ctx_tokens",
        "ctx_estimated",
        "at",
    }


def _add_hq_tokens(path: str, times: int) -> None:
    """One server process recording HQ calls (a child of the test below)."""
    import os

    os.environ["AGENT_SESSIONS_HQ_USAGE"] = path
    for _ in range(times):
        hq_usage.record(10, 19, False)


def test_competing_processes_never_lose_a_count(tmp_path):
    """Hermes on #1560: the in-process lock alone let two workers both read an empty counter and
    each write 19 — 19, not 38. The read → add → write runs under the cross-process lock."""
    import multiprocessing

    path = str(tmp_path / "hq.json")
    ctx = multiprocessing.get_context("fork")
    workers = [ctx.Process(target=_add_hq_tokens, args=(path, 40)) for _ in range(4)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(30)
        assert w.exitcode == 0
    assert json.loads((tmp_path / "hq.json").read_text())["today_tokens"] == 4 * 40 * 19


def test_a_new_day_resets_today_but_keeps_the_context(tmp_path, monkeypatch):
    path = tmp_path / "hq.json"
    monkeypatch.setenv("AGENT_SESSIONS_HQ_USAGE", str(path))
    path.write_text(
        json.dumps(
            {"day": "2000-01-01", "today_tokens": 999, "today_estimated": True, "ctx_tokens": 77}
        )
    )
    snap = hq_usage.snapshot()
    assert snap["today_tokens"] == 0 and snap["today_estimated"] is False
    assert snap["ctx_tokens"] == 77
    hq_usage.record(5, 9, False)
    doc = json.loads(path.read_text())
    assert doc["day"] == date.today().isoformat() and doc["today_tokens"] == 9


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '{"today_tokens": "lots"}'])
def test_a_broken_file_reads_as_zero(tmp_path, monkeypatch, content):
    path = tmp_path / "hq.json"
    path.write_text(content)
    monkeypatch.setenv("AGENT_SESSIONS_HQ_USAGE", str(path))
    assert hq_usage.snapshot()["today_tokens"] == 0


# ---------------------------------------------------------------------------- CPU / RAM


def test_cpu_is_sampled_lazily_and_cached(monkeypatch):
    telemetry._reset_for_tests()
    reads = iter([(1000, 800), (1100, 850), (1300, 900)])
    calls = []

    def fake():
        calls.append(1)
        return next(reads)

    monkeypatch.setattr(telemetry, "_read_stat", fake)
    monkeypatch.setattr(telemetry, "FIRST_GAP", 0)
    first = asyncio.run(telemetry.cpu_pct())
    assert first == 50.0  # 100 jiffies, 50 idle
    again = asyncio.run(telemetry.cpu_pct())
    assert again == 50.0 and len(calls) == 2  # cached: no third read inside the interval
    monkeypatch.setattr(telemetry, "MIN_INTERVAL", 0)
    assert asyncio.run(telemetry.cpu_pct()) == 75.0  # 200 jiffies, 50 idle
    telemetry._reset_for_tests()


def test_cpu_is_none_where_proc_stat_is_missing(monkeypatch):
    telemetry._reset_for_tests()

    def gone():
        raise OSError("no /proc")

    monkeypatch.setattr(telemetry, "_read_stat", gone)
    assert asyncio.run(telemetry.cpu_pct()) is None
    telemetry._reset_for_tests()


def test_memory_is_total_minus_available(monkeypatch):
    monkeypatch.setattr(
        telemetry.sysinfo, "_meminfo", lambda: {"mem_total": 16 * 2**30, "mem_available": 6 * 2**30}
    )
    m = telemetry.memory()
    assert m == {"used": 10 * 2**30, "total": 16 * 2**30, "pct": 62.5}


# ---------------------------------------------------------------------------- the route


def test_the_route_is_behind_the_login(auth_cfg):
    assert _client(auth_cfg).get("/api/telemetry").status_code == 401


def test_the_route_answers_a_logged_in_operator(auth_cfg, monkeypatch):
    async def cpu():
        return 23.0

    monkeypatch.setattr(telemetry, "cpu_pct", cpu)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/telemetry")
    assert r.status_code == 200
    body = r.json()
    assert body["cpu_pct"] == 23.0
    assert set(body["hq"]) >= {"ctx_tokens", "today_tokens", "ctx_estimated", "today_estimated"}
    assert r.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------- the one feed


def _registered_hq_body() -> dict:
    return {
        "model": "m",
        "messages": [
            {"role": "system", "content": prompts.effective(hq_contacts.PROMPT_IDS[0])},
            {"role": "system", "content": prompts.effective("ask_orchestrator")},
            {"role": "user", "content": "What needs me?"},
        ],
    }


def _post_through_transport(monkeypatch, body: dict, *, hq: bool) -> None:
    """One call through THE transport (`review._post_chat`) against a mock endpoint."""
    usage = {"usage": {"prompt_tokens": 40, "total_tokens": 50}, "choices": []}
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=usage))
    monkeypatch.setattr(
        review, "_client", lambda timeout: httpx.AsyncClient(transport=transport, timeout=timeout)
    )
    cfg = {
        "base_url": "http://127.0.0.1:9/v1",
        "api_key": "k",
        "model": "m",
    }  # loopback: no consent needed

    async def go():
        if hq:
            with hq_usage.hq_call():
                await review._post_chat(cfg, body)
        else:
            await review._post_chat(cfg, body)

    asyncio.run(go())


def test_identity_not_text_decides_an_hq_call(tmp_path, monkeypatch):
    """Hermes on #1560: the transport mapped prompt TEXT back to ids, so a non-HQ call whose
    registry text equals an HQ prompt's was counted as HQ. The same payload, sent by a caller
    that is not HQ's pipeline, is not counted; sent inside `hq_call()`, it is."""
    monkeypatch.setenv("AGENT_SESSIONS_HQ_USAGE", str(tmp_path / "hq.json"))
    _post_through_transport(monkeypatch, _registered_hq_body(), hq=False)
    assert hq_usage.snapshot()["today_tokens"] == 0
    _post_through_transport(monkeypatch, _registered_hq_body(), hq=True)
    assert hq_usage.snapshot()["today_tokens"] == 50


def test_the_hq_mark_is_scoped_to_its_block():
    assert hq_usage.in_hq_call() is False
    with hq_usage.hq_call():
        assert hq_usage.in_hq_call() is True
    assert hq_usage.in_hq_call() is False


def test_an_hq_call_is_counted_and_another_call_is_not(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_HQ_USAGE", str(tmp_path / "hq.json"))
    ok = httpx.Response(200, json={"usage": {"prompt_tokens": 40, "total_tokens": 50}})
    review._note_hq_usage(True, _registered_hq_body(), ok)
    review._note_hq_usage(False, _registered_hq_body(), ok)  # not HQ
    review._note_hq_usage(True, _registered_hq_body(), httpx.Response(500))  # failed call
    assert hq_usage.snapshot()["today_tokens"] == 50


def test_a_malformed_response_never_breaks_the_call(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_HQ_USAGE", str(tmp_path / "hq.json"))
    review._note_hq_usage(True, _registered_hq_body(), httpx.Response(200, content=b"<html>"))
    assert hq_usage.snapshot()["today_tokens"] == 0
