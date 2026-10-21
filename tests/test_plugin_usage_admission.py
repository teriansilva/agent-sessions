"""Captured usage sweeps preserve read views, but never retain permission to spawn."""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_sessions import agent_usage as usage
from agent_sessions.engines import registry
from agent_sessions.plugins import manager


@pytest.mark.parametrize("access", [False, True])
def test_revocation_between_reporters_skips_the_captured_callback(tmp_path, monkeypatch, access):
    entered, finish = threading.Event(), threading.Event()
    called = []
    target = "gemini" if access else "codex"
    before = registry.get(target)

    def first():
        entered.set()
        assert finish.wait(30)
        return usage.Report("claude", usage.SOURCE_PLAN, at=1)

    def revoked():
        called.append(True)
        return usage.Access(None) if access else usage.Report(target, usage.SOURCE_PLAN, at=1)

    monkeypatch.setattr(
        usage, "REPORTERS", {"claude": first, **({} if access else {target: revoked})}
    )
    monkeypatch.setattr(usage, "ACCESS_CHECKS", {target: revoked} if access else {})
    with ThreadPoolExecutor() as pool:
        result = pool.submit(usage.refresh, path=tmp_path / "usage.json", budgets={})
        assert entered.wait(30)
        try:
            manager.deactivate(str(uuid.uuid4()), target)
            assert not registry.admits(before)
        finally:
            finish.set()
        result.result(timeout=30)
    assert not called, "a captured callback ran after the provider was disabled"


@pytest.mark.parametrize("access", [False, True])
def test_revocation_after_binary_lookup_refuses_the_actual_usage_spawn(
    tmp_path, monkeypatch, access
):
    entered, finish = threading.Event(), threading.Event()
    spawned = []
    target = "gemini" if access else "claude"

    def binary(_engine):
        entered.set()
        assert finish.wait(30)
        return "/fixture/vendor"

    def spawn(*args, **kwargs):
        spawned.append(True)
        raise OSError("the revoked vendor must never spawn")

    callback = usage.check_gemini_access if access else usage.probe_claude
    monkeypatch.setattr(usage, "_probe_binary", binary)
    monkeypatch.setattr(usage.subprocess, "Popen", spawn)
    monkeypatch.setattr(usage, "REPORTERS", {} if access else {target: callback})
    monkeypatch.setattr(usage, "ACCESS_CHECKS", {target: callback} if access else {})
    with ThreadPoolExecutor() as pool:
        result = pool.submit(usage.refresh, path=tmp_path / "usage.json", budgets={})
        assert entered.wait(30)
        try:
            manager.deactivate(str(uuid.uuid4()), target)
        finally:
            finish.set()
        result.result(timeout=30)
    assert not spawned, "lookup admission was trusted after revocation"


def test_usage_spawn_holds_revocation_only_through_process_handoff(tmp_path, monkeypatch):
    import sys
    import time

    entered, allow_spawn = threading.Event(), threading.Event()
    answered, finish_read = threading.Event(), threading.Event()
    popen = usage.subprocess.Popen

    def spawn(*args, **kwargs):
        entered.set()
        assert allow_spawn.wait(30)
        return popen(*args, **kwargs)

    def done(_output):
        answered.set()
        assert finish_read.wait(30)
        return True

    def reporter():
        code, _ = usage._run(
            [sys.executable, "-c", "import time; print('READY', flush=True); time.sleep(30)"],
            done=done,
        )
        assert code == 0
        return usage.Report("claude", usage.SOURCE_PLAN, at=1)

    monkeypatch.setattr(usage.subprocess, "Popen", spawn)
    monkeypatch.setattr(usage, "REPORTERS", {"claude": reporter})
    monkeypatch.setattr(usage, "ACCESS_CHECKS", {})
    with ThreadPoolExecutor(max_workers=2) as pool:
        refresh = pool.submit(usage.refresh, path=tmp_path / "usage.json", budgets={})
        try:
            assert entered.wait(30)
            revoke = pool.submit(manager.deactivate, str(uuid.uuid4()), "claude")
            time.sleep(0.15)
            assert not revoke.done(), "revocation committed before spawn settled"
            allow_spawn.set()
            assert answered.wait(30)
            assert revoke.result(timeout=30)["state"] == "complete"
            assert not refresh.done(), "the probe is still waiting for its response"
        finally:
            allow_spawn.set()
            finish_read.set()
        refresh.result(timeout=30)
