# ruff: noqa: F811 — `world` is a fixture imported from test_ask_messages and requested by name
"""#1833's process-level acceptance (#1746 P2a tests 2 and 3), with REAL processes:

* two workers tap Send on one card at the same moment: the message is sent ONCE;
* a worker killed with SIGKILL between the durable `deciding` write and the send, and one killed
  after the send but before settling: both read `interrupted` after a restart, and neither is
  ever sent again — not by `recover`, not by another tap.

Each worker is a fresh interpreter sharing this test's stores (the conftest pins them in the
environment it inherits). Its provider is a fake transport that appends every message it receives
to a file, so the count is the provider's own, across processes.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from agent_sessions import ask_proposals
from test_ask_messages import _messenger, _propose, world  # noqa: F401 — the shared fixture

WORKER = textwrap.dedent(
    """
    import asyncio, json, os, time
    import httpx
    from agent_sessions import ask_messages, ask_proposals, connector_http

    LOG, MODE, PID = os.environ["SEND_LOG"], os.environ["MODE"], os.environ["PID"]

    async def resolve(host, port):
        return ["149.154.167.220"]

    def handler(request):
        if request.url.path.endswith("/sendMessage"):
            time.sleep(0.3)  # a slow provider widens any race
            with open(LOG, "a") as f:
                f.write(json.dumps(json.loads(request.content)) + "\\n")
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        return httpx.Response(404, json={"ok": False})

    connector_http._resolve = resolve
    connector_http._TRANSPORT = httpx.MockTransport(handler)

    async def block():
        open(os.environ["READY"], "w").write("ready")
        await asyncio.sleep(3600)

    if MODE == "before":
        ask_messages._HOOK_BEFORE_SEND = block
    elif MODE == "after":
        ask_messages._HOOK_AFTER_SEND = block

    async def main():
        go = os.environ.get("GO")
        while go and not os.path.exists(go):
            await asyncio.sleep(0.005)
        status, view = await ask_proposals.decide(PID, "send", bypass=False, registry=None)
        print(json.dumps({"status": status, "state": view["state"]}), flush=True)
        for task in list(ask_messages._TASKS.values()):
            await task

    asyncio.run(main())
    """
)


def _worker(tmp_path: Path, pid: str, mode: str, **env) -> subprocess.Popen:
    e = {**os.environ, "SEND_LOG": str(tmp_path / "sent.log"), "MODE": mode, "PID": pid, **env}
    return subprocess.Popen(
        [sys.executable, "-c", WORKER], env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )


def _sent(tmp_path: Path) -> list[str]:
    log = tmp_path / "sent.log"
    return log.read_text().splitlines() if log.exists() else []


def _wait_for(path: Path, timeout: float = 30.0) -> None:
    end = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < end, f"{path.name} never appeared"
        time.sleep(0.02)


def _state(cid: str, pid: str) -> str:
    _status, views = ask_proposals.views(cid)
    return views[pid]["state"]


def test_two_workers_tapping_at_once_send_once(world, monkeypatch, tmp_path):
    app, _tg, _ = world
    _messenger("telegram")
    cid, card, _ = _propose(app, monkeypatch, "only once, please")
    go = tmp_path / "go"
    procs = [_worker(tmp_path, card["action_id"], "race", GO=str(go)) for _ in range(2)]
    time.sleep(0.5)  # both interpreters up and waiting
    go.write_text("go")
    for p in procs:
        p.wait(timeout=60)
        assert p.returncode == 0, p.stderr.read().decode()[-2000:]
    assert len(_sent(tmp_path)) == 1
    assert _state(cid, card["action_id"]) == "sent"


@pytest.mark.parametrize("point", ["before", "after"])
def test_a_worker_killed_mid_send_is_interrupted_and_never_sent_again(
    world,
    monkeypatch,
    tmp_path,
    point,
):
    app, _tg, _ = world
    _messenger("telegram")
    cid, card, _ = _propose(app, monkeypatch, "maybe sent")
    pid = card["action_id"]
    ready = tmp_path / "ready"
    proc = _worker(tmp_path, pid, point, READY=str(ready))
    _wait_for(ready)
    assert _state(cid, pid) == "deciding"  # written durably before anything was sent
    expected = 0 if point == "before" else 1
    assert len(_sent(tmp_path)) == expected
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=30)
    # "A restart": the next read finds the lock free and settles the entry, never replays it.
    assert _state(cid, pid) == "interrupted"
    (row,) = [r for r in ask_proposals.ask_store.read_proposals(cid) if r["id"] == pid]
    assert "may have been sent" in row["reason"]
    # A second tap, from a new worker, returns the state and sends nothing.
    again = _worker(tmp_path, pid, "plain")
    out, err = again.communicate(timeout=60)
    assert again.returncode == 0, err.decode()[-2000:]
    assert '"state": "interrupted"' in out.decode()
    assert len(_sent(tmp_path)) == expected
