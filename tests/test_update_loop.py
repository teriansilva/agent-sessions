"""In-app daily auto-update loop (#538): live gating, worker-thread execution, status."""

from __future__ import annotations

import asyncio
import threading

from agent_sessions import update, update_loop


def test_sweep_disabled_never_touches_network(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SESSIONS_ENV_FILE", str(tmp_path / "env"))
    monkeypatch.delenv("AGENT_SESSIONS_AUTOUPDATE", raising=False)
    monkeypatch.setattr(
        update, "autoupdate", lambda: (_ for _ in ()).throw(AssertionError("network hit"))
    )
    assert asyncio.run(update_loop.sweep()) is None


def test_sweep_enabled_runs_off_the_event_loop_and_records(monkeypatch, tmp_path):
    envf = tmp_path / "env"
    envf.write_text("AGENT_SESSIONS_AUTOUPDATE=1\n")
    monkeypatch.setenv("AGENT_SESSIONS_ENV_FILE", str(envf))
    monkeypatch.setattr(update, "_LAST_AUTO", None)
    seen: dict[str, str] = {}

    def fake_autoupdate() -> str:
        seen["thread"] = threading.current_thread().name
        return "up-to-date"

    monkeypatch.setattr(update, "autoupdate", fake_autoupdate)
    assert asyncio.run(update_loop.sweep()) == "up-to-date"
    # The blocking work ran in a worker thread (asyncio.to_thread), not on the loop —
    # a slow `git ls-remote` must never stall the app.
    assert seen["thread"] != threading.main_thread().name
    la = update.last_auto()
    assert la is not None and la["result"] == "up-to-date"


def test_sweep_gating_is_live(monkeypatch, tmp_path):
    # The Settings toggle rewrites the env file; the next pass must see it without a
    # restart — the gate is re-read on every sweep.
    envf = tmp_path / "env"
    monkeypatch.setenv("AGENT_SESSIONS_ENV_FILE", str(envf))
    monkeypatch.delenv("AGENT_SESSIONS_AUTOUPDATE", raising=False)
    monkeypatch.setattr(update, "autoupdate", lambda: "up-to-date")
    envf.write_text("AGENT_SESSIONS_AUTOUPDATE=0\n")
    assert asyncio.run(update_loop.sweep()) is None
    envf.write_text("AGENT_SESSIONS_AUTOUPDATE=1\n")
    assert asyncio.run(update_loop.sweep()) == "up-to-date"
