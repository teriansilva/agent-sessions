"""#1605: the input after a bracketed paste waits until the agent has taken the paste in.

Measured against real claude (2.1.286 and 2.1.310, the managed and the catalog versions): a paste
naming a large image shows ``Pasting…`` and keeps a core busy for ~0.7–0.9 s while it becomes an
``[Image #N]`` attachment, and an Enter read in that time is DISCARDED — the composer's Send leaves
the message in the prompt until a second Enter. 2.1.312+ keeps the Enter instead. The fake agent
below reproduces the discarding behaviour with the same observable shape: a busy thread while it
takes the paste in, an Enter dropped during it.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid

from agent_sessions import tty_health, webterm

# A raw-mode TUI that takes a pasted image path in on a busy thread for INTAKE_S seconds and drops
# any Enter it reads meanwhile, like claude <= 2.1.310. It logs one line per event as JSON.
_INTAKE_AGENT = r"""
import json, os, sys, threading, time, tty
log, intake_s, paint = sys.argv[1], float(sys.argv[2]), sys.argv[3] == "paint"
with open(log + ".pid", "w") as f:
    f.write(str(os.getpid()))
tty.setraw(0)
os.write(1, b"READY")
if paint:  # what a handoff seed waits for: bracketed paste armed, a first paint
    os.write(1, b"\x1b[?2004h" + b"." * 4096)
events, buf, busy = [], b"", [None]

def note(what):
    events.append([round(time.monotonic(), 3), what])
    with open(log, "w") as f:
        json.dump(events, f)

def take_in():
    end = time.monotonic() + intake_s
    while time.monotonic() < end:
        pass  # a core busy, as claude's image intake is

end = time.time() + 15
while time.time() < end:
    buf += os.read(0, 65536)
    while True:
        if buf.startswith(b"\x1b[200~"):
            stop = buf.find(b"\x1b[201~")
            if stop < 0:
                break
            body, buf = buf[6:stop], buf[stop + 6:]
            note("paste")
            if b".png" in body:
                busy[0] = threading.Thread(target=take_in)
                busy[0].start()
                note("pasting")
        elif buf[:1] == b"\r":
            buf = buf[1:]
            if busy[0] is not None and busy[0].is_alive():
                note("enter-dropped")
            else:
                note("submitted")
        elif buf:
            buf = buf[1:]
        else:
            break
"""


class _Frames:
    """Hands out its frames back to back once the agent is raw (its setraw flushes input)."""

    def __init__(self, frames, linger_s=4.0, gone_when=None):
        self._frames = list(frames)
        self._linger_s = linger_s
        self._seen = b""
        self._ready = asyncio.Event()
        self._gone_when = gone_when  # the client vanishes once this is true (the heartbeat fails)

    async def receive(self):
        await asyncio.wait_for(self._ready.wait(), 10)
        if self._frames:
            return self._frames.pop(0)
        await asyncio.sleep(self._linger_s)
        return {"type": "websocket.disconnect"}

    async def send_bytes(self, b):
        self._seen += b
        if b"READY" in self._seen:
            self._ready.set()

    async def send_text(self, t):
        if self._gone_when is not None and self._gone_when():
            raise RuntimeError("client gone")

    async def close(self, code=None):
        pass


def _composer_frames(paste_body: str):
    """What `Compose.deliver()` sends: line-clear, bracketed paste, Enter."""
    paste = f"\x1b[200~{paste_body}\x1b[201~"
    return [{"text": json.dumps({"t": "i", "d": d})} for d in ("\x01\x0b", paste, "\r")]


def _run(
    tmp_path, monkeypatch, frames, *, intake_s=0.7, watch=True, key=None, seed=False, leave=False
):
    script = tmp_path / "intake_agent.py"
    script.write_text(_INTAKE_AGENT)
    log = tmp_path / "events.json"
    pidfile = tmp_path / "events.json.pid"

    def agent_process(_key):
        if not watch or not pidfile.exists():
            return None
        pid = int(pidfile.read_text())
        return pid, tty_health._start_time(pid)

    # The fake runs directly on our pty: the only hop is that pty, which `_input_settled` already
    # measures, and the engine process is the fake itself.
    monkeypatch.setattr(webterm.tty_health, "agent_input", lambda key: None)
    monkeypatch.setattr(webterm.tty_health, "agent_process", agent_process)
    argv = [sys.executable, str(script), str(log), str(intake_s), "paint" if seed else "-"]
    gone_when = None
    if leave:  # the viewer leaves while the agent is taking the image in (Send, navigate away)
        monkeypatch.setattr(webterm, "_HEARTBEAT_INTERVAL_S", 0.05)
        gone_when = lambda: log.exists() and "pasting" in log.read_text()  # noqa: E731
    key = key or f"claude:{uuid.uuid4()}"
    asyncio.run(
        webterm.run(
            _Frames(frames, gone_when=gone_when),
            argv,
            cwd=str(tmp_path),
            buf_key=key,
            seed_key=key if seed else None,
        )
    )
    return [what for _, what in json.loads(log.read_text())] if log.exists() else []


def test_one_send_with_an_image_submits_on_its_first_enter(tmp_path, monkeypatch):
    """The Enter after an image paste waits out the intake instead of being discarded in it."""
    events = _run(tmp_path, monkeypatch, _composer_frames("what is this? /tmp/shot.png"))
    assert "enter-dropped" not in events, f"the agent discarded the Send's Enter: {events}"
    assert events == ["paste", "pasting", "submitted"], events


def test_a_send_queued_behind_a_handoff_seed_still_submits_once(tmp_path, monkeypatch):
    """Hermes on #1855: frames queued while a seed is pending were joined into ONE write on
    release, so the image paste and its Enter reached the agent together and the Enter was
    discarded in the intake. The drain keeps frame boundaries and applies the same waits."""
    from agent_sessions import handoff

    handoff.reset_for_tests()
    monkeypatch.setattr(webterm, "_SEED_POLL_S", 0.05)
    monkeypatch.setattr(webterm, "_SEED_QUIET_S", 0.3)
    monkeypatch.setattr(webterm, "_SEED_SETTLE_S", 0.05)
    h = handoff.create_handle(
        "claude:11111111-1111-1111-1111-111111111111", "claude", "quick", "THE-SEED", cwd="/tmp"
    )
    key = handoff.commit(h)["id"]
    try:
        # The Send arrives at once, while the seed is still pending: all three frames queue.
        events = _run(
            tmp_path, monkeypatch, _composer_frames("x /tmp/shot.png"), key=key, seed=True
        )
    finally:
        handoff.reset_for_tests()
    assert "enter-dropped" not in events, f"the queued Send's Enter was discarded: {events}"
    # The seed first (its paste and its own Enter), then the Send, submitted once.
    assert events == ["paste", "submitted", "paste", "pasting", "submitted"], events


def test_a_viewer_leaving_during_the_intake_still_submits(tmp_path, monkeypatch):
    """Hermes on #1855: teardown cancelled the held wait and closed the pty, losing the Enter."""
    events = _run(tmp_path, monkeypatch, _composer_frames("x /tmp/shot.png"), leave=True)
    assert events == ["paste", "pasting", "submitted"], events


def test_a_viewer_leaving_during_the_seed_drain_still_submits(tmp_path, monkeypatch):
    from agent_sessions import handoff

    handoff.reset_for_tests()
    monkeypatch.setattr(webterm, "_SEED_POLL_S", 0.05)
    monkeypatch.setattr(webterm, "_SEED_QUIET_S", 0.3)
    monkeypatch.setattr(webterm, "_SEED_SETTLE_S", 0.05)
    h = handoff.create_handle(
        "claude:11111111-1111-1111-1111-111111111111", "claude", "quick", "THE-SEED", cwd="/tmp"
    )
    key = handoff.commit(h)["id"]
    try:
        events = _run(
            tmp_path,
            monkeypatch,
            _composer_frames("x /tmp/shot.png"),
            key=key,
            seed=True,
            leave=True,
        )
    finally:
        handoff.reset_for_tests()
    assert events == ["paste", "submitted", "paste", "pasting", "submitted"], events


def test_a_demoted_viewer_leaving_mid_intake_writes_nothing_more(tmp_path, monkeypatch):
    """A takeover during the hold: the displaced viewer's held Enter is never written."""
    gate = asyncio.Event()
    real = webterm.tty_health.agent_busy

    def busy_then_demoted(*a):
        gate.set()  # `_demotion_guard` fires while the Enter is held
        return real(*a)

    monkeypatch.setattr(webterm.tty_health, "agent_busy", busy_then_demoted)
    script = tmp_path / "intake_agent.py"
    script.write_text(_INTAKE_AGENT)
    log = tmp_path / "events.json"
    pidfile = tmp_path / "events.json.pid"
    monkeypatch.setattr(webterm.tty_health, "agent_input", lambda key: None)
    monkeypatch.setattr(
        webterm.tty_health,
        "agent_process",
        lambda key: (int(pidfile.read_text()), tty_health._start_time(int(pidfile.read_text()))),
    )
    asyncio.run(
        webterm.run(
            _Frames(_composer_frames("x /tmp/shot.png"), linger_s=1.5),
            [sys.executable, str(script), str(log), "0.7", "-"],
            cwd=str(tmp_path),
            buf_key=f"claude:{uuid.uuid4()}",
            read_only_gate=gate,
        )
    )
    events = [what for _, what in json.loads(log.read_text())]
    assert "submitted" not in events and "enter-dropped" not in events, events


def test_a_text_send_is_unchanged(tmp_path, monkeypatch):
    events = _run(tmp_path, monkeypatch, _composer_frames("just text"))
    assert events == ["paste", "submitted"], events


def test_an_unmeasurable_agent_gets_the_enter_at_once(tmp_path, monkeypatch):
    """No engine process to watch → the frame is written as before, never held."""
    events = _run(tmp_path, monkeypatch, _composer_frames("x /tmp/shot.png"), watch=False)
    assert events == ["paste", "pasting", "enter-dropped"], events


def test_the_hold_is_bounded(tmp_path, monkeypatch):
    """An agent that never goes quiet costs the frame at most the cap, then it is written."""
    monkeypatch.setattr(webterm, "_PASTE_INTAKE_MAX_S", 0.4)
    t0 = time.monotonic()
    events = _run(tmp_path, monkeypatch, _composer_frames("x /tmp/shot.png"), intake_s=6.0)
    assert events == ["paste", "pasting", "enter-dropped"], events
    assert time.monotonic() - t0 < 6.0  # the Enter went at the cap, not after the intake


def test_typing_without_a_paste_never_probes_the_agent(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(webterm.tty_health, "agent_busy", lambda *a: calls.append(a) or False)
    frames = [{"text": json.dumps({"t": "i", "d": d})} for d in ("a", "b", "\r")]
    events = _run(tmp_path, monkeypatch, frames)
    assert events == ["submitted"]
    assert calls == []


# --- the probe itself ---------------------------------------------------------------------------


def _spinner(seconds: float) -> subprocess.Popen:
    code = (
        "import sys, threading, time\n"
        "def spin():\n"
        "    end = time.monotonic() + float(sys.argv[1])\n"
        "    while time.monotonic() < end: pass\n"
        "threading.Thread(target=spin).start()\n"
        "time.sleep(float(sys.argv[1]) + 30)\n"
    )
    return subprocess.Popen([sys.executable, "-c", code, str(seconds)])


def _settle(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_agent_busy_sees_a_working_thread_and_then_none():
    p = _spinner(1.0)
    try:
        start = tty_health._start_time(p.pid)
        assert _settle(lambda: tty_health.agent_busy(p.pid, start) is True)
        # Spin over: only the sleeping main thread is left.
        assert _settle(lambda: tty_health.agent_busy(p.pid, start) is False)
    finally:
        p.kill()
        p.wait()


def test_agent_busy_refuses_a_different_process_on_the_pid():
    p = _spinner(0.0)
    try:
        start = tty_health._start_time(p.pid)
        assert start is not None
        assert tty_health.agent_busy(p.pid, start + 1) is None
    finally:
        p.kill()
        p.wait()
    assert tty_health.agent_busy(p.pid, start) is None  # gone


def test_agent_busy_counts_only_the_engines_own_threads():
    """A child the engine runs (a tool command) never holds input."""
    code = (
        "import subprocess, sys, time\n"
        "c = subprocess.Popen([sys.executable, '-c', 'while True: pass'])\n"
        "time.sleep(30)\n"
    )
    p = subprocess.Popen([sys.executable, "-c", code])
    try:
        start = tty_health._start_time(p.pid)
        assert _settle(lambda: len(_children(p.pid)) == 1)
        time.sleep(0.2)  # the parent is asleep now; its child spins
        assert not any(tty_health.agent_busy(p.pid, start) for _ in range(20))
    finally:
        for kid in _children(p.pid):
            os.kill(kid, 9)
        p.kill()
        p.wait()


def _children(pid: int) -> list[int]:
    try:
        with open(f"/proc/{pid}/task/{pid}/children") as fh:
            return [int(x) for x in fh.read().split()]
    except OSError:
        return []
