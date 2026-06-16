"""Single-active-viewer take-over route (#293) with the read-only fallback (#434).

Exercises ``routes.terminal._serve_takeover`` directly with a fake websocket +
registry and a stubbed ``webterm.run``, against a real (tmp) owner file. Covers:
a passive viewer streams READ-ONLY (gated, not inert), the owner streams ungated,
explicit force take-over, and demotion-mid-stream → flip to read-only in place
(no blank, no dropped stream). The cross-process CAS itself is covered by
``test_owner.py``.
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

    async def on_attach(self, engine: str, sid: str, viewer_id: object = None) -> None:
        self.attached.append((engine, sid))

    async def on_detach(self, engine: str, sid: str, viewer_id: object = None) -> None:
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


async def _noop_run(ws, argv, **kw):
    return


def test_passive_streams_read_only_not_inert(monkeypatch):
    seen = {}

    async def fake_run(ws, argv, *, read_only_gate=None, **kw):
        seen["gate_set"] = read_only_gate is not None and read_only_gate.is_set()
        seen["buf_key"] = kw.get("buf_key")

    monkeypatch.setattr(terminal.webterm, "run", fake_run)
    # Someone else already holds it (a live holder).
    owner._claim_sync(
        ENG, SID, conn_id="held", fp="fpB", tab_id="t2", label="Mac · Chrome", force=False
    )
    ws, reg = FakeWS(), FakeRegistry()
    asyncio.run(_serve(ws, reg, fp="fpA", tab_id="t1", label="iPhone · Safari"))
    # A read-only `secondary` role frame (with the holder), NOT an inert `gate`.
    assert not any(m.get("t") == "gate" for m in ws.sent)
    role = [m for m in ws.sent if m.get("t") == "role"]
    assert role and role[0]["role"] == "secondary"
    assert role[0]["holder"]["label"] == "Mac · Chrome"  # who's active, for the banner
    # It STREAMS — webterm.run ran — but gated read-only so it can never write the master.
    assert seen.get("gate_set") is True
    assert seen.get("buf_key") == KEY
    assert reg.attached == [(ENG, SID)]
    assert reg.detached == [(ENG, SID)]
    assert owner.owns(ENG, SID, "held")  # holder unchanged — passive never steals


def test_owner_streams_ungated_then_releases(monkeypatch):
    seen = {}

    async def fake_run(ws, argv, *, read_only_gate=None, **kw):
        seen["gate_set"] = read_only_gate is not None and read_only_gate.is_set()
        seen["buf_key"] = kw.get("buf_key")

    monkeypatch.setattr(terminal.webterm, "run", fake_run)
    ws, reg = FakeWS(), FakeRegistry()
    asyncio.run(_serve(ws, reg, fp="fpA", tab_id="t1", label="Mac"))
    assert {"t": "role", "role": "owner"} in ws.sent
    assert not any(m.get("t") == "gate" for m in ws.sent)
    assert seen.get("gate_set") is False  # owner is NOT gated — full read/write
    assert seen.get("buf_key") == KEY
    assert reg.attached == [(ENG, SID)]
    assert reg.detached == [(ENG, SID)]  # on_detach in the finally
    assert owner.read_owner(ENG, SID) is None  # released on clean exit


def test_force_takes_over_a_live_holder_and_streams(monkeypatch):
    monkeypatch.setattr(terminal.webterm, "run", _noop_run)
    owner._claim_sync(ENG, SID, conn_id="held", fp="fpB", tab_id="t2", label="Old", force=False)
    ws, reg = FakeWS(), FakeRegistry()
    asyncio.run(_serve(ws, reg, fp="fpA", tab_id="t1", label="New", force=True))
    assert {"t": "role", "role": "owner"} in ws.sent
    assert reg.attached == [(ENG, SID)]
    assert not owner.owns(ENG, SID, "held")  # displaced


def test_demotion_midstream_flips_to_read_only_without_dropping_stream(monkeypatch):
    # Tighten the lease/heartbeat poll so the demotion guard fires within the test.
    monkeypatch.setattr(terminal, "_HEARTBEAT_S", 0.01)

    async def fake_run(ws, argv, *, read_only_gate=None, **kw):
        # Another viewer (this or the other instance) force-takes the owner file mid-stream.
        owner._claim_sync(
            ENG, SID, conn_id="other", fp="fpB", tab_id="t2", label="Phone", force=True
        )
        # The guard should notice and flip THIS viewer to read-only without us stopping.
        for _ in range(500):
            if read_only_gate is not None and read_only_gate.is_set():
                break
            await asyncio.sleep(0.005)
        seen["gate_set"] = read_only_gate is not None and read_only_gate.is_set()

    seen = {}
    monkeypatch.setattr(terminal.webterm, "run", fake_run)
    ws, reg = FakeWS(), FakeRegistry()
    asyncio.run(_serve(ws, reg, fp="fpA", tab_id="t1", label="Desktop"))
    # We started as owner, then got demoted: gate flipped + a `secondary` frame naming the
    # new holder was sent — and NO inert `gate` frame.
    assert seen.get("gate_set") is True
    assert not any(m.get("t") == "gate" for m in ws.sent)
    secondary = [m for m in ws.sent if m.get("t") == "role" and m.get("role") == "secondary"]
    assert secondary and secondary[-1]["holder"]["label"] == "Phone"
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
