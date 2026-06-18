from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from agent_sessions import metadata


def test_empty_load_for_missing_file(tmp_home):
    assert metadata.load() == {}


def test_patch_creates_file_and_persists(tmp_home):
    m = metadata.patch("abc", title="API refactor", sticky=True, sort_key=10)
    assert m.title == "API refactor"
    assert m.sticky is True
    again = metadata.load()
    assert "abc" in again
    assert again["abc"].title == "API refactor"
    assert again["abc"].sticky is True
    assert again["abc"].sort_key == 10


def test_patch_merges_fields(tmp_home):
    metadata.patch("abc", title="first")
    metadata.patch("abc", sticky=True)
    state = metadata.load()
    assert state["abc"].title == "first"
    assert state["abc"].sticky is True


def test_patch_rejects_unknown_fields(tmp_home):
    with pytest.raises(ValueError):
        metadata.patch("abc", bogus="x")


def test_concurrent_writers_do_not_corrupt(tmp_home):
    """Two threads racing on the same uuid converge to a valid JSON file with both keys.

    This is the smoke test for fcntl.flock — without it, the read-modify-write
    races and one writer's data is lost OR the file ends up half-written.
    """
    errors: list[BaseException] = []

    def writer(uid: str, n: int):
        try:
            for _ in range(n):
                metadata.patch(uid, sort_key=42)
        except BaseException as e:  # pragma: no cover  (only if test fails)
            errors.append(e)

    t1 = threading.Thread(target=writer, args=("a", 20))
    t2 = threading.Thread(target=writer, args=("b", 20))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert errors == []
    state = metadata.load()
    assert "a" in state and "b" in state
    # Both must have the final sort_key value (last write wins for each key).
    assert state["a"].sort_key == 42
    assert state["b"].sort_key == 42
    # Underlying file must still be valid JSON.
    raw = Path(tmp_home / ".config" / "agent-sessions" / "metadata.json").read_text()
    json.loads(raw)


def test_load_tolerates_corrupt_file(tmp_home):
    p = Path(tmp_home / ".config" / "agent-sessions" / "metadata.json")
    p.parent.mkdir(parents=True)
    p.write_text("not json {")
    assert metadata.load() == {}


# ---- engine-qualified key migration (#11) -------------------------------------

_U = "11111111-1111-1111-1111-111111111111"


def _meta_path(tmp_home: Path) -> Path:
    return Path(tmp_home / ".config" / "agent-sessions" / "metadata.json")


def test_legacy_bare_uuid_keys_migrate_on_read(tmp_home):
    p = _meta_path(tmp_home)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({_U: {"title": "legacy"}}))
    loaded = metadata.load()
    assert f"claude:{_U}" in loaded
    assert loaded[f"claude:{_U}"].title == "legacy"
    assert _U not in loaded  # bare key normalized away


def test_first_patch_after_legacy_writes_bak_and_canonicalizes(tmp_home):
    p = _meta_path(tmp_home)
    p.parent.mkdir(parents=True)
    orig = json.dumps({_U: {"title": "legacy"}})
    p.write_text(orig)

    metadata.patch("claude:22222222-2222-2222-2222-222222222222", title="new")

    bak = p.with_name(p.name + ".bak")
    assert bak.exists() and json.loads(bak.read_text()) == json.loads(orig)
    data = json.loads(p.read_text())
    assert f"claude:{_U}" in data  # legacy key migrated in place
    assert "claude:22222222-2222-2222-2222-222222222222" in data
    assert _U not in data


def test_non_uuid_keys_are_left_alone(tmp_home):
    # Plain (non-UUID) keys must not be touched by the migration.
    metadata.patch("plain-key", title="x")
    assert "plain-key" in metadata.load()
    assert not _meta_path(tmp_home).with_name("metadata.json.bak").exists()


# ---- compose drafts (#477) ----------------------------------------------------


def test_draft_round_trips(tmp_home):
    d = {"text": "hello", "attachments": [{"name": "a.png", "path": "/x/a.png"}], "updated_at": 1.0}
    m = metadata.patch("claude:x", draft=d)
    assert m.draft == d
    again = metadata.load()["claude:x"]
    assert again.draft == d
    assert metadata.has_draft(again) is True


def test_draft_none_clears(tmp_home):
    metadata.patch("claude:x", draft={"text": "hi", "attachments": [], "updated_at": 1.0})
    metadata.patch("claude:x", draft=None)
    again = metadata.load()["claude:x"]
    assert again.draft is None
    assert metadata.has_draft(again) is False


def test_has_draft_false_for_whitespace_only(tmp_home):
    m = metadata.patch("claude:x", draft={"text": "   ", "attachments": [], "updated_at": 1.0})
    assert metadata.has_draft(m) is False


def test_has_draft_true_for_attachments_only(tmp_home):
    m = metadata.patch(
        "claude:x",
        draft={"text": "", "attachments": [{"name": "a", "path": "/p"}], "updated_at": 1.0},
    )
    assert metadata.has_draft(m) is True


def test_draft_survives_other_field_patch(tmp_home):
    d = {"text": "keep", "attachments": [], "updated_at": 1.0}
    metadata.patch("claude:x", draft=d)
    metadata.patch("claude:x", title="renamed")  # an unrelated write must not drop the draft
    again = metadata.load()["claude:x"]
    assert again.title == "renamed"
    assert again.draft == d


def test_load_ignores_non_dict_draft(tmp_home):
    p = _meta_path(tmp_home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"claude:x": {"draft": "oops"}}))
    assert metadata.load()["claude:x"].draft is None
    assert metadata.has_draft(metadata.load()["claude:x"]) is False


# ---- display_title meaningfulness rule (#284) --------------------------------
#
# The single shared helper is engine-agnostic: every provider (Claude scanner,
# opencode/codex/gemini/antigravity) feeds its first message through `display_title`,
# so pinning the rule here covers all of them. The contract: a user-set sidecar title
# is authoritative (kept verbatim, even one char); an auto-derived first message is
# only used when meaningful (strip() length >= 2 AND >= 1 alphanumeric), else "".


@pytest.mark.parametrize("bad", ["a", ".", "..", "--", "   ", "", "\t", " ! "])
def test_display_title_drops_meaningless_auto_derived_first_message(bad):
    # No manual title, no AI title: a stray keystroke / punctuation / whitespace first
    # message must NOT become the display title — it normalizes to "".
    assert metadata.display_title(metadata.SessionMeta(), bad) == ""


@pytest.mark.parametrize("good", ["go", "ok", "hi", "  go  ", "a1", "fix bug"])
def test_display_title_keeps_meaningful_auto_derived_first_message(good):
    # A real short prompt is kept verbatim (NOT stripped — search/display use the raw value).
    assert metadata.display_title(metadata.SessionMeta(), good) == good


def test_display_title_keeps_one_char_manual_rename_verbatim():
    # A user's manual rename is authoritative even at one char — the meaningfulness rule
    # applies ONLY to auto-derived candidates, never to `meta.title`.
    assert metadata.display_title(metadata.SessionMeta(title="x"), "a") == "x"
    assert metadata.display_title(metadata.SessionMeta(title="."), "first message") == "."


def test_display_title_manual_title_wins_over_ai_and_first_message():
    m = metadata.SessionMeta(title="Manual", ai_title="AI chose this")
    assert metadata.display_title(m, "first message") == "Manual"


def test_display_title_ai_title_fills_gap_when_no_manual_rename():
    # ai_title sits between the manual rename and the first message; it's used as-is
    # (the reviewer is trusted to produce a real title).
    m = metadata.SessionMeta(ai_title="Refactor the parser")
    assert metadata.display_title(m, "a") == "Refactor the parser"


def test_is_meaningful_threshold():
    assert metadata._is_meaningful("go") is True
    assert metadata._is_meaningful("a1") is True
    assert metadata._is_meaningful("a") is False  # length < 2
    assert metadata._is_meaningful("--") is False  # no alphanumeric
    assert metadata._is_meaningful("..") is False
    assert metadata._is_meaningful("  x ") is False  # strips to one char
    assert metadata._is_meaningful("  ") is False


# ---- chronological recap fields (#481) ----------------------------------------


def test_recap_fields_round_trip(tmp_home):
    m = metadata.patch("claude:x", ai_recap="Did A.\nThen B.", recap_fingerprint="fp123")
    assert (m.ai_recap, m.recap_fingerprint) == ("Did A.\nThen B.", "fp123")
    again = metadata.load()["claude:x"]
    assert again.ai_recap == "Did A.\nThen B."
    assert again.recap_fingerprint == "fp123"


def test_legacy_sidecar_without_recap_fields_loads_safely(tmp_home):
    # A pre-#481 row (no ai_recap / recap_fingerprint) loads with empty defaults, not an error.
    p = _meta_path(tmp_home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"claude:x": {"title": "old", "ai_summary": "s"}}))
    m = metadata.load()["claude:x"]
    assert m.ai_recap == "" and m.recap_fingerprint == ""
    # An unrelated later write neither invents nor drops them.
    metadata.patch("claude:x", title="renamed")
    again = metadata.load()["claude:x"]
    assert again.ai_recap == "" and again.ai_summary == "s"


def test_recap_survives_unrelated_patch(tmp_home):
    metadata.patch("claude:x", ai_recap="R", recap_fingerprint="f")
    metadata.patch("claude:x", ai_summary="new summary")  # a summary write must not drop recap
    again = metadata.load()["claude:x"]
    assert again.ai_recap == "R" and again.recap_fingerprint == "f"
