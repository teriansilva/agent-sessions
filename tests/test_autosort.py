"""AI auto-sorter (#424 Phase 6) against a MOCKED gateway (httpx.MockTransport — no real
network in CI) + a monkeypatched session scan.

Load-bearing guarantees: only genuinely-unassigned sessions are candidates (no explicit
project_id, resolves to a folder fallback), assignment is confidence-gated and never invents
an id, an unconfigured/empty setup is a no-op, and the background loop is gated off unless the
opt-in + a configured endpoint agree.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import httpx
import pytest

from agent_sessions import autosort, autosort_loop, metadata, prefs, projects, review

SECRET = "sk-autosort-test"  # noqa: S105 — test fixture value
BASE = "https://ai.test/v1"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(review, "_TRANSPORT", None)
    monkeypatch.setattr(autosort, "CALL_SPACING_S", 0)  # no inter-call sleep in tests
    yield


@pytest.fixture
def prefs_at_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))
    return tmp_path


@pytest.fixture
def configured(prefs_at_tmp):
    prefs.set_ai_review({"enabled": True, "base_url": BASE, "api_key": SECRET, "model": "m"})
    return prefs_at_tmp


def _seq_transport(replies: list[dict], calls: list | None = None):
    """A MockTransport that returns `replies` in order (last repeats)."""
    state = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(json.loads(request.content))
        reply = replies[min(state["i"], len(replies) - 1)]
        state["i"] += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(reply)}}]})

    return httpx.MockTransport(handler)


@dataclass
class FakeSession:
    engine: str
    uuid: str
    cwd: str
    first_user_message: str = ""
    short_uuid: str = ""


def _cand(key, cwd="/x", title="t", summary="s"):
    return {"key": key, "cwd": cwd, "title": title, "summary": summary}


def _proj(pid, name, folders=()):
    return projects.Project(id=pid, name=name, folders=tuple(folders))


# ---- candidate selection -------------------------------------------------------------


def test_candidate_payload_only_unassigned_folder_sessions(monkeypatch):
    sessions = [
        FakeSession("claude", "a", "/home/u/loose"),  # unassigned folder → candidate
        FakeSession("claude", "b", "/home/u/loose"),  # has explicit project_id → skip
        FakeSession("claude", "c", "/home/u/arch"),  # archived → skip
        FakeSession("claude", "d", "/home/u/owned"),  # cwd adopted by p-1 → skip (already belongs)
    ]
    metas = {
        "claude:a": metadata.SessionMeta(),
        "claude:b": metadata.SessionMeta(project_id="p-9"),
        "claude:c": metadata.SessionMeta(archived=True),
        "claude:d": metadata.SessionMeta(),
    }
    monkeypatch.setattr(autosort.engines, "scan_all", lambda: sessions)
    monkeypatch.setattr(autosort.metadata, "load", lambda: metas)
    monkeypatch.setattr(autosort.metadata, "load_aliases", lambda: {})
    index = {"p-1": _proj("p-1", "Owned", folders=("/home/u/owned",))}

    out = autosort._candidate_payload(index)
    assert [c["key"] for c in out] == ["claude:a"]
    assert out[0]["cwd"] == "/home/u/loose"


# ---- run_sort: classification + apply ------------------------------------------------


def test_run_sort_assigns_only_confident_known_ids(monkeypatch, configured):
    cands = [_cand("claude:a"), _cand("claude:b"), _cand("claude:c")]
    monkeypatch.setattr(autosort, "_candidate_payload", lambda idx: cands)
    monkeypatch.setattr(autosort.projects, "load", lambda: {"p-1": _proj("p-1", "Alpha")})
    monkeypatch.setattr(autosort.metadata, "resolve_key", lambda k: k)
    patched: list[tuple] = []
    monkeypatch.setattr(
        autosort.metadata, "patch", lambda key, **kw: patched.append((key, kw.get("project_id")))
    )
    # a → confident known id (assign); b → low confidence (skip); c → unknown id (skip)
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _seq_transport(
            [
                {"project_id": "p-1", "confidence": 0.95},
                {"project_id": "p-1", "confidence": 0.40},
                {"project_id": "p-nope", "confidence": 0.99},
            ]
        ),
    )

    report = asyncio.run(autosort.run_sort())
    assert patched == [("claude:a", "p-1")]
    assert [a["id"] for a in report["assigned"]] == ["claude:a"]
    assert report["low_confidence"] == 2  # low-conf b + unknown-id c
    assert report["scanned"] == 3
    assert report["errors"] == 0


def test_run_sort_caps_endpoint_calls(monkeypatch, configured):
    cands = [_cand(f"claude:{i}") for i in range(10)]
    monkeypatch.setattr(autosort, "_candidate_payload", lambda idx: cands)
    monkeypatch.setattr(autosort.projects, "load", lambda: {"p-1": _proj("p-1", "Alpha")})
    monkeypatch.setattr(autosort.metadata, "resolve_key", lambda k: k)
    monkeypatch.setattr(autosort.metadata, "patch", lambda key, **kw: None)
    calls: list = []
    monkeypatch.setattr(
        review, "_TRANSPORT", _seq_transport([{"project_id": "p-1", "confidence": 0.9}], calls)
    )
    report = asyncio.run(autosort.run_sort(cap=3))
    assert len(calls) == 3  # bounded — the rest wait for the next run
    assert report["scanned"] == 3
    assert report["candidates"] == 10


def test_run_sort_noop_without_projects(monkeypatch, configured):
    monkeypatch.setattr(autosort.projects, "load", lambda: {})
    calls: list = []
    monkeypatch.setattr(
        review, "_TRANSPORT", _seq_transport([{"project_id": "p-1", "confidence": 1}], calls)
    )
    report = asyncio.run(autosort.run_sort())
    assert report["skipped"] == "no projects"
    assert calls == []  # never reached the endpoint


def test_run_sort_noop_when_endpoint_unconfigured(monkeypatch, prefs_at_tmp):
    # ai_review is NOT configured (empty) → review.complete_json raises NotConfiguredError.
    monkeypatch.setattr(autosort, "_candidate_payload", lambda idx: [_cand("claude:a")])
    monkeypatch.setattr(autosort.projects, "load", lambda: {"p-1": _proj("p-1", "Alpha")})
    patched: list = []
    monkeypatch.setattr(autosort.metadata, "patch", lambda key, **kw: patched.append(key))
    report = asyncio.run(autosort.run_sort())
    assert report["skipped"] == "not configured"
    assert patched == []


# ---- background loop gating ----------------------------------------------------------


def test_loop_sweep_disabled_is_noop(monkeypatch, configured):
    # ai_review configured, but auto_sort.enabled is False (default) → gated off, no run_sort.
    called = {"n": 0}

    async def _fake_run_sort(**kw):
        called["n"] += 1
        return {}

    monkeypatch.setattr(autosort, "run_sort", _fake_run_sort)
    report = asyncio.run(autosort_loop.sweep())
    assert report["skipped"] == "disabled"
    assert called["n"] == 0


def test_loop_sweep_runs_when_enabled_and_configured(monkeypatch, configured):
    prefs.set_auto_sort({"enabled": True})
    called = {"n": 0}

    async def _fake_run_sort(**kw):
        called["n"] += 1
        return {"assigned": [], "candidates": 0, "scanned": 0}

    monkeypatch.setattr(autosort, "run_sort", _fake_run_sort)
    asyncio.run(autosort_loop.sweep())
    assert called["n"] == 1


def test_loop_disabled_by_env_kill_switch(monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_AUTO_SORT_LOOP", "0")
    assert autosort_loop.loop_enabled() is False
    # run() returns immediately without scheduling anything
    asyncio.run(autosort_loop.run())


# ---- prefs block ---------------------------------------------------------------------


def test_auto_sort_defaults_and_validation(prefs_at_tmp):
    d = prefs.get_auto_sort()
    assert d == {
        "enabled": False,
        "interval_minutes": 30,
        "confidence_min": 0.7,
        "max_per_pass": 8,
        "prompt": prefs.DEFAULT_AUTO_SORT_PROMPT,
    }
    assert prefs.validate_auto_sort_patch({"enabled": True, "interval_minutes": 15}) is None
    assert prefs.validate_auto_sort_patch({"enabled": "yes"}) is not None
    assert prefs.validate_auto_sort_patch({"interval_minutes": 1}) is not None  # below floor
    assert prefs.validate_auto_sort_patch({"bogus": 1}) is not None  # unknown key
    # New tunables (#459): bounds enforced.
    assert prefs.validate_auto_sort_patch({"confidence_min": 0.7}) is None
    assert prefs.validate_auto_sort_patch({"confidence_min": 0.4}) is not None  # below 0.5 floor
    assert prefs.validate_auto_sort_patch({"confidence_min": 1.0}) is not None  # above 0.95
    assert prefs.validate_auto_sort_patch({"confidence_min": True}) is not None  # bool rejected
    assert prefs.validate_auto_sort_patch({"max_per_pass": 8}) is None
    assert prefs.validate_auto_sort_patch({"max_per_pass": 0}) is not None
    assert prefs.validate_auto_sort_patch({"max_per_pass": 51}) is not None
    assert prefs.validate_auto_sort_patch({"prompt": "x" * 8001}) is not None
    prefs.set_auto_sort(
        {"enabled": True, "interval_minutes": 20, "confidence_min": 0.55, "max_per_pass": 3}
    )
    got = prefs.get_auto_sort()
    assert got["enabled"] is True
    assert got["interval_minutes"] == 20
    assert got["confidence_min"] == 0.55
    assert got["max_per_pass"] == 3


def test_auto_sort_empty_prompt_coerces_to_default(prefs_at_tmp):
    prefs.set_auto_sort({"prompt": "   "})  # whitespace can never strand the classifier
    assert prefs.get_auto_sort()["prompt"] == prefs.DEFAULT_AUTO_SORT_PROMPT
    prefs.set_auto_sort({"prompt": "Custom classifier prompt."})
    assert prefs.get_auto_sort()["prompt"] == "Custom classifier prompt."


def test_public_auto_sort_reports_endpoint_readiness(configured):
    pub = prefs.public_auto_sort()
    assert pub["configured"] is True  # mirrors the configured ai_review endpoint
    assert pub["default_prompt"] == prefs.DEFAULT_AUTO_SORT_PROMPT  # backs reset-to-default
    assert pub["confidence_min"] == 0.7
    assert pub["max_per_pass"] == 8
    assert "api_key" not in pub


# ---- run_sort tunables (#459) --------------------------------------------------------


def test_run_sort_threshold_from_prefs(monkeypatch, configured):
    # A 0.65-confidence known pick is skipped at the default 0.7 floor, but assigned once the
    # operator lowers the floor to 0.6 — the lever for the "no confident matches" case.
    prefs.set_auto_sort({"confidence_min": 0.6})
    monkeypatch.setattr(autosort, "_candidate_payload", lambda idx: [_cand("claude:a")])
    monkeypatch.setattr(autosort.projects, "load", lambda: {"p-1": _proj("p-1", "Alpha")})
    monkeypatch.setattr(autosort.metadata, "resolve_key", lambda k: k)
    monkeypatch.setattr(autosort.metadata, "patch", lambda key, **kw: None)
    monkeypatch.setattr(
        review, "_TRANSPORT", _seq_transport([{"project_id": "p-1", "confidence": 0.65}])
    )
    report = asyncio.run(autosort.run_sort())
    assert [a["id"] for a in report["assigned"]] == ["claude:a"]
    assert report["near_misses"] == []


def test_run_sort_reports_near_misses(monkeypatch, configured):
    # Known pick below the floor → not assigned, surfaced as a near-miss; an unknown-id pick is
    # counted in low_confidence but is NOT a near-miss (nothing to act on).
    cands = [_cand("claude:a"), _cand("claude:b")]
    monkeypatch.setattr(autosort, "_candidate_payload", lambda idx: cands)
    monkeypatch.setattr(autosort.projects, "load", lambda: {"p-1": _proj("p-1", "Alpha")})
    monkeypatch.setattr(autosort.metadata, "resolve_key", lambda k: k)
    monkeypatch.setattr(autosort.metadata, "patch", lambda key, **kw: None)
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _seq_transport(
            [
                {"project_id": "p-1", "confidence": 0.62},  # known, below floor → near-miss
                {"project_id": "p-nope", "confidence": 0.99},  # unknown id → not a near-miss
            ]
        ),
    )
    report = asyncio.run(autosort.run_sort())
    assert report["assigned"] == []
    assert report["low_confidence"] == 2
    assert report["near_misses"] == [{"id": "claude:a", "project_id": "p-1", "confidence": 0.62}]


def test_run_sort_cap_from_prefs(monkeypatch, configured):
    prefs.set_auto_sort({"max_per_pass": 2})
    cands = [_cand(f"claude:{i}") for i in range(10)]
    monkeypatch.setattr(autosort, "_candidate_payload", lambda idx: cands)
    monkeypatch.setattr(autosort.projects, "load", lambda: {"p-1": _proj("p-1", "Alpha")})
    monkeypatch.setattr(autosort.metadata, "resolve_key", lambda k: k)
    monkeypatch.setattr(autosort.metadata, "patch", lambda key, **kw: None)
    calls: list = []
    monkeypatch.setattr(
        review, "_TRANSPORT", _seq_transport([{"project_id": "p-1", "confidence": 0.9}], calls)
    )
    report = asyncio.run(autosort.run_sort())
    assert len(calls) == 2  # the configured per-run cap
    assert report["scanned"] == 2
    assert report["candidates"] == 10


def test_run_sort_uses_configured_prompt(monkeypatch, configured):
    prefs.set_auto_sort({"prompt": "CUSTOM-SORT-PROMPT"})
    monkeypatch.setattr(autosort, "_candidate_payload", lambda idx: [_cand("claude:a")])
    monkeypatch.setattr(autosort.projects, "load", lambda: {"p-1": _proj("p-1", "Alpha")})
    monkeypatch.setattr(autosort.metadata, "resolve_key", lambda k: k)
    monkeypatch.setattr(autosort.metadata, "patch", lambda key, **kw: None)
    calls: list = []
    monkeypatch.setattr(
        review, "_TRANSPORT", _seq_transport([{"project_id": "p-1", "confidence": 0.9}], calls)
    )
    asyncio.run(autosort.run_sort())
    assert calls[0]["messages"][0]["content"] == "CUSTOM-SORT-PROMPT"
