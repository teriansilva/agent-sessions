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
        engines.CLAUDE_BIN,
        "--resume",
        uuid,
    ]
    assert "--dangerously-skip-permissions" in prov.launch_argv(uuid, cwd="/x", bypass=True)


# --- new-session (launch-then-reconcile, #315) ---------------------------------------------

_U1 = "019e2ba1-1590-7003-8e4a-51ab62cec001"
_U2 = "019e2ba1-1590-7003-8e4a-51ab62cec002"


def test_codex_supports_new_and_reconciles():
    prov = engines.CodexProvider()
    assert prov.supports_new is True
    assert getattr(prov, "new_session_reconciles", False) is True


def test_codex_new_launch_argv_cd_and_bypass():
    prov = engines.CodexProvider()
    assert prov.new_launch_argv("new-x", cwd="/work", bypass=False) == [
        engines.CODEX_BIN,
        "--cd",
        "/work",
    ]
    argv = prov.new_launch_argv("new-x", cwd="/work", bypass=True)
    assert argv[:3] == [engines.CODEX_BIN, "--cd", "/work"]
    assert "--dangerously-bypass-approvals-and-sandbox" in argv


def test_codex_snapshot_empty_when_dir_missing(codex_root):
    # env points at a not-yet-created dir → valid EMPTY baseline (set()), NOT a None failure.
    assert engines.CodexProvider().snapshot_session_ids("/work") == set()


def test_codex_reconcile_single_new(codex_root):
    prov = engines.CodexProvider()
    snap = prov.snapshot_session_ids("/work")  # empty
    _write_rollout(codex_root, uuid=_U1, cwd="/work", first_user="hi")
    assert prov.reconcile_new_session("/work", snap) == _U1


def test_codex_reconcile_none_when_nothing_new(codex_root):
    _write_rollout(codex_root, uuid=_U1, cwd="/work", first_user="hi")
    prov = engines.CodexProvider()
    snap = prov.snapshot_session_ids("/work")
    assert snap == {_U1}
    assert prov.reconcile_new_session("/work", snap) is None  # no NEW id


def test_codex_reconcile_ambiguous_same_cwd(codex_root):
    prov = engines.CodexProvider()
    snap = prov.snapshot_session_ids("/work")
    _write_rollout(codex_root, uuid=_U1, cwd="/work", first_user="a", ts="2026-05-15T15-00-00")
    _write_rollout(codex_root, uuid=_U2, cwd="/work", first_user="b", ts="2026-05-15T16-00-00")
    result = prov.reconcile_new_session("/work", snap)
    assert isinstance(result, list) and set(result) == {_U1, _U2}  # caller fails safe


def test_codex_reconcile_ignores_other_cwd(codex_root):
    prov = engines.CodexProvider()
    snap = prov.snapshot_session_ids("/work")
    _write_rollout(codex_root, uuid=_U1, cwd="/elsewhere", first_user="x")
    assert prov.reconcile_new_session("/work", snap) is None  # different cwd → not ours


def test_codex_reconcile_pending_on_malformed_head(codex_root):
    # a new rollout whose cwd head isn't written/parseable yet → excluded → stays pending
    # (None), never misattributed (Hermes #315).
    prov = engines.CodexProvider()
    snap = prov.snapshot_session_ids("/work")
    d = codex_root / "2026" / "05" / "15"
    d.mkdir(parents=True)
    (d / f"rollout-2026-05-15T15-00-00-{_U1}.jsonl").write_text("{not json yet\n")  # no cwd
    assert prov.reconcile_new_session("/work", snap) is None


def test_codex_snapshot_none_on_walk_failure(codex_root, monkeypatch):
    import agent_sessions.engines.codex as cdx

    class _BadDir:
        def exists(self):
            return True

        def rglob(self, _pat):
            raise OSError("walk failed")

    monkeypatch.setattr(cdx.base, "_codex_sessions_dir", lambda *a, **k: _BadDir())
    # a read failure must be None (skip reconcile), never an empty set (would misattribute).
    assert engines.CodexProvider().snapshot_session_ids("/work") is None


def test_codex_new_placeholder_recognized_and_parses(codex_root):
    key = f"codex:new-{_U1}"
    assert engines.is_new_session_placeholder(key) is True
    assert engines.is_opencode_new_placeholder(key) is True  # back-compat alias is generic now
    # accepted ONLY on the new=1 launch path
    prov, native = engines.parse_key(key, allow_new_placeholder=True)
    assert prov.engine_id == "codex" and native.startswith("new-")
    with pytest.raises(engines.EngineError):
        engines.parse_key(key)  # NOT accepted on resume/attach


def test_codex_present_gates_new_session_advertisement(codex_root, monkeypatch):
    import agent_sessions.engines.codex as cdx

    monkeypatch.setattr(cdx.shutil, "which", lambda _b: None)  # no codex on PATH
    prov = engines.CodexProvider()
    assert prov.is_present() is False  # absent store + no bin → not advertised
    _write_rollout(codex_root, uuid=_U1, cwd="/work", first_user="hi")
    assert prov.is_present() is True and prov.supports_new is True  # present → advertised
