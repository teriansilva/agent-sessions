"""CopilotProvider discovery + launch argv + archive move + transcript/access wiring (#1565).

The synthetic store reproduces Copilot CLI 1.0.68/1.0.70's real on-disk layout, captured from a
live ``~/.copilot``: one directory per session under ``session-state/<uuid>/`` holding a
``workspace.yaml`` (written at session creation) and an ``events.jsonl`` event log, with archived
sessions MOVED to the sibling ``session-state-archive/<uuid>/`` tree.

``--no-auto-update`` is the load-bearing flag: without it the CLI self-downloads into
``~/.cache/copilot/pkg/`` and re-execs from there, outside the provenance-bound entrypoint. It
must ride on every console launch, probe and access check — those spawns are pinned here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_sessions import agent_tools, agent_usage, engines, metadata, start_evidence
from agent_sessions.engines import base
from agent_sessions.plugins import process as plugin_process

_SID = "11111111-2222-4333-8444-555555555555"
_SID2 = "aaaabbbb-cccc-4ddd-8eee-ffff00001111"
_SID3 = "b5470439-98ff-4212-a57e-d730681e1ad9"

_CREATED = "2026-07-19T14:19:03.061Z"
_UPDATED = "2026-07-19T15:20:04.500Z"
_TS1 = "2026-07-19T14:19:10.000Z"
_TS2 = "2026-07-19T14:25:00.000Z"


def _write_session(
    root,
    sid,
    cwd,
    *,
    archived=False,
    workspace=True,
    events=None,
):
    """One session dir under the live tree (or the archive tree). ``workspace=False`` omits
    ``workspace.yaml`` (a partial crash artifact — never a row); ``events`` is the list of
    ``events.jsonl`` records, None = no event log yet."""
    tree = root / ("session-state-archive" if archived else "session-state")
    sdir = tree / sid
    sdir.mkdir(parents=True, exist_ok=True)
    if workspace:
        (sdir / "workspace.yaml").write_text(
            f"id: {sid}\ncwd: {cwd}\nclient_name: github/cli\nuser_named: false\n"
            f"summary_count: 0\ncreated_at: {_CREATED}\nupdated_at: {_UPDATED}\n",
            encoding="utf-8",
        )
    if events is not None:
        (sdir / "events.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8"
        )
    return sdir


def _user(text, **extra):
    data = {"content": text}
    data.update(extra.pop("data", {}))
    rec = {"type": "user.message", "data": data, "timestamp": _TS1}
    rec.update(extra)
    return rec


def _assistant(text, model="gpt-5.5", ts=_TS2):
    return {
        "type": "assistant.message",
        "data": {"messageId": "m1", "model": model, "content": text},
        "timestamp": ts,
    }


@pytest.fixture
def copilot_home(tmp_path, monkeypatch):
    """A synthetic ``~/.copilot`` rooted in a tmp HOME — never the operator's real store."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(metadata, "_PATH", tmp_path / "metadata.json", raising=False)
    root = tmp_path / ".copilot"
    root.mkdir()
    return root


def _provider():
    return engines.get("copilot").kind if hasattr(engines.get("copilot"), "kind") else None


def _kind():
    from agent_sessions.engines.copilot import CopilotProvider

    return CopilotProvider()


# --- id pattern + routing ------------------------------------------------------------------


def test_id_pattern_is_a_bare_uuid():
    pat = _kind().id_pattern
    assert pat.fullmatch(_SID)
    assert not pat.fullmatch(f"{_SID}/../escape")
    assert not pat.fullmatch("session_" + _SID)
    assert not pat.fullmatch("not-a-uuid")


def test_parse_key_routes_engine_qualified_id(copilot_home):
    _write_session(copilot_home, _SID, "/home/u/proj")
    prov, native = engines.parse_key(f"copilot:{_SID}")
    assert prov.engine_id == "copilot"
    assert native == _SID


# --- scan -----------------------------------------------------------------------------------


def test_scan_reads_live_and_archive_trees(copilot_home):
    _write_session(
        copilot_home,
        _SID,
        "/home/u/proj",
        events=[
            {"type": "session.start", "data": {"sessionId": _SID}, "timestamp": _TS1},
            _user("Fix the flaky test"),
            _assistant("On it"),
        ],
    )
    _write_session(copilot_home, _SID2, "/home/u/old", archived=True, events=[_user("Old work")])
    rows = {r.uuid: r for r in _kind().scan()}
    assert set(rows) == {_SID, _SID2}
    row = rows[_SID]
    assert row.engine == "copilot"
    assert row.cwd == "/home/u/proj"
    assert row.first_user_message == "Fix the flaky test"
    assert row.archived is False
    assert row.created_at == pytest.approx(1784470743.061, abs=1)
    # Last real activity is the newest event timestamp, not a file mtime.
    assert row.last_mtime == pytest.approx(1784471100.0, abs=1)
    assert rows[_SID2].archived is True


def test_scan_skips_detached_subagent_sessions(copilot_home):
    _write_session(
        copilot_home,
        _SID,
        "/home/u/proj",
        events=[
            {
                "type": "session.start",
                "data": {"sessionId": _SID, "detachedFromSpawningParentSessionId": _SID2},
                "timestamp": _TS1,
            },
            _user("subagent work"),
        ],
    )
    assert _kind().scan() == []


def test_scan_title_ignores_synthetic_and_subagent_user_messages(copilot_home):
    _write_session(
        copilot_home,
        _SID,
        "/home/u/proj",
        events=[
            _user("/skill-run --fast", data={"source": "skill-skill-run"}),
            _user("continue", data={"isAutopilotContinuation": True}),
            _user("subagent asks", agentId="agent-1"),
            _user("The actual first human prompt"),
        ],
    )
    (row,) = _kind().scan()
    assert row.first_user_message == "The actual first human prompt"


def test_scan_skips_dirs_without_workspace_or_cwd(copilot_home):
    _write_session(copilot_home, _SID, "/home/u/proj", workspace=False)
    sdir = _write_session(copilot_home, _SID2, "/home/u/proj")
    (sdir / "workspace.yaml").write_text(f"id: {_SID2}\n", encoding="utf-8")  # no cwd
    assert _kind().scan() == []


def test_scan_without_events_uses_workspace_stamps(copilot_home):
    _write_session(copilot_home, _SID, "/home/u/proj")
    (row,) = _kind().scan()
    assert row.first_user_message == ""
    assert row.created_at == pytest.approx(1784470743.061, abs=1)
    assert row.last_mtime == pytest.approx(1784474404.5, abs=1)


def test_scan_checked_names_an_unreadable_session_dir(copilot_home, monkeypatch):
    _write_session(copilot_home, _SID, "/home/u/proj")
    monkeypatch.setattr(
        "agent_sessions.engines.copilot.CopilotProvider._row",
        lambda self, p, **kw: (_ for _ in ()).throw(OSError("nope")),
    )
    rows, problems = _kind().scan_checked()
    assert rows == []
    assert problems and "copilot" in problems[0]


def test_scan_checked_names_an_unreadable_workspace(copilot_home, monkeypatch):
    """An existing-but-unreadable workspace.yaml is a PROBLEM on the checked path (#993),
    never a silently absent row — the unchecked sidebar scan stays fail-soft."""
    _write_session(copilot_home, _SID, "/home/u/proj", events=[_user("hi")])
    real_open = Path.open

    def deny_workspace(self, *args, **kwargs):
        if self.name == "workspace.yaml":
            raise PermissionError("denied")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_workspace)
    rows, problems = _kind().scan_checked()
    assert rows == []
    assert problems and _SID in problems[0] and "PermissionError" in problems[0]
    assert _kind().scan() == []


def test_scan_checked_names_an_unreadable_events_log(copilot_home, monkeypatch):
    _write_session(copilot_home, _SID, "/home/u/proj", events=[_user("hi")])
    real_open = Path.open

    def deny_events(self, *args, **kwargs):
        if self.name == "events.jsonl":
            raise PermissionError("denied")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_events)
    rows, problems = _kind().scan_checked()
    assert rows == []
    assert problems and _SID in problems[0] and "PermissionError" in problems[0]


def test_scan_checked_tolerates_missing_partial_artifacts(copilot_home):
    """A genuinely ABSENT workspace.yaml / events.jsonl is a partial crash artifact, not a
    read failure: no row, and no problem either."""
    _write_session(copilot_home, _SID, "/home/u/proj", workspace=False)
    _write_session(copilot_home, _SID2, "/home/u/proj")  # no events.jsonl yet
    rows, problems = _kind().scan_checked()
    assert [r.uuid for r in rows] == [_SID2]
    assert problems == []


# --- lookup ----------------------------------------------------------------------------------


def test_lookup_reads_either_tree(copilot_home):
    _write_session(copilot_home, _SID, "/home/u/proj", events=[_user("live")])
    _write_session(copilot_home, _SID2, "/home/u/old", archived=True, events=[_user("archived")])
    kind = _kind()
    live = kind.lookup(_SID)
    assert live is not None and live.archived is False and live.first_user_message == "live"
    old = kind.lookup(_SID2)
    assert old is not None and old.archived is True
    assert kind.lookup(_SID3) is None
    assert kind.lookup("not-a-uuid") is None


# --- archive / unarchive (a real directory MOVE, like claude) ---------------------------------


def test_archive_moves_the_directory_and_sets_the_flag(copilot_home):
    _write_session(copilot_home, _SID, "/home/u/proj", events=[_user("work")])
    kind = _kind()
    kind.archive(_SID)
    assert not (copilot_home / "session-state" / _SID).exists()
    assert (copilot_home / "session-state-archive" / _SID / "events.jsonl").is_file()
    assert kind.archive_state(_SID) == "archived"
    # The vendor's `--resume` / `session/list` enumerate the live tree only.
    assert _kind().lookup(_SID).archived is True
    kind.unarchive(_SID)
    assert (copilot_home / "session-state" / _SID / "events.jsonl").is_file()
    assert not (copilot_home / "session-state-archive" / _SID).exists()
    assert kind.archive_state(_SID) == "not-archived"
    assert _kind().lookup(_SID).archived is False


def test_archive_state_is_not_an_existence_check(copilot_home):
    kind = _kind()
    assert kind.archive_state(_SID3) == "not-archived"  # ENOENT means "not here"
    assert kind.archive_state("not-a-uuid") == "not-archived"


def test_archive_rejects_a_bad_id(copilot_home):
    with pytest.raises(Exception, match="bad copilot id"):
        _kind().archive("../escape")


def test_archive_refuses_to_nest_a_recreated_live_directory(copilot_home):
    # A still-running Copilot recreated the live directory after the session was archived
    # (the sidecar flag kept it archived). Archiving again must not nest the live tree under
    # session-state-archive/<id>/<id>/, where neither the scanner nor the vendor sees it.
    _write_session(copilot_home, _SID, "/home/u/proj", archived=True, events=[_user("old")])
    _write_session(copilot_home, _SID, "/home/u/proj", events=[_user("new")])
    metadata.patch(f"copilot:{_SID}", archived=True)
    with pytest.raises(base.EngineError, match="both the live and archive trees"):
        _kind().archive(_SID)
    assert (copilot_home / "session-state" / _SID / "events.jsonl").is_file()
    assert (copilot_home / "session-state-archive" / _SID / "events.jsonl").is_file()
    assert not (copilot_home / "session-state-archive" / _SID / _SID).exists()
    assert metadata.get(f"copilot:{_SID}").archived is True


def test_unarchive_refuses_to_nest_over_a_live_directory(copilot_home):
    # The reverse direction: restoring must not nest the archived history under
    # session-state/<id>/<id>/ beside a live directory a running process recreated.
    _write_session(copilot_home, _SID, "/home/u/proj", archived=True, events=[_user("old")])
    _write_session(copilot_home, _SID, "/home/u/proj", events=[_user("new")])
    metadata.patch(f"copilot:{_SID}", archived=True)
    with pytest.raises(base.EngineError, match="both the live and archive trees"):
        _kind().unarchive(_SID)
    assert (copilot_home / "session-state" / _SID / "events.jsonl").is_file()
    assert (copilot_home / "session-state-archive" / _SID / "events.jsonl").is_file()
    assert not (copilot_home / "session-state" / _SID / _SID).exists()
    assert metadata.get(f"copilot:{_SID}").archived is True


def test_repeated_archive_of_an_already_moved_session_still_sets_the_flag(copilot_home):
    # src gone, dst present: no conflict — the move is a no-op and the sidecar is patched.
    _write_session(copilot_home, _SID, "/home/u/proj", archived=True, events=[_user("old")])
    _kind().archive(_SID)
    assert metadata.get(f"copilot:{_SID}").archived is True
    assert not (copilot_home / "session-state-archive" / _SID / _SID).exists()


def _rename_that_fills_the_destination(monkeypatch):
    """An os.rename stub for the interleaving a precheck cannot see: AFTER the destination was
    claimed empty, a still-running Copilot fills it. The real rename then fails ENOTEMPTY."""
    real_rename = os.rename

    def racing_rename(src, dst):
        Path(dst, "foreign.txt").write_text("a recreated directory\n", encoding="utf-8")
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", racing_rename)


def test_archive_refuses_a_destination_filled_mid_move(copilot_home, monkeypatch):
    _write_session(copilot_home, _SID, "/home/u/proj", events=[_user("work")])
    _rename_that_fills_the_destination(monkeypatch)
    with pytest.raises(base.EngineError, match="could not be moved"):
        _kind().archive(_SID)
    # Nothing moved, nothing nested, the foreign content is left for a human, and the sidecar
    # keeps its (unset) value.
    assert (copilot_home / "session-state" / _SID / "events.jsonl").is_file()
    claim = copilot_home / "session-state-archive" / _SID
    assert [p.name for p in claim.iterdir()] == ["foreign.txt"]
    assert metadata.get(f"copilot:{_SID}").archived is not True


def test_unarchive_refuses_a_destination_filled_mid_move(copilot_home, monkeypatch):
    _write_session(copilot_home, _SID, "/home/u/proj", archived=True, events=[_user("old")])
    metadata.patch(f"copilot:{_SID}", archived=True)
    _rename_that_fills_the_destination(monkeypatch)
    with pytest.raises(base.EngineError, match="could not be moved"):
        _kind().unarchive(_SID)
    assert (copilot_home / "session-state-archive" / _SID / "events.jsonl").is_file()
    claim = copilot_home / "session-state" / _SID
    assert [p.name for p in claim.iterdir()] == ["foreign.txt"]
    assert metadata.get(f"copilot:{_SID}").archived is True


def test_a_failed_move_raises_and_the_sidecar_keeps_its_value(copilot_home, monkeypatch):
    # EACCES/ENOSPC/… are NOT the idempotent "already moved": the transition fails loud and
    # the record is not flipped to a state the disk contradicts.
    _write_session(copilot_home, _SID, "/home/u/proj", events=[_user("work")])

    def deny(src, dst):
        raise PermissionError("read-only filesystem")

    monkeypatch.setattr(os, "rename", deny)
    with pytest.raises(base.EngineError, match="could not be moved"):
        _kind().archive(_SID)
    assert (copilot_home / "session-state" / _SID / "events.jsonl").is_file()
    assert not (copilot_home / "session-state-archive" / _SID).exists()  # the claim was undone
    assert metadata.get(f"copilot:{_SID}").archived is not True


# --- launch argv: --no-auto-update on every console spawn --------------------------------------


def test_new_launch_argv_pins_id_and_suppresses_self_update(copilot_home, engine_bin):
    b = engine_bin("copilot")
    prov = engines.get("copilot")
    assert prov.new_launch_argv(_SID, cwd="/home/u/proj", bypass=False) == [
        b,
        "--no-auto-update",
        "--session-id",
        _SID,
    ]


def test_resume_argv_carries_base_args_bypass_and_model(copilot_home, engine_bin):
    from agent_sessions import model_choice

    b = engine_bin("copilot")
    prov = engines.get("copilot")
    assert prov.launch_argv(_SID, cwd="/home/u/proj", bypass=True) == [
        b,
        "--no-auto-update",
        "--resume",
        _SID,
        "--yolo",
    ]
    # `--model` is honoured on resume too (the manifest says on_resume).
    model = model_choice.Selection("copilot", "gpt-5.5", "--model")
    assert prov.launch_argv(_SID, cwd="/home/u/proj", bypass=False, model=model) == [
        b,
        "--no-auto-update",
        "--resume",
        _SID,
        "--model",
        "gpt-5.5",
    ]


def test_launch_argv_is_a_literal_list_with_no_shell(copilot_home, engine_bin):
    engine_bin("copilot")
    argv = engines.get("copilot").new_launch_argv(_SID, cwd="/home/u/proj", bypass=True)
    assert all(isinstance(a, str) for a in argv)
    assert "--no-auto-update" in argv and "--yolo" in argv


# --- probe argv (copilot-prompt-pinned) --------------------------------------------------------


def test_probe_argv_disables_tools_and_self_update(copilot_home, engine_bin):
    b = engine_bin("copilot")
    prov = engines.get("copilot")
    new = plugin_process._probe_argv(prov, "new", _SID, "0" * 8 + "-0000-0000-0000-000000000000")
    assert new[:6] == [
        b,
        "--no-auto-update",
        "--no-custom-instructions",
        "--no-ask-user",
        "--disable-builtin-mcps",
        "--available-tools=none",
    ]
    assert "--session-id" in new and _SID in new and "--prompt" in new
    assert "--yolo" not in new and "--allow-all-tools" not in new
    resume = plugin_process._probe_argv(
        prov, "resume", _SID, "0" * 8 + "-0000-0000-0000-000000000000"
    )
    assert "--resume" in resume and "--session-id" not in resume
    assert "--no-auto-update" in resume


# --- start evidence (copilot-sessions) ----------------------------------------------------------


def test_start_evidence_found_absent_unreadable(copilot_home):
    native, cwd = _SID, "/home/u/proj"
    assert start_evidence.copilot_start_state(native, cwd)[0] == start_evidence.ABSENT
    _write_session(copilot_home, native, cwd)
    state, detail = start_evidence.copilot_start_state(native, cwd)
    assert state == start_evidence.FOUND and native in detail


def test_start_evidence_missing_store_is_unreadable(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.copilot at all
    state, detail = start_evidence.copilot_start_state(_SID, "/home/u/proj")
    assert state == start_evidence.UNREADABLE and "nothing to read" in detail


def test_start_evidence_binds_the_cwd(copilot_home):
    _write_session(copilot_home, _SID, "/home/u/proj")
    state, _ = start_evidence.copilot_start_state(_SID, "/home/u/other")
    assert state == start_evidence.ABSENT


def test_start_evidence_unreadable_workspace_is_unreadable_not_absent(copilot_home, monkeypatch):
    # An existing workspace.yaml that cannot be READ is "we could not look", never "the agent
    # did not start" — timeout diagnostics must not blame a start that may have happened.
    _write_session(copilot_home, _SID, "/home/u/proj")
    real_open = Path.open

    def deny_workspace(self, *args, **kwargs):
        if self.name == "workspace.yaml":
            raise PermissionError("denied")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_workspace)
    state, _ = start_evidence.copilot_start_state(_SID, "/home/u/proj")
    assert state == start_evidence.UNREADABLE


def test_start_evidence_without_a_cwd_binding_is_not_evidence(copilot_home):
    # A matching id with no cwd recorded yet (a torn write) is "not yet", never FOUND.
    sdir = _write_session(copilot_home, _SID, "/home/u/proj")
    (sdir / "workspace.yaml").write_text(f"id: {_SID}\ncreated_at: {_CREATED}\n", encoding="utf-8")
    state, detail = start_evidence.copilot_start_state(_SID, "/home/u/proj")
    assert state == start_evidence.ABSENT and "no working directory" in detail


# --- transcript (copilot-events) ------------------------------------------------------------------


def test_transcript_maps_human_context_and_assistant(copilot_home):
    from agent_sessions import transcript

    _write_session(
        copilot_home,
        _SID,
        "/home/u/proj",
        events=[
            {"type": "session.start", "data": {"sessionId": _SID, "selectedModel": "gpt-5.5"}},
            _user("Real question"),
            _user("/command run", data={"source": "command-run"}),
            _user("autopilot tick", data={"isAutopilotContinuation": True}),
            _user("subagent noise", agentId="agent-1"),
            _assistant("Real answer"),
            {"type": "tool.execution_start", "data": {"toolName": "glob"}, "timestamp": _TS2},
        ],
    )
    turns = transcript.adapter_for("copilot")(_SID, None)
    by_role = [(t.role, t.kind, t.text) for t in turns]
    assert ("user", "text", "Real question") in by_role
    assert ("assistant", "text", "Real answer") in by_role
    # Synthetic user events render as context, never as something the user said.
    contexts = [t.text for t in turns if t.kind == "context"]
    assert "/command run" in contexts and "autopilot tick" in contexts
    assert "subagent noise" not in [t.text for t in turns]
    assert "subagent noise" not in contexts


def test_transcript_reads_the_archive_tree_too(copilot_home):
    from agent_sessions import transcript

    _write_session(copilot_home, _SID, "/home/u/old", archived=True, events=[_user("old q")])
    turns = transcript.adapter_for("copilot")(_SID, None)
    assert [t.text for t in turns] == ["old q"]


def test_transcript_caps_renderable_turns_not_raw_records(copilot_home):
    """A tool-heavy exchange: prompt + 100 tool records + answer is 102 raw records, so a cap
    applied BEFORE filtering would drop the prompt and keep only the reply. The cap lands on
    renderable turns instead — both survive."""
    from agent_sessions import transcript

    tools = [
        {"type": "tool.execution_start", "data": {"toolName": "bash"}, "timestamp": _TS1}
        for _ in range(100)
    ]
    _write_session(
        copilot_home,
        _SID,
        "/home/u/proj",
        events=[_user("the prompt"), *tools, _assistant("the answer")],
    )
    turns = transcript.adapter_for("copilot")(_SID, None)
    texts = [t.text for t in turns]
    assert "the prompt" in texts and "the answer" in texts


def test_model_reader_prefers_assistant_then_change_then_start(copilot_home):
    from agent_sessions import transcript

    _write_session(
        copilot_home,
        _SID,
        "/home/u/proj",
        events=[
            {"type": "session.start", "data": {"selectedModel": "gpt-5"}},
            _assistant("hi", model="gpt-5.5"),
        ],
    )
    assert transcript.effective_model("copilot", _SID, None) == "gpt-5.5"
    _write_session(
        copilot_home,
        _SID2,
        "/home/u/proj",
        events=[
            {"type": "session.start", "data": {"selectedModel": "gpt-5"}},
            {
                "type": "session.model_change",
                "data": {"previousModel": "gpt-5", "newModel": "gpt-5.5"},
            },
        ],
    )
    assert transcript.effective_model("copilot", _SID2, None) == "gpt-5.5"
    _write_session(
        copilot_home,
        _SID3,
        "/home/u/proj",
        events=[{"type": "session.start", "data": {"selectedModel": "gpt-5"}}],
    )
    assert transcript.effective_model("copilot", _SID3, None) == "gpt-5"


# --- the memory MCP tool (copilot-additional-mcp-config) -----------------------------------------


def test_copilot_mcp_config_is_a_stdio_mcp_servers_table():
    payload = json.loads(
        agent_tools.copilot_mcp_config(
            agent_tools.Server(
                command="/usr/bin/python3",
                args=("-I", "-m", "agent_sessions.memory_mcp"),
                env={"BATTLELAB_MEMORY": "/x/y"},
            )
        )
    )
    entry = payload["mcpServers"]["battlelab_memory"]
    assert entry["type"] == "stdio"
    assert entry["command"] == "/usr/bin/python3"
    assert entry["args"] == ["-I", "-m", "agent_sessions.memory_mcp"]
    assert entry["env"] == {"BATTLELAB_MEMORY": "/x/y", **{k: entry["env"][k] for k in ()}}
    assert entry["tools"] == ["memory_index", "memory_read", "memory_search", "memory_write"]


def test_terminal_launch_appends_the_flag(monkeypatch):
    manifest = engines.manifest_of("copilot")
    env = {
        "BATTLELAB_MEMORY": "/tmp/m",
        "BATTLELAB_MEMORY_NOTEBOOK": "/tmp/n",
    }
    from agent_sessions import memory

    monkeypatch.setattr(memory, "ENV_KEYS", tuple(env))
    monkeypatch.setattr(memory, "ALL_ENV_KEYS", tuple(env))
    launch = agent_tools.terminal_launch(manifest, env, {})
    argv = launch.argv(["/bin/copilot", "--no-auto-update", "--session-id", _SID])
    assert argv[-2] == "--additional-mcp-config"
    assert json.loads(argv[-1])["mcpServers"]["battlelab_memory"]["type"] == "stdio"
    assert argv[:4] == ["/bin/copilot", "--no-auto-update", "--session-id", _SID]


# --- account access (copilot-acp-auth) ------------------------------------------------------------


def _acp_text(auth):
    init = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}}
    return json.dumps(init) + "\n" + json.dumps(auth) + "\n"


def test_access_parse_ok_denied_unknown():
    ok = agent_usage.parse_copilot_acp_auth(_acp_text({"jsonrpc": "2.0", "id": 2, "result": {}}))
    assert ok.state == agent_usage.ACCESS_OK and ok.observed_at
    denied = agent_usage.parse_copilot_acp_auth(
        _acp_text(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "error": {"code": -32000, "message": "authentication required"},
            }
        )
    )
    assert denied.state == agent_usage.ACCESS_DENIED
    assert denied.message == "authentication required"
    denied_text = agent_usage.parse_copilot_acp_auth(
        _acp_text({"jsonrpc": "2.0", "id": 2, "error": {"code": -1, "message": "Not logged in"}})
    )
    assert denied_text.state == agent_usage.ACCESS_DENIED
    # A transport/internal error is UNKNOWN, never denied; protocol drift is unknown too.
    other = agent_usage.parse_copilot_acp_auth(
        _acp_text({"jsonrpc": "2.0", "id": 2, "error": {"code": -32603, "message": "internal"}})
    )
    assert other.state is None
    assert agent_usage.parse_copilot_acp_auth("garbage\n").state is None
    wrong_id = _acp_text({"jsonrpc": "2.0", "id": 9, "result": {}})
    assert agent_usage.parse_copilot_acp_auth(wrong_id).state is None


def test_access_check_argv_suppresses_self_update(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return 0, (_acp_text({"jsonrpc": "2.0", "id": 2, "result": {}}).encode())

    monkeypatch.setattr(agent_usage, "_run", fake_run)
    got = agent_usage.check_copilot_access(binary="/bin/copilot")
    assert got.state == agent_usage.ACCESS_OK
    assert seen["argv"] == ["/bin/copilot", "--acp", "--no-auto-update"]


def test_access_check_timeout_is_unknown(monkeypatch):
    monkeypatch.setattr(agent_usage, "_run", lambda argv, **kw: (124, b""))
    got = agent_usage.check_copilot_access(binary="/bin/copilot")
    assert got.state is None and got.error


# --- store locations come from the manifest ------------------------------------------------------


def test_store_helpers_resolve_the_manifest_paths(copilot_home, tmp_path):
    assert base._copilot_dir(tmp_path) == tmp_path / ".copilot"
    assert base._copilot_sessions_dir(tmp_path) == tmp_path / ".copilot" / "session-state"
    assert base._copilot_archive_dir(tmp_path) == tmp_path / ".copilot" / "session-state-archive"


def test_provenance_admits_the_npm_loader_alias():
    """An adopted `npm i -g @github/copilot` install symlinks bin/copilot → npm-loader.js (#1565);
    the manifest's alias admits that entrypoint shape."""
    m = engines.manifest_of("copilot")
    assert m.binary.name == "copilot"
    assert "npm-loader.js" in m.binary.aliases
    assert m.binary.search_npm_global is True
