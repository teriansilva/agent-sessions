"""Revocation cannot commit in another worker while process creation is unsettled."""

from __future__ import annotations

import asyncio
import json
import os
import select
import subprocess
import sys
import uuid
from types import SimpleNamespace

import pytest

from agent_sessions import engines
from agent_sessions.plugins import admission, manager, storage


def test_revocation_waits_for_process_handoff_in_another_worker():
    prov = engines.get("claude")
    script = """
import sys
from agent_sessions.plugins import manager
print("ready", flush=True)
sys.stdin.readline()
print("attempting", flush=True)
manager.deactivate(sys.argv[1], "claude")
print("committed", flush=True)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(uuid.uuid4())],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ),
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        with admission.acquire(prov) as guard:
            assert not guard.reason
            child.stdin.write("start\n")
            child.stdin.flush()
            assert child.stdout.readline().strip() == "attempting"
            assert not select.select([child.stdout], [], [], 0.15)[0]
            assert "claude" not in manager.snapshot()["plugins"]
        assert child.stdout.readline().strip() == "committed"
        assert child.wait(timeout=10) == 0
        with admission.acquire(prov) as guard:
            assert guard.reason == "agent removed"
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


@pytest.mark.anyio
@pytest.mark.parametrize("cancel", [True, False])
async def test_abandoned_spawn_keeps_revocation_fenced_until_handoff(cancel):
    entered, finish = asyncio.Event(), asyncio.Event()
    proc = SimpleNamespace(returncode=None, killed=False)

    def kill():
        proc.killed = True
        proc.returncode = -9

    async def reap():
        return proc.returncode

    proc.kill, proc.wait = kill, reap

    async def create():
        entered.set()
        await finish.wait()
        return proc

    task = asyncio.create_task(
        admission.spawn(create, engines.get("claude"), timeout=10 if cancel else 0.01)
    )
    await entered.wait()
    try:
        if cancel:
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
        else:
            await asyncio.sleep(0.03)
        with pytest.raises(storage.StateError, match="busy"):
            manager.deactivate(str(uuid.uuid4()), "claude")
        assert not task.done()
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        await task
    assert proc.killed
    manager.deactivate(str(uuid.uuid4()), "claude")
    assert not engines.registry.admits(engines.get("claude"))


def test_unreadable_state_refuses_instead_of_using_captured_roster():
    prov = engines.get("claude")
    with storage.locked(manager.DOCUMENT) as path:
        path.write_text(json.dumps({"bad": "state"}))
    with admission.acquire(prov) as guard:
        assert guard.reason == admission.UNAVAILABLE
