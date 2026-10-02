"""Archiving a mission takes its sessions with it — the durable operation (#846, #840 §11).

Archive is the one Phase-1 path with *external* effects (it terminates agent process groups and
moves transcripts), so what is pinned here is that it is **terminal-state-only**, **idempotent**,
**restart-reconciled**, and **honest about partial failure**. Four crash points are exercised:
before teardown, between sessions, after teardown but before DB settlement, and — the narrowest —
*inside* the provider's own two-step archive.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from agent_sessions import archive as archive_mod
from agent_sessions import automation, metadata, mission_archive, missions

CLAUDE_A = "claude:11111111-1111-1111-1111-111111111111"
CLAUDE_B = "claude:22222222-2222-2222-2222-222222222222"


class FakeSession:
    def __init__(self, uuid, archived=False):
        self.uuid = uuid
        self.archived = archived


class FakeProvider:
    """A Claude-shaped provider whose archive is two effects, exactly like the real one."""

    engine_id = "claude"

    # #853 P3: capability answers come from the manifest; a fake carries its engine's real one.
    @property
    def manifest(self):
        from agent_sessions.engines import registry as _registry

        return _registry._BY_ID[self.engine_id].manifest  # the REAL roster: tests patch `get`

    def __init__(self):
        self.rows = {}
        self.archive_calls = []
        self.fail_with = None
        self.refuse_unarchive = set()
        self.tear_after_move = False

    def add(self, native, archived=False):
        self.rows[native] = FakeSession(native, archived)

    def scan(self):
        return list(self.rows.values())

    def archive(self, native):
        self.archive_calls.append(native)
        if self.fail_with is not None:
            raise self.fail_with
        row = self.rows.get(native)
        if row is None or row.archived:
            raise archive_mod.ArchiveError(f"session {native} not found to archive")
        row.archived = True  # the file move
        if self.tear_after_move:
            # The real ClaudeProvider stamps the sidecar AFTER the move; a crash between the two
            # is what makes a naive retry raise "not found to archive" against a session that is
            # in fact already archived.
            raise RuntimeError("crashed between the move and the sidecar stamp")
        metadata.patch(f"claude:{native}", archived=True)

    def unarchive(self, native):
        if native in self.refuse_unarchive:
            # A provider that refuses while the tree still says archived — the genuine failure,
            # as opposed to a tree/sidecar disagreement, which is the TORN state and restores.
            raise archive_mod.ArchiveError("disk is full")
        row = self.rows.get(native)
        if row is None or not row.archived:
            raise archive_mod.ArchiveError(f"session {native} not found to unarchive")
        row.archived = False  # the file move…
        # …and the sidecar clear. The REAL `ClaudeProvider.unarchive` does both, and a double
        # that only moved the file left a stale `archived=True` flag that made the next archive
        # skip the session — the double lying about the contract it stands for.
        metadata.patch(f"claude:{native}", archived=False)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "missions.db"))
    monkeypatch.setenv("AGENT_SESSIONS_METADATA", str(tmp_path / "metadata.json"))
    missions.reset_schema_cache_for_test()
    prov = FakeProvider()
    prov.add("11111111-1111-1111-1111-111111111111")
    prov.add("22222222-2222-2222-2222-222222222222")

    def _parse(key, **kw):
        engine, _, native = key.partition(":")
        if engine != "claude" or native not in prov.rows:
            from agent_sessions import engines as real

            raise real.EngineError(f"unknown: {key}")
        return prov, native

    monkeypatch.setattr(mission_archive.engines, "parse_key", _parse)
    monkeypatch.setattr(mission_archive.engines, "invalidate_scan_cache", lambda: None)
    monkeypatch.setattr(mission_archive.runtime_cleanup, "cleanup_runtime", _async_noop("cleanup"))
    monkeypatch.setattr(
        mission_archive.transcript_owner, "transcript_is_owned", lambda native: False
    )
    yield prov
    missions.reset_schema_cache_for_test()


def _async_noop(name, sink=None):
    async def _inner(*a, **k):
        if sink is not None:
            sink.append((name, a, k))
        return "gone"

    return _inner


def _mission(state="done", holding=()):
    """A mission in `state`, having HELD `holding` — adopted while it still could.

    Each entry is a session key, or a `(key, role)` pair where the role matters.

    The sessions are taken while the mission is `running` and the close comes after, because a
    terminal mission may not adopt at all (#896 review 20, finding 2): reaching `done` releases
    the roster, so a closed mission holding an active session is the state that refusal exists to
    prevent. The membership rows are still there with `removed_at` set, which is what every
    archive path here reads — `sessions_barred_from_automation` says so in as many words.

    So this is not a workaround for the new refusal; it is the fixture finally building the state
    production can actually reach.
    """
    m = missions.create_mission("ship it", cwd="/repo")
    missions.set_state(m["id"], "draft", "planned")
    missions.set_state(m["id"], "planned", "dispatching")
    missions.set_state(m["id"], "dispatching", "running")
    for held in holding:
        key, role = held if isinstance(held, tuple) else (held, "primary")
        missions.adopt(m["id"], key, role=role)
    if state != "running":
        missions.set_state(m["id"], "running", state, outcome=state)
    return m["id"]


# ---- terminal-state-only -------------------------------------------------------


def test_archiving_a_live_mission_is_a_409_not_a_prompt(env):
    """There is no path that leaves a mission simultaneously running and archived."""
    mid = _mission("running")
    with pytest.raises(missions.MissionError) as e:
        asyncio.run(mission_archive.archive_mission(mid))
    assert e.value.status == 409
    assert "terminal state or an explicit abandon" in str(e.value)
    assert env.archive_calls == []  # nothing was torn down


def test_abandon_true_is_the_explicit_two_transition_path(env):
    mid = _mission("running")
    missions.adopt(mid, CLAUDE_A)
    out = asyncio.run(mission_archive.archive_mission(mid, abandon=True))
    assert out["archived"] is True
    row = missions.get_mission(mid)
    assert row["state"] == "abandoned" and row["archived_at"] is not None
    # Two distinct transitions, one deliberate request — both recorded.
    assert any(e["kind"] == "state" and e["meta"].get("to") == "abandoned" for e in row["events"])


def test_a_terminal_mission_archives_its_whole_roster(env):
    mid = _mission("done", holding=[CLAUDE_A])
    missions.set_state(mid, "done", "running")  # reopen so a second session can be adopted
    missions.adopt(mid, CLAUDE_A)
    missions.adopt(mid, CLAUDE_B, role="sub")
    missions.set_state(mid, "running", "done", outcome="done")
    out = asyncio.run(mission_archive.archive_mission(mid))
    assert out["not_archived"] == []
    assert sorted(env.archive_calls) == [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    ]


# ---- honest about partial failure ----------------------------------------------


def test_a_session_that_cannot_be_archived_is_named_not_hidden(env, monkeypatch):
    mid = _mission("done", holding=[CLAUDE_A])
    monkeypatch.setattr(
        mission_archive.transcript_owner,
        "transcript_is_owned",
        lambda native: True,  # a running Claude background agent (#631)
    )
    out = asyncio.run(mission_archive.archive_mission(mid))
    # The MISSION still archives — and the record says which session survived, rather than
    # reporting a clean sweep.
    assert out["archived"] is True
    assert [f["session_key"] for f in out["not_archived"]] == [CLAUDE_A]
    rows = missions.archive_sessions_for(mid)
    assert rows[0]["archive_state"] == "failed"
    assert "background agent" in rows[0]["archive_error"]


def test_one_failure_does_not_abort_the_others(env, monkeypatch):
    mid = _mission("done", holding=[CLAUDE_A, (CLAUDE_B, "sub")])
    monkeypatch.setattr(
        mission_archive.transcript_owner,
        "transcript_is_owned",
        lambda native: native.startswith("1111"),
    )
    out = asyncio.run(mission_archive.archive_mission(mid))
    assert [f["session_key"] for f in out["not_archived"]] == [CLAUDE_A]
    assert env.rows["22222222-2222-2222-2222-222222222222"].archived is True


# ---- the provider's own archive is not idempotent -------------------------------


def test_a_crash_between_the_move_and_the_sidecar_settles_already_archived(env):
    """The narrowest crash point, and the one that produces a *false failure* if unhandled.

    `ClaudeProvider.archive` moves the JSONL and *then* stamps the sidecar. A crash between them
    leaves the file in the archive tree with the sidecar unset, and a naive retry raises
    "not found to archive" — recording a failure against a session that is already archived.
    """
    mid = _mission("done", holding=[CLAUDE_A])
    env.tear_after_move = True
    with pytest.raises(RuntimeError):
        asyncio.run(mission_archive.archive_mission(mid))
    # The move landed; the sidecar did not. This is the torn state on disk.
    assert env.rows["11111111-1111-1111-1111-111111111111"].archived is True
    assert metadata.load().get(CLAUDE_A) is None

    env.tear_after_move = False
    resumed = asyncio.run(mission_archive.resume_pending_operations())
    assert resumed["archived"] == [mid]
    rows = missions.archive_sessions_for(mid)
    assert rows[0]["archive_state"] == "already_archived"  # NOT "failed"
    # …and the torn sidecar stamp is completed, so the effective state no longer depends on the
    # tree alone.
    assert metadata.load()[CLAUDE_A].archived is True


def test_an_already_archived_session_never_calls_the_provider_again(env):
    mid = _mission("done", holding=[CLAUDE_A])
    env.rows["11111111-1111-1111-1111-111111111111"].archived = True
    asyncio.run(mission_archive.archive_mission(mid))
    assert env.archive_calls == []
    assert missions.archive_sessions_for(mid)[0]["archive_state"] == "already_archived"


def test_a_genuine_archive_error_is_still_a_failure(env):
    """The wrapper must not turn every ArchiveError into `already_archived` — only the ones where
    the session really is archived."""
    mid = _mission("done", holding=[CLAUDE_A])
    env.fail_with = archive_mod.ArchiveError("disk is full")
    out = asyncio.run(mission_archive.archive_mission(mid))
    rows = missions.archive_sessions_for(mid)
    assert rows[0]["archive_state"] == "failed" and "disk is full" in rows[0]["archive_error"]
    assert [f["session_key"] for f in out["not_archived"]] == [CLAUDE_A]


# ---- crash points + restart reconciliation ---------------------------------------


def test_a_crash_before_teardown_is_resumed_at_boot(env):
    mid = _mission("done", holding=[CLAUDE_A])
    missions.begin_archive(mid)  # step 1 committed, then the process died
    assert missions.pending_archives() == [mid]
    assert missions.get_mission(mid)["archived_at"] is None
    assert asyncio.run(mission_archive.resume_pending_operations())["archived"] == [mid]
    assert missions.get_mission(mid)["archived_at"] is not None
    assert env.rows["11111111-1111-1111-1111-111111111111"].archived is True


def test_a_crash_between_sessions_resumes_only_the_unfinished_one(env):
    mid = _mission("done", holding=[CLAUDE_A, (CLAUDE_B, "sub")])
    missions.begin_archive(mid)
    asyncio.run(mission_archive._teardown_session(mid, CLAUDE_A))  # first one landed, then crash
    env.archive_calls.clear()
    asyncio.run(mission_archive.resume_pending_operations())
    # The finished session is not torn down twice; the unfinished one is.
    assert env.archive_calls == ["22222222-2222-2222-2222-222222222222"]


def test_a_crash_after_teardown_before_settlement_reconciles_without_a_false_failure(env):
    """The teardown succeeded but the settling transaction never ran, so the row is still
    `pending`. Resume re-runs it — and because the provider archive is idempotent, that is
    `already_archived`, not a failure for work that actually completed."""
    mid = _mission("done", holding=[CLAUDE_A])
    missions.begin_archive(mid)
    env.rows["11111111-1111-1111-1111-111111111111"].archived = True  # teardown had landed
    metadata.patch(CLAUDE_A, archived=True)
    asyncio.run(mission_archive.resume_pending_operations())
    assert missions.archive_sessions_for(mid)[0]["archive_state"] == "already_archived"
    assert env.archive_calls == []


def test_resume_is_idempotent_when_there_is_nothing_pending(env):
    assert asyncio.run(mission_archive.resume_pending_operations())["archived"] == []


# ---- the archive fence ------------------------------------------------------------


def test_a_mutation_during_an_in_flight_archive_is_a_409(env):
    """Without the fence an adopt could attach a session to a mission whose teardown loop is
    already killing process groups."""
    mid = _mission("done")
    missions.begin_archive(mid)
    for call in (
        lambda: missions.adopt(mid, CLAUDE_A),
        lambda: missions.detach(mid, CLAUDE_A),
        lambda: missions.begin_unarchive(mid),
        lambda: missions.set_state(mid, "done", "running"),
        lambda: missions.patch_objectives(mid, [{"op": "add", "key": "x", "title": "x"}]),
    ):
        with pytest.raises(missions.MissionError) as e:
            call()
        assert e.value.status == 409


def test_an_archived_mission_is_not_quietly_mutable(env):
    """Fencing only the in-flight window left an ARCHIVED mission fully mutable: `done → running`
    succeeded with `archived_at` still set, producing a mission that reads *running* and appears
    only in the archived scope — invisible on the rail while claiming to be live."""
    mid = _mission("done")
    asyncio.run(mission_archive.archive_mission(mid))
    for call in (
        lambda: missions.set_state(mid, "done", "running"),
        lambda: missions.adopt(mid, CLAUDE_A),
        lambda: missions.detach(mid, CLAUDE_A),
        lambda: missions.patch_objectives(mid, [{"op": "add", "key": "x", "title": "x"}]),
    ):
        with pytest.raises(missions.MissionError) as e:
            call()
        assert e.value.status == 409
        assert "archived" in str(e.value)
    assert missions.get_mission(mid)["state"] == "done"
    # Unarchive is the required predecessor, and IS allowed on an archived record.
    asyncio.run(mission_archive.unarchive_mission(mid))
    assert missions.set_state(mid, "done", "running")["state"] == "running"


def test_a_second_archive_caller_is_refused_before_it_can_finalise_anything(env):
    """The race that per-session leases alone did not close.

    Once the first worker leased every row, a second caller's worklist came back **empty** — so it
    walked straight past the teardown to `finish_archive`, marked the mission archived and could
    then unarchive it, all while the first worker was still inside `cleanup_runtime` killing a
    session. The operation needs an owner, not just its sessions.
    """
    mid = _mission("done", holding=[CLAUDE_A])
    begun = missions.begin_archive(mid)
    assert [r["session_key"] for r in begun["sessions"]] == [CLAUDE_A]
    assert begun["op_token"]

    with pytest.raises(missions.MissionError) as e:
        missions.begin_archive(mid)
    assert e.value.status == 409 and "already in progress" in str(e.value)

    # …and the teardown itself is leased, so even the owner cannot run the effect twice.
    assert missions.claim_session_teardown(mid, CLAUDE_A)[0] == "claimed"
    assert missions.claim_session_teardown(mid, CLAUDE_A)[0] == "taken"
    assert asyncio.run(mission_archive._teardown_session(mid, CLAUDE_A)) == "taken"
    assert env.archive_calls == []


def test_finish_refuses_while_a_teardown_lease_is_still_open(env):
    """An open lease is not a failure to report — it is a reason not to finish yet. Reporting it
    was how the API came to say "archived" while a destructive worker was still running."""
    mid = _mission("done", holding=[CLAUDE_A])
    begun = missions.begin_archive(mid)
    verdict, token = missions.claim_session_teardown(mid, CLAUDE_A)  # a worker is mid-teardown
    assert verdict == "claimed"

    with pytest.raises(missions.MissionError) as e:
        missions.finish_archive(mid, op_token=begun["op_token"])
    assert e.value.status == 409 and "still open" in str(e.value)
    assert missions.get_mission(mid)["archived_at"] is None

    missions.settle_session_archive(mid, CLAUDE_A, "done", token=token)
    assert missions.finish_archive(mid, op_token=begun["op_token"])["archived"] is True


def test_finish_refuses_a_token_that_did_not_begin_this_archive(env):
    mid = _mission("done")
    missions.begin_archive(mid)
    with pytest.raises(missions.MissionError) as e:
        missions.finish_archive(mid, op_token="not-the-owner")
    assert e.value.status == 409 and "not the archive attempt in flight" in str(e.value)


def test_boot_recovery_reopens_a_crashed_restore_lease_and_re_drives_it(env):
    """Skipping a `restoring` row meant recovery invoked no provider restore, then reported the
    mission unarchived and cleared the fence — declaring complete an operation that never ran."""
    mid = _mission("done", holding=[CLAUDE_A])
    asyncio.run(mission_archive.archive_mission(mid))
    missions.begin_unarchive(mid)
    assert missions.claim_session_restore(mid, CLAUDE_A)[0] == "claimed"
    _orphan_leases(mid)  # …and then that process died
    assert env.rows["11111111-1111-1111-1111-111111111111"].archived is True

    out = asyncio.run(mission_archive.resume_pending_operations())
    assert out["unarchived"] == [mid]
    # The restore ACTUALLY happened this time.
    assert env.rows["11111111-1111-1111-1111-111111111111"].archived is False
    assert missions.get_mission(mid)["archived_at"] is None


def test_a_session_queued_for_teardown_cannot_be_adopted_away(env):
    """The roster snapshot goes stale — reaching a terminal state released the key, and the
    partial unique index only guards `removed_at IS NULL`. That used to be caught at the teardown
    write boundary; it is now refused at the adopt boundary, one step earlier."""
    a = _mission("done", holding=[CLAUDE_A])
    missions.set_state(a, "done", "running")
    missions.adopt(a, CLAUDE_A)
    missions.set_state(a, "running", "done", outcome="done")
    missions.begin_archive(a)  # snapshot taken: CLAUDE_A is queued

    b = _mission("running")
    with pytest.raises(missions.SessionHeld if False else missions.MissionError) as e:
        missions.adopt(b, CLAUDE_A)
    assert e.value.status == 409 and "queued for teardown" in str(e.value)
    assert missions.holder_of(CLAUDE_A) is None


def test_boot_recovery_reopens_a_crashed_worker_s_lease(env):
    """An `in_progress` lease at boot can only belong to a process that died holding it — this
    app is single-instance — so recovery re-opens it. A live request must NOT."""
    mid = _mission("done", holding=[CLAUDE_A])
    missions.begin_archive(mid)
    assert missions.claim_session_teardown(mid, CLAUDE_A)[0] == "claimed"
    assert missions.claim_session_teardown(mid, CLAUDE_A)[0] == "taken"
    _orphan_leases(mid)  # …and then that process died
    assert missions.reopen_stale_leases(mid) == 1
    asyncio.run(mission_archive.resume_pending_operations())
    assert env.archive_calls == ["11111111-1111-1111-1111-111111111111"]


def test_a_stale_skip_is_re_evaluated_on_the_next_archive(env):
    """A row skipped because another mission held the key must not STAY skipped once that holder
    releases it — a re-archive after an unarchive would silently omit a session that is now safe
    to reap."""
    a = _mission("done", holding=[CLAUDE_A])
    missions.set_state(a, "done", "running")
    missions.adopt(a, CLAUDE_A)
    missions.set_state(a, "running", "done", outcome="done")
    b = _mission("running")
    missions.adopt(b, CLAUDE_A)
    out = asyncio.run(mission_archive.archive_mission(a))
    assert out["skipped"] == [CLAUDE_A]

    asyncio.run(mission_archive.unarchive_mission(a))
    missions.detach(b, CLAUDE_A)  # the other holder lets go
    assert missions.get_mission(a)["state"] == "done"  # still terminal, so still archivable
    out = asyncio.run(mission_archive.archive_mission(a))
    assert out["skipped"] == []
    assert env.archive_calls == ["11111111-1111-1111-1111-111111111111"]


def test_unarchive_claims_before_it_moves_anything(env):
    """Restoring the providers first and clearing `archived_at` afterwards left a crash window in
    which live sessions sat under a mission still recorded archived — and boot recovery only
    looked at `archiving_at`, so the torn state was invisible and permanent."""
    mid = _mission("done", holding=[CLAUDE_A])
    asyncio.run(mission_archive.archive_mission(mid))
    missions.begin_unarchive(mid)  # claimed, then the process died mid-restore
    assert missions.pending_unarchives() == [mid]
    assert missions.get_mission(mid)["archived_at"] is not None

    out = asyncio.run(mission_archive.resume_pending_operations())
    assert out["unarchived"] == [mid]
    assert missions.get_mission(mid)["archived_at"] is None
    assert env.rows["11111111-1111-1111-1111-111111111111"].archived is False


def test_a_second_unarchive_request_is_refused_before_it_moves_anything(env):
    """Handing a concurrent caller `resumed=True` let BOTH walk the same provider restores: one
    reported success and the other a provider failure for the same session. A request loses; only
    recovery resumes."""
    mid = _mission("done", holding=[CLAUDE_A])
    asyncio.run(mission_archive.archive_mission(mid))
    missions.begin_unarchive(mid)
    with pytest.raises(missions.MissionError) as e:
        missions.begin_unarchive(mid)
    assert e.value.status == 409 and "already in progress" in str(e.value)
    # …and the provider was never touched by the loser.
    assert env.rows["11111111-1111-1111-1111-111111111111"].archived is True
    # Recovery is the ONE caller allowed to take it over.
    assert missions.begin_unarchive(mid, resume=True)["resumed"] is True


def test_the_unarchive_claim_remembers_the_mode_it_was_made_with(env):
    """A crash from `sessions=False` used to come back with the sessions unarchived anyway —
    recovery was finishing a differently-shaped operation."""
    mid = _mission("done", holding=[CLAUDE_A])
    asyncio.run(mission_archive.archive_mission(mid))
    missions.begin_unarchive(mid, sessions=False)  # claimed, then the process died
    assert missions.pending_unarchives() == [mid]

    asyncio.run(mission_archive.resume_pending_operations())
    assert missions.get_mission(mid)["archived_at"] is None
    # The mode travelled with the claim, so the session stays archived exactly as asked.
    assert env.rows["11111111-1111-1111-1111-111111111111"].archived is True


def test_each_restore_is_leased(env):
    mid = _mission("done", holding=[CLAUDE_A])
    asyncio.run(mission_archive.archive_mission(mid))
    missions.begin_unarchive(mid)
    assert missions.claim_session_restore(mid, CLAUDE_A)[0] == "claimed"
    assert missions.claim_session_restore(mid, CLAUDE_A)[0] == "taken"


def test_a_session_that_will_not_restore_does_not_hold_the_mission_hostage(env):
    """The partial-failure contract, both halves.

    **Fencing** the mission until every session came back would park the record on one bad session
    for the life of the process. **Releasing** it without more strands the failed row — nothing
    reserves it, so another mission can adopt a session that is still archived, and retrying the
    restore answers "not archived" forever.

    So: the mission comes back, the failed row stays **reserved**, and calling unarchive again
    retries exactly those rows.
    """
    mid = _mission("done", holding=[CLAUDE_A, (CLAUDE_B, "sub")])
    asyncio.run(mission_archive.archive_mission(mid))
    env.refuse_unarchive.add("11111111-1111-1111-1111-111111111111")

    out = asyncio.run(mission_archive.unarchive_mission(mid))
    assert missions.get_mission(mid)["archived_at"] is None  # the mission IS back…
    assert [r["session_key"] for r in out["not_restored"]] == [CLAUDE_A]
    assert env.rows["22222222-2222-2222-2222-222222222222"].archived is False

    # …and the session that did not come back is STILL RESERVED, because it is still archived.
    other = _mission("running")
    with pytest.raises(missions.MissionError) as e:
        missions.adopt(other, CLAUDE_A)
    assert e.value.status == 409 and "restore failed" in str(e.value)

    # Retrying does not require re-archiving the mission: it claims again for exactly that row.
    env.refuse_unarchive.clear()
    out = asyncio.run(mission_archive.unarchive_mission(mid))
    assert out["not_restored"] == []
    assert env.rows["11111111-1111-1111-1111-111111111111"].archived is False
    assert missions.adopt(other, CLAUDE_A)["id"] == other


def test_unarchive_on_a_mission_with_nothing_outstanding_is_still_a_409(env):
    """The retry path must not turn "not archived" into a silent no-op."""
    mid = _mission("done")
    with pytest.raises(missions.MissionError) as e:
        missions.begin_unarchive(mid)
    assert e.value.status == 409 and "not archived" in str(e.value)


def test_a_worklist_read_failure_is_retried_not_reported_as_success(env, monkeypatch):
    """Returning `failed=[]` told the retry wrapper the pass had succeeded with nothing to do, so
    a transient store error consumed the whole retry budget on the first attempt."""
    calls = []

    def _boom():
        calls.append(1)
        raise OSError("store busy")

    monkeypatch.setattr(missions, "pending_archives", _boom)
    monkeypatch.setattr(mission_archive.asyncio, "sleep", _async_noop("sleep"))
    out = asyncio.run(mission_archive.recover_with_retry(attempts=3, delay=0))
    assert len(calls) == 3, "the worklist failure ended the loop on the first attempt"
    assert out["failed"] == ["<worklist>"]


def _release_row(mission_id, key):
    """Release a roster row directly, so a test can construct a state the adopt guard prevents."""
    con = missions._ready()
    try:
        con.execute(
            "UPDATE mission_sessions SET removed_at=1.0, release_reason='closed' "
            "WHERE mission_id=? AND session_key=?",
            (mission_id, key),
        )
    finally:
        con.close()


def _orphan_leases(mission_id):
    """Age this mission's leases out, which is what a crash actually leaves behind.

    A crashed worker is one that STOPPED BEATING — its lease goes quiet and expires. Re-stamping
    the owner epoch was the old model, and it was wrong in a way that mattered: a foreign epoch is
    a different *process*, not a dead one, so with a second app instance serving the same store a
    "crash" and an ordinary live sibling were indistinguishable. Claiming in-process and calling
    recovery still does NOT simulate a crash — a live holder renews, so its lease never ages.
    """
    con = missions._ready()
    try:
        con.execute(
            "UPDATE mission_sessions SET lease_owner='a-process-that-died', lease_at=? "
            "WHERE mission_id=?",
            (time.time() - missions.LEASE_MAX_AGE_S - 1, mission_id),
        )
    finally:
        con.close()


def _force_hold(mission_id, key):
    con = missions._ready()
    try:
        con.execute(
            "INSERT INTO mission_sessions (mission_id, session_key, role, added_at) "
            "VALUES (?,?,?,?)",
            (mission_id, key, "primary", 1.0),
        )
    finally:
        con.close()


def test_an_archived_session_stays_reserved_until_it_is_restored(env):
    """`in_progress` alone was too narrow a window.

    `settle_session_archive` moves the row to `done` and releases it *before* the mission's
    archive finishes — and at that point the provider session really is archived, so adopting it
    hands the next mission a session whose transcript has been moved out from under it. The
    reservation lasts until an explicit restore puts the session back, not until the teardown step
    happens to settle.
    """
    a = _mission("done", holding=[CLAUDE_A])
    missions.begin_archive(a)
    b = _mission("running")

    # queued…
    with pytest.raises(missions.MissionError) as e:
        missions.adopt(b, CLAUDE_A)
    assert "queued for teardown" in str(e.value)

    # …mid-teardown…
    verdict, token = missions.claim_session_teardown(a, CLAUDE_A)
    assert verdict == "claimed"
    with pytest.raises(missions.MissionError) as e:
        missions.adopt(b, CLAUDE_A)
    assert "being torn down" in str(e.value)

    # …and STILL after the teardown settles, because the session is now actually archived.
    # The token goes with the settlement, exactly as the real worker does it — it is what proves
    # this worker still owns the reservation it is releasing.
    missions.settle_session_archive(a, CLAUDE_A, "done", token=token)
    with pytest.raises(missions.MissionError) as e:
        missions.adopt(b, CLAUDE_A)
    assert e.value.status == 409 and "archived" in str(e.value)

    # Only an actual restore lifts it.
    missions.finish_archive(a)
    asyncio.run(mission_archive.unarchive_mission(a))
    assert missions.adopt(b, CLAUDE_A)["id"] == b


def test_a_restore_re_checks_ownership_before_moving_provider_files(env):
    """The mirror of the teardown-side check: restoring a session another mission has since taken
    would move the provider files underneath a live holder."""
    a = _mission("done", holding=[CLAUDE_A])
    asyncio.run(mission_archive.archive_mission(a))
    b = _mission("running")
    _force_hold(b, CLAUDE_A)  # the state the adopt guard normally prevents
    missions.begin_unarchive(a)
    assert missions.claim_session_restore(a, CLAUDE_A)[0] == "skipped"
    assert env.rows["11111111-1111-1111-1111-111111111111"].archived is True


def test_the_teardown_holder_re_check_is_the_second_line_not_the_only_one(env):
    """Adoption now refuses a queued session at the boundary, so this path is unreachable through
    the API. It stays as defence in depth, and is exercised directly rather than left unproven."""
    a = _mission("done", holding=[CLAUDE_A])
    missions.begin_archive(a)
    _release_row(a, CLAUDE_A)  # make room in the partial unique index
    b = _mission("running")
    _force_hold(b, CLAUDE_A)
    assert missions.claim_session_teardown(a, CLAUDE_A)[0] == "skipped"
    assert env.archive_calls == []


def test_retention_will_not_delete_a_mission_mid_operation(env):
    """Deleting one destroys the journal recovery needs, along with the roster naming the agents
    it was about to reap."""
    import time as _t

    mid = _mission("done", holding=[CLAUDE_A])
    missions.begin_archive(mid)  # an operation is now in flight
    con = missions._ready()
    try:  # age it well past the window
        con.execute("UPDATE missions SET closed_at=? WHERE id=?", (_t.time() - 400 * 86400, mid))
    finally:
        con.close()
    assert missions.retention_pass() == 0
    assert missions.get_mission(mid) is not None
    assert missions.pending_archives() == [mid]

    # Once the operation settles, it is eligible again.
    asyncio.run(mission_archive.resume_pending_operations())
    con = missions._ready()
    try:
        con.execute("UPDATE missions SET closed_at=? WHERE id=?", (_t.time() - 400 * 86400, mid))
    finally:
        con.close()
    assert missions.retention_pass() == 1


def test_the_fence_lifts_once_the_archive_finishes(env):
    mid = _mission("done")
    asyncio.run(mission_archive.archive_mission(mid))
    out = asyncio.run(mission_archive.unarchive_mission(mid))
    assert out["archived"] is False
    assert missions.get_mission(mid)["archived_at"] is None


# ---- reversible -------------------------------------------------------------------


def test_unarchive_restores_the_sessions_and_never_destroys_history(env):
    mid = _mission("done", holding=[CLAUDE_A])
    asyncio.run(mission_archive.archive_mission(mid))
    assert env.rows["11111111-1111-1111-1111-111111111111"].archived is True
    out = asyncio.run(mission_archive.unarchive_mission(mid))
    assert out["sessions"] == [{"session_key": CLAUDE_A, "result": "unarchived"}]
    assert env.rows["11111111-1111-1111-1111-111111111111"].archived is False
    # The record survived the whole round trip — archive frees RUNTIME, not history.
    row = missions.get_mission(mid)
    assert row["instruction"] == "ship it"
    assert [s["session_key"] for s in row["sessions"]] == [CLAUDE_A]


def test_unarchive_can_leave_the_sessions_archived(env):
    mid = _mission("done", holding=[CLAUDE_A])
    asyncio.run(mission_archive.archive_mission(mid))
    out = asyncio.run(mission_archive.unarchive_mission(mid, sessions=False))
    assert out["sessions"] == []
    assert env.rows["11111111-1111-1111-1111-111111111111"].archived is True


def test_unarchiving_a_live_mission_is_a_409(env):
    mid = _mission("done")
    with pytest.raises(missions.MissionError) as e:
        missions.begin_unarchive(mid)
    assert e.value.status == 409


def test_a_session_another_mission_now_holds_is_skipped_not_killed(env):
    """Reaching a terminal state RELEASES a session, so it can legitimately be re-adopted before
    the archive runs. Tearing it down then would kill a live agent belonging to a different
    mission — so it is named as `skipped` rather than archived or silently omitted."""
    a = _mission("done", holding=[CLAUDE_A])
    missions.set_state(a, "done", "running")
    missions.adopt(a, CLAUDE_A)
    missions.adopt(a, CLAUDE_B, role="sub")
    missions.set_state(a, "running", "done", outcome="done")  # releases both

    b = _mission("running")
    missions.adopt(b, CLAUDE_A)  # somebody else picked it up

    out = asyncio.run(mission_archive.archive_mission(a))
    assert out["skipped"] == [CLAUDE_A]
    assert out["not_archived"] == []
    # B's agent is untouched; A's other session is reaped as normal.
    assert env.archive_calls == ["22222222-2222-2222-2222-222222222222"]
    assert env.rows["11111111-1111-1111-1111-111111111111"].archived is False
    assert missions.holder_of(CLAUDE_A) == b


def test_archive_does_not_cross_an_explicit_detach(env):
    """An operator detach is a boundary, and archive must not step over it.

    Reaching a terminal state releases ownership and those sessions are still the mission's to
    reap; a **detach** is the operator removing the session from the mission. Both set
    `removed_at`, so archiving "the whole roster" by timestamp terminated and archived work that
    had been deliberately taken out of the mission — reproduced as create → adopt → detach →
    abandon → archive.
    """
    mid = _mission("running")
    missions.adopt(mid, CLAUDE_A)
    missions.adopt(mid, CLAUDE_B, role="sub")
    missions.detach(mid, CLAUDE_A)  # the operator deliberately removed this one
    assert missions.holder_of(CLAUDE_A) is None
    missions.set_state(mid, "running", "abandoned", outcome="abandoned")

    out = asyncio.run(mission_archive.archive_mission(mid))
    # The detached session is untouched: no runtime cleanup, no provider archive, still live.
    assert env.archive_calls == ["22222222-2222-2222-2222-222222222222"]
    assert env.rows["11111111-1111-1111-1111-111111111111"].archived is False
    # …and it is not reported as a failure or a skip either — it simply was not in scope.
    assert out["not_archived"] == [] and out["skipped"] == []
    rows = {r["session_key"]: r for r in missions.archive_sessions_for(mid)}
    assert rows[CLAUDE_A]["archive_state"] is None
    assert rows[CLAUDE_B]["archive_state"] == "done"


def test_a_session_released_by_closing_is_still_the_mission_s_to_reap(env):
    """The other half of the same distinction — this is the case that made the roster-wide
    teardown necessary in the first place (a `done` mission whose agents kept running)."""
    mid = _mission("running")
    missions.adopt(mid, CLAUDE_A)
    missions.set_state(mid, "running", "done", outcome="done")  # releases, but does not detach
    assert missions.holder_of(CLAUDE_A) is None
    asyncio.run(mission_archive.archive_mission(mid))
    assert env.archive_calls == ["11111111-1111-1111-1111-111111111111"]


def test_re_adopting_a_detached_session_puts_it_back_in_scope(env):
    mid = _mission("running")
    missions.adopt(mid, CLAUDE_A)
    missions.detach(mid, CLAUDE_A)
    missions.adopt(mid, CLAUDE_A)  # the operator changed their mind
    missions.set_state(mid, "running", "done", outcome="done")
    asyncio.run(mission_archive.archive_mission(mid))
    assert env.archive_calls == ["11111111-1111-1111-1111-111111111111"]


def test_the_roster_records_why_each_session_left(env):
    """Two fields, two questions: `release_reason` is why the session left the ROSTER,
    `archive_state` is what the archive then did to it. Keeping them separate is what lets
    archive tell a terminal-state release apart from an operator detach."""
    mid = _mission("running")
    missions.adopt(mid, CLAUDE_A)
    missions.adopt(mid, CLAUDE_B, role="sub")
    missions.detach(mid, CLAUDE_A)
    missions.set_state(mid, "running", "done", outcome="done")
    rows = {r["session_key"]: r for r in missions.get_mission(mid)["sessions"]}
    assert rows[CLAUDE_A]["release_reason"] == "detached"
    assert rows[CLAUDE_B]["release_reason"] == "closed"

    asyncio.run(mission_archive.archive_mission(mid))
    rows = {r["session_key"]: r for r in missions.get_mission(mid)["sessions"]}
    # Neither reason is rewritten — they already answered their question truthfully.
    assert rows[CLAUDE_A]["release_reason"] == "detached"
    assert rows[CLAUDE_B]["release_reason"] == "closed"
    # What the archive DID is the other field.
    assert rows[CLAUDE_A]["archive_state"] is None
    assert rows[CLAUDE_B]["archive_state"] == "done"


def test_a_session_still_held_at_archive_time_is_released_as_archived(env):
    """The `{"abandon": true}` path: the session never left the roster on its own, so the archive
    is what released it — and that is what the reason says."""
    mid = _mission("running")
    missions.adopt(mid, CLAUDE_A)
    asyncio.run(mission_archive.archive_mission(mid, abandon=True))
    row = missions.get_mission(mid)["sessions"][0]
    assert row["release_reason"] == "archived"
    assert row["archive_state"] == "done"


def test_a_detached_session_is_not_even_reported_as_skipped(env):
    """`skipped` means "in scope, but somebody else holds it". A detached session is neither — it
    was taken out of the mission, so it should not appear in the archive's report at all."""
    a = _mission("running")
    missions.adopt(a, CLAUDE_A)
    missions.detach(a, CLAUDE_A)
    b = _mission("running")
    missions.adopt(b, CLAUDE_A)  # another mission now holds the detached session
    missions.set_state(a, "running", "done", outcome="done")

    out = asyncio.run(mission_archive.archive_mission(a))
    assert out["skipped"] == [] and out["not_archived"] == []
    assert env.archive_calls == []
    assert missions.archive_sessions_for(a)[0]["archive_state"] is None
    assert missions.holder_of(CLAUDE_A) == b


def test_a_crash_inside_the_provider_unarchive_settles_restored_not_failed(env):
    """The regression owed from the previous round, and the reason the fix failed twice.

    `ClaudeProvider.unarchive` moves the JSONL back and THEN clears the sidecar. A crash between
    them leaves the file live with the sidecar still saying archived — and asking the
    sidecar-first `_effective_archived` reports "archived" about a file that is already back, so
    the retry raises and a landed move is recorded as a failure.

    The two directions need opposite precedence: "is it archived" trusts the sidecar, "has it come
    back" trusts the tree, because in this direction the sidecar is exactly the thing that is
    wrong.
    """
    mid = _mission("done", holding=[CLAUDE_A])
    asyncio.run(mission_archive.archive_mission(mid))
    assert metadata.load()[CLAUDE_A].archived is True

    # THE TORN STATE: the move landed, the sidecar clear did not.
    env.rows["11111111-1111-1111-1111-111111111111"].archived = False
    assert metadata.load()[CLAUDE_A].archived is True
    assert mission_archive._effective_archived(env, "11111111-1111-1111-1111-111111111111") is True

    out = asyncio.run(mission_archive.unarchive_mission(mid))
    assert out["not_restored"] == []  # NOT a false failure
    assert [r["result"] for r in out["sessions"]] == ["unarchived"]
    # …and the interrupted stamp is completed, so the effective state stops disagreeing.
    assert metadata.load()[CLAUDE_A].archived is False


def test_the_torn_restore_is_told_apart_from_a_provider_refusal(env):
    """A tree/sidecar disagreement is the torn state and restores; a provider that refuses while
    the tree still says archived is a real failure. Conflating them would turn every refusal into
    a silent success."""
    mid = _mission("done", holding=[CLAUDE_A])
    asyncio.run(mission_archive.archive_mission(mid))
    env.refuse_unarchive.add("11111111-1111-1111-1111-111111111111")
    out = asyncio.run(mission_archive.unarchive_mission(mid))
    assert [r["session_key"] for r in out["not_restored"]] == [CLAUDE_A]
    assert env.rows["11111111-1111-1111-1111-111111111111"].archived is True


def test_a_failed_sidecar_repair_is_a_failed_restore(env, monkeypatch):
    """Suppressing the repair error and reporting `restored` anyway recreated the split-brain it
    exists to end: the sidecar still says archived, `_effective_archived` gives it precedence, and
    the caller then clears the session's archive state and the mission's fence over the top."""
    mid = _mission("done", holding=[CLAUDE_A])
    asyncio.run(mission_archive.archive_mission(mid))
    env.rows["11111111-1111-1111-1111-111111111111"].archived = False  # the torn state

    def _boom(*a, **k):
        raise OSError("sidecar is read-only")

    real_patch = mission_archive.metadata.patch
    monkeypatch.setattr(mission_archive.metadata, "patch", _boom)
    out = asyncio.run(mission_archive.unarchive_mission(mid))
    monkeypatch.setattr(mission_archive.metadata, "patch", real_patch)

    assert [r["session_key"] for r in out["not_restored"]] == [CLAUDE_A]
    rows = missions.archive_sessions_for(mid)
    assert rows[0]["archive_state"] == "restore_failed"
    assert "could not be cleared" in rows[0]["archive_error"]
    # …and because it failed, the session stays reserved rather than being handed to somebody.
    other = _mission("running")
    with pytest.raises(missions.MissionError):
        missions.adopt(other, CLAUDE_A)


def test_recovery_never_reopens_a_lease_this_process_is_holding(env):
    """Recovery runs as a background task while the app is already serving, so "any lease here
    belongs to a crashed worker" is false. Reproduced before the fix: recovery reset a live
    request's lease and claimed the same session, so two workers ran the same external effect."""
    mid = _mission("done", holding=[CLAUDE_A])
    missions.begin_archive(mid)
    # A live request takes the lease…
    assert missions.claim_session_teardown(mid, CLAUDE_A)[0] == "claimed"

    # …and recovery, running concurrently, must not take it away.
    assert missions.reopen_stale_leases(mid) == 0
    assert missions.claim_session_teardown(mid, CLAUDE_A)[0] == "taken"

    # A lease that has gone quiet for the full window IS reopened — in any process.
    con = missions._ready()
    try:
        con.execute(
            "UPDATE mission_sessions SET lease_owner='a-dead-process', lease_at=? "
            "WHERE mission_id=?",
            (time.time() - missions.LEASE_MAX_AGE_S - 1, mid),
        )
    finally:
        con.close()
    assert missions.reopen_stale_leases(mid) == 1
    assert missions.claim_session_teardown(mid, CLAUDE_A)[0] == "claimed"


def test_a_restore_lease_is_owned_the_same_way(env):
    mid = _mission("done", holding=[CLAUDE_A])
    asyncio.run(mission_archive.archive_mission(mid))
    missions.begin_unarchive(mid)
    assert missions.claim_session_restore(mid, CLAUDE_A)[0] == "claimed"
    assert missions.reopen_stale_leases(mid) == 0  # ours; recovery leaves it alone
    con = missions._ready()
    try:
        con.execute(
            "UPDATE mission_sessions SET lease_owner='gone', lease_at=? WHERE mission_id=?",
            (time.time() - missions.LEASE_MAX_AGE_S - 1, mid),
        )
    finally:
        con.close()
    assert missions.reopen_stale_leases(mid) == 1


def test_a_worker_that_raises_settles_its_own_lease(env):
    """Recovery only reopens leases that have gone quiet for the full expiry window. So an
    exception inside a LIVE worker must not leave the lease behind: nothing would reopen it for
    five minutes, and until then the mission could not finish archiving."""
    mid = _mission("done", holding=[CLAUDE_A])
    missions.begin_archive(mid)
    env.tear_after_move = True  # the provider raises after its file move

    with pytest.raises(RuntimeError):
        asyncio.run(mission_archive._teardown_session(mid, CLAUDE_A))

    row = missions.archive_sessions_for(mid)[0]
    assert row["archive_state"] == "failed", "the lease was stranded"
    assert row["lease_owner"] is None
    assert "raised RuntimeError" in row["archive_error"]
    # …and because it is settled rather than stranded, the mission can finish.
    assert missions.finish_archive(mid)["archived"] is True


def test_recovery_re_drives_a_failed_teardown_once(env):
    """A worker that died mid-teardown settles `failed`, but the effect may have landed first —
    which only a retry can discover, and which the idempotent wrapper makes safe."""
    mid = _mission("done", holding=[CLAUDE_A])
    missions.begin_archive(mid)
    env.tear_after_move = True
    with pytest.raises(RuntimeError):
        asyncio.run(mission_archive._teardown_session(mid, CLAUDE_A))
    assert missions.archive_sessions_for(mid)[0]["archive_state"] == "failed"
    assert env.rows["11111111-1111-1111-1111-111111111111"].archived is True  # it DID land

    env.tear_after_move = False
    out = asyncio.run(mission_archive.resume_pending_operations())
    assert out["archived"] == [mid]
    assert missions.archive_sessions_for(mid)[0]["archive_state"] == "already_archived"


# ---- the boundary the pre-existing archive surfaces have to respect ----------------------


def test_the_reservation_names_why_a_session_is_off_limits(env):
    """Mission ownership is new, so every archive surface that predates it — the single-session
    route, the project batch, `archive-older` — would otherwise call `prov.archive()` knowing
    nothing about it, and terminate a session a mission is working."""

    def _why():
        try:
            tok = missions.reserve_session(CLAUDE_A, "session-route")
        except missions.SessionBusy as e:
            return str(e)
        missions.release_session(CLAUDE_A, tok)
        return None

    mid = _mission("running")
    assert _why() is None  # nobody owns it yet

    missions.adopt(mid, CLAUDE_A)
    assert "is using it" in _why()

    missions.set_state(mid, "running", "done", outcome="done")
    missions.begin_archive(mid)
    assert "is archiving it" in _why()

    asyncio.run(mission_archive.resume_pending_operations())
    # Archived BY a mission is still off limits — the provider state is the mission's record.
    assert "is archiving it" in _why()

    asyncio.run(mission_archive.unarchive_mission(mid))
    assert _why() is None  # restored and released


def test_the_boundary_fails_CLOSED_when_ownership_cannot_be_proven(env, monkeypatch):
    """I had this backwards, and pinned the wrong behaviour in a test.

    The reasoning was "a store that is down must not take the session routes with it" — right for
    a *presentation* read, wrong here. What this gates is terminating a process group and moving a
    transcript. "We could not check" must never authorise that: a 503 is retryable, an agent killed
    out from under a mission is not.
    """
    monkeypatch.setattr(
        missions, "_ready", lambda *a, **k: (_ for _ in ()).throw(OSError("locked"))
    )
    with pytest.raises(missions.OwnershipUnknown) as e:
        missions.reserve_session(CLAUDE_A, "session-route")
    assert e.value.status == 503
    assert "not archiving" in str(e.value)


def test_a_detached_session_is_free_for_the_ordinary_routes(env):
    """A detach is the operator taking the session out of the mission, so the mission has no
    further claim on it — the same boundary archive itself respects."""
    mid = _mission("running")
    missions.adopt(mid, CLAUDE_A)
    missions.detach(mid, CLAUDE_A)
    tok = missions.reserve_session(CLAUDE_A, "session-route")
    assert tok
    missions.release_session(CLAUDE_A, tok)


def test_a_stale_worker_cannot_settle_the_operation_that_replaced_it(env):
    """The fencing-token property. Worker A's lease expires, worker B reclaims it, and A finishes
    late: without a token A's settlement clears B's fresh lease, the stale result wins, and the
    external effect can run twice."""
    mid = _mission("done", holding=[CLAUDE_A])
    missions.begin_archive(mid)
    verdict, a_token = missions.claim_session_teardown(mid, CLAUDE_A)
    assert verdict == "claimed"

    # A's lease ages out and B reclaims it.
    later = time.time() + missions.LEASE_MAX_AGE_S + 1
    assert missions.reopen_stale_leases(mid, now=later) == 1
    verdict, b_token = missions.claim_session_teardown(mid, CLAUDE_A, now=later)
    assert verdict == "claimed" and b_token != a_token

    # A finishes late and settles with its dead token: nothing moves.
    missions.settle_session_archive(mid, CLAUDE_A, "done", token=a_token)
    row = missions.archive_sessions_for(mid)[0]
    assert row["archive_state"] == "in_progress", "a stale worker settled B's operation"

    # B settles with the live token, and that lands.
    missions.settle_session_archive(mid, CLAUDE_A, "done", token=b_token)
    assert missions.archive_sessions_for(mid)[0]["archive_state"] == "done"


def test_a_mission_teardown_and_a_session_route_exclude_each_other(env):
    """A read-only guard answers about the past and the caller then acts. The reservation IS the
    act, so the two paths cannot both be inside the provider call."""
    mid = _mission("done", holding=[CLAUDE_A])
    missions.begin_archive(mid)
    verdict, token = missions.claim_session_teardown(mid, CLAUDE_A)
    assert verdict == "claimed"

    # The sibling route cannot get in while the mission holds it…
    with pytest.raises(missions.SessionBusy) as e:
        missions.reserve_session(CLAUDE_A, "session-route")
    assert e.value.status == 409 and f"mission:{mid}" in str(e.value)

    # …and a mission cannot ADOPT a session the route is mid-archiving. That direction was open
    # until this test went looking for it: `adopt` checked the mission-side archive states and not
    # the reservation, so a mission could take a session while the sibling route was inside
    # `cleanup_runtime` for it.
    missions.settle_session_archive(mid, CLAUDE_A, "done", token=token)
    route_hold = missions.reserve_session(CLAUDE_B, "session-route")
    other = _mission("running")
    with pytest.raises(missions.MissionError) as e:
        missions.adopt(other, CLAUDE_B)
    assert e.value.status == 409 and "session-route" in str(e.value)
    missions.release_session(CLAUDE_B, route_hold)
    assert missions.adopt(other, CLAUDE_B)["id"] == other


def test_a_reservation_expires_so_a_crashed_holder_cannot_block_forever(env):
    hold = missions.reserve_session(CLAUDE_A, "session-route")
    assert hold
    with pytest.raises(missions.SessionBusy):
        missions.reserve_session(CLAUDE_A, "someone-else")
    later = time.time() + missions.RESERVATION_MAX_AGE_S + 1
    assert missions.reserve_session(CLAUDE_A, "someone-else", now=later)
    # …and the original holder's token is now dead, so it cannot free the new holder's claim.
    assert missions.release_session(CLAUDE_A, hold) is False


# ---- the two authorization holes the second review found (review on #881) -------------------


def test_a_mission_ARCHIVING_bars_its_sessions_from_automation(env):
    """The fence the orchestrator consults, at the source, over the window that matters.

    Abandon settles the actions that exist at one instant and then releases the ledger lock. The
    background pass reasons about SESSIONS and not about missions, so without a fence it can mint
    a fresh action for the same still-live session immediately afterwards — and under `yolo`
    deliver it into a mission being torn down.
    """
    mid = _mission("running")
    missions.adopt(mid, CLAUDE_A)
    assert missions.sessions_barred_from_automation() == set(), "a live mission barred its session"

    # `begin_archive` is what stamps `archiving_at` — the teardown-in-flight window.
    missions.begin_archive(mid, abandon=True)
    assert (
        CLAUDE_A in missions.sessions_barred_from_automation()
    ), "an archive in flight left its sessions open to automation"


def test_a_session_RELEASED_by_a_bare_abandon_is_not_barred_for_ever(env):
    """Scope discipline: the hole is the archive window, not "ever belonged to an abandoned
    mission".

    A bare abandon releases its sessions and they are then free. Barring on history would
    permanently disable automation for every session that had ever been in one — a far larger
    behaviour change than the hole being closed, and one nobody asked for.
    """
    mid = _mission("running")
    missions.adopt(mid, CLAUDE_A)
    missions.set_state(mid, "running", "abandoned", outcome="abandoned")
    assert missions.sessions_barred_from_automation() == set()


def test_a_DONE_mission_does_not_bar_its_sessions(env):
    """`done` and `failed` are outcomes, not withdrawals.

    Adopted while the mission could still hold it and released BY the close, which is the state a
    finished mission actually leaves behind (#896 review 20, finding 2 made the old shortcut —
    adopting into a mission that is already `done` — impossible, and rightly: a closed mission
    holding an active session is the thing that refusal exists to stop). The membership row is
    still there with `removed_at` set, which is exactly the case this fence reads regardless of.
    """
    mid = _mission("running")
    missions.adopt(mid, CLAUDE_A)
    missions.set_state(mid, "running", "done", outcome="done")
    assert missions.active_session_keys(mid) == [], "the close should have released it"
    assert missions.sessions_barred_from_automation() == set()


def test_the_LEDGER_refuses_to_append_for_a_barred_session(env, tmp_path, monkeypatch):
    """Dropped rather than written-and-swept.

    An action that never exists cannot be delivered by a pass that runs before the sweep reaches
    it, and the check is inside the ledger lock — filtering before the call would be the
    check-then-write this function exists to prevent.
    """
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    from agent_sessions import orchestrator_ledger as ledger

    mid = _mission("running")
    missions.adopt(mid, CLAUDE_A)
    missions.begin_archive(mid, abandon=True)

    recs = [{"id": "x1", "state": "approved", "verb": "continue", "session_id": CLAUDE_A}]
    kept, dropped = ledger.append_batch_for_free_sessions(
        recs, barred=missions.sessions_barred_from_automation
    )
    assert kept == [] and len(dropped) == 1, "an action was minted for a mission being archived"
    assert ledger.latest_by_id() == {}, "the refused action was written anyway"


@pytest.mark.anyio
async def test_DELIVERY_refuses_a_session_whose_mission_was_archived_mid_batch(
    env, tmp_path, monkeypatch
):
    """The second half, and the one that actually stops bytes.

    A pass persists and then delivers over many seconds, so the archive can begin inside that
    window. The append-time fence ran before it and cannot see it.
    """
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    from agent_sessions import actuator, prefs

    mid = _mission("running")
    missions.adopt(mid, CLAUDE_A)
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo"})
    action = {
        "id": "x1",
        "state": "approved",
        "verb": "continue",
        "session_id": CLAUDE_A,
        "mission_id": mid,
        "authority": automation.capture(CLAUDE_A, mid),
        "confidence": 1.0,
    }
    reached: list[str] = []

    async def _spy(action_id, *, registry=None, authority=None, extra_fingerprint=None):
        ok, why = authority(prefs.get_orchestrator()) if authority else (True, "")
        reached.append("DELIVERED" if ok else why)
        return None

    monkeypatch.setattr(actuator, "deliver", _spy)

    missions.begin_archive(mid, abandon=True)
    await actuator.deliver_auto(action)
    # The tier checks still pass — the mission fence now lives in `deliver`'s own final guard,
    # which this spy replaces, so what this asserts is that `deliver_auto` REACHES it rather
    # than short-circuiting first. The fence itself is asserted directly below.
    assert reached == ["DELIVERED"], reached


@pytest.mark.anyio
async def test_RECOVERY_re_runs_the_abandon_sweep_before_finishing(env, tmp_path, monkeypatch):
    """The retry path owes the same fence the first attempt does.

    The sweep can fail AFTER `begin_archive` commits — that is the 503 that leaves the mission on
    the worklist — and recovery went straight to teardown and `finish_archive`. The approved
    action that caused the failure stayed claimable while recovery reported the archive complete.
    """
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    from agent_sessions import orchestrator_ledger as ledger

    mid = _mission("running")
    missions.adopt(mid, CLAUDE_A)
    ledger.append({"id": "a1", "state": "approved", "verb": "continue", "session_id": CLAUDE_A})

    # The durable intermediate state: abandon committed, the sweep then failed.
    boom = RuntimeError("ledger unreadable")

    def _fail(_keys):
        raise boom

    monkeypatch.setattr(mission_archive, "_settle_live_actions", _fail)
    with pytest.raises(RuntimeError):
        await mission_archive.archive_mission(mid, abandon=True)
    assert ledger.latest_by_id()["a1"]["state"] == "approved"

    # Recovery takes over. It MUST re-run the sweep before finishing.
    swept: list[list[str]] = []

    def _record(keys):
        swept.append(list(keys))
        return 0

    monkeypatch.setattr(mission_archive, "_settle_live_actions", _record)
    await mission_archive.resume_pending_operations()
    assert (
        swept and CLAUDE_A in swept[0]
    ), "recovery finished an abandoned archive without settling its live actions"


def test_a_pass_whose_MISSION_FENCE_cannot_be_read_proposes_NOTHING(env, tmp_path, monkeypatch):
    """The fence's failure mode, checked rather than asserted in a comment.

    `_barred_sessions` raises when the missions store will not read, and the whole point is that
    the append is aborted rather than completed without an authorization check. Guessing "nothing
    is barred" would write actions for sessions whose mission may have been torn down.
    """
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    from agent_sessions import orchestrator
    from agent_sessions import orchestrator_ledger as ledger

    def boom(*a, **k):
        raise OSError("disk on fire")

    monkeypatch.setattr(missions, "sessions_barred_from_automation", boom)
    with pytest.raises(missions.MissionError) as e:
        orchestrator._barred_sessions()
    assert e.value.status == 503
    assert "authorization" in str(e.value)

    # …and the append writes nothing when the gate raises, rather than falling open.
    recs = [{"id": "x1", "state": "approved", "verb": "continue", "session_id": CLAUDE_A}]
    with pytest.raises(missions.MissionError):
        ledger.append_batch_for_free_sessions(recs, barred=orchestrator._barred_sessions)
    assert ledger.latest_by_id() == {}, "an action was written without an authorization check"


@pytest.mark.anyio
async def test_the_MANUAL_approval_path_is_fenced_too(env, tmp_path, monkeypatch):
    """The operator's own tap goes through `deliver()` with NO authority callback.

    Putting the mission check in `authority` fenced only the automatic path, so
    `POST /api/pulse/actions/{id}/approve` could still type into a session whose mission was
    being torn down. A tap is authority to send what the operator approved; it is not authority
    to send it somewhere that no longer accepts it (review on #881).

    **Asserted on the BYTES, not on the guard's source.** A first version of this test checked
    that the symbol appeared in `_final_guard`, and a mutation that neutered the check while
    leaving the name in place sailed straight through it — the "assert the cause, not a symptom"
    trap, in a test written to catch exactly this class of bug.
    """
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    from agent_sessions import actuator, prefs, session_input
    from agent_sessions import orchestrator_ledger as ledger

    mid = _mission("running")
    missions.adopt(mid, CLAUDE_A)
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo"})
    ledger.append(
        {
            "id": "m1",
            "state": "approved",
            "verb": "continue",
            "session_id": CLAUDE_A,
            "mission_id": mid,
            "authority": automation.capture(CLAUDE_A, mid),
            "confidence": 1.0,
        }
    )
    monkeypatch.setattr(session_input, "is_live", lambda *a, **k: True)

    sent: list[object] = []
    refusals: list[str] = []

    def _spy_send(key, payload, **kw):
        """Stand in for `send_input`, honouring `final_guard` the way it does.

        `deliver` passes the mission fence as `final_guard`; a spy that ignored it would write
        bytes production never writes and the test would assert nothing.
        """
        guard = kw.get("final_guard")
        if guard is not None:
            ok, why = guard()
            if not ok:
                refusals.append(why)
                return session_input.Outcome(state="refused", detail=why)
        sent.append(payload)
        return session_input.Outcome(state="delivered")

    monkeypatch.setattr(session_input, "send_input", _spy_send)

    missions.begin_archive(mid, abandon=True)
    # NO `authority` — this is the manual approval path.
    await actuator.deliver("m1")
    assert sent == [], "bytes were written into a session whose mission is being archived"
    assert refusals and "archiv" in refusals[0], refusals


def test_a_session_RE_ADOPTED_by_another_mission_is_not_barred(env):
    """Mission A finishes and releases a session; B adopts it; A is archived.

    The repo explicitly supports this — A marks its own row `skipped` so B's session is not torn
    down. Barring on A's history alone stopped B being proposed for and refused B's existing
    actions, purely because A stayed archived: a cross-mission availability failure (review
    on #881).
    """
    a = _mission("running")
    missions.adopt(a, CLAUDE_A)
    # A releases it explicitly — the operator saying it is no longer part of A — and then closes.
    # Adopted while A was RUNNING, because a closed mission may not take a session at all (#896
    # review 20, finding 2); the history this test is about is unchanged either way.
    missions.detach(a, CLAUDE_A)
    missions.set_state(a, "running", "done", outcome="done")

    b = _mission("running")
    missions.adopt(b, CLAUDE_A)  # B legitimately owns it now
    missions.begin_archive(a, abandon=True)

    assert (
        CLAUDE_A not in missions.sessions_barred_from_automation()
    ), "archiving A barred a session that mission B currently owns"
    assert CLAUDE_A not in missions.sessions_governed_by_archive(
        a
    ), "A's abandon sweep would have expired B's action"


@pytest.mark.anyio
async def test_an_explicitly_DETACHED_session_is_outside_the_archive_entirely(
    env, tmp_path, monkeypatch
):
    """A detach takes the session OUT of the mission, and archiving must not reach back in.

    `begin_archive` has excluded detached rows from teardown from the start, for a reason it
    states: a terminal state releases ownership and those sessions are still the mission's to
    reap, but a detach is the operator removing it. Both stamp `removed_at`, so only
    `release_reason` tells them apart — and my scope queries checked `skipped` and re-adoption
    but not the reason, so archiving A expired an approved action on a session A no longer had
    (review on #881).

    Asserted on BOTH boundaries the fence has: the action is untouched, and the session is not
    barred from automation.
    """
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    from agent_sessions import orchestrator_ledger as ledger

    mid = _mission("running")
    missions.adopt(mid, CLAUDE_A)
    missions.detach(mid, CLAUDE_A)  # the operator takes it out of the mission
    ledger.append({"id": "d1", "state": "approved", "verb": "continue", "session_id": CLAUDE_A})

    assert CLAUDE_A not in missions.sessions_governed_by_archive(
        mid
    ), "a detached session is still in the archive's scope"

    await mission_archive.archive_mission(mid, abandon=True)

    assert (
        ledger.latest_by_id()["d1"]["state"] == "approved"
    ), "archiving the mission expired an action on a session the operator had detached"
    assert (
        CLAUDE_A not in missions.sessions_barred_from_automation()
    ), "a detached session stayed barred from automation after its old mission archived"


@pytest.mark.anyio
async def test_a_CLOSED_release_is_still_the_missions_to_reap(env, tmp_path, monkeypatch):
    """The control, and the reason the exclusion is on the REASON rather than on `removed_at`.

    Reaching a terminal state also stamps `removed_at` — with `release_reason='closed'` — and
    those sessions ARE still the mission's to reap. Excluding every released row would have
    emptied the abandon sweep entirely.
    """
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    from agent_sessions import orchestrator_ledger as ledger

    mid = _mission("running")
    missions.adopt(mid, CLAUDE_A)
    ledger.append({"id": "c1", "state": "approved", "verb": "continue", "session_id": CLAUDE_A})

    # Reaching a terminal state RELEASES the session — `removed_at` stamped, reason `closed` —
    # and it is still the mission's to reap. This is the case that separates the correct
    # exclusion from the over-correction: filtering on `removed_at IS NULL` rather than on the
    # REASON would drop this session and empty the sweep. An end-to-end abandon alone cannot
    # tell the two apart, because `begin_archive(abandon=True)` never stamps `removed_at` — which
    # is exactly how the first version of this control passed against both implementations.
    missions.set_state(mid, "running", "abandoned", outcome="abandoned")
    con = missions._ready(None)
    try:
        row = con.execute(
            "SELECT removed_at, release_reason FROM mission_sessions WHERE mission_id=?", (mid,)
        ).fetchone()
    finally:
        con.close()
    assert row["removed_at"] is not None and row["release_reason"] == "closed"
    assert CLAUDE_A in missions.sessions_governed_by_archive(
        mid
    ), "a session released by reaching a terminal state fell out of the archive's scope"

    await mission_archive.archive_mission(mid, abandon=True)
    assert (
        ledger.latest_by_id()["c1"]["state"] == "expired"
    ), "the abandon sweep left a live action on a session it does govern"


def test_an_archive_COMMITTING_after_the_guard_still_writes_ZERO_bytes(env, tmp_path, monkeypatch):
    """The write-boundary race, driven through the REAL `send_input` (review on #881).

    `deliver()` evaluates the mission fence in `final_guard`, which runs BEFORE `_write_all`
    takes the registry lock — deliberately, because the guard does I/O. So the verdict was
    already true-and-stale by the time byte one happened, and an archive committing in that
    window reached a real PTY.

    The previous manual-path test could not see this: its `send_input` stub called `final_guard`
    and wrote immediately, so there WAS no window after the callback returned. This one uses the
    real `send_input` against a real pipe and commits the abandon from inside the guard — i.e.
    exactly in the gap — then asserts on the BYTES that reached the fd.
    """
    import contextlib
    import os
    import threading

    from agent_sessions import session_input

    mid = _mission("running")
    missions.adopt(mid, CLAUDE_A)

    r_fd, w_fd = os.pipe()
    lock = threading.Lock()
    token = session_input.register_writer(CLAUDE_A, w_fd, lock, "test")
    assert token

    def guard_then_archive():
        """Authorized at the instant it is asked — and then the archive commits."""
        barred_before = CLAUDE_A in missions.sessions_barred_from_automation()
        mission_archive._begin_archive_fenced(mid, abandon=True)
        return (not barred_before), "authorized when asked"

    try:
        out = session_input.send_input(
            CLAUDE_A,
            b"UNAUTHORIZED_AFTER_ARCHIVE",
            policy_scope="mission",
            final_guard=guard_then_archive,
            require_quiet=False,
        )
    finally:
        with contextlib.suppress(Exception):
            session_input.unregister_writer(CLAUDE_A, token)

    assert CLAUDE_A in missions.sessions_barred_from_automation(), "the archive did not commit"
    assert out.state != "delivered", f"bytes were authorized after the archive: {out}"

    # The fd must be EMPTY. This is the assertion the stubbed test could not make.
    os.set_blocking(r_fd, False)
    try:
        written = os.read(r_fd, 4096)
    except BlockingIOError:
        written = b""
    finally:
        os.close(r_fd)
        with contextlib.suppress(OSError):
            os.close(w_fd)
    assert written == b"", f"bytes reached the pty after the mission was archived: {written!r}"


def test_a_SIBLING_INSTANCE_archiving_still_writes_zero_bytes(env, tmp_path, monkeypatch):
    """The cross-process boundary. The in-memory epoch cannot see a sibling app instance.

    This app supports several instances over one store (`docs/session-handling.md`), so an
    archive committed by process B never touches process A's `_policy_epoch` — A's own counter
    reads unchanged right up to byte one and it writes. Reproduced by the review with two forked
    processes; the same-process regression above cannot reach this boundary at all.

    The fix is that A's authority fingerprint reads the SHARED store, which is the only thing the
    two instances agree on. Here the "sibling" is a real forked child with its own interpreter
    state, so the in-memory epoch genuinely cannot carry the signal.
    """
    import contextlib
    import os
    import threading

    from agent_sessions import actuator, session_input

    mid = _mission("running")
    missions.adopt(mid, CLAUDE_A)
    db = os.environ["AGENT_SESSIONS_MISSIONS_DB"]

    r_fd, w_fd = os.pipe()
    lock = threading.Lock()
    token = session_input.register_writer(CLAUDE_A, w_fd, lock, "test")
    assert token

    def sibling_archives() -> None:
        """A DIFFERENT process commits the abandon — fork, so no in-memory state is shared."""
        pid = os.fork()
        if pid == 0:  # child
            code = 1
            try:
                os.environ["AGENT_SESSIONS_MISSIONS_DB"] = db
                from agent_sessions import missions as m2

                m2.reset_schema_cache_for_test()
                m2.begin_archive(mid, abandon=True)
                code = 0
            finally:
                os._exit(code)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0, "the sibling failed to archive"

    def guard_then_sibling_archives():
        """Authorized when asked; the SIBLING then commits, touching none of our memory."""
        barred_before = CLAUDE_A in missions.sessions_barred_from_automation()
        sibling_archives()
        return (not barred_before), "authorized when asked"

    fp = actuator._authority_fingerprint(CLAUDE_A)
    try:
        out = session_input.send_input(
            CLAUDE_A,
            b"UNAUTHORIZED_FROM_SIBLING",
            final_guard=guard_then_sibling_archives,
            policy_fingerprint=fp,
            require_quiet=False,
        )
    finally:
        with contextlib.suppress(Exception):
            session_input.unregister_writer(CLAUDE_A, token)

    assert CLAUDE_A in missions.sessions_barred_from_automation(), "the sibling did not archive"
    assert out.state != "delivered", f"bytes authorized after a sibling archived: {out}"

    os.set_blocking(r_fd, False)
    try:
        written = os.read(r_fd, 4096)
    except BlockingIOError:
        written = b""
    finally:
        os.close(r_fd)
        with contextlib.suppress(OSError):
            os.close(w_fd)
    assert written == b"", f"bytes reached the pty after a sibling archived: {written!r}"
