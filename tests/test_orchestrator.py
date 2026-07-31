"""Pulse orchestrator — decision core, gates, ledger and routes (#726 Phase 1).

Pinned here, in rough order of how badly it would hurt to get them wrong:

* **The two pre-model gates.** A live, syntactically valid ``shell:*`` key must never reach the
  model (it is an agentless ``bash -l``, so a "continue" nudge would *execute*), and neither
  must an ``orchestrator_excluded`` session.
* **Anti-hallucination.** Ids are validated against the slice ACTUALLY SENT this pass, not the
  catalog; unknown/malformed ids are dropped rather than acted on.
* **The autonomy ceiling is enforced, not defaulted** — ``allowed_verbs`` cannot name a verb
  outside ``AUTO_VERBS_V1``, via the validator *or* by hand-editing prefs.json.
* **Tier + threshold decide state**, and a low-confidence action escalates rather than acting.
* **Ledger crash semantics** — torn tail dropped, ``claimed`` recovered to ``indeterminate``
  and never auto-retried, compaction bounded.
* **Route contract** — cache-only GET, 409 unconfigured, 422 on a bad evidence kind, CSRF.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_sessions import (
    aitasks,
    metadata,
    orchestrator,
    orchestrator_loop,
    prefs,
    pulse,
    pulse_chat,
    review,
)
from agent_sessions import (
    orchestrator_ledger as ledger,
)
from agent_sessions.main import create_app

SECRET = "sk-orch-test"  # noqa: S105 — test fixture value
BASE = "https://ai.test/v1"


@dataclass
class FakeSession:
    engine: str
    uuid: str
    cwd: str
    last_mtime: float
    first_user_message: str = "first message"
    archived: bool = False

    @property
    def short_uuid(self) -> str:
        return self.uuid[:8]


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Every test gets its own prefs + ledger, and a reset loop fingerprint cache."""
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    monkeypatch.setattr(review, "_TRANSPORT", None)
    orchestrator_loop.reset_state()
    aitasks.reset()
    yield
    aitasks.reset()


@pytest.fixture
def configured_ai():
    prefs.set_ai_review({"enabled": True, "base_url": BASE, "api_key": SECRET, "model": "m"})


def _setup(monkeypatch, sessions, meta=None):
    monkeypatch.setattr(pulse.engines, "scan_all", lambda: sessions)
    monkeypatch.setattr(pulse.metadata, "load", lambda *a, **k: meta or {})
    monkeypatch.setattr(pulse.metadata, "load_aliases", lambda *a, **k: {})
    monkeypatch.setattr(pulse.projects, "load", lambda *a, **k: {})
    monkeypatch.setattr(orchestrator.metadata, "load", lambda *a, **k: meta or {})
    monkeypatch.setattr(orchestrator.metadata, "load_aliases", lambda *a, **k: {})


def _transport(payload: dict, calls: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(payload)}}]}
        )

    return httpx.MockTransport(handler)


# --- the two pre-model gates ------------------------------------------------------------


def test_shell_session_never_reaches_the_model(monkeypatch):
    """A `shell:*` key is perfectly well-formed, so `parse_key` cannot catch it. The capability
    gate must: `shell` is a bare `bash -l`, and a nudge typed into one is a shell command."""
    now = time.time()
    _setup(
        monkeypatch,
        [
            FakeSession("shell", "11111111-1111-4111-8111-111111111111", "/a", now),
            FakeSession("claude", "22222222-2222-4222-8222-222222222222", "/a", now),
        ],
    )
    cards, skipped = orchestrator.eligible_cards(now=now)
    assert [c["engine"] for c in cards] == ["claude"]
    assert skipped["engine"] == 1


def test_capability_is_default_deny_for_an_unknown_provider():
    """A provider that never declares the flag is NOT actuable. This is what stops the next
    agentless engine from silently inheriting the hole `shell` would have opened."""
    from agent_sessions.engines import registry

    class NewEngine:
        engine_id = "brandnew"

    assert registry.supports_orchestrator_input(NewEngine()) is False
    assert registry.supports_orchestrator_input(None) is False
    assert "shell" not in registry.orchestrator_input_engines()
    assert {"claude", "codex", "opencode", "gemini", "antigravity", "kimi"} <= (
        registry.orchestrator_input_engines()
    )


def test_orchestrator_excluded_session_is_filtered_before_the_model(monkeypatch):
    now = time.time()
    key = "claude:33333333-3333-4333-8333-333333333333"
    _setup(
        monkeypatch,
        [FakeSession("claude", "33333333-3333-4333-8333-333333333333", "/a", now)],
        {key: metadata.SessionMeta(orchestrator_excluded=True)},
    )
    cards, skipped = orchestrator.eligible_cards(now=now)
    assert cards == []
    assert skipped["excluded"] == 1


def test_exclusion_is_separate_from_review_excluded(tmp_home):  # noqa: ARG001 — isolates $HOME
    """Opting out of orchestration must not opt the session out of AI review — that is the whole
    reason this is its own flag rather than a reuse of `review_excluded`."""
    key = "claude:44444444-4444-4444-8444-444444444444"
    m = metadata.patch(key, orchestrator_excluded=True)
    assert m.orchestrator_excluded is True
    assert m.review_excluded is False  # untouched — the session is still AI-reviewed
    assert metadata.get(key).orchestrator_excluded is True  # round-trips through the sidecar


# --- anti-hallucination -----------------------------------------------------------------


def test_ids_validated_against_the_slice_actually_sent():
    sent = {"claude:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa": {}}
    obj = {
        "assessment": "x",
        "actions": [
            {
                "session_id": "claude:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "verb": "continue",
                "confidence": 0.9,
            },
            {
                "session_id": "claude:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "verb": "continue",
                "confidence": 0.9,
            },  # never sent → dropped
            {"session_id": "not-a-key", "verb": "continue", "confidence": 0.9},
            {
                "session_id": "claude:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "verb": "continue",
                "confidence": 0.9,
            },  # duplicate → collapsed
        ],
    }
    _, actions = orchestrator._validate_actions(obj, sent)
    assert [a["session_id"] for a in actions] == ["claude:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"]


def test_unknown_verb_and_evidence_kind_are_rejected():
    sent = {"claude:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa": {}}
    obj = {
        "actions": [
            {
                "session_id": "claude:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "verb": "rm -rf",
                "confidence": 1.0,
            },
        ]
    }
    assert orchestrator._validate_actions(obj, sent)[1] == []
    obj["actions"][0]["verb"] = "continue"
    obj["actions"][0]["evidence"] = "../../etc/passwd"
    assert orchestrator._validate_actions(obj, sent)[1][0]["evidence"] == "none"


def test_incomplete_choose_and_answer_degrade_to_escalate():
    """A `choose` with no usable option must not be invented into a deliverable action — the
    honest outcome is to show the operator the session."""
    sent = {"claude:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa": {}}
    for bad in ({"verb": "choose", "option": 99}, {"verb": "choose"}, {"verb": "answer"}):
        obj = {
            "actions": [
                {
                    "session_id": "claude:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "confidence": 0.99,
                    **bad,
                }
            ]
        }
        assert orchestrator._validate_actions(obj, sent)[1][0]["verb"] == "escalate"


def test_confidence_is_clamped_not_trusted():
    sent = {"claude:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa": {}}
    for raw, want in ((5, 1.0), (-3, 0.0), ("high", 0.0), (True, 0.0)):
        obj = {
            "actions": [
                {
                    "session_id": "claude:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "verb": "continue",
                    "confidence": raw,
                }
            ]
        }
        assert orchestrator._validate_actions(obj, sent)[1][0]["confidence"] == want


# --- the enforced autonomy ceiling ------------------------------------------------------


def test_allowed_verbs_ceiling_rejects_answer_and_choose():
    err = prefs.validate_orchestrator_patch({"allowed_verbs": ["continue", "answer"]})
    assert err is not None and "answer" in err
    assert prefs.validate_orchestrator_patch({"allowed_verbs": ["continue"]}) is None


def test_ceiling_survives_a_hand_edited_prefs_file(tmp_path):
    """The validator only sees API writes. A file edited on disk must be clamped on READ, or
    the ceiling is advisory rather than enforced."""
    p = tmp_path / "prefs.json"
    p.write_text(
        json.dumps({"orchestrator": {"allowed_verbs": ["continue", "answer", "dispatch"]}})
    )
    assert prefs.get_orchestrator(p)["allowed_verbs"] == ["continue"]


def test_yolo_never_auto_approves_a_verb_outside_the_ceiling():
    cfg = dict(prefs.get_orchestrator())
    cfg.update(enabled=True, autonomy="yolo", allowed_verbs=["continue"], confidence_min=0.5)
    assert orchestrator._decide({"verb": "continue", "confidence": 0.99}, cfg) == "approved"
    # answer/choose are outside the ceiling → supervised no matter how confident
    assert orchestrator._decide({"verb": "answer", "confidence": 0.99}, cfg) == "proposed"
    assert orchestrator._decide({"verb": "choose", "confidence": 0.99}, cfg) == "proposed"


def test_tier_and_threshold_decide_state():
    cfg = dict(prefs.get_orchestrator())
    cfg.update(enabled=True, allowed_verbs=["continue"], confidence_min=0.75)
    cfg["autonomy"] = "suggest"
    assert orchestrator._decide({"verb": "continue", "confidence": 0.99}, cfg) == "proposed"
    cfg["autonomy"] = "yolo"
    assert orchestrator._decide({"verb": "continue", "confidence": 0.74}, cfg) == "escalated"
    assert orchestrator._decide({"verb": "continue", "confidence": 0.75}, cfg) == "approved"
    cfg["autonomy"] = "off"
    assert orchestrator._decide({"verb": "continue", "confidence": 0.99}, cfg) == "proposed"
    # decisions, not deliveries — never consult the ceiling
    assert orchestrator._decide({"verb": "escalate", "confidence": 0.1}, cfg) == "escalated"
    assert orchestrator._decide({"verb": "observe", "confidence": 0.1}, cfg) == "observed"


# --- the pass ---------------------------------------------------------------------------


def test_run_pass_records_proposals_with_a_precondition(monkeypatch, configured_ai):
    now = time.time()
    uid = "55555555-5555-4555-8555-555555555555"
    _setup(monkeypatch, [FakeSession("claude", uid, "/a", now)])
    monkeypatch.setattr(orchestrator.scrollback, "live_tail_text", lambda *a, **k: "› waiting")
    calls: list = []
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _transport(
            {
                "assessment": "one idle",
                "actions": [
                    {
                        "session_id": f"claude:{uid}",
                        "verb": "continue",
                        "confidence": 0.9,
                        "rationale": "stopped early",
                    }
                ],
            },
            calls,
        ),
    )
    report = asyncio.run(orchestrator.run_pass(now=now))
    assert len(calls) == 1  # exactly ONE endpoint call per pass
    assert len(report["actions"]) == 1
    rec = report["actions"][0]
    assert rec["verb"] == "continue" and rec["state"] == "proposed"
    # a deliverable verb binds the screen it was derived from, for Phase 2 to re-verify
    assert set(rec["precondition"]) == {"key", "screen_fingerprint", "prompt_class", "observed_at"}
    assert ledger.get(rec["id"])["state"] == "proposed"


def test_run_pass_respects_max_actions_per_pass(monkeypatch, configured_ai):
    now = time.time()
    uids = [f"6666666{i}-6666-4666-8666-666666666666" for i in range(6)]
    _setup(monkeypatch, [FakeSession("claude", u, "/a", now) for u in uids])
    prefs.set_orchestrator({"max_actions_per_pass": 2})
    monkeypatch.setattr(orchestrator.scrollback, "live_tail_text", lambda *a, **k: "x")
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _transport(
            {
                "actions": [
                    {"session_id": f"claude:{u}", "verb": "observe", "confidence": 0.5}
                    for u in uids
                ]
            }
        ),
    )
    assert len(asyncio.run(orchestrator.run_pass(now=now))["actions"]) == 2


def test_run_pass_unconfigured_raises_before_touching_the_filesystem(monkeypatch):
    called = []
    monkeypatch.setattr(orchestrator, "eligible_cards", lambda **k: called.append(1) or ([], {}))
    with pytest.raises(review.NotConfiguredError):
        asyncio.run(orchestrator.run_pass())
    assert called == []


# --- ledger ------------------------------------------------------------------------------


def test_ledger_torn_tail_is_dropped(tmp_path):
    p = tmp_path / "l.jsonl"
    ledger.append({"id": "a", "state": "proposed"}, p)
    with p.open("a") as fh:
        fh.write('{"id": "b", "sta')  # crash mid-append
    assert [r["id"] for r in ledger.read_all(p)] == ["a"]


def test_ledger_is_0600(tmp_path):
    p = tmp_path / "l.jsonl"
    ledger.append({"id": "a", "state": "proposed"}, p)
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_claimed_recovers_to_indeterminate_and_is_never_retried(tmp_path):
    """Nothing on disk can prove whether a `claimed` action's bytes reached the PTY, so it must
    be parked rather than retried (double-delivery) or assumed delivered (silent drop)."""
    p = tmp_path / "l.jsonl"
    ledger.append({"id": "a", "state": "claimed", "verb": "choose", "option": 1}, p)
    assert ledger.recover_claimed(p) == ["a"]
    rec = ledger.get("a", p)
    assert rec["state"] == "indeterminate"
    assert rec["verb"] == "choose"  # merge-forward keeps the original payload for the operator
    assert ledger.recover_claimed(p) == []  # idempotent


def test_expiry_skips_claimed_actions(tmp_path):
    p = tmp_path / "l.jsonl"
    past = time.time() - 1
    ledger.append({"id": "old", "state": "proposed", "expires_at": past}, p)
    ledger.append({"id": "mid", "state": "claimed", "expires_at": past}, p)
    assert ledger.expire_due(path=p) == ["old"]
    assert ledger.get("mid", p)["state"] == "claimed"


def test_compaction_keeps_live_and_bounds_history(tmp_path):
    p = tmp_path / "l.jsonl"
    ledger.append({"id": "live", "state": "proposed"}, p)
    for i in range(20):
        ledger.append({"id": f"done{i}", "state": "delivered"}, p)
    kept = ledger.compact(p, history_max=5)
    states = [r["state"] for r in ledger.latest_by_id(p).values()]
    assert kept == 6
    assert states.count("proposed") == 1 and states.count("delivered") == 5


# --- loop gating -------------------------------------------------------------------------


def test_loop_skips_when_disabled_unconfigured_and_unchanged(monkeypatch):
    assert asyncio.run(orchestrator_loop.sweep())["skipped"] == "disabled"
    prefs.set_orchestrator({"enabled": True})
    assert asyncio.run(orchestrator_loop.sweep())["skipped"] == "unconfigured"
    prefs.set_ai_review({"enabled": True, "base_url": BASE, "api_key": SECRET, "model": "m"})
    monkeypatch.setattr(orchestrator, "eligible_cards", lambda **k: ([], {}))
    assert asyncio.run(orchestrator_loop.sweep())["skipped"] == "empty"


def test_loop_skips_an_unchanged_world(monkeypatch, configured_ai):
    prefs.set_orchestrator({"enabled": True})
    cards = [
        {
            "id": "claude:x",
            "state": "idle",
            "intervention_required": False,
            "_review_fingerprint": "fp",
        }
    ]
    monkeypatch.setattr(orchestrator, "eligible_cards", lambda **k: (cards, {}))
    ran = []

    async def fake_pass(**kw):
        ran.append(1)
        return {"actions": []}

    monkeypatch.setattr(orchestrator, "run_pass", fake_pass)
    assert asyncio.run(orchestrator_loop.sweep()).get("ran") is True
    assert asyncio.run(orchestrator_loop.sweep())["skipped"] == "unchanged"
    assert len(ran) == 1
    # a session that now needs the user IS a change
    cards[0]["intervention_required"] = True
    assert asyncio.run(orchestrator_loop.sweep()).get("ran") is True


def test_loop_env_kill_switch(monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LOOP", "0")
    assert orchestrator_loop.loop_enabled() is False
    asyncio.run(orchestrator_loop.run())  # returns immediately, never sweeps


# --- routes -------------------------------------------------------------------------------


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


def test_get_orchestrator_never_runs_a_pass(auth_cfg, fake_jsonl, monkeypatch):  # noqa: ARG001
    """Same contract as `GET /api/pulse`: cache-only, instant, never triggers work."""
    ran = []
    monkeypatch.setattr(orchestrator, "run_pass", lambda **k: ran.append(1))
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    body = c.get("/api/pulse/orchestrator").json()
    assert body["config"]["autonomy"] == "suggest"
    assert body["config"]["auto_verbs_ceiling"] == ["continue"]
    assert body["pending"] == [] and body["feed"] == []
    assert ran == []


def test_config_exposes_the_ceiling_so_the_ui_can_show_it(auth_cfg, fake_jsonl):  # noqa: ARG001
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    orch = c.get("/api/config").json()["orchestrator"]
    assert orch["auto_verbs_ceiling"] == ["continue"]
    assert orch["allowed_verbs"] == ["continue"]
    assert "api_key" not in orch  # holds no secret of its own


def test_orchestrate_unconfigured_is_409(auth_cfg, fake_jsonl):  # noqa: ARG001
    """Contrast with /scan (which degrades to 200): a decision has no non-LLM fallback, so an
    unconfigured endpoint says so rather than returning an empty 'nothing needs you'."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post("/api/pulse/orchestrate", headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin})
    assert r.status_code == 409
    assert r.json()["configured"] is False


def test_orchestrate_requires_csrf(auth_cfg, fake_jsonl):  # noqa: ARG001
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.post("/api/pulse/orchestrate", headers={"Origin": auth_cfg.origin})
    assert r.status_code == 403


def test_evidence_rejects_bad_kind_and_unknown_id(auth_cfg, fake_jsonl):  # noqa: ARG001
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    key = "claude:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    assert c.get(f"/api/pulse/evidence/{key}?kind=../etc/passwd").status_code == 422
    assert c.get("/api/pulse/evidence/not-a-key?kind=screen").status_code == 404
    # a valid id with no observed output is an honest "unavailable", not an error
    body = c.get(f"/api/pulse/evidence/{key}?kind=screen").json()
    assert body["kind"] == "screen" and body["available"] is False


def test_prefs_route_rejects_a_verb_above_the_ceiling(auth_cfg, fake_jsonl):  # noqa: ARG001
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    r = c.post(
        "/api/prefs", json={"orchestrator": {"allowed_verbs": ["continue", "answer"]}}, headers=hdr
    )
    assert r.status_code == 422
    r = c.post("/api/prefs", json={"orchestrator": {"autonomy": "yolo"}}, headers=hdr)
    assert r.status_code == 200
    assert r.json()["orchestrator"]["autonomy"] == "yolo"
    # unknown key is a 422, never a silent no-op
    assert c.post("/api/prefs", json={"orchestrator": {"nope": 1}}, headers=hdr).status_code == 422


def test_orchestrator_exclude_toggle_does_not_touch_review_exclude(auth_cfg, fake_jsonl):  # noqa: ARG001
    """The whole reason this is its own flag: withdrawing agency must leave AI review alone."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    sid = c.get("/api/sessions").json()["sessions"][0]["id"]
    r = c.post(f"/api/sessions/{sid}/orchestrator-exclude", json={"excluded": True}, headers=hdr)
    assert r.status_code == 200 and r.json()["orchestrator_excluded"] is True
    row = next(s for s in c.get("/api/sessions").json()["sessions"] if s["id"] == sid)
    assert row["orchestrator_excluded"] is True
    assert row["review_excluded"] is False  # untouched
    # absent body toggles
    r = c.post(f"/api/sessions/{sid}/orchestrator-exclude", headers=hdr)
    assert r.json()["orchestrator_excluded"] is False
    assert c.post("/api/sessions/not-a-key/orchestrator-exclude", headers=hdr).status_code == 404


def test_orchestrator_exclude_requires_csrf(auth_cfg, fake_jsonl):  # noqa: ARG001
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    sid = c.get("/api/sessions").json()["sessions"][0]["id"]
    r = c.post(f"/api/sessions/{sid}/orchestrator-exclude", headers={"Origin": auth_cfg.origin})
    assert r.status_code == 403


# --- Hermes #727 review: regression tests for each reported defect ----------------------


def test_non_finite_confidence_is_zero_not_maximum():
    """`max(0, min(1, nan))` returns 1.0 — so a NaN confidence would clamp to MAXIMUM and
    auto-approve under yolo. json.loads accepts NaN, so this is reachable from a real model."""
    sent = {"claude:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa": {}}
    for raw in ("NaN", "Infinity", "-Infinity"):
        obj = json.loads(
            '{"actions":[{"session_id":"claude:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",'
            '"verb":"continue","confidence":' + raw + "}]}"
        )
        action = orchestrator._validate_actions(obj, sent)[1][0]
        assert action["confidence"] == 0.0, f"{raw} must not become usable confidence"
        cfg = dict(prefs.get_orchestrator())
        cfg.update(enabled=True, autonomy="yolo", allowed_verbs=["continue"], confidence_min=0.5)
        assert orchestrator._decide(action, cfg) == "escalated"


def test_concurrent_append_during_compaction_is_not_lost(tmp_path):
    """compact() replaces the ledger's inode; an append landing in the gap would write to the
    old, now-unlinked file and vanish — silently losing an approval or a claim."""
    import threading

    p = tmp_path / "l.jsonl"
    for i in range(30):
        ledger.append({"id": f"done{i}", "state": "delivered"}, p)
    ledger.append({"id": "live", "state": "proposed"}, p)

    errors: list = []

    def compactor():
        try:
            ledger.compact(p, history_max=2)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def appender():
        try:
            for i in range(20):
                ledger.append({"id": f"late{i}", "state": "proposed"}, p)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    # The race window is narrow, so repeat: with the lock this can never fail, and more
    # rounds only raise the odds of catching a REGRESSION that removes it.
    for round_ in range(4):
        errors.clear()
        ts = [threading.Thread(target=compactor), threading.Thread(target=appender)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert errors == []
        ids = set(ledger.latest_by_id(p))
        missing = {f"late{i}" for i in range(20)} - ids
        assert not missing, f"round {round_}: appends lost during compaction: {sorted(missing)}"


def test_concurrent_transitions_do_not_lose_one(tmp_path):
    """Two transitions racing must both land; a read-modify-append that isn't atomic can drop
    one entirely."""
    import threading

    p = tmp_path / "l.jsonl"
    for i in range(10):
        ledger.append({"id": f"a{i}", "state": "proposed"}, p)
    threads = [
        threading.Thread(target=ledger.transition, args=(f"a{i}", "rejected"), kwargs={"path": p})
        for i in range(10)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    states = {k: v["state"] for k, v in ledger.latest_by_id(p).items()}
    assert all(s == "rejected" for s in states.values()), states


def test_concurrent_partial_prefs_saves_both_survive(tmp_path):
    """Two partial saves must not erase each other: read-modify-write has to happen inside the
    file lock, or an acknowledged setting silently reverts."""
    import threading

    p = tmp_path / "prefs.json"
    prefs.set_orchestrator({}, p)
    barrier = threading.Barrier(2)

    def save(patch):
        barrier.wait()
        for _ in range(15):
            prefs.set_orchestrator(patch, p)

    ts = [
        threading.Thread(target=save, args=({"enabled": True},)),
        threading.Thread(target=save, args=({"autonomy": "yolo"},)),
    ]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    got = prefs.get_orchestrator(p)
    assert got["enabled"] is True and got["autonomy"] == "yolo", got


def test_the_age_bucket_tracks_AGE_not_the_activity_timestamp():
    """The digest sends `age_hours = (now - last_activity)/3600`, which advances while a session
    sits idle. Bucketing `last_activity` itself is CONSTANT for an idle session, so the digest
    could move from 1h to 2h with an identical fingerprint and the sweep would skip."""
    base_cfg = dict(prefs.get_orchestrator())
    card = {
        "id": "claude:x",
        "state": "idle",
        "intervention_required": False,
        "_review_fingerprint": "fp",
        "_recap_fingerprint": "",
        "last_activity": 10_000.0,
    }
    # Same session, same everything — only the clock advanced past an hour boundary.
    at_1h = orchestrator_loop.world_fingerprint([card], base_cfg, now=10_000.0 + 3600)
    at_2h = orchestrator_loop.world_fingerprint([card], base_cfg, now=10_000.0 + 7300)
    assert at_1h != at_2h, "an idle session ageing past an hour must invalidate the fingerprint"
    # And a sub-bucket tick must NOT, or every sweep would pay for a call.
    assert at_1h == orchestrator_loop.world_fingerprint([card], base_cfg, now=10_000.0 + 3900)


def test_policy_change_invalidates_change_detection():
    """Raising the tier or rewriting the prompt changes what a pass would DECIDE, so it must
    move the fingerprint — otherwise every sweep skips as 'unchanged' and the new policy never
    takes effect."""
    cards = [
        {
            "id": "claude:x",
            "state": "idle",
            "intervention_required": False,
            "_review_fingerprint": "fp",
        }
    ]
    base = dict(prefs.get_orchestrator())
    fp0 = orchestrator_loop.world_fingerprint(cards, base)
    for field, value in (
        ("autonomy", "yolo"),
        ("confidence_min", 0.9),
        ("allowed_verbs", []),
        ("max_actions_per_pass", 9),
        ("prompt", "totally different prompt"),
        ("nudge_template", "different nudge"),
    ):
        assert orchestrator_loop.world_fingerprint(cards, {**base, field: value}) != fp0, field


def test_consecutive_passes_actually_cover_the_tail(monkeypatch, configured_ai):
    """Not just "pass 2 runs" — pass 2 must see DIFFERENT sessions.

    Clearing the fingerprint alone re-ran `cards[:DIGEST_MAX]`, i.e. the same deterministic
    slice forever: cards beyond the cap were never once shown to the model, while every sweep
    still paid for a call. The window has to advance.
    """
    prefs.set_orchestrator({"enabled": True})
    n = orchestrator.DIGEST_MAX + 12
    cards = [
        {
            "id": f"claude:{i:08d}-0000-4000-8000-000000000000",
            "engine": "claude",
            "state": "idle",
            "intervention_required": False,
            "_review_fingerprint": "fp",
            "_recap_fingerprint": "",
            "last_activity": 1000.0,
            "title": f"s{i}",
            "project": {"name": "p", "id": "p1"},
            "ai_summary": "x",
        }
        for i in range(n)
    ]
    monkeypatch.setattr(orchestrator, "eligible_cards", lambda **k: (cards, {}))
    monkeypatch.setattr(orchestrator.scrollback, "live_tail_text", lambda *a, **k: "x")
    monkeypatch.setattr(review, "_TRANSPORT", _transport({"actions": []}))

    seen: list[set[str]] = []
    real_pass = orchestrator.run_pass

    async def spy(**kw):
        rep = await real_pass(**kw)
        seen.append(set(rep["consumed_ids"]))
        return rep

    monkeypatch.setattr(orchestrator, "run_pass", spy)
    asyncio.run(orchestrator_loop.sweep())
    asyncio.run(orchestrator_loop.sweep())

    assert len(seen) == 2, "the second sweep must run, not skip as unchanged"
    assert seen[0] != seen[1], "consecutive passes sent the SAME slice — the tail is starved"
    tail = {c["id"] for c in cards[orchestrator.DIGEST_MAX :]}
    assert tail & seen[1], "cards beyond the digest cap were never consumed"


def test_over_cap_passes_eventually_consume_every_session(monkeypatch, configured_ai):
    """The failure this guards is subtle: with <= DIGEST_MAX cards the rotation offset wraps to
    0, so an over-cap pass re-sends the IDENTICAL slice and — with deterministic model output —
    re-records the identical first action forever while the sessions behind it are never
    reached. Asserting the report fields is not enough; this asserts eventual CONSUMPTION."""
    now = time.time()
    uids = [f"a{i}bbbbbb-cccc-4ddd-8eee-ffffffffffff" for i in range(3)]
    keys = [f"claude:{u}" for u in uids]
    _setup(monkeypatch, [FakeSession("claude", u, "/a", now) for u in uids])
    prefs.set_orchestrator({"max_actions_per_pass": 1})
    monkeypatch.setattr(orchestrator.scrollback, "live_tail_text", lambda *a, **k: "x")
    # A deterministic model: always proposes for every session it is shown, in the same order.
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _transport(
            {"actions": [{"session_id": k, "verb": "observe", "confidence": 0.5} for k in keys]}
        ),
    )
    acted: set[str] = set()
    for _ in range(3):
        report = asyncio.run(orchestrator.run_pass(now=now))
        acted |= {a["session_id"] for a in report["actions"]}
    assert acted == set(keys), f"sessions never consumed: {sorted(set(keys) - acted)}"


def test_a_session_with_a_pending_action_is_not_proposed_again(monkeypatch):
    """The mechanism behind eventual consumption: an action already awaiting the operator makes
    its session ineligible, so each pass necessarily considers sessions the last one did not."""
    now = time.time()
    uid = "beefbeef-1111-4111-8111-111111111111"
    key = f"claude:{uid}"
    _setup(monkeypatch, [FakeSession("claude", uid, "/a", now)])
    assert [c["id"] for c in orchestrator.eligible_cards(now=now)[0]] == [key]
    ledger.append({"id": "p1", "state": "proposed", "session_id": key})
    cards, skipped = orchestrator.eligible_cards(now=now)
    assert cards == [] and skipped["pending"] == 1
    # Once it settles, the session is available again.
    ledger.transition("p1", "rejected")
    assert [c["id"] for c in orchestrator.eligible_cards(now=now)[0]] == [key]


def test_disabling_mid_flight_prevents_auto_approval(monkeypatch, configured_ai):
    """An operator who switches orchestration off while the model call is in flight must not
    find an auto-approved action waiting afterwards."""
    now = time.time()
    uid = "99999999-9999-4999-8999-999999999999"
    _setup(monkeypatch, [FakeSession("claude", uid, "/a", now)])
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo", "confidence_min": 0.5})
    monkeypatch.setattr(orchestrator.scrollback, "live_tail_text", lambda *a, **k: "x")

    def handler(request):
        # The operator disables orchestration WHILE the endpoint call is in flight.
        prefs.set_orchestrator({"enabled": False})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "actions": [
                                        {
                                            "session_id": f"claude:{uid}",
                                            "verb": "continue",
                                            "confidence": 0.99,
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
    report = asyncio.run(orchestrator.run_pass(now=now))
    states = [a["state"] for a in report["actions"]]
    assert "approved" not in states, f"auto-approved after being disabled mid-flight: {states}"


def test_disable_then_reenable_does_not_inherit_a_stale_unchanged_verdict(
    monkeypatch, configured_ai
):
    """Turning it back on must actually do something on the next sweep."""
    prefs.set_orchestrator({"enabled": True})
    cards = [
        {
            "id": "claude:x",
            "state": "idle",
            "intervention_required": False,
            "_review_fingerprint": "fp",
            "_recap_fingerprint": "",
            "last_activity": 1000.0,
        }
    ]
    monkeypatch.setattr(orchestrator, "eligible_cards", lambda **k: (cards, {}))
    runs = []

    async def fake_pass(**kw):
        runs.append(1)
        return {"actions": [], "truncated": False, "next_offset": 0}

    monkeypatch.setattr(orchestrator, "run_pass", fake_pass)
    assert asyncio.run(orchestrator_loop.sweep()).get("ran") is True
    assert asyncio.run(orchestrator_loop.sweep())["skipped"] == "unchanged"
    prefs.set_orchestrator({"enabled": False})
    assert asyncio.run(orchestrator_loop.sweep())["skipped"] == "disabled"
    prefs.set_orchestrator({"enabled": True})
    assert (
        asyncio.run(orchestrator_loop.sweep()).get("ran") is True
    ), "re-enabling inherited the stale fingerprint and skipped"
    assert len(runs) == 2


def test_a_session_excluded_mid_pass_is_dropped_before_being_recorded(monkeypatch, configured_ai):
    """Config and eligibility are snapshotted before the model call. If the operator withdraws
    agency while it is in flight, the stale response must not still be recorded as approved."""
    now = time.time()
    uid = "77777777-7777-4777-8777-777777777777"
    key = f"claude:{uid}"
    _setup(monkeypatch, [FakeSession("claude", uid, "/a", now)])
    monkeypatch.setattr(orchestrator.scrollback, "live_tail_text", lambda *a, **k: "x")
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _transport({"actions": [{"session_id": key, "verb": "continue", "confidence": 0.99}]}),
    )
    calls = {"n": 0}
    real = orchestrator.eligible_cards

    def flaky(**kw):
        # First call = the pre-model digest (session eligible). Second = the post-model
        # re-check, by which point the operator has excluded it.
        calls["n"] += 1
        cards, skipped = real(**kw)
        return ([], skipped) if calls["n"] > 1 else (cards, skipped)

    monkeypatch.setattr(orchestrator, "eligible_cards", flaky)
    report = asyncio.run(orchestrator.run_pass(now=now))
    assert report["actions"] == []
    assert report["dropped_ineligible"] == 1


def test_a_disabled_orchestrator_never_auto_approves():
    """`enabled` is not merely a scheduler toggle: with orchestration off, nothing may reach
    the approved state even at yolo with maximum confidence."""
    cfg = dict(prefs.get_orchestrator())
    cfg.update(enabled=False, autonomy="yolo", allowed_verbs=["continue"], confidence_min=0.5)
    assert orchestrator._decide({"verb": "continue", "confidence": 1.0}, cfg) == "proposed"


def test_recap_evidence_resolves_a_reconciled_sidecar_key(monkeypatch, tmp_home):  # noqa: ARG001
    """A reconciled opencode/codex session keeps its sidecar under the PLACEHOLDER key. Reading
    the logical key directly reports a real recap as unavailable — and the card, which resolves
    properly, would disagree with its own evidence."""
    placeholder = "opencode:new-abc123"
    real = "opencode:ses_1234567890abcdefghijklmn"
    metadata.patch(placeholder, ai_recap="what actually happened")
    monkeypatch.setattr(metadata, "resolve_key", lambda k: placeholder if k == real else k)
    assert orchestrator.evidence_for(real, "recap")["available"] is True
    assert "what actually happened" in orchestrator.evidence_for(real, "recap")["text"]


def test_work_restored_by_expiry_is_reconsidered(monkeypatch, configured_ai):
    """The interaction the pending-drain introduced: a pass stores the fingerprint of the world
    BEFORE its proposal exists. While that proposal is live the session is ineligible and
    sweeps return `empty` without touching the fingerprint — so when it expires, the world
    matches the saved value exactly and the sweep skips as `unchanged`, deferring restored work
    until some unrelated change moves the fingerprint."""
    prefs.set_orchestrator({"enabled": True})
    key = "claude:dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    card = {
        "id": key,
        "state": "idle",
        "intervention_required": False,
        "_review_fingerprint": "fp",
        "_recap_fingerprint": "",
        "last_activity": time.time(),
    }
    pending: list[str] = []
    monkeypatch.setattr(
        orchestrator,
        "eligible_cards",
        lambda **k: (([] if pending else [card]), {"engine": 0, "excluded": 0, "pending": 0}),
    )
    runs = []

    async def fake_pass(**kw):
        runs.append(1)
        pending.append("p")  # the pass appends a proposal → the session is now ineligible
        ledger.append(
            {"id": "act", "state": "proposed", "session_id": key, "expires_at": time.time() - 1}
        )
        return {"actions": [], "truncated": False, "next_offset": 0}

    monkeypatch.setattr(orchestrator, "run_pass", fake_pass)

    assert asyncio.run(orchestrator_loop.sweep()).get("ran") is True
    assert asyncio.run(orchestrator_loop.sweep())["skipped"] == "empty"
    # The action expires and its session becomes eligible again.
    pending.clear()
    assert (
        asyncio.run(orchestrator_loop.sweep()).get("ran") is True
    ), "restored work was skipped as unchanged instead of being reconsidered"
    assert len(runs) == 2


def test_expiry_outside_the_sweep_still_invalidates(monkeypatch, configured_ai):
    """Settlement happens on FOUR paths, not one: the scheduled sweep, `GET
    /api/pulse/orchestrator`'s housekeeping, an operator approving/rejecting, and startup
    recovery. Patching only the sweep-owned path leaves the others hidden — an operator who
    refreshes Pulse after a proposal lapses settles it via the route, and the next sweep then
    sees `expired=[]` with a world identical to the pre-proposal one.

    This is the outcome-level version: expire through the ROUTE's call, then assert a pass
    actually runs."""
    prefs.set_orchestrator({"enabled": True})
    key = "claude:eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    card = {
        "id": key,
        "state": "idle",
        "intervention_required": False,
        "_review_fingerprint": "fp",
        "_recap_fingerprint": "",
        "last_activity": time.time(),
    }
    pending: list[str] = []
    monkeypatch.setattr(
        orchestrator,
        "eligible_cards",
        lambda **k: (([] if pending else [card]), {"engine": 0, "excluded": 0, "pending": 0}),
    )
    runs = []

    async def fake_pass(**kw):
        runs.append(1)
        pending.append("p")
        ledger.append(
            {"id": "act-1", "state": "proposed", "session_id": key, "expires_at": time.time() - 1}
        )
        return {"actions": [], "truncated": False, "next_offset": 0}

    monkeypatch.setattr(orchestrator, "run_pass", fake_pass)
    assert asyncio.run(orchestrator_loop.sweep()).get("ran") is True

    # The operator refreshes Pulse. The state GET does its own housekeeping — settling the
    # action WITHOUT the sweep ever seeing an expiry of its own.
    assert ledger.expire_due() == ["act-1"]
    pending.clear()

    report = asyncio.run(orchestrator_loop.sweep())
    assert (
        report.get("ran") is True
    ), f"work restored by route-triggered expiry was hidden by the old fingerprint: {report}"
    assert len(runs) == 2


def test_an_idle_world_still_skips(monkeypatch, configured_ai):
    """The generation must not defeat change detection: with nothing appended and nothing
    changed, consecutive sweeps must still skip. Otherwise every interval pays for a call."""
    prefs.set_orchestrator({"enabled": True})
    card = {
        "id": "claude:ffffffff-ffff-4fff-8fff-ffffffffffff",
        "state": "idle",
        "intervention_required": False,
        "_review_fingerprint": "fp",
        "_recap_fingerprint": "",
        "last_activity": time.time(),
    }
    monkeypatch.setattr(orchestrator, "eligible_cards", lambda **k: ([card], {}))

    async def fake_pass(**kw):
        # A pass that proposes nothing writes nothing, so the generation is stable.
        return {"actions": [], "truncated": False, "next_offset": 0}

    monkeypatch.setattr(orchestrator, "run_pass", fake_pass)
    assert asyncio.run(orchestrator_loop.sweep()).get("ran") is True
    assert asyncio.run(orchestrator_loop.sweep())["skipped"] == "unchanged"
    assert asyncio.run(orchestrator_loop.sweep())["skipped"] == "unchanged"


def test_the_exclude_route_writes_INSIDE_the_write_fence(auth_cfg, fake_jsonl, monkeypatch):  # noqa: ARG001
    """A behavioural proof, through the real route, that the opt-out participates in the fence.

    `check_precondition` reads `orchestrator_excluded` in the final guard, which runs BEFORE
    `_write_all` takes the lock — so an opt-out committing in that window used to be invisible
    to the fence and the session still received input. Announcing AFTER the write is not
    enough either: between the write and the bump the stored state has already changed while
    the epoch still reads old.

    So the assertion is specifically that the metadata write happens WITH the registry lock
    held, which is what makes it un-interleavable with a send.
    """
    from agent_sessions import session_input

    observed: list[bool] = []
    real_patch = metadata.patch

    def watched_patch(*a, **k):
        if "orchestrator_excluded" in k:
            observed.append(session_input._lock.locked())
        return real_patch(*a, **k)

    monkeypatch.setattr(metadata, "patch", watched_patch)

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    sid = c.get("/api/sessions").json()["sessions"][0]["id"]
    r = c.post(f"/api/sessions/{sid}/orchestrator-exclude", json={"excluded": True}, headers=hdr)

    assert r.status_code == 200 and r.json()["orchestrator_excluded"] is True
    assert observed, "the exclude route never wrote orchestrator_excluded"
    assert all(observed), (
        "the opt-out was written WITHOUT the registry lock — it can land between the final "
        "guard and byte one, and the session still receives input"
    )


def test_the_chat_route_delivers_its_own_yolo_approvals(auth_cfg, fake_jsonl, monkeypatch):  # noqa: ARG001
    """Route-level proof, because the chat function alone cannot show this.

    Phase 2 added `deliver_pass_actions` and wired it into the scheduled and manual passes; the
    chat ROUTE predated it. So a `yolo` instruction produced an `approved` record that nothing
    delivered — those sweeps only deliver what their own `run_pass()` produced, and the chat's
    live action makes that session ineligible for them. It sat until a tap or expiry.

    Asserting on `orchestrator_chat.ask()` would prove only that the record is approved. What
    matters is that the ROUTE hands it to the actuator, so that is what this drives.
    """
    from agent_sessions import actuator

    prefs.set_ai_review({"enabled": True, "base_url": BASE, "api_key": SECRET, "model": "m"})
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo"})
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    sid = c.get("/api/sessions").json()["sessions"][0]["id"]

    monkeypatch.setattr(orchestrator.scrollback, "live_tail_text", lambda *a, **k: "x")
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _transport(
            {
                "intent": "instruct",
                "answer": "On it.",
                "actions": [
                    {"session_id": sid, "verb": "continue", "confidence": 0.99, "rationale": "go"}
                ],
            }
        ),
    )

    handed_to_actuator: list = []

    async def spy_deliver(records, *, registry=None):
        # Behaves like the real helper: it SETTLES the ledger. A spy that merely returns
        # "delivered" without persisting would let the route's ledger re-read report the stale
        # `approved` row and the test would pass for the wrong reason — the ledger is the
        # authority on state, which is the whole point of the re-read.
        handed_to_actuator.extend(records)
        out = []
        for r in records:
            ledger.compare_and_set(r["id"], frozenset({"approved"}), "delivered")
            out.append({**r, "state": "delivered"})
        return out

    monkeypatch.setattr(actuator, "deliver_pass_actions", spy_deliver)

    r = c.post("/api/pulse/chat", json={"query": "keep it going"}, headers=hdr)
    assert r.status_code == 200, r.text

    assert handed_to_actuator, (
        "the chat route never handed its approved action to the actuator — a yolo instruction "
        "returns an approved record that nothing delivers, and it sits until a tap or expiry"
    )
    # And the response must report the settled state, not the pre-delivery `approved` row.
    states = [a.get("state") for a in r.json().get("actions", [])]
    assert "approved" not in states, f"the reply still showed a pre-delivery state: {states}"


def test_the_chat_response_matches_the_ledger_when_another_caller_wins(  # noqa: PLR0913
    auth_cfg, fake_jsonl, monkeypatch
):  # noqa: ARG001
    """`deliver_pass_actions` deliberately omits an action another caller already claimed —
    `deliver_auto` returns None, or `deliver` raises NotDeliverable. Refreshing the response
    from that sparse list therefore leaves the ORIGINAL `approved` row in place while the
    ledger already says `delivered`, telling the operator a tap is still needed for something
    that has been sent.

    Reachable because this route persists the record BEFORE awaiting delivery, and
    approve/delivery callers are not fenced by the chat single-flight. So the ledger — not the
    helper's return value — is the authority on state.
    """
    from agent_sessions import actuator

    prefs.set_ai_review({"enabled": True, "base_url": BASE, "api_key": SECRET, "model": "m"})
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo"})
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    sid = c.get("/api/sessions").json()["sessions"][0]["id"]

    monkeypatch.setattr(orchestrator.scrollback, "live_tail_text", lambda *a, **k: "x")
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _transport(
            {
                "intent": "instruct",
                "answer": "On it.",
                "actions": [
                    {"session_id": sid, "verb": "continue", "confidence": 0.99, "rationale": "go"}
                ],
            }
        ),
    )

    async def another_caller_wins(records, *, registry=None):
        # Somebody else claimed and delivered it first, so this helper reports NOTHING —
        # exactly the sparse-list case. The ledger still moves.
        for r in records:
            ledger.compare_and_set(r["id"], frozenset({"approved"}), "delivered")
        return []

    monkeypatch.setattr(actuator, "deliver_pass_actions", another_caller_wins)

    r = c.post("/api/pulse/chat", json={"query": "keep it going"}, headers=hdr)
    assert r.status_code == 200, r.text

    reported = r.json()["actions"]
    assert reported, "the chat returned no actions"
    for a in reported:
        in_ledger = ledger.get(a["id"])
        assert a["state"] == in_ledger["state"], (
            f"the response said {a['state']!r} while the ledger says {in_ledger['state']!r} — "
            "the operator is told a tap is still needed for an action already settled"
        )


def test_a_history_question_never_reaches_the_actuator(auth_cfg, fake_jsonl, monkeypatch):  # noqa: ARG001
    """A read-only question must never cause a write.

    `ask()` overloads `actions`: for `instruct` it holds what the turn created, but for
    `history` it holds recent LEDGER ROWS shown for audit. Dispatching unconditionally meant
    asking "what did you do?" could hand an old `approved` row to the actuator and — under
    `yolo`, where approved means send — type it into a live session.
    """
    from agent_sessions import actuator

    prefs.set_ai_review({"enabled": True, "base_url": BASE, "api_key": SECRET, "model": "m"})
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo"})  # the dangerous tier
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    sid = c.get("/api/sessions").json()["sessions"][0]["id"]

    # An APPROVED row already in the ledger — exactly what a history answer surfaces.
    ledger.append(
        {
            "id": "old1",
            "state": "approved",
            "verb": "continue",
            "confidence": 0.99,
            "session_id": sid,
            "engine": "claude",
            "ts": time.time(),
        }
    )
    monkeypatch.setattr(orchestrator.scrollback, "live_tail_text", lambda *a, **k: "x")
    monkeypatch.setattr(review, "_TRANSPORT", _transport({"intent": "history"}))

    reached_actuator: list = []

    async def spy(records, *, registry=None):
        reached_actuator.extend(records)
        return []

    monkeypatch.setattr(actuator, "deliver_pass_actions", spy)

    r = c.post("/api/pulse/chat", json={"query": "what did you do?"}, headers=hdr)
    assert r.status_code == 200, r.text

    assert not reached_actuator, (
        "read-only history rows were passed to the actuator — asking a question can type into "
        f"a live session under yolo: {reached_actuator}"
    )
    # The history rows must still come back for display.
    assert r.json().get("intent") == "history"


# --- bell clearing + the reject linkage (#752) ---------------------------------------------


def _seed_note(action_id: str, title: str = "needs you", session: str = "claude:aaa"):
    from agent_sessions import notifications

    return notifications.add(
        title=title, project="p", session_id=session, engine="claude", action_id=action_id
    )


def test_dismiss_route_clears_named_rows_and_then_everything(
    auth_cfg, fake_jsonl, tmp_path, monkeypatch
):  # noqa: ARG001
    from agent_sessions import notifications

    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "n.json"))
    a = _seed_note("act-1", "one")
    _seed_note("act-2", "two")
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)

    r = c.post(
        "/api/pulse/notifications/dismiss",
        json={"ids": [a["id"]]},
        headers={"Origin": auth_cfg.origin, "X-CSRF-Token": csrf},
    )
    assert r.status_code == 200 and r.json()["dismissed"] == 1
    assert [n["title"] for n in r.json()["notifications"]] == ["two"]

    # Clearing everything has to be asked for explicitly.
    r = c.post(
        "/api/pulse/notifications/dismiss",
        json={"all": True},
        headers={"Origin": auth_cfg.origin, "X-CSRF-Token": csrf},
    )
    assert r.json()["dismissed"] == 1
    assert notifications.listing()["notifications"] == []


def test_a_malformed_dismiss_body_never_clears_the_bell(
    auth_cfg, fake_jsonl, tmp_path, monkeypatch
):  # noqa: ARG001
    """Fails CLOSED, unlike `/read`.

    `/read` coerces a missing or wrong-typed body to "every row", which is harmless for a
    read-flag. For a delete it means a plausible client typo — `{"ids": "n1"}` instead of a
    list — silently empties the operator's whole bell.
    """
    from agent_sessions import notifications

    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "n.json"))
    _seed_note("act-1", "one")
    _seed_note("act-2", "two", session="codex:bbb")
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    h = {"Origin": auth_cfg.origin, "X-CSRF-Token": csrf}

    for body in ({}, {"ids": "n1"}, {"ids": [1, 2]}, {"all": "yes"}, [], "nope"):
        r = c.post("/api/pulse/notifications/dismiss", json=body, headers=h)
        assert r.status_code == 422, f"{body!r} was accepted"
    assert len(notifications.listing()["notifications"]) == 2


def test_dismiss_route_requires_csrf(auth_cfg, fake_jsonl, tmp_path, monkeypatch):  # noqa: ARG001
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "n.json"))
    _seed_note("act-1")
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.post(
        "/api/pulse/notifications/dismiss",
        json={},
        headers={"Origin": auth_cfg.origin},  # no token
    )
    assert r.status_code == 403
    from agent_sessions import notifications

    assert len(notifications.listing()["notifications"]) == 1


def test_rejecting_an_action_also_retires_its_bell_row(auth_cfg, fake_jsonl, tmp_path, monkeypatch):  # noqa: ARG001
    """Deciding it in Pulse must not leave the operator to dismiss it a second time."""
    from agent_sessions import notifications

    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "n.json"))
    ledger.append(
        {"id": "act-1", "state": "escalated", "verb": "escalate", "session_id": "claude:aaa"}
    )
    _seed_note("act-1", "mine")
    _seed_note("act-other", "someone else's", session="codex:bbb")

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/pulse/actions/act-1/reject",
        headers={"Origin": auth_cfg.origin, "X-CSRF-Token": csrf},
    )
    assert r.status_code == 200 and r.json()["state"] == "rejected"
    assert [n["title"] for n in notifications.listing()["notifications"]] == ["someone else's"]


def test_a_failed_reject_never_destroys_an_alert(auth_cfg, fake_jsonl, tmp_path, monkeypatch):  # noqa: ARG001
    """The 404 and 409 paths must leave the bell alone.

    Clearing on a *failed* reject would retire the operator's only pointer to an action that is
    still live — or one already delivered, where the alert is the record that it happened.
    """
    from agent_sessions import notifications

    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "n.json"))
    ledger.append(
        {"id": "act-done", "state": "delivered", "verb": "continue", "session_id": "claude:aaa"}
    )
    _seed_note("act-done", "already delivered")
    _seed_note("act-ghost", "never existed", session="codex:bbb")

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    h = {"Origin": auth_cfg.origin, "X-CSRF-Token": csrf}

    assert c.post("/api/pulse/actions/act-done/reject", headers=h).status_code == 409
    assert c.post("/api/pulse/actions/act-ghost/reject", headers=h).status_code == 404
    assert len(notifications.listing()["notifications"]) == 2


def test_approving_an_action_also_retires_its_bell_row(auth_cfg, fake_jsonl, tmp_path, monkeypatch):  # noqa: ARG001
    """Delivering it is resolving it — the alert must not need a second dismissal."""
    from agent_sessions import actuator, notifications

    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "n.json"))
    _seed_note("act-1", "mine")
    _seed_note("act-other", "someone else's", session="codex:bbb")

    async def _delivered(action_id, **_k):
        return {"id": action_id, "state": "delivered"}

    monkeypatch.setattr(actuator, "deliver", _delivered)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/pulse/actions/act-1/approve",
        headers={"Origin": auth_cfg.origin, "X-CSRF-Token": csrf},
    )
    assert r.status_code == 200 and r.json()["state"] == "delivered"
    assert [n["title"] for n in notifications.listing()["notifications"]] == ["someone else's"]


def test_a_failed_approval_never_retires_the_alert(auth_cfg, fake_jsonl, tmp_path, monkeypatch):  # noqa: ARG001
    """A 409 means nothing was written, so the operator's pointer to a still-live action stays.

    Both failure shapes: the `stale`/`expired` body that returns 409, and `NotDeliverable`.
    """
    from agent_sessions import actuator, notifications

    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "n.json"))
    _seed_note("act-stale", "still mine")
    _seed_note("act-undeliverable", "also mine", session="codex:bbb")

    async def _stale(action_id, **_k):
        if action_id == "act-undeliverable":
            raise actuator.NotDeliverable("not deliverable")
        return {"id": action_id, "state": "stale", "detail": "the session moved on"}

    monkeypatch.setattr(actuator, "deliver", _stale)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    h = {"Origin": auth_cfg.origin, "X-CSRF-Token": csrf}

    assert c.post("/api/pulse/actions/act-stale/approve", headers=h).status_code == 409
    assert c.post("/api/pulse/actions/act-undeliverable/approve", headers=h).status_code == 409
    assert len(notifications.listing()["notifications"]) == 2


# --- Ask matches name what is waiting on the session ---------------------------------------


def _async_ret(value):
    async def _f(*_a, **_k):
        return value

    return _f


def _ask(c, csrf, cfg):
    return c.post(
        "/api/pulse/ask",
        json={"query": "where was I"},
        headers={"Origin": cfg.origin, "X-CSRF-Token": csrf},
    ).json()


def _matches(*ids):
    return {
        "answer": "found",
        "stage": "catalog",
        "configured": True,
        "matches": [{"id": i, "why": "w"} for i in ids],
    }


def test_ask_matches_are_annotated_with_the_live_action(auth_cfg, fake_jsonl, monkeypatch):  # noqa: ARG001
    """Finding the session is half an answer; "and something is waiting there" is the rest."""
    ledger.append(
        {"id": "act-1", "state": "escalated", "verb": "escalate", "session_id": "claude:aaa"}
    )
    monkeypatch.setattr(pulse_chat, "ask", _async_ret(_matches("claude:aaa", "codex:bbb")))
    c = _client(auth_cfg)
    a, b = _ask(c, _login(c, auth_cfg), auth_cfg)["matches"]
    assert a["pending"] == {"action_id": "act-1", "state": "escalated", "verb": "escalate"}
    assert "pending" not in b, "a session with nothing waiting must carry no flag"


def test_a_finished_action_is_history_not_an_errand(auth_cfg, fake_jsonl, monkeypatch):  # noqa: ARG001
    """Flagging a delivered or expired action sends the operator somewhere nothing waits —
    the fastest way to make them stop trusting the flag."""
    for i, state in enumerate(("delivered", "expired", "rejected")):
        ledger.append(
            {"id": f"a{i}", "state": state, "verb": "continue", "session_id": "claude:aaa"}
        )
    monkeypatch.setattr(pulse_chat, "ask", _async_ret(_matches("claude:aaa")))
    c = _client(auth_cfg)
    assert "pending" not in _ask(c, _login(c, auth_cfg), auth_cfg)["matches"][0]


def test_the_model_cannot_invent_a_pending_action(auth_cfg, fake_jsonl, monkeypatch):  # noqa: ARG001
    """The whole reason this is computed server-side.

    A model that can name a session can claim one needs attention. A false "something is waiting
    for you" costs a wasted trip AND the credibility of every true flag, so whatever the model
    says here is discarded and replaced by what the ledger actually holds.
    """
    payload = _matches("claude:aaa")
    payload["matches"][0]["pending"] = {"action_id": "made-up", "state": "escalated", "verb": "x"}
    monkeypatch.setattr(pulse_chat, "ask", _async_ret(payload))
    c = _client(auth_cfg)
    got = _ask(c, _login(c, auth_cfg), auth_cfg)["matches"][0]
    assert "pending" not in got, "a model-authored flag survived to the client"
