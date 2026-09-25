"""Opt-in usage analytics (#1009): consent gating, the daily budget, revocation orderings, the exact
report, and the two routes. No test reaches the network — every send goes through an
``httpx.MockTransport``, and ``conftest`` keeps the server-wide switch off for everything else."""

from __future__ import annotations

import asyncio
import json
import re
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_sessions import analytics, prefs
from agent_sessions.main import create_app

TODAY = "2026-09-16"
TOMORROW = "2026-09-17"


class Umami:
    """A fake `/api/send`: records every request and answers from a script (default: recorded)."""

    def __init__(self, *answers):
        self.requests: list[httpx.Request] = []
        self.answers = list(answers)
        self.before_answer = None  # a hook that runs while the request is "on the wire"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.before_answer:
            self.before_answer()
        answer = self.answers.pop(0) if self.answers else "recorded"
        if isinstance(answer, Exception):
            raise answer
        if answer == "recorded":
            return httpx.Response(200, json={"cache": "x", "sessionId": "s", "visitId": "v"})
        if answer == "beep":
            return httpx.Response(200, json={"beep": "boop"})
        return httpx.Response(answer, json={"error": {"status": answer}})


@pytest.fixture
def umami(monkeypatch):
    fake = Umami()
    monkeypatch.setenv("AGENT_SESSIONS_ANALYTICS", "1")
    monkeypatch.setattr(analytics, "_TRANSPORT", httpx.MockTransport(fake))
    monkeypatch.setattr(prefs, "utc_today", lambda: TODAY)
    return fake


def _set_day(monkeypatch, day: str) -> None:
    monkeypatch.setattr(prefs, "utc_today", lambda: day)


def _restart() -> None:
    """What a process restart loses: the in-memory spacing and in-flight flag. Prefs survive."""
    analytics._last_attempt = None
    analytics._in_flight = False


# ---- consent gating -------------------------------------------------------------------------


def test_undecided_is_off_and_sends_nothing(umami):
    assert prefs.get_analytics() == {"enabled": False, "decided": False}
    assert analytics.send_once() == "not-due"
    assert umami.requests == []
    # A guard that bails must not create the block as a side effect.
    assert "analytics" not in prefs._load(prefs._default_path())


def test_declined_sends_nothing(umami):
    prefs.set_analytics_consent(False)
    assert prefs.get_analytics() == {"enabled": False, "decided": True}
    assert analytics.send_once() == "not-due"
    assert umami.requests == []


def test_kill_switch_sends_nothing_even_with_consent(umami, monkeypatch):
    prefs.set_analytics_consent(True)
    monkeypatch.setenv("AGENT_SESSIONS_ANALYTICS", "0")
    assert analytics.public_state()["available"] is False

    async def go():
        analytics.note_active()
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert umami.requests == []


# ---- the report itself ----------------------------------------------------------------------


def test_report_is_exactly_the_documented_payload(umami, monkeypatch):
    monkeypatch.setattr(analytics, "get_version", lambda: "0.21.0")
    monkeypatch.setattr(analytics, "os_name", lambda: "Linux")
    prefs.set_analytics_consent(True)
    install_id = prefs.analytics_state()["install_id"]

    assert analytics.send_once() == "recorded"

    (req,) = umami.requests
    assert req.method == "POST"
    assert str(req.url) == "https://analytics.superstatus.io/api/send"
    assert re.fullmatch(r"BattleLab/\S+ \(\w+\)", req.headers["user-agent"])
    assert req.headers["user-agent"] == "BattleLab/0.21.0 (Linux)"
    body = json.loads(req.content)
    assert body == {
        "type": "event",
        "payload": {
            "website": analytics.WEBSITE_ID,
            "id": install_id,
            "hostname": "battlelab-app",
            "url": "/0.21.0",
            "title": "BattleLab",
            "os": "Linux",
        },
    }
    assert "ip" not in body["payload"]
    # A pageview, not a named event: Umami's Visitors figure excludes custom events.
    assert "name" not in body["payload"]


def test_the_user_agent_shape_survives_a_source_build_version():
    ua = analytics.user_agent("0.0.0+ab12cd3", "Darwin")
    assert re.fullmatch(r"BattleLab/\S+ \(\w+\)", ua)


def test_install_id_is_random_uuid4_and_not_returned_by_the_public_view(umami):
    prefs.set_analytics_consent(True)
    iid = prefs.analytics_state()["install_id"]
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", iid)
    assert set(analytics.public_state()) == {"enabled", "decided", "available"}


# ---- the daily budget -----------------------------------------------------------------------


def test_one_settled_report_per_day(umami, monkeypatch):
    prefs.set_analytics_consent(True)
    assert analytics.send_once() == "recorded"
    _restart()
    assert analytics.send_once() == "not-due"
    assert len(umami.requests) == 1
    _set_day(monkeypatch, TOMORROW)
    assert analytics.send_once() == "recorded"
    assert len(umami.requests) == 2


@pytest.mark.parametrize(
    "answer, outcome",
    [("beep", "bot-filtered"), (403, "refused"), (400, "refused")],
)
def test_non_server_errors_settle_the_day(umami, answer, outcome):
    umami.answers = [answer]
    prefs.set_analytics_consent(True)
    assert analytics.send_once() == outcome
    assert prefs.analytics_state()["last_sent_day"] == TODAY
    assert analytics.send_once() == "not-due"
    assert len(umami.requests) == 1


def test_transient_failures_stop_at_three_requests_even_across_restarts(umami):
    umami.answers = [503, httpx.ConnectTimeout("t"), 502, 500]
    prefs.set_analytics_consent(True)
    for _ in range(3):
        assert analytics.send_once() == "transient"
        _restart()
    assert analytics.send_once() == "not-due"
    assert len(umami.requests) == 3
    assert "last_sent_day" not in prefs.analytics_state()


def test_record_then_timeout_is_retried_and_bounded(umami):
    # Umami wrote the first one; the response was lost. It is retried — a second pageview for this
    # install today, never a second visitor — and the retry settles the day.
    umami.answers = [httpx.ReadTimeout("lost"), "recorded"]
    prefs.set_analytics_consent(True)
    assert analytics.send_once() == "transient"
    assert analytics.send_once() == "recorded"
    ids = {json.loads(r.content)["payload"]["id"] for r in umami.requests}
    assert len(ids) == 1


def test_same_day_off_on_after_a_settled_report_sends_nothing_more(umami, monkeypatch):
    prefs.set_analytics_consent(True)
    first_id = prefs.analytics_state()["install_id"]
    assert analytics.send_once() == "recorded"
    prefs.set_analytics_consent(False)
    prefs.set_analytics_consent(True)
    new_id = prefs.analytics_state()["install_id"]
    assert new_id != first_id
    assert analytics.send_once() == "not-due"
    assert len(umami.requests) == 1
    _set_day(monkeypatch, TOMORROW)
    assert analytics.send_once() == "recorded"
    assert json.loads(umami.requests[-1].content)["payload"]["id"] == new_id


def test_same_day_off_on_after_an_unsettled_attempt_waits_for_tomorrow(umami, monkeypatch):
    # Recorded by Umami, response lost: the day is neither settled nor spent. A new id minted now
    # would count a second visitor, so the grant exhausts today's budget.
    umami.answers = [httpx.ReadTimeout("lost")]
    prefs.set_analytics_consent(True)
    assert analytics.send_once() == "transient"
    prefs.set_analytics_consent(False)
    prefs.set_analytics_consent(True)
    assert analytics.send_once() == "not-due"
    assert len(umami.requests) == 1
    _set_day(monkeypatch, TOMORROW)
    assert analytics.send_once() == "recorded"


def test_a_grant_on_a_day_without_attempts_can_send_that_day(umami):
    prefs.set_analytics_consent(False)
    prefs.set_analytics_consent(True)
    assert analytics.send_once() == "recorded"


def test_a_failed_durable_claim_sends_nothing(umami, monkeypatch):
    prefs.set_analytics_consent(True)

    def disk_full(step, path=None):
        raise OSError("no space left on device")

    monkeypatch.setattr(prefs, "update_analytics", disk_full)
    with pytest.raises(OSError):
        analytics.send_once()
    assert umami.requests == []


# ---- revocation orderings -------------------------------------------------------------------


def test_revoke_before_the_claim_sends_nothing(umami):
    prefs.set_analytics_consent(True)
    prefs.set_analytics_consent(False)
    assert analytics.send_once() == "not-due"
    assert umami.requests == []


def test_revoke_between_claim_and_dispatch_sends_nothing_and_spends_the_attempt(umami, monkeypatch):
    prefs.set_analytics_consent(True)
    monkeypatch.setattr(analytics, "_after_claim", lambda: prefs.set_analytics_consent(False))
    assert analytics.send_once() == "withdrawn"
    assert umami.requests == []
    state = prefs.analytics_state()
    assert "install_id" not in state
    assert state["attempts"] == 1


def test_revoke_during_the_post_is_not_written_back(umami):
    prefs.set_analytics_consent(True)
    umami.before_answer = lambda: prefs.set_analytics_consent(False)
    assert analytics.send_once() == "recorded"
    assert len(umami.requests) == 1  # disclosed: a request on the wire is not recalled
    state = prefs.analytics_state()
    assert "install_id" not in state
    assert "last_sent_day" not in state


def test_revoke_and_regrant_during_the_post_does_not_settle_the_new_id(umami):
    prefs.set_analytics_consent(True)
    old = prefs.analytics_state()["install_id"]

    def toggle():
        prefs.set_analytics_consent(False)
        prefs.set_analytics_consent(True)

    umami.before_answer = toggle
    analytics.send_once()
    state = prefs.analytics_state()
    assert state["install_id"] != old
    assert "last_sent_day" not in state
    # …and the new id still waits for tomorrow: this day already had an attempt.
    assert analytics.send_once() == "not-due"


# ---- note_active: off the loop, single-flight, spaced ---------------------------------------


def test_note_active_does_not_block_and_is_single_flight(umami):
    prefs.set_analytics_consent(True)
    release = threading.Event()
    umami.before_answer = lambda: release.wait(5)

    async def go():
        analytics.note_active()
        analytics.note_active()  # in flight → no second task
        await asyncio.sleep(0.2)
        assert len(umami.requests) == 1  # started, and blocked on the wire
        release.set()
        for _ in range(50):
            if not analytics._in_flight:
                break
            await asyncio.sleep(0.02)

    asyncio.run(go())
    assert len(umami.requests) == 1
    assert prefs.analytics_state()["last_sent_day"] == TODAY


def test_note_active_spaces_retries_within_a_process(umami):
    umami.answers = [503, 503]
    prefs.set_analytics_consent(True)

    async def once():
        analytics.note_active()
        for _ in range(100):
            await asyncio.sleep(0.02)
            if not analytics._in_flight and umami.requests:
                break

    asyncio.run(once())
    asyncio.run(once())  # within the hour: nothing new
    assert len(umami.requests) == 1


def test_note_active_never_raises(umami, monkeypatch):
    prefs.set_analytics_consent(True)

    def boom():
        raise RuntimeError("corrupt")

    monkeypatch.setattr(prefs, "analytics_state", boom)
    analytics.note_active()  # no running loop, broken state: still silent


# ---- routes ---------------------------------------------------------------------------------


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


def test_config_carries_the_public_state_only(auth_cfg, umami):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/config").json()["analytics"] == {
        "enabled": False,
        "decided": False,
        "available": True,
    }


def test_consent_write_accepts_only_json_booleans(auth_cfg, umami):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    for bad in (1, 0, "true", None, [], {}):
        r = c.post("/api/prefs", json={"analytics_consent": bad}, headers=hdr)
        assert r.status_code == 422, bad
    # A mixed payload with a bad consent persists nothing.
    r = c.post("/api/prefs", json={"theme": "light", "analytics_consent": "yes"}, headers=hdr)
    assert r.status_code == 422
    assert prefs.get_theme() == "dark"
    assert prefs.get_analytics()["decided"] is False

    r = c.post("/api/prefs", json={"analytics_consent": False}, headers=hdr)
    assert r.status_code == 200
    assert r.json() == {"analytics": {"enabled": False, "decided": True, "available": True}}


def test_consent_write_is_csrf_guarded(auth_cfg, umami):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.post("/api/prefs", json={"analytics_consent": True}, headers={"Origin": auth_cfg.origin})
    assert r.status_code in (401, 403)
    assert prefs.get_analytics()["decided"] is False


def test_consent_then_config_fetch_sends_one_report(auth_cfg, umami):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    assert umami.requests == []  # the login's config fetch came before any decision
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    assert c.post("/api/prefs", json={"analytics_consent": True}, headers=hdr).status_code == 200
    for _ in range(3):
        assert c.get("/api/config").status_code == 200
    for _ in range(100):
        if prefs.analytics_state().get("last_sent_day") == TODAY:
            break
        threading.Event().wait(0.02)
    assert len(umami.requests) == 1
    assert (
        json.loads(umami.requests[0].content)["payload"]["id"]
        == (prefs.analytics_state()["install_id"])
    )


def test_a_report_cancelled_before_it_starts_releases_the_in_flight_claim(umami):
    """#1153: `note_active` claims `_in_flight`, then schedules the send on the running loop. If
    that loop shuts down before the task ever runs — a server stopping, or a per-request test
    loop closing under load — `_run` (whose `finally` releases the claim) never executes. The
    claim then stays set for the life of the process, and every later config fetch returns
    early: the day's report is never sent."""
    prefs.set_analytics_consent(True)

    async def scheduled_then_cancelled():
        analytics.note_active()
        assert analytics._in_flight
        for task in list(analytics._tasks):
            task.cancel()  # before its first step: `_run` never starts
        await asyncio.sleep(0)

    asyncio.run(scheduled_then_cancelled())
    assert analytics._in_flight is False
    # …so the next config fetch starts the report after all.
    asyncio.run(_note_and_settle())
    assert len(umami.requests) == 1


async def _note_and_settle():
    analytics.note_active()
    for _ in range(100):
        await asyncio.sleep(0.02)
        if not analytics._in_flight and not analytics._tasks:
            break


def test_a_report_cancelled_while_QUEUED_behind_a_busy_executor_releases_the_claim(umami):
    """Hermes 5292: the coroutine can hand `_run` to the executor and suspend while the job is
    still queued; cancelling then drops the queued job, so `_run` never runs its `finally`."""
    import concurrent.futures

    prefs.set_analytics_consent(True)
    busy = threading.Event()

    async def queued_then_cancelled():
        loop = asyncio.get_running_loop()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(pool)
        loop.run_in_executor(pool, busy.wait, 10)  # the one worker is occupied
        analytics.note_active()
        await asyncio.sleep(0.05)  # the dispatch has submitted `_run`; it waits in the queue
        tasks = list(analytics._tasks)
        assert tasks and analytics._in_flight
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        busy.set()

    asyncio.run(queued_then_cancelled())
    assert analytics._in_flight is False and umami.requests == []
    asyncio.run(_note_and_settle())
    assert len(umami.requests) == 1


def test_a_report_cancelled_while_its_worker_RUNS_keeps_the_claim_until_it_finishes(umami):
    prefs.set_analytics_consent(True)
    on_wire, release = threading.Event(), threading.Event()

    def hold():
        on_wire.set()
        release.wait(10)

    umami.before_answer = hold

    async def cancelled_mid_send():
        analytics.note_active()
        assert await asyncio.to_thread(on_wire.wait, 5)  # the worker is inside the send
        tasks = list(analytics._tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # The worker still owns the claim: a second report must not start beside it.
        assert analytics._in_flight is True
        analytics.note_active()
        assert analytics._tasks == set()
        release.set()
        for _ in range(250):
            await asyncio.sleep(0.02)
            if not analytics._in_flight:
                break

    asyncio.run(cancelled_mid_send())
    assert analytics._in_flight is False
    assert len(umami.requests) == 1
