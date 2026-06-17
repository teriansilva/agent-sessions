"""AntigravityProvider (agy) discovery + launch argv + engine-qualified id routing (#422).

Mirrors ``tests/test_gemini.py``. The synthetic store reproduces agy 1.0.8's real layout under
``~/.gemini/antigravity-cli/``: ``conversations/<uuid>.db`` (a SQLite db whose
``trajectory_metadata_blob`` embeds the workspace as a length-delimited ``file://`` URI) plus
``brain/<uuid>/.system_generated/logs/transcript.jsonl``.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from agent_sessions import engines
from agent_sessions import transcript as T

_UUID = "7f0ee6e0-1467-4f7d-843b-4e70e15e73f5"
_UUID2 = "11112222-3333-4444-5555-666677778888"


def _varint(n: int) -> bytes:
    """protobuf little-endian base-128 varint (how agy length-prefixes the file:// workspace)."""
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _make_db(path, cwd):
    """Minimal agy conversation db: the ``trajectory_metadata_blob`` 'main' row whose blob embeds
    the workspace as a varint-length-delimited ``file://`` URI, exactly as agy writes it. A trailing
    byte that *looks* like a path char (``z``) follows the URI to prove the length prefix — not
    greedy path-char consumption — is what delimits the cwd."""
    uri = f"file://{cwd}".encode()
    blob = b"\n\x26\n" + _varint(len(uri)) + uri + b"z\xe8\x07"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE `trajectory_metadata_blob` "
        "(`id` text DEFAULT 'main', `data` blob, PRIMARY KEY(`id`))"
    )
    con.execute("INSERT INTO trajectory_metadata_blob (id, data) VALUES ('main', ?)", (blob,))
    con.commit()
    con.close()


def _make_transcript(root, uuid, *, user="hello agy", assistant="hi there"):
    d = root / "brain" / uuid / ".system_generated" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    recs = [
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-06-16T12:07:30Z",
            # agy wraps the human text and appends metadata blocks the model sees but we drop.
            "content": (
                f"<USER_REQUEST>\n{user}\n</USER_REQUEST>\n"
                "<ADDITIONAL_METADATA>\nThe current local time is: x.\n</ADDITIONAL_METADATA>"
            ),
        },
        {"step_index": 1, "source": "SYSTEM", "type": "CONVERSATION_HISTORY", "status": "DONE"},
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "content": assistant,
        },
    ]
    (d / "transcript.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")


def _make_conversation(root, uuid, cwd, **kw):
    _make_db(root / "conversations" / f"{uuid}.db", cwd)
    _make_transcript(root, uuid, **kw)


@pytest.fixture
def agy(tmp_path, monkeypatch):
    root = tmp_path / "antigravity-cli"
    (root / "conversations").mkdir(parents=True)
    (root / "cache").mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_ANTIGRAVITY_DIR", str(root))
    return root


def test_scan_discovers_sessions(agy):
    _make_conversation(agy, _UUID, "/home/u/proj", user="investigate the issues", assistant="ok")
    prov = engines.AntigravityProvider()
    assert prov.is_present() is True
    sessions = prov.scan()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.engine == "antigravity"
    assert s.uuid == _UUID
    assert s.cwd == "/home/u/proj"  # length-prefixed file:// scrape; trailing junk ignored
    assert s.first_user_message == "investigate the issues"
    assert s.archived is False


def test_scan_cwd_cache_fast_path(agy):
    # cache/last_conversations.json (cwd->uuid) is the robust JSON fast path; it resolves the cwd
    # even when the db blob can't be read (here: an empty, unreadable db file).
    (agy / "conversations" / f"{_UUID}.db").touch()
    _make_transcript(agy, _UUID)
    (agy / "cache" / "last_conversations.json").write_text(json.dumps({"/home/u/cached": _UUID}))
    assert [s.cwd for s in engines.AntigravityProvider().scan()] == ["/home/u/cached"]


def test_scan_cwd_from_db_blob(agy):
    # no cache entry -> cwd comes from the db's trajectory_metadata_blob file:// URI.
    _make_conversation(agy, _UUID, "/home/u/from-db")
    assert [s.cwd for s in engines.AntigravityProvider().scan()] == ["/home/u/from-db"]


def test_scan_cwd_from_db_blob_long_path(agy):
    # a deep cwd whose file:// URI exceeds 127 bytes uses a multi-byte protobuf varint length —
    # the backward varint decode must still frame it exactly (not just single-byte lengths).
    deep = "/home/u/" + "/".join(f"seg{i:02d}" for i in range(20))  # well over 127 chars
    _make_conversation(agy, _UUID, deep)
    assert [s.cwd for s in engines.AntigravityProvider().scan()] == [deep]


def test_scan_skips_unresolvable_cwd(agy):
    # neither a cache entry nor a file:// in the db -> the row is skipped (fail-soft), never a
    # bogus empty-cwd entry (mirrors gemini's unmapped-project skip).
    db = agy / "conversations" / f"{_UUID}.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE `trajectory_metadata_blob` (`id` text, `data` blob)")
    con.execute("INSERT INTO trajectory_metadata_blob VALUES ('main', ?)", (b"no uri here",))
    con.commit()
    con.close()
    _make_transcript(agy, _UUID)
    assert engines.AntigravityProvider().scan() == []


def test_scan_failsoft_skips_garbage_and_non_uuid(agy):
    _make_conversation(agy, _UUID, "/home/u/good")
    (agy / "conversations" / "not-a-uuid.db").write_text("x")  # non-uuid name -> skipped
    (agy / "conversations" / f"{_UUID2}.db").write_text(
        "not sqlite"
    )  # corrupt db -> skipped, no crash
    sessions = engines.AntigravityProvider().scan()
    assert [s.uuid for s in sessions] == [_UUID]
    assert all(s.cwd for s in sessions)


def test_launch_argv_resume_and_bypass():
    prov = engines.AntigravityProvider()
    assert prov.launch_argv(_UUID, cwd="/x", bypass=False) == [
        engines.AGY_BIN,
        "--conversation",
        _UUID,
    ]
    assert prov.launch_argv(_UUID, cwd="/x", bypass=True) == [
        engines.AGY_BIN,
        "--conversation",
        _UUID,
        "--dangerously-skip-permissions",
    ]


def _write_cache(root, cwd_to_uuid):
    """agy's cache/last_conversations.json is a cwd → latest-uuid map (the scan fast-path)."""
    (root / "cache" / "last_conversations.json").write_text(json.dumps(cwd_to_uuid))


def test_new_session_launch_then_reconcile_supported():
    # agy now supports new sessions via launch-then-reconcile (#449), like codex.
    prov = engines.AntigravityProvider()
    assert prov.supports_new is True
    assert prov.new_session_reconciles is True
    # Fresh launch: no `--conversation` (agy mints the id); bypass → --dangerously-skip-permissions.
    assert prov.new_launch_argv("new-x", cwd="/x", bypass=False) == [engines.AGY_BIN]
    assert prov.new_launch_argv("new-x", cwd="/x", bypass=True) == [
        engines.AGY_BIN,
        "--dangerously-skip-permissions",
    ]


def test_present_and_supports_new_gates_new_session_picker(agy):
    # The new-session dropdown lists providers that are present + supports_new (#449).
    prov = engines.AntigravityProvider()
    assert prov.is_present() is True  # the agy fixture dir exists
    assert prov.supports_new is True


def test_reconcile_finds_new_conversation_via_db_blob(agy):
    prov = engines.AntigravityProvider()
    _make_conversation(agy, _UUID, "/home/u/proj")  # cwd resolved from the SQLite blob
    before = prov.snapshot_session_ids("/home/u/proj")
    assert before == {_UUID}
    _make_conversation(agy, _UUID2, "/home/u/proj")  # agy mints a new one in the same cwd
    assert prov.reconcile_new_session("/home/u/proj", before) == _UUID2


def test_reconcile_resolves_cwd_via_cache_fast_path(agy):
    prov = engines.AntigravityProvider()
    (agy / "conversations" / f"{_UUID2}.db").touch()  # db present; cwd comes from the cache map
    _write_cache(agy, {"/home/u/work": _UUID2})
    assert prov.reconcile_new_session("/home/u/work", set()) == _UUID2


def test_reconcile_none_when_nothing_new(agy):
    prov = engines.AntigravityProvider()
    _make_conversation(agy, _UUID, "/home/u/proj")
    snap = prov.snapshot_session_ids("/home/u/proj")
    assert prov.reconcile_new_session("/home/u/proj", snap) is None  # nothing minted yet → poll


def test_reconcile_ambiguous_two_new_same_cwd_fails_safe(agy):
    prov = engines.AntigravityProvider()
    _make_conversation(agy, _UUID, "/home/u/proj")
    _make_conversation(agy, _UUID2, "/home/u/proj")
    # both new since an empty snapshot, same cwd → ambiguous: return the list, never guess.
    assert prov.reconcile_new_session("/home/u/proj", set()) == sorted([_UUID, _UUID2])


def test_reconcile_ignores_other_cwd(agy):
    prov = engines.AntigravityProvider()
    _make_conversation(agy, _UUID2, "/elsewhere")  # a new conversation in a DIFFERENT cwd
    assert prov.reconcile_new_session("/home/u/proj", set()) is None


def test_snapshot_none_on_walk_failure(agy, monkeypatch):
    import agent_sessions.engines.antigravity as A

    def boom(self, pattern):
        raise OSError("walk failed")

    monkeypatch.setattr(A.Path, "glob", boom)
    # A transient listing failure → None so the caller skips reconciliation (never misattributes).
    assert engines.AntigravityProvider().snapshot_session_ids("/home/u/proj") is None


def test_parse_key_routes_antigravity():
    prov, native = engines.parse_key(f"antigravity:{_UUID}")
    assert prov.engine_id == "antigravity"
    assert native == _UUID
    with pytest.raises(engines.EngineError):
        engines.parse_key("antigravity:not-a-uuid")


def test_in_registry_and_coexists_with_gemini():
    ids = [p.engine_id for p in engines.all_providers()]
    assert "antigravity" in ids and "gemini" in ids  # both engines, side by side


def test_archive_unarchive_ride_engine_qualified_sidecar(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(engines._metadata, "patch", lambda key, **kw: calls.append((key, kw)))
    prov = engines.AntigravityProvider()
    prov.archive(_UUID)
    prov.unarchive(_UUID)
    assert calls == [
        (f"antigravity:{_UUID}", {"archived": True}),
        (f"antigravity:{_UUID}", {"archived": False}),
    ]


def test_transcript_adapter_renders_user_and_assistant(agy, tmp_path):
    # the adapter must read the SAME env-overridden store the provider scans (Hermes #313): the
    # AGENT_SESSIONS_ANTIGRAVITY_DIR override wins over the passed `home`.
    _make_conversation(agy, _UUID, "/home/u/proj", user="ping", assistant="pong")
    assert T.adapter_for("antigravity") is not None
    turns = T.adapter_for("antigravity")(_UUID, tmp_path / "wrong-home")
    assert [(t.role, t.text) for t in turns] == [("user", "ping"), ("assistant", "pong")]
    # unknown id -> no turns (caller keeps the raw-byte fallback)
    assert T.adapter_for("antigravity")(_UUID2, tmp_path / "wrong-home") == []
