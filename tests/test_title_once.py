"""Title new sessions once (#2332).

With "Rename sessions with the reviewed title" off (now the default), a NEW session gets ONE title
from a review of its first messages and keeps it. Only with a configured AI endpoint, once per
session (persisted), with one title-only call, and never for a session that existed when the
title-once baseline was taken. The endpoint is an ``httpx.MockTransport``; prefs, metadata,
scrollback and the baseline are per-test tmp stores.
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest

from agent_sessions import ai_review_loop, engines, metadata, prefs, review, webterm
from agent_sessions.metadata import SessionMeta

SECRET = "sk-review-secret-31337"  # noqa: S105 — test fixture value
BASE = "https://ai.test/v1"
SID = "claude:11111111-1111-1111-1111-111111111111"  # exists in fake_jsonl


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(review, "_TRANSPORT", None)
    review._models_cache.clear()
    ai_review_loop._LAST_ATTEMPT.clear()
    ai_review_loop._TITLE_ATTEMPTED.clear()
    yield
    review._models_cache.clear()
    ai_review_loop._LAST_ATTEMPT.clear()
    ai_review_loop._TITLE_ATTEMPTED.clear()


def _endpoint(*, enabled: bool) -> None:
    prefs.set_ai_review(
        {
            "egress_acknowledged": True,
            "enabled": enabled,
            "base_url": BASE,
            "api_key": SECRET,
            "model": "test-model",
        }
    )


def _transport(
    calls: list, titles: list[str] | None = None, *, insufficient: bool = False, statuses=()
):
    """Answers the tail review with the next title from ``titles`` (or a fixed one) and the recap
    with a recap, so a full review is two calls (#481) and a title-once review one. ``statuses``:
    HTTP statuses for the first calls (an outage), 200 after."""
    titles = list(titles if titles is not None else ["Fix the tests"])
    statuses = list(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if statuses:
            return httpx.Response(statuses.pop(0), json={"error": "down"})
        payload = json.loads(request.content)
        if "returning to a coding-agent session" in payload["messages"][0]["content"]:
            body: dict = {"recap": "Cloned repo.\nFixed tests."}
        elif insufficient:
            body = {"insufficient": True}
        else:
            title = titles.pop(0) if len(titles) > 1 else titles[0]
            body = {
                "summary": "Editing tests",
                "title": title,
                "intervention_required": False,
                "reason": "",
            }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(body)}}]})

    return httpx.MockTransport(handler)


class _Registry:
    def __init__(self, keys):
        self._keys = keys

    def snapshot(self):
        return [{"id": k, "engine": "claude", "sid": k.split(":", 1)[-1]} for k in self._keys]


def _sweep(keys=(SID,)):
    return asyncio.run(ai_review_loop.sweep(_Registry(list(keys))))


@pytest.fixture
def created(monkeypatch):
    """Take the baseline with NO sessions, then give SID a creation time after it: a new session."""
    ages: dict[str, float] = {}
    monkeypatch.setattr(ai_review_loop, "_created_index", lambda: dict(ages))
    monkeypatch.setattr(ai_review_loop, "_existing_keys", list)
    since, existing = ai_review_loop.title_once_baseline()
    assert existing == frozenset()
    ages[SID] = since + 10
    return since, ages


# ---- the prefs block ---------------------------------------------------------------------------


def test_fresh_defaults_rename_off_and_title_once_on(tmp_path):
    path = tmp_path / "none.json"
    block = prefs.get_session_review(path)
    assert block["rename_sessions"] is False
    assert block["title_once"] is True
    assert prefs.renames_sessions(path) is False
    assert prefs.titles_new_sessions_once(path) is True


def test_a_stored_rename_choice_is_kept(tmp_path):
    path = tmp_path / "prefs.json"
    path.write_text(json.dumps({"session_review": {"rename_sessions": True, "notify": True}}))
    assert prefs.renames_sessions(path) is True
    # Renaming on: the title follows every review, so there is nothing to title once.
    assert prefs.titles_new_sessions_once(path) is False


def test_a_write_stores_only_what_was_saved(tmp_path):
    """ "Default" means unset: saving another field must not materialise rename_sessions, so it
    keeps following the default (the bug the old full-block write had)."""
    path = tmp_path / "prefs.json"
    echoed = prefs.set_session_review({"notify": False}, path)
    assert echoed["notify"] is False and echoed["rename_sessions"] is False
    raw = json.loads(path.read_text())["session_review"]
    assert raw == {"notify": False}
    prefs.set_session_review({"title_once": False}, path)
    assert json.loads(path.read_text())["session_review"] == {"notify": False, "title_once": False}
    assert prefs.titles_new_sessions_once(path) is False


def test_title_once_is_a_strict_boolean():
    ok = prefs.validate_session_review_patch
    assert ok({"title_once": False}) is None
    assert ok({"title_once": "off"}) is not None
    assert ok({"title_once": 0}) is not None
    assert ok({"title_once_since": 1.0}) is not None  # nothing server-owned is patchable


def test_rename_saved_means_stored(tmp_path):
    path = tmp_path / "prefs.json"
    assert prefs.rename_sessions_saved(path) is False
    prefs.set_session_review({"notify": False}, path)
    assert prefs.rename_sessions_saved(path) is False
    prefs.set_session_review({"rename_sessions": False}, path)
    assert prefs.rename_sessions_saved(path) is True


# ---- the display rule --------------------------------------------------------------------------


def test_display_precedence():
    meta = SessionMeta(ai_title="Latest review title", initial_title="First title")
    assert metadata.display_title(meta, "do the thing", use_ai_title=True) == "Latest review title"
    assert metadata.display_title(meta, "do the thing", use_ai_title=False) == "First title"
    meta.title = "Mine"
    assert metadata.display_title(meta, "do the thing", use_ai_title=False) == "Mine"
    bare = SessionMeta(ai_title="Latest review title")
    assert metadata.display_title(bare, "do the thing", use_ai_title=False) == "do the thing"


# ---- the loop ----------------------------------------------------------------------------------


def test_a_new_session_is_titled_exactly_once(tmp_home, fake_jsonl, created, monkeypatch):
    _endpoint(enabled=False)  # periodic reviews OFF: only the once-pass may call
    calls: list = []
    monkeypatch.setattr(review, "_TRANSPORT", _transport(calls, ["First title", "Later title"]))
    assert _sweep() == ([SID], 0)
    assert len(calls) == 1  # ONE title-only call: no recap
    meta = metadata.get(SID)
    assert meta.initial_title == "First title"
    # Only the title is kept: no flag that nothing would clear, no summary, no review stamp.
    assert meta.intervention_required is False and meta.ai_summary == "" and meta.ai_title == ""
    assert meta.reviewed_at is None and meta.review_fingerprint == ""
    # Not again — unchanged, changed, or after a restart (the in-memory state is gone).
    assert _sweep() == ([], 0)
    webterm._buffer_append(SID, b"more work moves the fingerprint\r\n")
    assert _sweep() == ([], 0)
    ai_review_loop._TITLE_ATTEMPTED.clear()
    ai_review_loop._LAST_ATTEMPT.clear()
    assert _sweep() == ([], 0)
    assert len(calls) == 1
    assert metadata.get(SID).initial_title == "First title"


def test_no_endpoint_no_call(tmp_home, fake_jsonl, created, monkeypatch):
    calls: list = []
    monkeypatch.setattr(review, "_TRANSPORT", _transport(calls))
    assert _sweep() == ([], 0)
    assert calls == []
    assert metadata.get(SID).initial_title == ""


def test_a_session_from_before_the_stamp_is_never_reviewed(
    tmp_home, fake_jsonl, created, monkeypatch
):
    since, ages = created
    ages[SID] = since - 3600
    _endpoint(enabled=False)
    calls: list = []
    monkeypatch.setattr(review, "_TRANSPORT", _transport(calls))
    assert _sweep() == ([], 0)
    assert calls == []


def test_unknown_creation_time_is_never_new(tmp_home, fake_jsonl, created, monkeypatch):
    _since, ages = created
    ages.clear()
    _endpoint(enabled=False)
    calls: list = []
    monkeypatch.setattr(review, "_TRANSPORT", _transport(calls))
    assert _sweep() == ([], 0)
    assert calls == []


@pytest.mark.parametrize(
    "block",
    [{"rename_sessions": True}, {"title_once": False}],
    ids=["renaming-on", "option-off"],
)
def test_no_once_pass_when_renaming_is_on_or_the_option_is_off(
    tmp_home, fake_jsonl, created, monkeypatch, block
):
    _endpoint(enabled=False)
    prefs.set_session_review(block)
    calls: list = []
    monkeypatch.setattr(review, "_TRANSPORT", _transport(calls))
    assert _sweep() == ([], 0)
    assert calls == []


def test_a_manual_title_is_never_reviewed_for(tmp_home, fake_jsonl, created, monkeypatch):
    _endpoint(enabled=False)
    metadata.patch(SID, title="My own name")
    calls: list = []
    monkeypatch.setattr(review, "_TRANSPORT", _transport(calls))
    assert _sweep() == ([], 0)
    assert calls == []


def test_an_input_already_reviewed_is_adopted_without_a_call(
    tmp_home, fake_jsonl, created, monkeypatch
):
    _endpoint(enabled=False)
    calls: list = []
    monkeypatch.setattr(review, "_TRANSPORT", _transport(calls, ["Reviewed now"]))
    asyncio.run(review.run_review(SID))  # the operator pressed "Review now"
    assert len(calls) == 2 and metadata.get(SID).initial_title == ""
    assert _sweep() == ([], 0)
    assert len(calls) == 2
    assert metadata.get(SID).initial_title == "Reviewed now"


def test_an_insufficient_input_is_not_asked_again_until_it_changes(
    tmp_home, fake_jsonl, created, monkeypatch
):
    _endpoint(enabled=False)
    calls: list = []
    monkeypatch.setattr(review, "_TRANSPORT", _transport(calls, insufficient=True))
    assert _sweep() == ([], 0)
    assert len(calls) == 1
    assert _sweep() == ([], 0)
    assert len(calls) == 1
    webterm._buffer_append(SID, b"the first real exchange\r\n")
    assert _sweep() == ([], 0)
    assert len(calls) == 2
    assert metadata.get(SID).initial_title == ""


def test_an_empty_title_is_not_asked_again_until_the_input_changes(
    tmp_home, fake_jsonl, created, monkeypatch
):
    _endpoint(enabled=False)
    calls: list = []
    monkeypatch.setattr(review, "_TRANSPORT", _transport(calls, [""]))
    assert _sweep() == ([SID], 0)
    assert _sweep() == ([], 0)
    assert len(calls) == 1 and metadata.get(SID).initial_title == ""
    webterm._buffer_append(SID, b"the first real exchange\r\n")
    _sweep()
    assert len(calls) == 2


def test_a_screen_only_session_gets_one_attempt_per_server_run(
    tmp_home, fake_jsonl, created, monkeypatch
):
    _endpoint(enabled=False)
    monkeypatch.setattr(review, "keeps_transcript", lambda key: False)
    calls: list = []
    monkeypatch.setattr(review, "_TRANSPORT", _transport(calls, insufficient=True))
    assert _sweep() == ([], 0)
    webterm._buffer_append(SID, b"the screen keeps changing\r\n")
    assert _sweep() == ([], 0)
    assert len(calls) == 1


def test_an_endpoint_failure_is_retried(tmp_home, fake_jsonl, created, monkeypatch):
    _endpoint(enabled=False)
    calls: list = []
    monkeypatch.setattr(review, "_TRANSPORT", _transport(calls, ["Back up"], statuses=[503]))
    assert _sweep() == ([], 1)
    assert metadata.get(SID).initial_title == ""
    assert metadata.get(SID).review_failed_at is None  # a title attempt writes nothing on failure
    assert _sweep() == ([SID], 0)  # same input, retried
    assert len(calls) == 2 and metadata.get(SID).initial_title == "Back up"


def test_nothing_pending_tracks_no_sweep(tmp_home, fake_jsonl, created, monkeypatch):
    _since, ages = created
    ages.clear()
    _endpoint(enabled=False)

    def no_track(*a, **k):
        raise AssertionError("an idle once-pass must not register an AI task")

    monkeypatch.setattr(ai_review_loop.aitasks, "track", no_track)
    assert _sweep() == ([], 0)


def test_with_periodic_reviews_on_the_first_title_sticks(
    tmp_home, fake_jsonl, created, monkeypatch
):
    _endpoint(enabled=True)
    calls: list = []
    monkeypatch.setattr(review, "_TRANSPORT", _transport(calls, ["First title", "Later title"]))
    assert _sweep() == ([SID], 0)
    webterm._buffer_append(SID, b"more work moves the fingerprint\r\n")
    assert _sweep() == ([SID], 0)
    meta = metadata.get(SID)
    assert meta.ai_title == "Later title"  # the review keeps running…
    assert meta.initial_title == "First title"  # …but the once-title never moves
    assert metadata.display_title(meta, "", use_ai_title=prefs.renames_sessions()) == "First title"


# ---- the baseline, with the REAL scan (no mocked creation times) -------------------------------

NEW_SID = "claude:66666666-6666-6666-6666-666666666666"


def test_an_existing_session_stays_untitled_after_its_next_turn_and_a_new_one_is_titled(
    tmp_home, fake_jsonl, monkeypatch
):
    """The fake transcripts carry no timestamps, so the scanner's `created_at` is the file time —
    the fallback some engines always use. It moves with every write, so a creation-time guard
    alone would take an existing session for new after its next turn. The baseline does not."""
    _endpoint(enabled=False)
    calls: list = []
    monkeypatch.setattr(review, "_TRANSPORT", _transport(calls, ["New one"]))
    proj = fake_jsonl / ".claude" / "projects" / "-home-user-claude-repo-a"
    assert _sweep([SID]) == ([], 0)  # first pass takes the baseline: SID exists
    since, existing = ai_review_loop.title_once_baseline()
    assert SID in existing
    # SID's next turn: its file (and so its fallback creation time) moves past the baseline.
    old = proj / f"{SID.split(':')[1]}.jsonl"
    with old.open("a") as fh:
        fh.write('{"type":"user","message":{"content":"and another thing"}}\n')
    os.utime(old, (since + 30, since + 30))
    # A session created after the baseline.
    new = proj / f"{NEW_SID.split(':')[1]}.jsonl"
    new.write_text('{"type":"user","message":{"content":"set up the release job"}}\n')
    os.utime(new, (since + 30, since + 30))
    engines.invalidate_scan_cache()
    assert {s.created_at >= since for s in engines.scan_all_cached() if s.uuid in str(old)} == {
        True
    }  # the guard a created_at check alone would have relied on is really fooled here
    assert _sweep([SID, NEW_SID]) == ([NEW_SID], 0)
    assert len(calls) == 1
    assert metadata.get(NEW_SID).initial_title == "New one"
    assert metadata.get(SID).initial_title == ""


# ---- upgrade: reviewed sessions keep their names when the default turns renaming off ----------


def test_the_default_flip_keeps_existing_ai_titles_without_a_call(
    tmp_home, fake_jsonl, monkeypatch
):
    calls: list = []
    monkeypatch.setattr(review, "_TRANSPORT", _transport(calls))
    metadata.patch(SID, ai_title="Refit repo-a")
    other = "claude:22222222-2222-2222-2222-222222222222"
    metadata.patch(other, ai_title="Reviewed", title="Mine")
    ai_review_loop.title_once_baseline()
    assert calls == []
    assert metadata.get(SID).initial_title == "Refit repo-a"
    assert metadata.get(other).initial_title == ""  # a manual title already names it
    shown = metadata.display_title(metadata.get(SID), "", use_ai_title=prefs.renames_sessions())
    assert shown == "Refit repo-a"
    # Taken once: a later AI title is not copied over it.
    metadata.patch(SID, ai_title="Something newer")
    ai_review_loop.title_once_baseline()
    assert metadata.get(SID).initial_title == "Refit repo-a"


@pytest.mark.parametrize("stored", [False, True], ids=["chose-off", "chose-on"])
def test_a_saved_rename_choice_adopts_nothing(tmp_home, fake_jsonl, stored):
    """Renaming the operator turned off stays off — their sessions keep their own titles; one they
    left on keeps following every review. A value an older version saved counts as a choice."""
    prefs.set_session_review({"rename_sessions": stored})
    metadata.patch(SID, ai_title="Refit repo-a")
    ai_review_loop.title_once_baseline()
    assert metadata.get(SID).initial_title == ""


def test_an_unreadable_sidecar_is_never_rewritten(tmp_home):
    path = tmp_home / ".config" / "agent-sessions" / "metadata.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert metadata.adopt_initial_titles() == 0
    assert path.read_text() == "{not json"


def test_the_baseline_ignores_a_warm_stale_listing(tmp_home, monkeypatch):
    """Hermes on PR #2334: the sidebar's cached scan can predate a session already on disk. The
    baseline must come from a fresh, complete scan, or that session would later pass as new."""
    _endpoint(enabled=False)
    calls: list = []
    monkeypatch.setattr(review, "_TRANSPORT", _transport(calls))
    proj = tmp_home / ".claude" / "projects" / "-home-user-claude-repo-a"
    proj.mkdir(parents=True)
    # The suite runs with the snapshot cache off (`_isolate_scan_cache`); turn it on so the listing
    # really is retained, as in production. The fixture resets it to 0 afterwards.
    engines.set_scan_cache_ttl(3600.0)
    engines.invalidate_scan_cache()
    assert engines.scan_all_cached() == []  # warm, and empty
    old = proj / f"{SID.split(':')[1]}.jsonl"
    old.write_text('{"type":"user","message":{"content":"first message on repo-a"}}\n')
    assert engines.scan_all_cached() == []  # the cached listing really is stale now
    since, existing = ai_review_loop.title_once_baseline()  # cache NOT invalidated
    assert SID in existing
    with old.open("a") as fh:
        fh.write('{"type":"user","message":{"content":"and another thing"}}\n')
    os.utime(old, (since + 30, since + 30))
    engines.invalidate_scan_cache()
    assert _sweep() == ([], 0)
    assert calls == []
    assert metadata.get(SID).initial_title == ""


def test_no_baseline_while_a_store_is_unreadable_then_one_after_it_recovers(
    tmp_home, fake_jsonl, monkeypatch
):
    _endpoint(enabled=False)
    calls: list = []
    monkeypatch.setattr(review, "_TRANSPORT", _transport(calls))
    real = engines.scan_all_checked
    trouble = ["claude: its session store could not be read (PermissionError)"]
    monkeypatch.setattr(engines, "scan_all_checked", lambda: (real()[0], list(trouble)))
    with pytest.raises(RuntimeError):
        ai_review_loop.title_once_baseline()
    assert not prefs.title_once_path().exists()
    assert _sweep() == ([], 0)  # no baseline: no once-pass, nothing sent
    assert calls == []
    trouble.clear()  # the store recovers
    _since, existing = ai_review_loop.title_once_baseline()
    assert SID in existing
