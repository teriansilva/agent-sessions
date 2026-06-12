"""Single-active-viewer take-over route (#293) — the flag-on attach behaviour.

Exercises ``routes.terminal._serve_takeover`` directly with a fake websocket +
registry and a stubbed ``webterm.run``, against a real (tmp) owner file. Covers:
passive is inert (gate frame, no attach/stream), owner streams, explicit force
take-over, and demotion-mid-stream → gate. The cross-process CAS itself is
covered by ``test_owner.py``.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent_sessions import owner
from agent_sessions.routes import terminal

ENG, SID, KEY = "claude", "ses_take-1", "claude:ses_take-1"


@pytest.fixture(autouse=True)
def _runtime_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_RUNTIME_DIR", str(tmp_path / "pty"))


class FakeWS:
    """Minimal websocket double: records sent JSON frames; ``receive`` drains a
    scripted queue then reports disconnect."""

    def __init__(self, incoming=None):
        self.sent: list[dict] = []
        self._incoming = list(incoming or [])

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def receive(self) -> dict:
        if self._incoming:
            return self._incoming.pop(0)
        return {"type": "websocket.disconnect"}


class FakeRegistry:
    def __init__(self):
        self.attached: list[tuple[str, str]] = []
        self.detached: list[tuple[str, str]] = []

    async def on_attach(self, engine: str, sid: str) -> None:
        self.attached.append((engine, sid))

    async def on_detach(self, engine: str, sid: str) -> None:
        self.detached.append((engine, sid))


def _serve(ws, registry, *, fp, tab_id, force=False, label=""):
    return terminal._serve_takeover(
        ws,
        registry=registry,
        engine=ENG,
        phys_native=SID,
        phys_key=KEY,
        argv=["/bin/true"],
        cwd="/",
        init_cols=80,
        init_rows=24,
        lock=None,
        have=0,
        fp=fp,
        tab_id=tab_id,
        force=force,
        label=label,
    )


def test_passive_is_inert_sends_gate_and_never_attaches(monkeypatch):
    ran = []
    monkeypatch.setattr(terminal.webterm, "run", lambda *a, **k: ran.append(True))
    # Someone else already holds it (a live holder).
    owner._claim_sync(
        ENG, SID, conn_id="held", fp="fpB", tab_id="t2", label="Mac · Chrome", force=False
    )
    ws, reg = FakeWS(), FakeRegistry()
    asyncio.run(_serve(ws, reg, fp="fpA", tab_id="t1", label="iPhone · Safari"))
    gate = [m for m in ws.sent if m.get("t") == "gate"]
    assert len(gate) == 1
    assert gate[0]["holder"]["label"] == "Mac · Chrome"  # who holds it, for the gate
    assert ran == []  # webterm.run NEVER called for a passive viewer
    assert reg.attached == []  # and no PTY attach/stream
    assert owner.owns(ENG, SID, "held")  # holder unchanged


def test_owner_streams_then_releases(monkeypatch):
    seen = {}

    async def fake_run(ws, argv, **kw):
        seen["stop_event"] = kw.get("stop_event")
        seen["buf_key"] = kw.get("buf_key")

    monkeypatch.setattr(terminal.webterm, "run", fake_run)
    ws, reg = FakeWS(), FakeRegistry()
    asyncio.run(_serve(ws, reg, fp="fpA", tab_id="t1", label="Mac"))
    assert {"t": "role", "role": "owner"} in ws.sent
    assert not any(m.get("t") == "gate" for m in ws.sent)  # owner, not gated
    assert reg.attached == [(ENG, SID)]
    assert reg.detached == [(ENG, SID)]  # on_detach in the finally
    assert seen["buf_key"] == KEY
    assert owner.read_owner(ENG, SID) is None  # released on clean exit


def test_force_takes_over_a_live_holder_and_streams(monkeypatch):
    monkeypatch.setattr(terminal.webterm, "run", _noop_run)
    owner._claim_sync(ENG, SID, conn_id="held", fp="fpB", tab_id="t2", label="Old", force=False)
    ws, reg = FakeWS(), FakeRegistry()
    asyncio.run(_serve(ws, reg, fp="fpA", tab_id="t1", label="New", force=True))
    assert {"t": "role", "role": "owner"} in ws.sent
    assert reg.attached == [(ENG, SID)]
    assert not owner.owns(ENG, SID, "held")  # displaced


def test_demotion_midstream_shows_gate(monkeypatch):
    async def fake_run(ws, argv, *, stop_event=None, **kw):
        # Another viewer (this or the other instance) force-takes the owner file.
        owner._claim_sync(
            ENG, SID, conn_id="other", fp="fpB", tab_id="t2", label="Phone", force=True
        )
        stop_event.set()

    monkeypatch.setattr(terminal.webterm, "run", fake_run)
    ws, reg = FakeWS(), FakeRegistry()
    asyncio.run(_serve(ws, reg, fp="fpA", tab_id="t1", label="Desktop"))
    gate = [m for m in ws.sent if m.get("t") == "gate"]
    assert len(gate) == 1
    assert gate[0]["holder"]["label"] == "Phone"  # who took it
    # We were demoted, so our release was a no-op — the new owner stands.
    assert owner.owns(ENG, SID, "other")
    assert reg.detached == [(ENG, SID)]


def test_same_device_reconnect_reclaims_as_owner(monkeypatch):
    monkeypatch.setattr(terminal.webterm, "run", _noop_run)
    # Our own prior connection (same fp+tab) still holds the file — a reconnect.
    owner._claim_sync(ENG, SID, conn_id="prev", fp="fpA", tab_id="t1", label="Mac", force=False)
    ws, reg = FakeWS(), FakeRegistry()
    asyncio.run(_serve(ws, reg, fp="fpA", tab_id="t1", label="Mac"))
    assert {"t": "role", "role": "owner"} in ws.sent  # reclaimed, not gated
    assert reg.attached == [(ENG, SID)]


async def _noop_run(ws, argv, **kw):
    return
