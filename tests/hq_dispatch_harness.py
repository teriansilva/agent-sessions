"""Shared pieces for HQ's Dispatches tests (#1837): a REAL loopback chat-completions endpoint that
counts and records every request (so separate worker processes are counted too), the AI setup, and
a runner for child worker/producer processes that a test can SIGKILL at a named point."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Hostile values planted in every field `hq_notify` must never send.
SENTINELS = {
    "row_title": "SENTINEL-ROW-TITLE-7f3a",
    "row_reason": "SENTINEL-ROW-REASON-91bc",
    "run_reason": "SENTINEL-RUN-REASON-55de",
    "escalation_reason": "SENTINEL-ESCALATION-REASON-a1a1",
    "session_title": "SENTINEL-SESSION-TITLE-c0c0",
    "objective": "SENTINEL-OBJECTIVE-e2e2",
}


class FakeEndpoint:
    """``POST /v1/chat/completions`` on 127.0.0.1. ``reply`` is the content to answer with;
    ``status`` the HTTP status; ``hang`` makes every request wait for ``release`` (a kill after the
    endpoint received a request); ``delay`` slows each answer."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.reply: str | None = "HQ's line."
        self.status = 200
        self.delay = 0.0
        self.hang = False
        self.extra: dict = {}
        #: Raw response bodies to answer with, one per request, before the normal reply resumes.
        self.raw: list[object] = []
        self.release = threading.Event()
        self._lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                with outer._lock:
                    outer.requests.append(json.loads(raw or b"{}"))
                if outer.hang:
                    outer.release.wait(60)
                if outer.delay:
                    time.sleep(outer.delay)
                body = {
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": outer.reply, **outer.extra},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }
                with outer._lock:
                    if outer.raw:
                        body = outer.raw.pop(0)
                data = json.dumps(body).encode()
                try:
                    self.send_response(outer.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def count(self) -> int:
        with self._lock:
            return len(self.requests)

    def bodies(self) -> list[str]:
        with self._lock:
            return [json.dumps(r, ensure_ascii=False) for r in self.requests]

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


def set_up_hq(base: str) -> None:
    """An AI endpoint configured and its egress acknowledged: HQ is set up."""
    from agent_sessions import prefs

    prefs.set_ai_review(
        {
            "egress_acknowledged": True,
            "enabled": True,
            "base_url": base,
            "api_key": "sk-hq-dispatch-test",
            "model": "m",
        }
    )


def gate(tmp_path: Path) -> Path:
    """The gate file children wait on (``gate:<point>``); create it to let them all go on."""
    return tmp_path / "gate.open"


def child_env(tmp_path: Path) -> dict:
    env = dict(os.environ)
    env["HQD_GATE"] = str(gate(tmp_path))
    env["PYTHONPATH"] = str(ROOT / "src")
    env["HOME"] = str(tmp_path / "child-home")  # nothing a child writes lands in a real home
    (tmp_path / "child-home").mkdir(exist_ok=True)
    return env


#: The prelude every child runs: the kill point (print the point's name, then wait to be killed),
#: or a GATE (``gate:<point>``: print, then wait until the parent opens the gate file, so two
#: workers can be lined up at the same step before either goes on — no scheduling luck).
_PRELUDE = """
import asyncio, json, os, sys, time
from agent_sessions import hq_dispatch
STOP = set(json.loads(sys.argv[1]))
SEEN = {}
def _hook(name):
    SEEN[name] = SEEN.get(name, 0) + 1
    tag = f"{name}#{SEEN[name]}"
    if "gate:" + name in STOP or "gate:" + tag in STOP:
        print("AT " + tag, flush=True)
        deadline = time.monotonic() + 120
        while not os.path.exists(os.environ["HQD_GATE"]) and time.monotonic() < deadline:
            time.sleep(0.02)
        return
    if name in STOP or tag in STOP:
        print("AT " + tag, flush=True)
        time.sleep(600)
hq_dispatch._POINT = _hook
"""


def spawn(tmp_path: Path, body: str, stop: list[str] | tuple[str, ...] = (), *args: str):
    """A child process running ``body`` after the prelude. ``stop`` names the points (``name`` or
    ``name#n`` for its n-th occurrence) at which it announces itself and waits to be killed."""
    script = textwrap.dedent(_PRELUDE) + textwrap.dedent(body)
    return subprocess.Popen(
        [sys.executable, "-c", script, json.dumps(list(stop)), *args],
        env=child_env(tmp_path),
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def wait_at(proc, timeout: float = 60.0) -> str:
    """The point a child stopped at (its ``AT …`` line). Fails if it exits first."""
    import select

    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        assert left > 0, "the child never reached its kill point"
        ready, _, _ = select.select([proc.stdout], [], [], left)
        if ready:
            line = proc.stdout.readline()
            if not line:
                raise AssertionError(f"the child exited first: {proc.stderr.read()[-2000:]}")
            if line.startswith("AT "):
                return line[3:].strip()


def finish(proc, timeout: float = 60.0) -> str:
    out, err = proc.communicate(timeout=timeout)
    assert proc.returncode == 0, err[-3000:]
    return out


#: A writer pass at ``now`` (argv[2]).
PASS = """
print(json.dumps(asyncio.run(hq_dispatch.pass_once(now=float(sys.argv[2])))), flush=True)
"""

#: Announce automation failures through the real producer (`automation_runner.notify`): argv[2] is
#: a JSON list of [automation id, run id, name]. A "push" is recorded in argv[3] (a file), standing
#: in for a device: the fanout is the real call site, its sender is the stand-in.
PRODUCE = """
from agent_sessions import automation_runner, notifications
pushes = sys.argv[3]
notifications.list_subscriptions = lambda *a, **k: [{"id": "dev"}]
def _fanout(rec, *a, **k):
    with open(pushes, "a") as fh:
        fh.write(json.dumps({"title": rec.get("title"), "action_id": rec.get("action_id")}) + "\\n")
    return {"sent": 1, "pruned": 0, "failed": 0}
notifications.fanout = _fanout
for aid, rid, name in json.loads(sys.argv[2]):
    automation_runner.notify({
        "automation": {"id": aid, "name": name},
        "run": {"id": rid, "outcome": "failed", "reason": "run reason"},
        "episode": ("open", f"automation:{aid}:{rid}"),
    })
print("DONE", flush=True)
"""
