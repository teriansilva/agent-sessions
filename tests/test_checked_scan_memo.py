"""#1976: the checked walk and Copilot rows ride the per-file memo — and still miss when they must.

Before #1976 the needs-you sync's checked walk re-parsed every Claude, Codex and Copilot transcript
each minute (6–8 s of CPU on the target host), and every plain walk re-read all of Copilot's event
logs. These pin both halves of the fix: an unchanged file is built ONCE, and nothing about the
checked walk's contract moved — an unreadable file is named on every pass, a failed build is never
stored, and the plain walk's entries (which may be fail-soft `None`s) are never served to it.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import agent_sessions.engines.codex as cdx
from agent_sessions import scancache, scanner
from agent_sessions.engines.copilot import CopilotProvider

_SID = "11111111-2222-4333-8444-555555555555"
_U1 = "019e2ba1-1590-7003-8e4a-51ab62cec96e"


def _age(p: Path, seconds: float = 5.0) -> Path:
    """Backdate past the racy window, so a HIT asserted below is a hit for the right reason."""
    ns = time.time_ns() - int(seconds * 1_000_000_000)
    os.utime(p, ns=(ns, ns))
    return p


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return _age(p)


class _Count:
    """Wrap a builder and count how often it actually ran."""

    def __init__(self, fn):
        self.fn, self.calls = fn, 0

    def __call__(self, *a, **kw):
        self.calls += 1
        return self.fn(*a, **kw)


def _count_method(monkeypatch, cls, name) -> _Count:
    """Count calls to ``cls.name`` — a plain function wrapper, so it still binds as a method."""
    counter = _Count(getattr(cls, name))

    def wrapper(self, *a, **kw):
        return counter(self, *a, **kw)

    monkeypatch.setattr(cls, name, wrapper)
    return counter


# --- scancache: `also` and `keep` ---------------------------------------------------------------


def test_also_files_are_part_of_the_identity(tmp_path):
    d = tmp_path / "sess"
    d.mkdir()
    ws = _write(d / "workspace.yaml", "id: x\n")
    ev = d / "events.jsonl"  # absent to begin with
    _age(d)
    build = _Count(lambda: "row")

    def get():
        return scancache.memoized("ns", d, build, also=(ws, ev))

    assert get() == get() == "row"
    assert build.calls == 1  # unchanged directory + files → built once
    _write(ev, "{}\n")  # appears (directory touched too, by the create)
    _age(d)
    get()
    assert build.calls == 2
    _write(ws, "id: y\n")  # rewritten in place: the directory's own identity is unchanged
    get()
    assert build.calls == 3
    ev.unlink()  # disappears
    _age(d)
    get()
    assert build.calls == 4
    get()
    assert build.calls == 4  # and settles again


def test_a_recent_also_file_is_not_memoised(tmp_path):
    d = tmp_path / "sess"
    d.mkdir()
    ws = d / "workspace.yaml"
    ws.write_text("id: x\n")  # just written: inside the racy window
    _age(d)
    build = _Count(lambda: "row")
    for _ in range(2):
        scancache.memoized("ns", d, build, also=(ws,))
    assert build.calls == 2


def test_keep_rejects_a_row_without_storing_it(tmp_path):
    f = _write(tmp_path / "a.jsonl", "x")
    build = _Count(lambda: ("row", False))
    for _ in range(2):
        assert scancache.memoized("ns", f, build, keep=lambda r: r[1]) == ("row", False)
    assert build.calls == 2
    assert scancache.stats()["entries"] == 0


def test_a_raising_build_stores_nothing(tmp_path):
    f = _write(tmp_path / "a.jsonl", "x")

    def boom():
        raise OSError("unreadable")

    build = _Count(boom)
    for _ in range(2):
        with pytest.raises(OSError):
            scancache.memoized("ns", f, build)
    assert build.calls == 2
    assert scancache.stats()["entries"] == 0


# --- Claude's checked walk ----------------------------------------------------------------------


def _claude_tree(home: Path) -> list[Path]:
    proj = home / ".claude" / "projects" / "-home-user-x"
    files = [
        _write(proj / f"{c * 8}-{c * 4}-{c * 4}-{c * 4}-{c * 12}.jsonl", line)
        for c, line in (
            ("7", '{"type":"user","message":{"content":"one"}}\n'),
            ("8", '{"type":"user","message":{"content":"two"}}\n'),
        )
    ]
    _age(proj)
    return files


def test_the_claude_checked_walk_builds_each_unchanged_transcript_once(tmp_path, monkeypatch):
    _claude_tree(tmp_path)
    build = _Count(scanner.session_from_jsonl)
    monkeypatch.setattr(scanner, "session_from_jsonl", build)
    first, p1 = scanner.scan_checked(home=tmp_path)
    second, p2 = scanner.scan_checked(home=tmp_path)
    assert build.calls == 2  # the second pass read no transcript
    assert first == second and len(first) == 2 and p1 == p2 == []


def test_a_memoised_transcript_that_becomes_unreadable_is_still_named(tmp_path):
    """`chmod` leaves the identity (dev, ino, size, mtime) untouched, so the memo alone would hit.
    The readability probe runs before it on every walk, and that is what keeps this a problem."""
    if os.geteuid() == 0:
        pytest.skip("root reads a 0o000 file")
    good, bad = _claude_tree(tmp_path)
    rows, problems = scanner.scan_checked(home=tmp_path)
    assert len(rows) == 2 and problems == []
    bad.chmod(0o000)
    try:
        rows, problems = scanner.scan_checked(home=tmp_path)
        assert [s.uuid[0] for s in rows] == ["7"]
        assert [p for p in problems if p.startswith("claude:") and bad.name in p]
    finally:
        bad.chmod(0o600)


def test_the_checked_walk_is_never_served_the_plain_walks_entries(tmp_path, monkeypatch):
    """The plain walk may cache a fail-soft `None`; the checked walk must build for itself."""
    _claude_tree(tmp_path)
    scanner.scan(home=tmp_path)  # fills the plain namespaces
    build = _Count(scanner.session_from_jsonl)
    monkeypatch.setattr(scanner, "session_from_jsonl", build)
    scanner.scan_checked(home=tmp_path)
    assert build.calls == 2


def test_a_changed_transcript_misses_on_the_checked_walk(tmp_path):
    good, _bad = _claude_tree(tmp_path)
    scanner.scan_checked(home=tmp_path)
    _write(good, '{"type":"user","message":{"content":"edited first message"}}\n')
    rows, _ = scanner.scan_checked(home=tmp_path)
    assert "edited first message" in {s.first_user_message for s in rows}


# --- Codex's checked walk -----------------------------------------------------------------------


def test_the_codex_checked_walk_builds_each_unchanged_rollout_once(tmp_path, monkeypatch):
    root = tmp_path / "codex-sessions"
    monkeypatch.setenv("AGENT_SESSIONS_CODEX_SESSIONS_DIR", str(root))
    lines = [
        {"timestamp": "t", "type": "session_meta", "payload": {"id": _U1, "cwd": "/home/u/p"}},
        {
            "timestamp": "t",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            },
        },
    ]
    _write(
        root / "2026/05/15" / f"rollout-2026-05-15T15-33-57-{_U1}.jsonl",
        "\n".join(json.dumps(x) for x in lines) + "\n",
    )
    build = _count_method(monkeypatch, cdx.CodexProvider, "_row_from_path")
    prov = cdx.CodexProvider()
    first = prov.scan_checked()
    second = prov.scan_checked()
    assert build.calls == 1
    assert first == second and [s.uuid for s in first[0]] == [_U1]


# --- Copilot, both walks ------------------------------------------------------------------------


def _copilot_session(home: Path, *, updated: bool = True, events: bool = True) -> Path:
    sdir = home / ".copilot" / "session-state" / _SID
    stamp = "updated_at: 2026-07-19T15:20:04.500Z\n" if updated else ""
    _write(
        sdir / "workspace.yaml",
        f"id: {_SID}\ncwd: /home/u/proj\ncreated_at: 2026-07-19T14:19:03.061Z\n{stamp}",
    )
    if events:
        _write(sdir / "events.jsonl", _event("hello") + "\n")
    _age(sdir)
    return sdir


def _event(text: str) -> str:
    return json.dumps(
        {
            "type": "user.message",
            "data": {"content": text},
            "timestamp": "2026-07-19T14:19:10.000Z",
        }
    )


@pytest.fixture
def copilot(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    build = _count_method(monkeypatch, CopilotProvider, "_row_keyed")
    return tmp_path, build


@pytest.mark.parametrize("walk", ["scan", "scan_checked"])
def test_a_copilot_session_is_read_once_per_walk_kind(copilot, walk):
    home, build = copilot
    _copilot_session(home)
    prov = CopilotProvider()
    run = getattr(prov, walk)
    assert run() == run()
    assert build.calls == 1


def test_the_copilot_walks_keep_separate_entries(copilot):
    home, build = copilot
    _copilot_session(home)
    prov = CopilotProvider()
    prov.scan()
    prov.scan_checked()
    assert build.calls == 2


def test_a_copilot_event_log_rewritten_in_place_misses(copilot):
    home, build = copilot
    sdir = _copilot_session(home)
    prov = CopilotProvider()
    assert prov.scan()[0].first_user_message == "hello"
    _write(sdir / "events.jsonl", _event("a different opening line") + "\n")
    assert prov.scan()[0].first_user_message == "a different opening line"
    assert build.calls == 2


def test_a_copilot_workspace_rewritten_in_place_misses(copilot):
    home, _build = copilot
    sdir = _copilot_session(home)
    prov = CopilotProvider()
    assert prov.scan()[0].cwd == "/home/u/proj"
    _write(
        sdir / "workspace.yaml",
        f"id: {_SID}\ncwd: /home/u/moved\nupdated_at: 2026-07-19T15:20:04.500Z\n",
    )
    assert prov.scan()[0].cwd == "/home/u/moved"


def test_a_copilot_row_from_the_directory_mtime_fallback_is_not_stored(copilot):
    """No event and no `updated_at`: `last_mtime` is the newest mtime of ANY file in the directory,
    which is outside the key — so the row is rebuilt every walk rather than served stale."""
    home, build = copilot
    _copilot_session(home, updated=False, events=False)
    prov = CopilotProvider()
    prov.scan()
    prov.scan()
    assert build.calls == 2


@pytest.mark.parametrize("name", ["workspace.yaml", "events.jsonl"])
def test_a_memoised_copilot_session_whose_file_becomes_unreadable_is_still_named(copilot, name):
    """`checked_rows` probes only the session DIRECTORY; the files inside it are what the row
    reads, and a `chmod` leaves their identity — and so the memo key — unchanged."""
    if os.geteuid() == 0:
        pytest.skip("root reads a 0o000 file")
    home, _build = copilot
    sdir = _copilot_session(home)
    prov = CopilotProvider()
    rows, problems = prov.scan_checked()
    assert len(rows) == 1 and problems == []
    target = sdir / name
    target.chmod(0o000)
    try:
        rows, problems = prov.scan_checked()
        assert rows == []
        assert problems and _SID in problems[0] and "PermissionError" in problems[0]
    finally:
        target.chmod(0o600)
