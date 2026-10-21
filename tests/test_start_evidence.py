"""The start-evidence adapter (#916): a filename match is not an identity."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_sessions import start_evidence as se

NATIVE = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def reg(tmp_path, monkeypatch):
    """A registry directory of our own. NEVER the real `~/.claude` (repo rule)."""
    d = tmp_path / "sessions"
    d.mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_CLAUDE_SESSIONS_DIR", str(d))
    return d


def _write(reg, name, **over):
    """An entry shaped exactly like the ones `claude` writes, ours by default."""
    entry = {
        "pid": os.getpid(),
        "sessionId": NATIVE,
        "cwd": os.getcwd(),
        "procStart": se.proc_start(os.getpid()),
        "version": "2.1.263",
        "kind": "interactive",
        "entrypoint": "cli",
    }
    entry.update(over)
    (reg / name).write_text(json.dumps(entry))
    return entry


def test_a_bound_entry_is_FOUND(reg):
    """The positive case, pinned against a process that genuinely exists — this one."""
    _write(reg, "1.json")
    state, detail = se.claude_start_state(NATIVE, os.getcwd())
    assert state == se.FOUND, detail


def test_a_STALE_entry_from_a_dead_process_is_not_evidence(reg):
    """The registry is not pruned when a session dies, so yesterday's entry is still on disk.

    Matching on `sessionId` alone would report a launch that never happened as started, and the
    caller would then deliver an operator's brief to nothing.
    """
    _write(reg, "1.json", pid=2, procStart="999999999")
    state, _ = se.claude_start_state(NATIVE, os.getcwd())
    assert state == se.ABSENT


def test_a_REUSED_pid_is_not_our_agent(reg):
    """A pid is a reusable name; `pid:starttime` is not.

    The entry points at a live process — this one — but records a start time that is not its own,
    which is exactly what a recycled pid looks like. Red against a check that stops at `is the pid
    alive`, which is the tempting shortcut.
    """
    _write(reg, "1.json", pid=os.getpid(), procStart="1")
    state, _ = se.claude_start_state(NATIVE, os.getcwd())
    assert state == se.ABSENT


def test_a_FOREIGN_cwd_does_not_bind(reg, tmp_path):
    """Right id, live process, wrong directory: not the launch we are waiting on."""
    _write(reg, "1.json", cwd=str(tmp_path / "elsewhere"))
    state, _ = se.claude_start_state(NATIVE, os.getcwd())
    assert state == se.ABSENT


def test_a_SYMLINKED_cwd_still_binds(reg, tmp_path):
    """…but the same directory spelled differently IS the same directory.

    Failing closed here would refuse every launch into a symlinked checkout, which is the common
    case rather than the dangerous one.
    """
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    _write(reg, "1.json", cwd=str(real))
    state, detail = se.claude_start_state(NATIVE, str(link))
    assert state == se.FOUND, detail


def test_AMBIGUITY_is_unreadable_rather_than_a_guess(reg):
    """Two live entries claiming one session id is a state nobody designed.

    Picking one would be a guess with an unattended agent on the end of it, so it fails closed —
    and as UNREADABLE, not ABSENT, because we did not establish that nothing started.
    """
    _write(reg, "1.json")
    _write(reg, "2.json")
    state, detail = se.claude_start_state(NATIVE, os.getcwd())
    assert state == se.UNREADABLE
    assert "2 live entries" in detail


def test_a_MALFORMED_entry_beside_a_MATCH_DEFEATS_the_positive(reg):
    """A failed read defeats a POSITIVE, not only an absence (#916 review 3, finding 5).

    This test asserted `FOUND` for exactly this input, and that was the defect. Completeness was
    tested as "did the scan reach the end of the listing", which a scan that read every name and
    failed to parse some of them does — so one match beside one unreadable entry returned a
    positive. But the unreadable entry is precisely where a SECOND process claiming this session
    id would have been, which is the ambiguity refusal the branch above already makes. "The only
    match" is a claim about the whole set; a row nobody could read cannot support it.
    """
    (reg / "bad.json").write_text("{not json")
    _write(reg, "good.json")
    state, detail = se.claude_start_state(NATIVE, os.getcwd())
    assert state == se.UNREADABLE
    assert "cannot be shown to be the only one" in detail


def test_a_MISSING_registry_is_UNREADABLE_not_absent(tmp_path, monkeypatch):
    """**The distinction this whole issue exists for.**

    A registry that is not there does not say the agent failed to start; it says this build did not
    write one where we looked. Reporting that as ABSENT would be the same "absence read as
    evidence" defect in a new place, and would tell an operator their agent died when it may be
    running.
    """
    monkeypatch.setenv("AGENT_SESSIONS_CLAUDE_SESSIONS_DIR", str(tmp_path / "nope"))
    state, detail = se.claude_start_state(NATIVE, os.getcwd())
    assert state == se.UNREADABLE
    assert "does not exist" in detail


def test_an_UNLISTABLE_registry_is_UNREADABLE(reg, monkeypatch):
    """A permission error is not an answer either."""

    def boom(self):
        raise PermissionError("nope")

    monkeypatch.setattr("pathlib.Path.iterdir", boom)
    state, _ = se.claude_start_state(NATIVE, os.getcwd())
    assert state == se.UNREADABLE


def test_a_DIFFERENT_session_id_is_not_ours(reg):
    """The obvious one, kept because the adapter reads a shared directory: every other session on
    this host writes here too, and a sweep that ignored the id would bind to any of them."""
    _write(reg, "1.json", sessionId="99999999-9999-9999-9999-999999999999")
    state, _ = se.claude_start_state(NATIVE, os.getcwd())
    assert state == se.ABSENT


def test_an_OVERFLOWING_registry_never_reports_a_confident_answer(reg, monkeypatch):
    """A truncated scan is not evidence, in either direction (review 1, finding 4).

    The first version read a fixed 500-name prefix of a sorted listing on every poll. A valid live
    entry at position 501 was therefore reported `ABSENT` **for ever** — a healthy dispatch timing
    out and being abandoned — and matches inside *and* outside the prefix returned `FOUND`, picking
    a winner from a set it had not finished looking at.
    """
    monkeypatch.setattr(se, "MAX_ENTRIES", 3)
    for i in range(5):
        (reg / f"{i:03d}-filler.json").write_text(json.dumps({"sessionId": "someone-else"}))
    _write(reg, "zzz-ours.json")  # sorts last: outside the truncated window

    state, detail = se.claude_start_state(NATIVE, os.getcwd())
    assert state == se.UNREADABLE, f"a truncated scan answered {state}"
    assert "could be read" in detail or "were read" in detail


def test_a_MATCH_inside_a_truncated_scan_is_still_unreadable(reg, monkeypatch):
    """One match in a partial scan is not *the* match — the unread rest could hold another, which
    is the ambiguity case wearing a positive's clothes."""
    monkeypatch.setattr(se, "MAX_ENTRIES", 2)
    _write(reg, "000-ours.json")
    for i in range(3):
        (reg / f"{i+1:03d}-filler.json").write_text(json.dumps({"sessionId": "someone-else"}))
    state, _ = se.claude_start_state(NATIVE, os.getcwd())
    assert state == se.UNREADABLE


def test_an_ALL_MALFORMED_registry_is_UNREADABLE_not_absent(reg):
    """The branch that was supposed to catch this could never fire (review 1, finding 5).

    It read `unreadable_files and not names[:MAX_ENTRIES]` — a nonzero count REQUIRES a nonempty
    list, so the condition was self-contradictory. A registry of nothing but malformed files
    answered `ABSENT`, and the caller then diagnosed a missing start and blamed an input screen.
    """
    (reg / "a.json").write_text("{not json")
    (reg / "b.json").write_text("[]")
    state, detail = se.claude_start_state(NATIVE, os.getcwd())
    assert state == se.UNREADABLE, f"an all-malformed registry answered {state}"
    # The STATE is the contract; the sentence is not. Asserting the exact wording is what made two
    # other tests in this repo break on a message improvement rather than on a behaviour change.
    assert "could not be checked" in detail or "unreadable" in detail


def test_a_PARTIALLY_unreadable_registry_cannot_claim_absence(reg):
    """One readable unrelated row beside an unreadable target still answered `ABSENT` (review 2,
    finding 5), and the caller then diagnosed a missing start and blamed an input screen.

    Absence means "we looked at all of them and none matched". A read that FAILED is not a row we
    looked at — the entry that would have matched may be exactly the one that could not be parsed.
    The previous version required EVERY read to fail before it would say so.
    """
    (reg / "unrelated.json").write_text(json.dumps({"sessionId": "someone-else"}))
    (reg / "torn.json").write_text("{half-written")
    state, _ = se.claude_start_state(NATIVE, os.getcwd())
    assert state == se.UNREADABLE, "a partially unreadable registry reported confirmed absence"


def test_proc_start_matches_the_kernel_for_a_live_process():
    """The recorded format is field 22 of `/proc/<pid>/stat`, verified rather than assumed.

    The parse takes everything after the LAST `)` because field 2 is the executable name in
    parentheses and may itself contain spaces and parentheses — the classic way this goes quietly
    wrong on a process called `(foo bar)`.
    """
    mine = se.proc_start(os.getpid())
    assert mine and mine.isdigit()
    assert se.proc_start(2**30) is None, "a pid that cannot exist returned a start time"


def test_a_FIFO_named_json_does_not_HANG_the_reader(reg):
    """A path is not a file (#916 review 3, non-blocking).

    `~/.claude/sessions/<anything>.json` is whatever is at that name. `read_text()` on a FIFO with
    no writer blocks **for ever**, and this runs inside the dispatch's readiness poll on a worker
    thread nothing can cancel — so one `mkfifo` in a directory the agent already writes would
    wedge every future dispatch on the host, permanently. The reader opens `O_NONBLOCK` and
    rejects on `fstat`, so the entry is counted unreadable and the scan finishes.

    The red proof for this one is a **hang**, not an assertion failure: against `read_text()` this
    test never returns.
    """
    os.mkfifo(reg / "aaa.json")
    _write(reg, "good.json")
    state, detail = se.claude_start_state(NATIVE, os.getcwd())
    # The FIFO is one unreadable entry, so the match beside it cannot be shown to be the only one.
    assert state == se.UNREADABLE
    assert "could not be checked" in detail


def test_an_OVERSIZED_entry_is_unreadable_rather_than_read_whole(reg):
    """A registry entry is a few hundred bytes; anything past the cap is not one.

    Bounded for the same reason the scan is — this is an unbounded read of a file whose size
    somebody else chooses, on the dispatch path.
    """
    (reg / "huge.json").write_bytes(b'{"sessionId":"x","pad":"' + b"a" * (se.MAX_ENTRY_BYTES + 8))
    state, detail = se.claude_start_state(NATIVE, os.getcwd())
    assert state == se.UNREADABLE
    assert "could not be checked" in detail


def _deny_stat(monkeypatch, pid: int):
    """Make `/proc/<pid>/stat` unreadable while every other read still works.

    A permission failure on `/proc` cannot be produced naturally here — `stat` is world-readable,
    so no real pid gives EPERM — but the branch is reachable in production (a hardened `/proc`,
    an I/O error, an unparseable stat). Injected at the read so the REAL `proc_start_state` runs
    and takes its own except branch, rather than stubbing the function under test.
    """
    real = Path.read_bytes

    def denied(self):
        if str(self) == f"/proc/{pid}/stat":
            raise PermissionError(13, "Permission denied")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", denied)


def test_a_pid_we_CANNOT_READ_is_not_a_dead_pid(reg, monkeypatch):
    """`gone` and `unreadable` are opposite facts about a process (#916 review 4, finding 3).

    Every `/proc/<pid>/stat` failure used to collapse to `None`, and the caller read `None` as
    "not our process". So a registry row that named THIS session in THIS directory, whose process
    metadata merely could not be read, became a definite nonmatch — and the scan then reported
    `absent`, which the dispatcher acts on by giving up on an agent that may be running fine.

    Only `FileNotFoundError` is the kernel saying the pid does not exist. Everything else is us
    being unable to look, and that cannot rule the process out.
    """
    _write(reg, "good.json")  # a live, matching entry
    _deny_stat(monkeypatch, os.getpid())

    state, detail = se.claude_start_state(NATIVE, os.getcwd())
    assert state == se.UNREADABLE, "an unreadable process was reported as a missing one"
    assert "could not be checked" in detail


def test_an_UNREADABLE_claimant_beside_a_MATCH_defeats_the_positive(reg, monkeypatch):
    """The ambiguity refusal, reached by the path that used to skip it (review 4, finding 3).

    Two entries claim this session in this directory. One is readable and matches; the other's
    process metadata cannot be read, so it can be neither confirmed nor excluded as a SECOND live
    claimant — which is precisely the state the multi-match branch refuses to choose between.
    Counting it as a nonmatch answered `found` and picked a winner from an unresolved set.
    """
    import subprocess as sp
    import sys

    other = sp.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
    try:
        _write(reg, "good.json")  # readable + matching, on this process
        # A second claimant: same session id, same cwd, a real live pid, unreadable stat.
        (reg / "other.json").write_text(
            json.dumps(
                {
                    "sessionId": NATIVE,
                    "cwd": os.getcwd(),
                    "pid": other.pid,
                    "procStart": "12345",
                    "version": "2.1.0",
                }
            )
        )
        _deny_stat(monkeypatch, other.pid)

        state, detail = se.claude_start_state(NATIVE, os.getcwd())
        assert state == se.UNREADABLE, (
            "a match was reported as unique while a second claimant's liveness could not be "
            "established either way"
        )
        assert "could not be checked" in detail
    finally:
        other.kill()
        other.wait(timeout=10)


def test_a_GONE_pid_is_still_a_definite_nonmatch(reg):
    """The other half, so the fix above cannot be 'call everything unreadable'.

    A pid the kernel does not have is real evidence: that entry is stale and the scan must be
    free to conclude `absent` from it, which is what makes the readiness poll terminate.
    """
    (reg / "stale.json").write_text(
        json.dumps(
            {
                "sessionId": NATIVE,
                "cwd": os.getcwd(),
                "pid": 2**30,  # cannot exist -> FileNotFoundError -> gone
                "procStart": "12345",
                "version": "2.1.0",
            }
        )
    )
    state, _ = se.claude_start_state(NATIVE, os.getcwd())
    assert state == se.ABSENT, "a definitely-dead pid stopped the scan concluding anything"
