"""CodexProvider discovery + launch argv, and the decoupled launch_argv contract."""

from __future__ import annotations

import json

import pytest

from agent_sessions import engines


def _write_rollout(root, *, uuid, cwd, first_user, day="2026/05/15", ts="2026-05-15T15-33-57"):
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"rollout-{ts}-{uuid}.jsonl"
    lines = [
        {"timestamp": "t", "type": "session_meta", "payload": {"id": uuid, "cwd": cwd}},
        {"timestamp": "t", "type": "event_msg", "payload": {"type": "task_started"}},
        {
            "timestamp": "t",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": first_user}],
            },
        },
    ]
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return f


@pytest.fixture
def codex_root(tmp_path, monkeypatch):
    root = tmp_path / "codex-sessions"
    monkeypatch.setenv("AGENT_SESSIONS_CODEX_SESSIONS_DIR", str(root))
    return root


def test_codex_scan_discovers_sessions(codex_root):
    uuid = "019e2ba1-1590-7003-8e4a-51ab62cec96e"
    _write_rollout(codex_root, uuid=uuid, cwd="/home/u/proj", first_user="why is X broken?")
    prov = engines.CodexProvider()
    assert prov.is_present() is True
    sessions = prov.scan()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.engine == "codex"
    assert s.uuid == uuid
    assert s.cwd == "/home/u/proj"
    assert s.first_user_message == "why is X broken?"
    assert s.archived is False


def test_codex_scan_failsoft_on_garbage(codex_root):
    # a non-rollout file + a corrupt rollout must not break the scan AND must not
    # emit a bogus empty-cwd session (Hermes PR #50 review): no usable cwd -> no row.
    d = codex_root / "2026" / "05" / "15"
    d.mkdir(parents=True)
    (d / "notes.txt").write_text("ignore me")
    bad = d / "rollout-x-019e2ba1-1590-7003-8e4a-51ab62cec96e.jsonl"
    bad.write_text("{not json\n")  # parse errors only -> no cwd -> skipped, no crash
    # a valid sibling still scans fine
    _write_rollout(
        codex_root,
        uuid="019e2ba1-1590-7003-8e4a-51ab62cec999",
        cwd="/home/u/ok",
        first_user="hi",
        ts="2026-05-15T16-00-00",
    )
    sessions = engines.CodexProvider().scan()
    assert [s.uuid for s in sessions] == ["019e2ba1-1590-7003-8e4a-51ab62cec999"]
    assert all(s.cwd for s in sessions)  # never an empty-cwd row


def test_codex_launch_argv():
    prov = engines.CodexProvider()
    argv = prov.launch_argv("019e2ba1-1590-7003-8e4a-51ab62cec96e", cwd="/x", bypass=True)
    assert argv == [engines.CODEX_BIN, "resume", "019e2ba1-1590-7003-8e4a-51ab62cec96e"]


def test_parse_key_routes_codex():
    uuid = "019e2ba1-1590-7003-8e4a-51ab62cec96e"
    prov, native = engines.parse_key(f"codex:{uuid}")
    assert prov.engine_id == "codex"
    assert native == uuid
    with pytest.raises(engines.EngineError):
        engines.parse_key("codex:not-a-uuid")


def test_launch_argv_contract_all_present_providers():
    # every provider exposes launch_argv returning a non-empty argv list
    for prov in engines.all_providers():
        argv = prov.launch_argv(
            "ses_abcd1234"
            if prov.engine_id == "opencode"
            else "019e2ba1-1590-7003-8e4a-51ab62cec96e",
            cwd="/tmp/x",
            bypass=False,
        )
        assert isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)


def test_claude_launch_argv_bypass_flag():
    prov = engines.get("claude")
    uuid = "019e2ba1-1590-7003-8e4a-51ab62cec96e"
    assert prov.launch_argv(uuid, cwd="/x", bypass=False) == [
        engines.zellij.CLAUDE_BIN,
        "--resume",
        uuid,
    ]
    assert "--dangerously-skip-permissions" in prov.launch_argv(uuid, cwd="/x", bypass=True)
