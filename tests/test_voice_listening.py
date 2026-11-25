"""#1996 — a call listens in its own languages, waits when the Commander trails off, and keeps
what was said together.

From the operator's calls of 2026-11-13: a hesitation inside an English request came back as
Icelandic ("Það er að það er"), and a short English reply as a German sentence HQ then answered
in German; HQ answered the first part of a request the Commander had not finished; and a running
transcript was on by default.

- The worker confines the speech server's choice of language to the ones the call listens in,
  and tells it the call's language so far, where the server lists that feature.
- A stop that ended a sentence the worker's turn model took for unfinished makes the draft wait
  `UNFINISHED_PAUSE_S` instead of the turn-taking setting's pause.
- Captions are off unless turned on.
"""

from __future__ import annotations

import asyncio

import httpx

from agent_sessions import hq_calls_settings, voice_calls
from agent_sessions import speech_endpoints as se
from agent_sessions.voice_turns import ended_unfinished, stt_language_fields
from test_speech_endpoints import BASE, _mount, _resolve, _tone, run  # noqa: F401
from test_voice_confirm import Rig
from test_voice_confirm import run as run_call

# --- the languages a call listens in go to the speech server ------------------------------------


def _fields(request: httpx.Request) -> dict[str, str]:
    """The text fields of a multipart request, by name."""
    out = {}
    for part in request.content.split(b"\r\n--")[:-1]:
        head, _, value = part.partition(b"\r\n\r\n")
        if b"filename=" in head or b'name="' not in head:
            continue
        name = head.split(b'name="', 1)[1].split(b'"', 1)[0].decode()
        out[name] = value.decode()
    return out


def _transcribe(monkeypatch, **kw) -> dict[str, str]:
    seen: dict[str, str] = {}

    def handler(r: httpx.Request):
        seen.update(_fields(r))
        return httpx.Response(200, json={"text": "ok", "language": "english"})

    _mount(monkeypatch, handler)
    run(se.transcribe(se.Endpoint(BASE, "whisper-x"), _tone(0.3, 16000), 16000, **kw))
    return seen


def test_a_plain_request_carries_neither_field(monkeypatch):
    sent = _transcribe(monkeypatch)
    assert "languages" not in sent and "language_hint" not in sent


def test_the_call_s_languages_and_its_language_so_far_are_sent(monkeypatch):
    sent = _transcribe(monkeypatch, languages=("de", "en"), language_hint="en", detect=True)
    assert sent["languages"] == "de,en" and sent["language_hint"] == "en"
    assert "language" not in sent  # the server chooses, among those


def test_a_hint_outside_the_call_s_languages_is_not_sent(monkeypatch):
    sent = _transcribe(monkeypatch, languages=("de", "en"), language_hint="is")
    assert sent["languages"] == "de,en" and "language_hint" not in sent


def test_a_fixed_language_wins_and_the_list_is_left_out(monkeypatch):
    sent = _transcribe(monkeypatch, language="de", languages=("de", "en"), language_hint="en")
    assert sent["language"] == "de"
    assert "languages" not in sent and "language_hint" not in sent


def _features(monkeypatch, reply) -> frozenset:
    _mount(monkeypatch, lambda r: reply)
    return run(se.stt_features(se.Endpoint(BASE, "whisper-1")))


def test_a_server_that_lists_the_feature_for_the_model_has_it(monkeypatch):
    mine = {"id": "whisper-1", "features": ["languages", "language_hint", "magic"]}
    models = {"data": [{"id": "other"}, mine]}
    got = _features(monkeypatch, httpx.Response(200, json=models))
    assert got == {"languages", "language_hint"}  # only names this app knows


def test_a_lone_model_under_another_id_counts(monkeypatch):
    models = {"data": [{"id": "faster-whisper", "features": ["languages"]}]}
    assert _features(monkeypatch, httpx.Response(200, json=models)) == {"languages"}


def test_no_list_no_feature(monkeypatch):
    for reply in (
        httpx.Response(200, json={"data": [{"id": "whisper-1"}]}),  # lists none
        httpx.Response(200, json={"data": [{"id": "a", "features": ["languages"]}, {"id": "b"}]}),
        httpx.Response(200, json={"data": [{"id": "whisper-1", "features": "languages"}]}),
        httpx.Response(200, json={"data": "nope"}),
        httpx.Response(200, content=b"not json"),
        httpx.Response(404),
        httpx.Response(500),
    ):
        assert _features(monkeypatch, reply) == frozenset()


# --- which fields a request carries: each only if the server listed it (Hermes 8138) -------------

EN_DE = frozenset({"en", "de"})


def test_a_server_that_lists_only_languages_is_never_sent_a_hint():
    only = frozenset({"languages"})
    assert stt_language_fields(only, EN_DE, None) == {"languages": ("de", "en")}
    # ...also once the call's language is known: the hint is its own feature
    assert stt_language_fields(only, EN_DE, "en") == {"languages": ("de", "en")}


def test_a_server_that_lists_both_gets_the_hint_once_the_language_is_known():
    both = frozenset({"languages", "language_hint"})
    assert stt_language_fields(both, EN_DE, None) == {"languages": ("de", "en")}
    assert stt_language_fields(both, EN_DE, "en") == {
        "languages": ("de", "en"),
        "language_hint": "en",
    }
    assert "language_hint" not in stt_language_fields(both, EN_DE, "is")  # not one it listens in


def test_nothing_listed_or_nothing_listened_in_sends_nothing():
    assert stt_language_fields(frozenset(), EN_DE, "en") == {}
    assert stt_language_fields(frozenset({"language_hint"}), EN_DE, "en") == {}  # no list, no hint
    assert stt_language_fields(frozenset({"languages", "language_hint"}), None, "en") == {}


def test_a_languages_only_server_is_asked_without_a_hint_before_and_after_detection(monkeypatch):
    """Worker to request: the fields the worker computes, through the real transcription call,
    against a strict server that refuses a field it did not list."""
    seen: list[dict] = []

    def handler(r: httpx.Request):
        fields = _fields(r)
        seen.append(fields)
        if "language_hint" in fields:
            return httpx.Response(422, json={"detail": "unknown field language_hint"})
        return httpx.Response(200, json={"text": "ok", "language": "english"})

    _mount(monkeypatch, handler)
    ep = se.Endpoint(BASE, "whisper-x")
    for detected in (None, "en"):
        fields = stt_language_fields(frozenset({"languages"}), EN_DE, detected)
        tr = run(se.transcribe(ep, _tone(0.3, 16000), 16000, detect=True, **fields))
        assert tr.text == "ok"
    assert [("language_hint" in f, f.get("languages")) for f in seen] == [
        (False, "de,en"),
        (False, "de,en"),
    ]


def test_a_server_that_lists_both_is_asked_with_the_hint(monkeypatch):
    seen: list[dict] = []

    def handler(r: httpx.Request):
        seen.append(_fields(r))
        return httpx.Response(200, json={"text": "ok", "language": "english"})

    _mount(monkeypatch, handler)
    fields = stt_language_fields(frozenset({"languages", "language_hint"}), EN_DE, "en")
    run(se.transcribe(se.Endpoint(BASE, "whisper-x"), _tone(0.3, 16000), 16000, **fields))
    assert seen[0]["languages"] == "de,en" and seen[0]["language_hint"] == "en"


# --- a sentence the turn model took for unfinished ----------------------------------------------


def test_a_turn_that_ended_on_the_wait_was_unfinished():
    assert ended_unfinished(100.0, 101.0, 1.0)  # ended a whole wait after the detector's stop
    assert ended_unfinished(100.0, 100.95, 1.0)
    assert not ended_unfinished(100.0, 100.4, 1.0)  # ended at once: the sentence was finished
    assert not ended_unfinished(None, 101.0, 1.0)  # no segment seen
    assert not ended_unfinished(100.0, 101.0, None)  # no wait configured
    assert not ended_unfinished(100.0, 101.0, 0.0)


def _draft_rig(monkeypatch) -> Rig:
    r = Rig(monkeypatch, {"answer": "Done."})
    r.call.commit_pause = voice_calls.COMMIT_PAUSE_S["natural"]
    return r


def _pause(r: Rig, unfinished: bool) -> float:
    r.call.stopped_unfinished = unfinished
    return r.call._commit_pause_now()


def test_hq_waits_longer_after_an_unfinished_sentence(monkeypatch):
    r = _draft_rig(monkeypatch)
    assert _pause(r, False) == voice_calls.COMMIT_PAUSE_S["natural"]
    assert _pause(r, True) == voice_calls.UNFINISHED_PAUSE_S
    assert voice_calls.UNFINISHED_PAUSE_S > voice_calls.COMMIT_PAUSE_S["patient"]


def test_a_setting_longer_than_that_keeps_its_own_pause(monkeypatch):
    r = _draft_rig(monkeypatch)
    r.call.commit_pause = 5.0
    assert _pause(r, True) == 5.0


def test_the_worker_s_stop_says_whether_the_sentence_was_unfinished(monkeypatch):
    import time

    r = _draft_rig(monkeypatch)

    async def go():
        await r.ev(t="user_started")
        await r.ev(t="user_stopped", turn=r.turn, at=time.monotonic(), unfinished=True)
        assert r.call.stopped_unfinished is True
        await r.ev(t="user_started")
        await r.ev(t="user_stopped", turn=r.turn, at=time.monotonic())  # an older worker
        assert r.call.stopped_unfinished is False
        await r.ev(t="user_stopped", turn=r.turn, at=time.monotonic(), unfinished="yes")
        assert r.call.stopped_unfinished is False  # only a real true counts

    run_call(go())


def test_an_unfinished_stop_holds_the_commit_until_the_longer_pause_has_passed(monkeypatch):
    """On the real clock, with short pauses: a finished sentence would commit after 0.15 s, an
    unfinished one only after 0.6 s."""
    import time

    monkeypatch.setattr(voice_calls, "WORKER_STOP_S", 0.0)
    monkeypatch.setattr(voice_calls, "UNFINISHED_PAUSE_S", 0.6)
    r = _draft_rig(monkeypatch)
    r.call.commit_pause = 0.15
    committed: list[float] = []

    async def commit(d):
        committed.append(time.monotonic())
        r.call.draft = None

    r.call._commit_draft = commit

    async def go():
        d = object()
        r.call.draft = d
        started = r.call.user_stopped_at = time.monotonic()
        r.call.stopped_unfinished = True
        task = asyncio.create_task(r.call._commit_when_quiet(d))
        await asyncio.sleep(0.35)  # well past the setting's own pause
        assert committed == []
        await asyncio.wait_for(task, 3)
        assert committed and committed[0] - started >= 0.6

    run_call(go())


def test_a_finished_stop_commits_after_the_setting_s_own_pause(monkeypatch):
    import time

    monkeypatch.setattr(voice_calls, "WORKER_STOP_S", 0.0)
    monkeypatch.setattr(voice_calls, "UNFINISHED_PAUSE_S", 5.0)
    r = _draft_rig(monkeypatch)
    r.call.commit_pause = 0.15
    committed: list[float] = []

    async def commit(d):
        committed.append(time.monotonic())
        r.call.draft = None

    r.call._commit_draft = commit

    async def go():
        d = object()
        r.call.draft = d
        started = r.call.user_stopped_at = time.monotonic()
        r.call.stopped_unfinished = False
        await asyncio.wait_for(r.call._commit_when_quiet(d), 3)
        assert 0.15 <= committed[0] - started < 2.0

    run_call(go())


# --- captions --------------------------------------------------------------------------------


def test_captions_are_off_unless_turned_on():
    assert hq_calls_settings.get()["captions"] is False
    hq_calls_settings.save({"captions": True})
    assert hq_calls_settings.get()["captions"] is True  # a choice the operator made is kept
