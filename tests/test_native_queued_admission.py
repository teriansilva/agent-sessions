"""A queued message survives the vendor CLI's own self-update, and is never lost without a
reason (#1538).

The real contained worker runs against a launcher laid out the way Claude Code installs itself:
``~/.local/bin/claude`` is a symlink into ``~/.local/share/claude/versions/<ver>``, and an update
retargets it while a turn is busy. Every layout is built under the test's temporary HOME.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

import test_native_runtime as rt
from agent_sessions import native_journal, native_runtime
from agent_sessions import structured_runtime as runtime
from agent_sessions.engines import registry

host = rt.host
project = rt.project
ENGINES, frames, ident, settle = rt.ENGINES, rt.frames, rt.ident, rt.settle


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _fake(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n" + rt.FAKE.read_text())
    path.chmod(0o755)
    return path


def _point(link: Path, target: Path) -> None:
    """Retarget ``link`` atomically, the way an installer swaps its launcher."""
    staged = link.with_name(link.name + ".new")
    staged.symlink_to(target)
    os.replace(staged, link)


@pytest.fixture
def launcher(host, tmp_home, monkeypatch):
    """``~/.local/bin/claude`` → ``~/.local/share/claude/versions/2.1.300``, found by the
    in-tree claude manifest's search path (no env override)."""
    monkeypatch.delenv("AGENT_SESSIONS_CLAUDE_BIN", raising=False)
    versions = tmp_home / ".local" / "share" / "claude" / "versions"
    old = _fake(versions / "2.1.300")
    for d in (tmp_home / ".local", tmp_home / ".local" / "share", versions.parent, versions):
        d.chmod(0o755)
    bindir = tmp_home / ".local" / "bin"
    bindir.mkdir(parents=True)
    bindir.chmod(0o755)
    link = bindir / "claude"
    link.symlink_to(old)
    return link


async def _busy_with_one_queued(project) -> tuple[str, str, str]:
    key = (await runtime.create_session(ENGINES["claude"], str(project), operation_id=ident()))[
        "session_key"
    ]
    first, queued = ident(), ident()
    await runtime.submit_turn(key, operation_id=first, text="WAIT_RELEASE")
    assert (await runtime.submit_turn(key, operation_id=queued, text="follow-up"))[
        "state"
    ] == "queued"
    return key, first, queued


def _users(project) -> list[dict]:
    return [f for f in frames(project) if f["frame"].get("type") == "user"]


@pytest.mark.anyio
async def test_a_queued_message_is_delivered_after_the_cli_updated_itself(
    host, project, launcher, tmp_home
):
    """The incident (2026-10-20): Claude Code installed a new version while a turn was busy, and
    the message queued behind it was dropped `not_sent`. It goes to the running process."""
    key, _, queued = await _busy_with_one_queued(project)
    pid = _users(project)[0]["pid"]
    new = _fake(tmp_home / ".local" / "share" / "claude" / "versions" / "2.1.304")
    _point(launcher, new)
    (project / "release-queued-turn").touch()
    snap, delivered = await settle(key, queued)
    assert delivered["state"] == "completed", delivered
    assert delivered["reply"] == "echo:follow-up"
    assert "not_sent" not in delivered
    users = _users(project)
    assert len(users) == 2
    # The RUNNING process took it: nothing new was executed for the delivery.
    assert {u["pid"] for u in users} == {pid}
    assert len(host.launches) == 1


@pytest.mark.anyio
async def test_a_launcher_retargeted_outside_the_versions_directory_still_delivers(
    host, project, launcher, tmp_home
):
    """Policy (#1538): the selected first-party launcher is trusted for any provenance-valid
    target. An operator-owned regular executable outside ``versions/`` delivers too."""
    key, _, queued = await _busy_with_one_queued(project)
    elsewhere = _fake(tmp_home / "opt" / "my-claude" / "claude")
    for d in (tmp_home / "opt", elsewhere.parent):
        d.chmod(0o755)
    _point(launcher, elsewhere)
    (project / "release-queued-turn").touch()
    _, delivered = await settle(key, queued)
    assert delivered["state"] == "completed" and delivered["reply"] == "echo:follow-up"
    assert len(_users(project)) == 2


async def _refused(key: str, queued: str, project) -> dict:
    (project / "release-queued-turn").touch()
    _, turn = await settle(key, queued)
    assert turn["state"] == "failed", turn
    assert len(_users(project)) == 1  # nothing was written to the agent
    return turn


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["removed", "not_regular", "writable_by_others"])
async def test_a_launcher_provenance_refuses_is_settled_agent_changed_with_nothing_written(
    host, project, launcher, tmp_home, change
):
    """The fresh resolution must PASS provenance: a launcher that is gone, points at a
    non-regular file, or at one other users could rewrite is refused, never delivered."""
    key, _, queued = await _busy_with_one_queued(project)
    if change == "removed":
        launcher.unlink()
    elif change == "not_regular":
        target = tmp_home / ".local" / "share" / "claude" / "versions" / "dir"
        target.mkdir()
        target.chmod(0o755)
        _point(launcher, target)
    else:
        loose = _fake(tmp_home / ".local" / "share" / "claude" / "versions" / "2.1.305")
        loose.chmod(0o757)
        _point(launcher, loose)
    turn = await _refused(key, queued, project)
    assert turn["not_sent"]["code"] == "agent_changed"
    assert "switched to a different install" in turn["reason"]
    root = native_runtime._journal_root(registry.get(ENGINES["claude"]))
    op = native_journal.read(root, key.partition(":")[2]).operations[queued]
    assert op.reason_code == "agent_changed"


@pytest.mark.anyio
async def test_a_removed_agent_is_refused_with_its_reason(host, project, launcher):
    from agent_sessions.plugins import manager

    key, first, queued = await _busy_with_one_queued(project)
    root = native_runtime._journal_root(registry.get(ENGINES["claude"]))
    manager.deactivate(ident(), ENGINES["claude"])
    (project / "release-queued-turn").touch()
    for _ in range(400):
        journal = native_journal.read(root, key.partition(":")[2])
        if journal.operations[queued].handoff == "not_sent":
            break
        await asyncio.sleep(0.05)
    op = journal.operations[queued]
    assert op.handoff == "not_sent" and op.reason_code == "agent_removed"
    assert op.receipt()["reason_code"] == "agent_removed"
    assert len(_users(project)) == 1


# --- the worker ended with the message still queued (#1538 Architecture 4) ---------------------


async def _stranded(host, project) -> tuple[str, str, Path, str]:
    """A message queued behind a turn, then its worker killed: it can never go out."""
    key = (await runtime.create_session(ENGINES["codex"], str(project), operation_id=ident()))[
        "session_key"
    ]
    await runtime.submit_turn(key, operation_id=ident(), text="HANG")
    queued = ident()
    await runtime.submit_turn(key, operation_id=queued, text="waiting")
    worker = (await runtime.snapshot(key))["native"]["worker"]
    host.kill(worker)
    root = native_runtime._journal_root(registry.get(ENGINES["codex"]))
    return key, queued, root, worker


@pytest.mark.anyio
async def test_a_worker_that_ended_with_a_message_queued_records_why_durably(host, project):
    key, queued, root, worker = await _stranded(host, project)
    sid = key.partition(":")[2]
    _, turn = await settle(key, queued)
    assert turn["state"] == "failed" and turn["not_sent"]["code"] == "worker_ended"
    journal = native_journal.read(root, sid)
    op = journal.operations[queued]
    # Written once, under the session lock, with the original claim's worker: the read-back.
    assert (op.handoff, op.reason_code) == ("not_sent", "worker_ended")
    assert op.receipt()["reason_code"] == "worker_ended"
    revision = journal.revision
    for _ in range(3):  # idempotent: later reads write nothing more
        await runtime.snapshot(key)
    assert native_journal.read(root, sid).revision == revision
    # A cold read (no cached fold) and a history page show the same reason.
    native_journal._CACHE.clear()
    reread = next(t for t in (await runtime.snapshot(key))["turns"] if t["turn_id"] == queued)
    assert reread["not_sent"]["code"] == "worker_ended"
    # The successor never replays it: the same operation id only observes the refusal.
    again = await runtime.submit_turn(key, operation_id=queued, text="waiting")
    assert again["state"] == "failed"


@pytest.mark.anyio
async def test_worker_ended_is_derived_when_it_cannot_be_written(host, project):
    """The fallback: with the lifecycle lock held elsewhere, the snapshot still shows the reason
    and Send again's code; nothing is written until a later read can take the lock."""
    from agent_sessions import native_state

    key, queued, root, _ = await _stranded(host, project)
    sid = key.partition(":")[2]
    with native_state.session_lock(sid, wait=0):
        turn = next(t for t in (await runtime.snapshot(key))["turns"] if t["turn_id"] == queued)
        assert turn["state"] == "failed" and turn["not_sent"]["code"] == "worker_ended"
        assert native_journal.read(root, sid).operations[queued].handoff == "queued"
    await runtime.snapshot(key)
    assert native_journal.read(root, sid).operations[queued].reason_code == "worker_ended"


@pytest.mark.anyio
async def test_worker_ended_is_not_written_while_an_older_live_worker_runs(
    host, project, monkeypatch
):
    """A live worker from before #1538 refuses a journal record carrying `reason_code`, so the
    durable write waits for a worker that advertises `handoff_reasons`; the reason is derived."""
    from agent_sessions import native_state

    key, queued, root, _ = await _stranded(host, project)
    older = ident()
    native_state.update_lifecycle(older, phase="ready")  # no `handoff_reasons`
    monkeypatch.setattr(native_runtime, "_live_worker", lambda _sid: older)
    turn = next(t for t in (await runtime.snapshot(key))["turns"] if t["turn_id"] == queued)
    assert turn["not_sent"]["code"] == "worker_ended"
    assert native_journal.read(root, key.partition(":")[2]).operations[queued].handoff == "queued"


# --- #1538 review ------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_failed_liveness_read_never_settles_a_live_workers_queued_message(
    host, project, monkeypatch
):
    """`_live_worker` reads `/proc/<pid>/cgroup`, and ANY read error says "not running". That is
    not exit evidence: the message stays pending, nothing is written, and the live worker's real
    handoff then goes through."""
    key = (await runtime.create_session(ENGINES["codex"], str(project), operation_id=ident()))[
        "session_key"
    ]
    root = native_runtime._journal_root(registry.get(ENGINES["codex"]))
    await runtime.submit_turn(key, operation_id=ident(), text="WAIT_RELEASE")
    queued = ident()
    await runtime.submit_turn(key, operation_id=queued, text="follow-up")
    real = host.worker_running
    monkeypatch.setattr(host, "worker_running", lambda worker, pid: False)  # a /proc read error
    for _ in range(2):
        turn = next(t for t in (await runtime.snapshot(key))["turns"] if t["turn_id"] == queued)
        assert turn["state"] == "queued" and "not_sent" not in turn, turn
    assert native_journal.read(root, key.partition(":")[2]).operations[queued].handoff == "queued"
    monkeypatch.setattr(host, "worker_running", real)
    (project / "release-queued-turn").touch()
    _, delivered = await settle(key, queued)
    assert delivered["state"] == "completed" and delivered["reply"] == "echo:follow-up"


@pytest.mark.anyio
async def test_send_again_from_two_tabs_is_delivered_once(host, project):
    """Send again submits the server-derived retry id; two tabs pressing it at once submit the
    SAME operation, and the journal's idempotency delivers it once."""
    key, queued, _, _ = await _stranded(host, project)
    _, turn = await settle(key, queued)
    retry = turn["not_sent"]["retry"]
    assert turn["not_sent"]["retried"] is False
    # A successor is running (two submits racing to relaunch a dead generation is a separate,
    # older limit), so both tabs reach the same live worker.
    warm = ident()
    await runtime.submit_turn(key, operation_id=warm, text="warm")
    await settle(key, warm, state=("completed",))
    results = await asyncio.gather(
        runtime.submit_turn(key, operation_id=retry, text="waiting"),
        runtime.submit_turn(key, operation_id=retry, text="waiting"),
        return_exceptions=True,
    )
    assert any(not isinstance(r, BaseException) for r in results), results
    snap, sent = await settle(key, retry, state=("completed",))
    assert sent["reply"] == "echo:waiting"
    original = next(t for t in snap["turns"] if t["turn_id"] == queued)
    assert original["not_sent"]["retried"] is True
    starts = [
        f["frame"]
        for f in frames(project)
        if f["frame"].get("method") == "turn/start"
        and "".join(i.get("text", "") for i in f["frame"]["params"]["input"]) == "waiting"
    ]
    assert len(starts) == 1
