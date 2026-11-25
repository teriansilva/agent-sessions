"""`append_batch_for_free_sessions(supersede=…)` — retire and append in ONE ledger hold (#1960).

HQ's message replaces a proposal nobody has started delivering. The retirement and the append
share the critical section, so a claim, an approval or another proposal racing it leaves the
waiting action alone and drops the append: never two live actions for one session.
"""

from __future__ import annotations

import time
import uuid

import pytest

from agent_sessions import orchestrator_ledger as ledger

SID = "claude:00000000-0000-4000-8000-0000000000c1"
OTHER = "claude:00000000-0000-4000-8000-0000000000c2"


@pytest.fixture
def path(tmp_path):
    return tmp_path / "ledger.jsonl"


def _rec(sid=SID, state="proposed"):
    return {"id": uuid.uuid4().hex, "state": state, "ts": time.time(), "session_id": sid}


def _states(path):
    return {r["id"]: r["state"] for r in ledger.latest_by_id(path).values()}


def test_a_waiting_proposal_is_retired_and_the_replacement_kept_in_one_hold(path):
    old = _rec()
    ledger.append(old, path)
    new = _rec()
    kept, dropped = ledger.append_batch_for_free_sessions(
        [new], path, supersede={old["id"]: old["ts"]}, supersede_detail="superseded by HQ"
    )
    assert [r["id"] for r in kept] == [new["id"]] and dropped == []
    latest = ledger.latest_by_id(path)
    assert latest[old["id"]]["state"] == "stale"
    assert latest[old["id"]]["detail"] == "superseded by HQ"
    assert latest[new["id"]]["state"] == "proposed"


def test_a_proposal_that_moved_since_it_was_read_is_left_alone(path):
    old = _rec()
    ledger.append(old, path)
    new = _rec()
    kept, dropped = ledger.append_batch_for_free_sessions(
        [new], path, supersede={old["id"]: old["ts"] - 1.0}
    )
    assert kept == [] and [r["id"] for r in dropped] == [new["id"]]
    assert _states(path) == {old["id"]: "proposed"}


def test_a_claimed_action_is_never_retired(path):
    old = _rec()
    ledger.append(old, path)
    claimed = ledger.claim(old["id"], ledger.CLAIMABLE_STATES, path)
    assert claimed is not None
    new = _rec()
    kept, _dropped = ledger.append_batch_for_free_sessions(
        [new], path, supersede={old["id"]: claimed["ts"]}
    )
    assert kept == [] and _states(path) == {old["id"]: "claimed"}


def test_only_an_action_of_a_session_in_the_batch_is_retired(path):
    other = _rec(sid=OTHER)
    ledger.append(other, path)
    new = _rec()
    kept, _dropped = ledger.append_batch_for_free_sessions(
        [new], path, supersede={other["id"]: other["ts"]}
    )
    assert [r["id"] for r in kept] == [new["id"]]
    assert _states(path)[other["id"]] == "proposed"


def test_a_replacement_its_own_guard_rejects_retires_nothing(path):
    """Hermes on #2135: a secret registered before the hold makes the record guard drop the
    replacement — the waiting proposal must then stay exactly as it was."""
    old = _rec()
    ledger.append(old, path)
    new = _rec()
    kept, dropped = ledger.append_batch_for_free_sessions(
        [new], path, supersede={old["id"]: old["ts"]}, record_guard=lambda r: False
    )
    assert kept == [] and [r["id"] for r in dropped] == [new["id"]]
    assert _states(path) == {old["id"]: "proposed"}


def test_another_live_action_besides_the_named_one_retires_nothing(path):
    named = _rec()
    ledger.append(named, path)
    unnamed = _rec()
    ledger.append(unnamed, path)
    new = _rec()
    kept, _dropped = ledger.append_batch_for_free_sessions(
        [new], path, supersede={named["id"]: named["ts"]}
    )
    assert kept == []
    assert _states(path) == {named["id"]: "proposed", unnamed["id"]: "proposed"}


def test_a_failed_replacement_write_leaves_the_original_pending(path, monkeypatch):
    """Hermes on #2135: a write failing (ENOSPC) — or a crash — between "retire" and "append" must
    not leave neither. The retirement rides on the replacement's own line, so it fails with it."""
    import errno

    old = _rec()
    ledger.append(old, path)
    real = ledger._append_locked

    def full_disk(p, rec, line):
        if rec.get("id") != old["id"]:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(p, rec, line)

    monkeypatch.setattr(ledger, "_append_locked", full_disk)
    new = _rec()
    with pytest.raises(OSError):
        ledger.append_batch_for_free_sessions([new], path, supersede={old["id"]: old["ts"]})
    monkeypatch.setattr(ledger, "_append_locked", real)
    assert _states(path) == {old["id"]: "proposed"}


def test_the_retirement_is_one_line_with_the_replacement(path):
    old = _rec()
    ledger.append(old, path)
    new = _rec()
    before = path.read_text().count("\n")
    ledger.append_batch_for_free_sessions(
        [new], path, supersede={old["id"]: old["ts"]}, supersede_detail="superseded by HQ"
    )
    assert path.read_text().count("\n") == before + 1  # one write, not two
    latest = ledger.latest_by_id(path)
    assert latest[old["id"]]["state"] == "stale"
    assert latest[old["id"]]["superseded_by"] == new["id"]
    assert latest[old["id"]]["detail"] == "superseded by HQ"


def test_a_compaction_keeps_the_superseded_state(path):
    old = _rec()
    ledger.append(old, path)
    new = _rec()
    ledger.append_batch_for_free_sessions([new], path, supersede={old["id"]: old["ts"]})
    ledger.compact(path)
    assert _states(path) == {old["id"]: "stale", new["id"]: "proposed"}
