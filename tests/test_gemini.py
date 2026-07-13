"""GeminiProvider discovery + launch argv, and the engine-qualified id routing."""

from __future__ import annotations

import json

import pytest

from agent_sessions import engines, metadata

_HASH = "8ad9da2d06678582e3bba0f92d01dd679572a5a60e707f588fc87045de2509ee"


def _write_chat(root, *, sid, project_hash, first_user, slug="proj", ts="2026-05-15T06-24"):
    chats = root / slug / "chats"
    chats.mkdir(parents=True, exist_ok=True)
    f = chats / f"session-{ts}-{sid[:8]}.jsonl"
    lines = [
        # header record: carries the full sessionId + projectHash
        {
            "sessionId": sid,
            "projectHash": project_hash,
            "startTime": "2026-05-15T06:24:20.123Z",
            "lastUpdated": "2026-05-15T06:24:20.123Z",
            "kind": "main",
        },
        # first user turn
        {"id": "x", "timestamp": "t", "type": "user", "content": [{"text": first_user}]},
        {"$set": {"lastUpdated": "2026-05-15T06:24:22.115Z"}},  # a control record (ignored)
    ]
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return f


@pytest.fixture
def gemini_tmp(tmp_path, monkeypatch):
    root = tmp_path / "gemini-tmp"
    root.mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_GEMINI_TMP_DIR", str(root))
    # project-map.json maps the projectHash to the real cwd (gemini's own mapping).
    (root / "project-map.json").write_text(json.dumps({_HASH: "/home/u/proj"}))
    return root


def test_gemini_scan_discovers_sessions(gemini_tmp):
    sid = "96fb77fc-9c1a-4453-b27b-d78d8012dd2c"
    _write_chat(gemini_tmp, sid=sid, project_hash=_HASH, first_user="investigate the issues")
    prov = engines.GeminiProvider()
    assert prov.is_present() is True
    sessions = prov.scan()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.engine == "gemini"
    assert s.uuid == sid
    assert s.cwd == "/home/u/proj"  # resolved via project-map.json[projectHash]
    assert s.first_user_message == "investigate the issues"
    assert s.archived is False


def test_gemini_scan_skips_unmapped_project(gemini_tmp):
    # a session whose projectHash isn't in project-map.json has no usable cwd -> skipped
    # (never a bogus empty-cwd row), mirroring the codex fail-soft rule.
    _write_chat(
        gemini_tmp,
        sid="96fb77fc-9c1a-4453-b27b-d78d8012dd2c",
        project_hash="deadbeef" * 8,
        first_user="orphan",
        slug="orphan",
    )
    assert engines.GeminiProvider().scan() == []


def test_gemini_scan_failsoft_on_garbage(gemini_tmp):
    chats = gemini_tmp / "proj" / "chats"
    chats.mkdir(parents=True)
    (chats / "session-2026-05-15T06-24-deadbeef.jsonl").write_text("{not json\n")  # no header
    (chats / "notes.txt").write_text("ignore")  # not a session-*.jsonl
    good = "96fb77fc-9c1a-4453-b27b-d78d8012dd2c"
    _write_chat(gemini_tmp, sid=good, project_hash=_HASH, first_user="ok", ts="2026-05-15T07-00")
    sessions = engines.GeminiProvider().scan()
    assert [s.uuid for s in sessions] == [good]
    assert all(s.cwd for s in sessions)


def test_gemini_launch_argv_resume_and_bypass():
    prov = engines.GeminiProvider()
    sid = "96fb77fc-9c1a-4453-b27b-d78d8012dd2c"
    assert prov.launch_argv(sid, cwd="/x", bypass=False) == [engines.GEMINI_BIN, "--resume", sid]
    bypass = prov.launch_argv(sid, cwd="/x", bypass=True)
    assert bypass[:3] == [engines.GEMINI_BIN, "--resume", sid]
    assert "--yolo" in bypass and "--skip-trust" in bypass


def test_gemini_new_launch_argv_pins_session_id():
    prov = engines.GeminiProvider()
    sid = "96fb77fc-9c1a-4453-b27b-d78d8012dd2c"
    assert prov.new_launch_argv(sid, cwd="/x", bypass=False) == [
        engines.GEMINI_BIN,
        "--session-id",
        sid,
    ]


def test_parse_key_routes_gemini():
    sid = "96fb77fc-9c1a-4453-b27b-d78d8012dd2c"
    prov, native = engines.parse_key(f"gemini:{sid}")
    assert prov.engine_id == "gemini"
    assert native == sid
    with pytest.raises(engines.EngineError):
        engines.parse_key("gemini:not-a-uuid")


def test_gemini_in_registry():
    assert any(p.engine_id == "gemini" for p in engines.all_providers())


def test_gemini_first_user_raw_and_title_normalized_at_display(gemini_tmp):
    # Stored first_user_message stays RAW (search haystack, Hermes on PR #672); the sidebar
    # fallback title is one bounded line via metadata.display_title (#670).
    sid = "96fb77fc-9c1a-4453-b27b-d78d8012dd2c"
    long_first = "investigate " + "y" * 200
    raw = long_first + "\nsecond line"
    _write_chat(gemini_tmp, sid=sid, project_hash=_HASH, first_user=raw)
    s = engines.GeminiProvider().scan()[0]
    assert s.first_user_message == raw
    title = metadata.display_title(metadata.SessionMeta(), s.first_user_message)
    assert title == long_first[:120]
    assert "\n" not in title
