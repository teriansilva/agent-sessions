"""Handoff into a native API session (#2141).

Pins the issue's acceptance contract against a fake structured runtime (the real create / submit
are pinned by their own suites): the capability (`API_HANDOFF_READY`, readiness at prepare AND
commit, `can_bypass`), the binding (one target, one operation id per handle, replayed by every
commit), the seed-first fence at the shared submit boundary, every row of the failure table, the
skip-permissions start (brief sent once its launch is ready, released on discard), and provenance
plus archive-after-acceptance.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from agent_sessions import engines, handoff, handoff_api, metadata, structured_runtime
from agent_sessions.main import create_app
from agent_sessions.plugins import kinds

#: The REAL facade submit, captured before any fixture replaces it (never `monkeypatch.undo()`,
#: which would also drop conftest's isolation pins).
_REAL_SUBMIT = structured_runtime.submit_turn
_SRC = "11111111-1111-1111-1111-111111111111"  # fake_jsonl session in /home/user/claude/repo-a
SOURCE = f"claude:{_SRC}"


@pytest.fixture(autouse=True)
def _reset():
    handoff.reset_for_tests()
    yield
    handoff.reset_for_tests()


class FakeRuntime:
    """The structured facade as `handoff_api` uses it: create, submit, snapshot."""

    def __init__(self):
        self.creates: list[dict] = []
        self.submits: list[dict] = []
        self.turns: dict[str, list[str]] = {}
        self.submit_result: object = {"handoff": "sent"}
        self.record_on_refusal = False
        self.snap_extra: dict = {}

    async def create_session(self, engine, cwd, *, operation_id, bypass, wait_ready=True, **_):
        self.creates.append(
            {"engine": engine, "cwd": cwd, "id": operation_id, "bypass": bypass, "wait": wait_ready}
        )
        self.turns.setdefault(f"{engine}:{operation_id}", [])
        return {"session_key": f"{engine}:{operation_id}", **self.snap_extra}

    async def submit_turn(self, key, *, operation_id, text, **_):
        self.submits.append({"key": key, "id": operation_id, "text": text})
        result = self.submit_result
        if isinstance(result, structured_runtime.StructuredError):
            if self.record_on_refusal:
                self.turns.setdefault(key, []).append(operation_id)
            raise result
        if isinstance(result, BaseException):
            raise result
        self.turns.setdefault(key, []).append(operation_id)
        return {"session_key": key, **result}

    async def snapshot(self, key, **_):
        return {
            "session_key": key,
            "turns": [{"turn_id": t} for t in self.turns.get(key, [])],
            **self.snap_extra,
        }


@pytest.fixture
def rt(monkeypatch):
    fake = FakeRuntime()
    monkeypatch.setattr(structured_runtime, "create_session", fake.create_session)
    monkeypatch.setattr(structured_runtime, "submit_turn", fake.submit_turn)
    monkeypatch.setattr(structured_runtime, "snapshot", fake.snapshot)
    return fake


@pytest.fixture
def archived(monkeypatch):
    calls: list[str] = []

    async def archive_key(key):
        calls.append(key)
        return True, None

    from agent_sessions.routes import sessions as sessions_routes

    monkeypatch.setattr(sessions_routes, "archive_key", archive_key)
    return calls


@pytest.fixture
def patched_meta(monkeypatch):
    writes: list[tuple[str, dict]] = []
    monkeypatch.setattr(metadata, "patch", lambda key, **kw: writes.append((key, kw)))
    monkeypatch.setattr(metadata, "resolve_key", lambda key: key)
    return writes


def _handle(target="claude-api", seed="# brief\n[user] continue the auth fix"):
    return handoff.create_handle(SOURCE, target, "quick", seed, cwd="/home/user/claude/repo-a")


def _run(coro):
    return asyncio.run(coro)


# ---- capability ---------------------------------------------------------------------------------


def test_every_api_kind_says_whether_it_takes_a_handoff():
    assert set(kinds.API_HANDOFF_READY) == set(kinds.API_KINDS)
    assert all(type(v) is bool for v in kinds.API_HANDOFF_READY.values())


def test_a_ready_api_client_is_a_target_without_the_pty_bits(monkeypatch):
    prov = engines.get("claude-api")
    m = engines.manifest_of(prov)
    assert not m.can("seed_start") and not m.can("handoff_target")  # still PTY-only bits
    assert handoff.handoff_target_state(prov, present=True) == (True, None)


def test_an_api_kind_switched_off_is_refused(monkeypatch):
    prov = engines.get("codex-api")
    monkeypatch.setitem(kinds.API_HANDOFF_READY, "codex-app-server", False)
    assert handoff.handoff_target_state(prov, present=True) == (False, "no seed-capable start yet")


def test_an_api_client_that_cannot_start_says_why(monkeypatch):
    monkeypatch.setattr(
        engines.registry, "api_unavailable_reason", lambda p: "Claude Code is not signed in"
    )
    prov = engines.get("claude-api")
    assert handoff.handoff_target_state(prov, present=False) == (
        False,
        "Claude Code is not signed in",
    )


def test_the_chat_agent_is_never_a_target():
    prov = engines.get("apichat")
    if prov is None:
        pytest.skip("no apichat in this roster")
    ok, _ = handoff.handoff_target_state(prov, present=True)
    assert ok is False


# ---- binding ------------------------------------------------------------------------------------


def test_the_turn_id_is_a_stable_uuid_the_shared_boundary_accepts():
    a = handoff.api_turn_id("h-1")
    assert a == handoff.api_turn_id("h-1") != handoff.api_turn_id("h-2")
    assert uuid.UUID(a)
    assert structured_runtime._operation_id(a) == a


def test_commit_api_binds_once_and_replays():
    h = _handle()
    first = handoff.commit_api(h, bypass=False, archive_source=True)
    again = handoff.commit_api(h, bypass=False, archive_source=True)
    assert first["id"] == again["id"]
    assert first["id"].startswith("claude-api:")
    uuid.UUID(first["native"])  # a pinned id: no placeholder for an API client
    with pytest.raises(handoff.HandoffError) as exc:
        handoff.commit_api(h, bypass=True, archive_source=True)
    assert exc.value.status == 409


def test_a_console_commit_cannot_be_replayed_as_an_api_one():
    h = _handle(target="claude")
    handoff.commit(h)
    with pytest.raises(handoff.HandoffError) as exc:
        handoff.commit_api(h, bypass=False, archive_source=False)
    assert exc.value.status == 409


def test_an_api_brief_is_never_a_pty_seed():
    h = _handle()
    key = handoff.commit_api(h, bypass=False, archive_source=False)["id"]
    assert handoff.has_pending_seed(key) is False
    assert handoff.claim_seed(key) is None


# ---- the seed-first fence -----------------------------------------------------------------------


def test_the_fence_admits_only_the_brief_until_it_settles():
    h = _handle()
    key = handoff.commit_api(h, bypass=False, archive_source=False)["id"]
    own = handoff.api_turn_id(h)
    other = str(uuid.uuid4())
    assert handoff.api_blocks(key, other) is True
    assert handoff.api_blocks(key, own) is False
    for outcome in ("not_sent",):
        handoff.api_settle(key, outcome)
    assert handoff.api_blocks(key, other) is False  # a recorded refusal lifts it


def test_the_shared_submit_boundary_refuses_a_send_ahead_of_the_brief(monkeypatch):
    h = _handle()
    key = handoff.commit_api(h, bypass=False, archive_source=False)["id"]
    engine, _, native = key.partition(":")
    monkeypatch.setattr(structured_runtime, "_key", lambda k: (engine, native))
    reached = []

    class Adapter:
        async def submit(self, *a, **kw):  # pragma: no cover — must never be reached
            reached.append(a)

    monkeypatch.setattr(structured_runtime, "_adapter", lambda e, op: Adapter())
    with pytest.raises(structured_runtime.StructuredError) as exc:
        _run(structured_runtime.submit_turn(key, operation_id=str(uuid.uuid4()), text="hi"))
    assert exc.value.status == 409
    assert "handoff brief" in exc.value.detail
    assert reached == []


# ---- delivery: the failure table ----------------------------------------------------------------


def _committed(rt, *, bypass=False, archive=True, seed=None):
    h = _handle()
    out = _run(handoff_api.commit(h, seed, bypass=bypass, archive_source=archive))
    return h, out


def test_a_guarded_handoff_creates_ready_then_sends_the_brief_once(rt, archived, patched_meta):
    h, out = _committed(rt)
    assert out["delivery"] == "sent"
    assert rt.creates == [
        {
            "engine": "claude-api",
            "cwd": "/home/user/claude/repo-a",
            "id": out["native"],
            "bypass": False,
            "wait": True,  # the internal default: ready before the first turn
        }
    ]
    assert [s["id"] for s in rt.submits] == [handoff.api_turn_id(h)]
    assert "continue the auth fix" in rt.submits[0]["text"]
    # Provenance on both sides, then the source archived.
    keys = {k for k, _ in patched_meta}
    assert out["id"] in keys and SOURCE in keys
    assert archived == [SOURCE]
    # A replay after success changes nothing.
    again = _run(handoff_api.commit(h, None, bypass=False, archive_source=True))
    assert again["delivery"] == "sent" and len(rt.creates) == 1 and len(rt.submits) == 1


def test_unchecked_archive_leaves_the_source_alone(rt, archived, patched_meta):
    _committed(rt, archive=False)
    assert archived == []


def test_a_refusal_before_any_claim_retries_under_the_same_id(rt, archived, patched_meta):
    rt.submit_result = structured_runtime.StructuredError(409, "this session is starting")
    h, out = _committed(rt)
    assert out["delivery"] is None and out["detail"]
    key = out["id"]
    assert handoff.api_blocks(key, str(uuid.uuid4())) is True  # still bound, still first
    rt.submit_result = {"handoff": "sent"}
    again = _run(handoff_api.commit(h, None, bypass=False, archive_source=True))
    assert again["id"] == key and again["delivery"] == "sent"
    assert [s["id"] for s in rt.submits] == [handoff.api_turn_id(h)] * 2
    assert archived == [SOURCE]


def test_a_recorded_not_sent_is_never_resubmitted_and_lifts_the_fence(rt, archived, patched_meta):
    rt.submit_result = structured_runtime.StructuredError(409, "this turn was not sent")
    rt.record_on_refusal = True  # the journal holds the turn: a real recorded refusal
    h, out = _committed(rt)
    assert out["delivery"] == "not_sent"
    assert handoff.api_blocks(out["id"], str(uuid.uuid4())) is False  # Send again is free
    rt.submit_result = {"handoff": "sent"}
    again = _run(handoff_api.commit(h, None, bypass=False, archive_source=True))
    assert again["delivery"] == "not_sent"
    assert len(rt.submits) == 1  # the commit retry replays, never resubmits
    assert archived == [] and patched_meta == []


@pytest.mark.parametrize(
    "result",
    [
        TimeoutError("lost"),
        structured_runtime.StructuredError(503, "worker unreachable"),
        {"handoff": "uncertain"},
    ],
)
def test_an_uncertain_outcome_is_never_resent(rt, archived, patched_meta, result):
    rt.submit_result = result
    h, out = _committed(rt)
    assert out["delivery"] == "uncertain"
    rt.submit_result = {"handoff": "sent"}
    again = _run(handoff_api.commit(h, None, bypass=False, archive_source=True))
    assert again["delivery"] == "uncertain" and len(rt.submits) == 1
    assert archived == []


def test_a_queued_brief_writes_provenance_but_does_not_archive(rt, archived, patched_meta):
    rt.submit_result = {"handoff": "queued"}
    _h, out = _committed(rt)
    assert out["delivery"] == "queued"
    assert patched_meta and archived == []


def test_concurrent_commits_send_the_brief_once(rt, archived, patched_meta):
    h = _handle()

    async def both():
        return await asyncio.gather(
            handoff_api.commit(h, None, bypass=False, archive_source=False),
            handoff_api.commit(h, None, bypass=False, archive_source=False),
        )

    a, b = _run(both())
    assert a["id"] == b["id"]
    assert len(rt.submits) == 1


# ---- skip permissions: the brief follows the operator's Start -----------------------------------


def test_a_skip_handoff_creates_unstarted_and_sends_nothing(rt, archived, patched_meta):
    h, out = _committed(rt, bypass=True)
    assert out["delivery"] == "pending_start"
    assert rt.creates[0]["bypass"] is True  # a skip creation launches nothing until Start
    assert rt.submits == []
    assert handoff.api_blocks(out["id"], str(uuid.uuid4())) is True


def test_the_brief_is_sent_once_the_start_is_ready(rt, archived, patched_meta, monkeypatch):
    monkeypatch.setattr(handoff_api, "START_POLL_S", 0.0)
    h, out = _committed(rt, bypass=True)
    key = out["id"]

    async def start_then_ready():
        rt.snap_extra = {"launch": {"state": "starting"}}
        assert handoff_api.after_start(key) is True
        handoff_api.after_start(key)  # a repeated Start: a second waiter, still ONE send
        await asyncio.sleep(0)
        assert rt.submits == []  # not while it is still starting
        rt.snap_extra = {"launch": {"state": "ready"}}
        await asyncio.gather(*list(handoff_api._TASKS))

    _run(start_then_ready())
    assert [s["id"] for s in rt.submits] == [handoff.api_turn_id(h)]
    assert archived == [SOURCE]


def test_a_failed_start_keeps_the_brief_for_start_again(rt, archived, patched_meta, monkeypatch):
    monkeypatch.setattr(handoff_api, "START_POLL_S", 0.0)
    h, out = _committed(rt, bypass=True)
    key = out["id"]

    async def go(state):
        rt.snap_extra = {"launch": {"state": state}}
        handoff_api.after_start(key)
        await asyncio.gather(*list(handoff_api._TASKS))

    _run(go("failed"))
    assert rt.submits == [] and handoff.api_binding(key)["delivery"] is None
    _run(go("ready"))  # Start again
    assert [s["id"] for s in rt.submits] == [handoff.api_turn_id(h)]


def test_a_discarded_start_releases_the_brief(rt, archived, patched_meta, monkeypatch):
    monkeypatch.setattr(handoff_api, "START_POLL_S", 0.0)
    _h, out = _committed(rt, bypass=True)
    key = out["id"]

    async def go():
        rt.snap_extra = {"start_discarded": True}
        handoff_api.after_start(key)
        await asyncio.gather(*list(handoff_api._TASKS))

    _run(go())
    assert rt.submits == [] and archived == [] and patched_meta == []
    assert handoff.api_binding(key)["delivery"] == "released"
    assert handoff.api_blocks(key, str(uuid.uuid4())) is False


# ---- the route ----------------------------------------------------------------------------------


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


def _hdr(csrf, cfg):
    return {"X-CSRF-Token": csrf, "Origin": cfg.origin}


@pytest.fixture
def ready_api(monkeypatch):
    """Every API client can start; console binaries are launchable."""
    state = {"ready": True}
    monkeypatch.setattr(
        engines, "launchable_bin", lambda p: f"/usr/bin/{p.engine_id}" if p else None
    )
    real = engines.registry.can_start
    monkeypatch.setattr(
        engines.registry,
        "can_start",
        lambda p: state["ready"] if handoff.is_api_target(p) else real(p),
    )
    monkeypatch.setattr(engines.registry, "api_unavailable_reason", lambda p: "not signed in")
    return state


def _prepare(c, csrf, cfg, target):
    r = c.post(
        "/api/handoff/prepare",
        json={"source_id": SOURCE, "target_engine": target, "mode": "quick"},
        headers=_hdr(csrf, cfg),
    )
    assert r.status_code == 200, r.text
    return r.json()["handle"]


def test_the_engines_tile_and_the_route_agree_on_an_api_target(
    auth_cfg, fake_jsonl, ready_api, rt, archived, patched_meta
):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    rows = {r["id"]: r for r in c.get("/api/engines").json()["engines"]}
    assert rows["claude-api"]["supports_seed_start"] is True
    handle = _prepare(c, csrf, auth_cfg, "claude-api")
    r = c.post(
        "/api/handoff",
        json={"handle": handle, "bypass": False, "archive_source": True},
        headers=_hdr(csrf, auth_cfg),
    )
    assert r.status_code == 200, r.text
    assert r.json()["api"] is True and r.json()["delivery"] == "sent"


def test_readiness_is_checked_again_at_commit(auth_cfg, fake_jsonl, ready_api, rt):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    handle = _prepare(c, csrf, auth_cfg, "claude-api")
    ready_api["ready"] = False  # signed out between prepare and commit
    r = c.post("/api/handoff", json={"handle": handle}, headers=_hdr(csrf, auth_cfg))
    assert r.status_code == 422 and "not signed in" in r.json()["detail"]
    assert rt.creates == []


def test_skip_is_refused_for_a_client_that_cannot_map_it(auth_cfg, fake_jsonl, ready_api, rt):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    handle = _prepare(c, csrf, auth_cfg, "opencode2-api")
    r = c.post(
        "/api/handoff", json={"handle": handle, "bypass": True}, headers=_hdr(csrf, auth_cfg)
    )
    assert r.status_code == 422 and "skip permission prompts" in r.json()["detail"]
    assert rt.creates == []


@pytest.mark.parametrize("bad", ["true", 1, None])
def test_the_options_are_real_booleans(auth_cfg, fake_jsonl, ready_api, rt, bad):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    handle = _prepare(c, csrf, auth_cfg, "claude-api")
    r = c.post("/api/handoff", json={"handle": handle, "bypass": bad}, headers=_hdr(csrf, auth_cfg))
    assert r.status_code == 422
    assert rt.creates == []


def test_a_console_target_refuses_bypass_and_commits_as_before(auth_cfg, fake_jsonl, ready_api):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    handle = _prepare(c, csrf, auth_cfg, "codex")
    r = c.post(
        "/api/handoff", json={"handle": handle, "bypass": True}, headers=_hdr(csrf, auth_cfg)
    )
    assert r.status_code == 422
    r = c.post("/api/handoff", json={"handle": handle}, headers=_hdr(csrf, auth_cfg))
    assert r.status_code == 200 and "api" not in r.json()


# ---- Send again completes a handoff whose first delivery was a recorded refusal (#2149) ---


def _not_sent(rt):
    rt.submit_result = structured_runtime.StructuredError(409, "this turn was not sent")
    rt.record_on_refusal = True
    h, out = _committed(rt)
    assert out["delivery"] == "not_sent"
    return h, out, rt.submits[0]["text"]


def test_a_successful_send_again_completes_provenance_and_archive_once(rt, archived, patched_meta):
    h, out, brief = _not_sent(rt)
    key, successor = out["id"], str(uuid.uuid4())
    assert archived == [] and patched_meta == []
    _run(handoff_api.reconcile_successor(key, successor, brief, "sent"))
    assert archived == [SOURCE]
    assert {k for k, _ in patched_meta} == {key, SOURCE}
    # Exactly once: a second accepted send (or a replay of the same one) changes nothing.
    _run(handoff_api.reconcile_successor(key, str(uuid.uuid4()), brief, "sent"))
    assert archived == [SOURCE]
    # …and the commit replay now reports it delivered, still without resending.
    again = _run(handoff_api.commit(h, None, bypass=False, archive_source=True))
    assert again["delivery"] == "sent" and len(rt.submits) == 1


@pytest.mark.parametrize(
    "case",
    ["uncertain", "other text", "own id", "queued"],
)
def test_send_again_reconciles_only_a_confirmed_resend_of_the_brief(
    rt, archived, patched_meta, case
):
    h, out, brief = _not_sent(rt)
    key = out["id"]
    op, text, code = str(uuid.uuid4()), brief, "sent"
    if case == "uncertain":
        code = "uncertain"
    elif case == "other text":
        text = "an unrelated message"
    elif case == "own id":
        op = handoff.api_turn_id(h)
    elif case == "queued":
        code = "queued"
    _run(handoff_api.reconcile_successor(key, op, text, code))
    if case == "queued":
        assert patched_meta and archived == []  # provenance only: it may still end not_sent
    else:
        assert archived == [] and patched_meta == []
        assert handoff.api_binding(key)["delivery"] == "not_sent"


def test_the_real_submit_boundary_reconciles_send_again(rt, archived, patched_meta, monkeypatch):
    h, out, brief = _not_sent(rt)
    key = out["id"]
    engine, _, native = key.partition(":")
    successor = str(uuid.uuid4())

    class Adapter:
        async def submit(self, *a, **kw):
            return {"handoff": "sent"}

        async def snapshot(self, engine_id, native_id):
            return {"revision": 2, "turns": [{"turn_id": successor, "kind": "operator"}]}

    # The REAL facade submit (the fake runtime above only stood in for handoff_api's calls).
    monkeypatch.setattr(structured_runtime, "submit_turn", _REAL_SUBMIT)
    monkeypatch.setattr(structured_runtime, "_key", lambda k: (engine, native))
    monkeypatch.setattr(structured_runtime, "_adapter", lambda e, op: Adapter())
    monkeypatch.setattr(structured_runtime, "_turn", lambda t, *a: {"turn_id": t["turn_id"]})
    from agent_sessions.routes import sessions as sessions_routes

    calls: list[str] = []

    async def archive_key(k):
        calls.append(k)
        return True, None

    monkeypatch.setattr(sessions_routes, "archive_key", archive_key)
    monkeypatch.setattr(metadata, "patch", lambda k, **kw: None)
    monkeypatch.setattr(metadata, "resolve_key", lambda k: k)
    sent = _run(structured_runtime.submit_turn(key, operation_id=successor, text=brief))
    assert sent["handoff"] == "sent"
    assert calls == [SOURCE]
