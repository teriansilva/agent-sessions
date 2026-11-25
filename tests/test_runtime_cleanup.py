"""Unit tests for ``runtime_cleanup.cleanup_runtime`` (#523) — the shared archive teardown.

Hermetic: every primitive (reaper / scrollback / owner / sessionlock / ptybridge / physical_key)
is monkeypatched, so these assert the ORCHESTRATION — order, physical-key targeting, the
spare/guard branches, and best-effort suppression — without spawning a real PTY or touching the
lock/runtime dirs. The primitives themselves are pinned by test_reaper / test_sessionlock /
test_scrollback. Async is driven via ``asyncio.run`` (matching test_reaper).
"""

from __future__ import annotations

import asyncio

from agent_sessions import runtime_cleanup


class _FakeLock:
    def __init__(self, rec: list) -> None:
        self._rec = rec

    def release(self) -> None:
        self._rec.append("release")


def _wire(monkeypatch, *, phys="claude:U", outcome="kill", lock=True) -> dict:
    """Record every primitive call; return the recorder.

    ``physical_key`` maps the logical key → ``phys``; ``terminate_master`` returns ``outcome``
    (and honours ``spare_if`` like the real reaper); ``sessionlock.acquire`` returns a fake
    lock, or ``None`` when ``lock=False`` (a held lock / new generation owns the socket).
    """
    calls: dict = {"order": [], "release": []}

    monkeypatch.setattr(runtime_cleanup.engines, "physical_key", lambda key, aliases=None: phys)

    async def _term(engine, sid, *, key=None, grace_s=3.0, spare_if=None):
        calls["term"] = {"engine": engine, "sid": sid, "key": key, "spare_if": spare_if}
        calls["order"].append("term")
        if spare_if is not None and not spare_if():
            return "spared"
        return outcome

    monkeypatch.setattr(runtime_cleanup.reaper, "terminate_master", _term)

    def _clear_sb(keys=None):
        calls["scrollback"] = list(keys) if keys is not None else None
        calls["order"].append("scrollback")
        return {}

    monkeypatch.setattr(runtime_cleanup.scrollback, "clear_scrollback", _clear_sb)

    def _clear_owner(engine, sid):
        calls["owner"] = {"engine": engine, "sid": sid}
        calls["order"].append("owner")
        return True

    monkeypatch.setattr(runtime_cleanup.owner, "clear_owner", _clear_owner)

    def _acquire(key):
        calls["acquire"] = key
        calls["order"].append("acquire")
        return _FakeLock(calls["release"]) if lock else None

    monkeypatch.setattr(runtime_cleanup.sessionlock, "acquire", _acquire)

    class _SockPath:
        def __init__(self, engine, sid) -> None:
            self.engine, self.sid = engine, sid

        def unlink(self) -> None:
            calls["unlink"] = {"engine": self.engine, "sid": self.sid}
            calls["order"].append("unlink")

    monkeypatch.setattr(runtime_cleanup.ptybridge, "socket_path", lambda e, s: _SockPath(e, s))
    return calls


def test_full_teardown_sequence(monkeypatch):
    calls = _wire(monkeypatch, phys="claude:U", outcome="kill")
    out = asyncio.run(runtime_cleanup.cleanup_runtime("claude", "U"))
    assert out == "kill"
    # Terminate FIRST, keyed by the physical key + native; archive passes no spare_if (force).
    assert calls["term"] == {"engine": "claude", "sid": "U", "key": "claude:U", "spare_if": None}
    assert calls["scrollback"] == ["claude:U"]
    assert calls["owner"] == {"engine": "claude", "sid": "U"}
    assert calls["acquire"] == "claude:U"
    assert calls["unlink"] == {"engine": "claude", "sid": "U"}
    assert calls["release"] == ["release"]  # lock released after the unlink
    assert calls["order"] == ["term", "scrollback", "owner", "acquire", "unlink"]


def test_resolves_physical_key_for_alias(monkeypatch):
    # A reconciled opencode placeholder id aliases to its real ses_… — cleanup must target the
    # PHYSICAL key, not the logical placeholder (Hermes re-review).
    calls = _wire(monkeypatch, phys="opencode:ses_real", outcome="term")
    out = asyncio.run(runtime_cleanup.cleanup_runtime("opencode", "new-abc"))
    assert out == "term"
    assert calls["term"]["sid"] == "ses_real" and calls["term"]["key"] == "opencode:ses_real"
    assert calls["scrollback"] == ["opencode:ses_real"]
    assert calls["owner"] == {"engine": "opencode", "sid": "ses_real"}
    assert calls["unlink"] == {"engine": "opencode", "sid": "ses_real"}


def test_spared_skips_all_cleanup(monkeypatch):
    # spare_if vetoes → master SPARED → no scrollback / owner / socket teardown runs.
    calls = _wire(monkeypatch, outcome="term")
    out = asyncio.run(runtime_cleanup.cleanup_runtime("claude", "U", spare_if=lambda: False))
    assert out == "spared"
    assert calls["order"] == ["term"]
    assert "scrollback" not in calls and "owner" not in calls and "acquire" not in calls


def test_socket_unlink_skipped_when_lock_held(monkeypatch):
    # Split-brain guard: lock NOT acquirable (a new generation owns it) → leave the socket alone.
    calls = _wire(monkeypatch, lock=False)
    asyncio.run(runtime_cleanup.cleanup_runtime("claude", "U"))
    assert calls["acquire"] == "claude:U"
    assert "unlink" not in calls  # never unlink a path whose lock we couldn't acquire
    assert calls["release"] == []  # nothing acquired ⇒ nothing to release


def test_best_effort_survives_a_failing_step(monkeypatch):
    calls = _wire(monkeypatch, outcome="kill")

    def _boom(keys=None):
        calls["order"].append("scrollback-boom")
        raise RuntimeError("scrollback exploded")

    monkeypatch.setattr(runtime_cleanup.scrollback, "clear_scrollback", _boom)
    out = asyncio.run(runtime_cleanup.cleanup_runtime("claude", "U"))
    assert out == "kill"  # never raises
    assert calls["owner"] == {"engine": "claude", "sid": "U"}  # owner still cleared
    assert calls["unlink"] == {"engine": "claude", "sid": "U"}  # socket still unlinked


def test_socket_unlink_oserror_suppressed(monkeypatch):
    calls = _wire(monkeypatch)

    class _BadSock:
        def __init__(self, engine, sid) -> None:
            pass

        def unlink(self) -> None:
            raise OSError("already gone")

    monkeypatch.setattr(runtime_cleanup.ptybridge, "socket_path", lambda e, s: _BadSock(e, s))
    out = asyncio.run(runtime_cleanup.cleanup_runtime("claude", "U"))
    assert out == "kill"  # a clean exit already unlinked its own socket → a miss is fine
    assert calls["release"] == ["release"]  # lock still released in the finally
