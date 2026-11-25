"""Pulse "Ask" — natural-language session retrieval (#522): engine + route.

The engine is tested against a monkeypatched session scan + metadata and a
``httpx.MockTransport`` LLM (CI never touches the network), mirroring ``test_pulse.py``.
Pinned: the full-history catalog (no Pulse window) + archived/excluded exclusion, the
keyword prefilter's never-drop-a-hit cap, id validation against the slice actually sent
(invented/duplicate ids dropped), the 0/1/2-LLM-call cases (empty catalog / no matches /
happy path), per-candidate Stage-2 skip + total-failure degrade, history clamping, and the
route contract (422 bounds, unconfigured 409, endpoint-down 502, concurrent-ask 409 with
the activity snapshot, CSRF gate).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass

import httpx
import pytest

from agent_sessions import aitasks, metadata, missions, prefs, prompts, pulse_chat, review

SECRET = "sk-pulse-chat-test"  # noqa: S105 — test fixture value
BASE = "https://ai.test/v1"

NOW = 1_000_000.0
OLD = NOW - 45 * 86400  # far outside any Pulse window (max 30d)


@dataclass
class FakeSession:
    engine: str
    uuid: str
    cwd: str
    last_mtime: float
    first_user_message: str = "first message"
    archived: bool = False

    @property
    def short_uuid(self) -> str:
        return self.uuid[:8]


@pytest.fixture(autouse=True)
def _reset_activity():
    aitasks.reset()
    yield
    aitasks.reset()


@pytest.fixture
def configured_ai(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(review, "_TRANSPORT", None)
    prefs.set_ai_review(
        {
            "egress_acknowledged": True,
            "enabled": True,
            "base_url": BASE,
            "api_key": SECRET,
            "model": "m",
        }
    )
    return tmp_path


def _setup(monkeypatch, sessions, meta=None):
    monkeypatch.setattr(pulse_chat.pulse.engines, "scan_all", lambda: sessions)
    monkeypatch.setattr(pulse_chat.pulse.metadata, "load", lambda *a, **k: meta or {})
    monkeypatch.setattr(pulse_chat.pulse.metadata, "load_aliases", lambda *a, **k: {})
    monkeypatch.setattr(pulse_chat.pulse.projects, "load", lambda *a, **k: {})


def _uuid(i: int) -> str:
    return f"00000000-0000-4000-8000-{i:012d}"


def _sessions(n: int, *, mtime: float = NOW - 100) -> list[FakeSession]:
    return [FakeSession("claude", _uuid(i), f"/proj/p{i}", mtime) for i in range(n)]


def _seq_transport(payloads: list[dict], calls: list | None = None):
    """A MockTransport answering the i-th completion with ``payloads[i]`` (last one
    repeats). ``calls`` collects each request's decoded body for prompt assertions."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if calls is not None:
            calls.append(body)
        payload = payloads[min((len(calls) if calls is not None else 1) - 1, len(payloads) - 1)]
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(payload)}}]}
        )

    return httpx.MockTransport(handler)


# ---- catalog ------------------------------------------------------------------------


def test_catalog_covers_full_history_and_filters(monkeypatch):
    ancient = FakeSession("claude", _uuid(1), "/a", OLD)
    archived = FakeSession("claude", _uuid(2), "/a", NOW - 50, archived=True)
    excluded = FakeSession("claude", _uuid(3), "/a", NOW - 50)
    recent = FakeSession("claude", _uuid(4), "/a", NOW - 50)
    meta = {f"claude:{_uuid(3)}": metadata.SessionMeta(review_excluded=True)}
    _setup(monkeypatch, [ancient, archived, excluded, recent], meta)
    ids = [c["id"] for c in pulse_chat.build_catalog(now=NOW)]
    # The 45-day-old session IS in the catalog (no window); archived/excluded are not.
    assert f"claude:{_uuid(1)}" in ids
    assert f"claude:{_uuid(4)}" in ids
    assert f"claude:{_uuid(2)}" not in ids
    assert f"claude:{_uuid(3)}" not in ids


def test_prefilter_caps_slice_and_keeps_every_keyword_hit(monkeypatch):
    # 200 recent noise sessions + ONE old session whose title matches the query. The hit
    # must survive the 150-cap even though recency alone would have dropped it.
    noise = _sessions(200)
    hit = FakeSession("claude", _uuid(999), "/proj/ws", OLD)
    meta = {
        f"claude:{_uuid(999)}": metadata.SessionMeta(
            title="fix websocket reconnect backoff", review_fingerprint="fp"
        )
    }
    _setup(monkeypatch, [*noise, hit], meta)
    catalog = pulse_chat.build_catalog(now=NOW)
    slice_ = pulse_chat._prefilter(catalog, "the websocket reconnect bug I worked on")
    assert len(slice_) == pulse_chat.CATALOG_SLICE_MAX
    assert any(c["id"] == f"claude:{_uuid(999)}" for c in slice_)


def test_prefilter_keeps_a_recap_only_keyword_hit(monkeypatch):
    # The topic lives ONLY in the session's ai_recap — not its title, summary, cwd, or
    # project (#653). Before the recap fed the haystack this session was a non-hit and, being
    # the oldest, would have been dropped past the 150-cap by the 200 recent-noise sessions.
    noise = _sessions(200)
    hit = FakeSession("claude", _uuid(999), "/proj/generic", OLD)
    meta = {
        f"claude:{_uuid(999)}": metadata.SessionMeta(
            title="refactor the stream layer",  # deliberately no "backpressure"
            ai_recap="investigated the mux backpressure deadlock; fixed the writer",
            review_fingerprint="fp",
        )
    }
    _setup(monkeypatch, [*noise, hit], meta)
    catalog = pulse_chat.build_catalog(now=NOW)
    target = next(c for c in catalog if c["id"] == f"claude:{_uuid(999)}")
    # Guard the premise: the keyword is ONLY in the recap, so a hit here is the recap's doing.
    assert "backpressure" not in (
        f"{target.get('title') or ''} {target.get('ai_summary') or ''} "
        f"{target.get('cwd') or ''}".lower()
    )
    assert "backpressure" in pulse_chat._card_haystack(target)
    slice_ = pulse_chat._prefilter(catalog, "which session had the backpressure deadlock?")
    assert len(slice_) == pulse_chat.CATALOG_SLICE_MAX
    assert any(c["id"] == f"claude:{_uuid(999)}" for c in slice_)


def test_catalog_entry_prefers_bounded_recap_over_summary():
    # A conservative cap, pinned so the Stage-1 prompt can't silently grow with recap length,
    # and roomier than the one-line summary so the model sees the chronological brief (#653).
    assert pulse_chat.CATALOG_RECAP_MAX == 500
    assert pulse_chat.CATALOG_RECAP_MAX > pulse_chat.SUMMARY_MAX
    entry = pulse_chat._catalog_entry(
        {
            "id": f"claude:{_uuid(1)}",
            "_ai_recap": "R" * 2000,
            "ai_summary": "one liner",
            "last_activity": NOW,
        },
        NOW,
    )
    assert entry["summary"] == "R" * pulse_chat.CATALOG_RECAP_MAX
    # A short recap passes through whole (never padded, never the summary).
    short = pulse_chat._catalog_entry(
        {
            "id": f"claude:{_uuid(2)}",
            "_ai_recap": "did the thing",
            "ai_summary": "s",
            "last_activity": NOW,
        },
        NOW,
    )
    assert short["summary"] == "did the thing"


def test_catalog_entry_falls_back_to_summary_when_no_recap():
    # No recap (older/un-recapped session) → byte-for-byte the pre-#653 behaviour: the
    # ai_summary capped at SUMMARY_MAX, not CATALOG_RECAP_MAX.
    for recap in (None, ""):
        entry = pulse_chat._catalog_entry(
            {
                "id": f"claude:{_uuid(3)}",
                "_ai_recap": recap,
                "ai_summary": "S" * 300,
                "last_activity": NOW,
            },
            NOW,
        )
        assert entry["summary"] == "S" * pulse_chat.SUMMARY_MAX


def test_recap_is_retrieval_input_only_and_never_leaks(configured_ai, monkeypatch):
    # The recap feeds the Stage-1 catalog the model ranks on, but the internal _ai_recap key
    # (like every _-prefixed field) is stripped before any card reaches the client (#653).
    assert "_ai_recap" not in pulse_chat._public_card(
        {"id": "x", "title": "t", "_ai_recap": "secret", "_review_fingerprint": "fp"}
    )
    target = f"claude:{_uuid(1)}"
    _setup(
        monkeypatch,
        _sessions(3),
        {target: metadata.SessionMeta(ai_recap="secret chronological recap of the mux work")},
    )
    calls: list = []
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _seq_transport(
            [
                {"answer": "cat", "matches": [{"id": target, "why": "recap mentions it"}]},
                {"answer": "the mux session.", "matches": [{"id": target, "why": "confirmed"}]},
            ],
            calls,
        ),
    )
    monkeypatch.setattr(review, "gather_input", lambda key, n: ("user: mux tail", "fp"))
    result = asyncio.run(pulse_chat.ask("the mux work session?"))
    # Stage 1 saw the recap as the target's summary (retrieval input)...
    stage1_catalog = json.loads(calls[0]["messages"][-1]["content"])["catalog"]
    target_entry = next(e for e in stage1_catalog if e["id"] == target)
    assert target_entry["summary"] == "secret chronological recap of the mux work"
    # ...but the returned card carries NO _-prefixed field (recap never leaves the server).
    (match,) = result["matches"]
    assert not any(k.startswith("_") for k in match)


# ---- ask(): call counts, stages, validation -----------------------------------------


def test_empty_catalog_makes_zero_llm_calls(configured_ai, monkeypatch):
    _setup(monkeypatch, [])
    calls: list = []
    monkeypatch.setattr(review, "_TRANSPORT", _seq_transport([{}], calls))
    result = asyncio.run(pulse_chat.ask("anything?"))
    assert result["stage"] == "empty"
    assert result["matches"] == []
    assert result["answer"]  # a deterministic server-side line, not model output
    assert calls == []


def test_no_matches_is_one_call_stage_catalog(configured_ai, monkeypatch):
    _setup(monkeypatch, _sessions(3))
    calls: list = []
    payload = {"answer": "Nothing like that.", "matches": []}
    monkeypatch.setattr(review, "_TRANSPORT", _seq_transport([payload], calls))
    result = asyncio.run(pulse_chat.ask("did I ever port this to zig?"))
    assert result["stage"] == "catalog"
    assert result["matches"] == []
    assert result["answer"] == "Nothing like that."
    assert len(calls) == 1


def test_happy_path_is_exactly_two_calls_and_cards_carry_why(configured_ai, monkeypatch):
    sessions = _sessions(3)
    sessions[1].first_user_message = "the websocket reconnect bug"  # grounds the pick (#2016)
    _setup(monkeypatch, sessions)
    target = f"claude:{_uuid(1)}"
    calls: list = []
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _seq_transport(
            [
                {"answer": "cat", "matches": [{"id": target, "why": "title mentions it"}]},
                {
                    "answer": "That was your p1 session.",
                    "matches": [{"id": target, "why": "transcript confirms"}],
                },
            ],
            calls,
        ),
    )
    monkeypatch.setattr(review, "gather_input", lambda key, n: ("user: reconnect stuff", "fp"))
    result = asyncio.run(pulse_chat.ask("the reconnect bug session?"))
    assert len(calls) == 2
    assert result["stage"] == "content"
    assert result["answer"] == "That was your p1 session."
    (match,) = result["matches"]
    assert match["why"] == "transcript confirms"
    # The match is the full public Pulse-card shape (the frontend Card renders it as-is).
    for key in (
        "id",
        "engine",
        "title",
        "cwd",
        "project",
        "last_activity",
        "ai_summary",
        "intervention_required",
        "intervention_reason",
        "reviewed_at",
        "live",
        "state",
    ):
        assert key in match, key
    assert "synthesis" not in match  # the slow scan's line went with the scan (#2179)
    assert "_review_fingerprint" not in match
    # Stage 2 saw the transcript tail for the candidate it was asked about.
    stage2_user = json.loads(calls[1]["messages"][-1]["content"])
    assert stage2_user["candidates"][0]["transcript_tail"] == "user: reconnect stuff"


def test_invented_and_duplicate_ids_are_dropped(configured_ai, monkeypatch):
    _setup(monkeypatch, _sessions(2))
    known = f"claude:{_uuid(0)}"
    payload = {
        "answer": "found",
        "matches": [
            {"id": "claude:99999999-9999-4999-8999-999999999999", "why": "invented"},
            {"id": known, "why": "real"},
            {"id": known, "why": "duplicate"},
            {"id": "not-a-key", "why": "junk shape"},
        ],
    }
    monkeypatch.setattr(review, "_TRANSPORT", _seq_transport([payload]))
    # No Stage 2: force every gather to fail so the Stage-1 validation is what we observe.
    monkeypatch.setattr(
        review, "gather_input", lambda *a: (_ for _ in ()).throw(review.ReviewError("none"))
    )
    result = asyncio.run(pulse_chat.ask("which one?"))
    assert [m["id"] for m in result["matches"]] == [known]
    assert result["matches"][0]["why"] == "real"


# ---- missions in the catalog (#1069) --------------------------------------------------


def _mission(title: str, instruction: str) -> str:
    """A real mission row in the per-test store (conftest isolates the DB)."""
    return missions.create_mission(instruction, title=title)["id"]


def test_missions_join_the_catalog_and_come_back_as_mission_matches(configured_ai, monkeypatch):
    sessions = _sessions(2)
    sessions[1].first_user_message = "p1 does the reconnect work"  # grounds the pick (#2016)
    _setup(monkeypatch, sessions)
    mid = _mission("Stabilise terminal reconnects", "fix the websocket reconnect storm")
    key = f"mission:{mid}"
    session = f"claude:{_uuid(1)}"
    calls: list = []
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _seq_transport(
            [
                {
                    "answer": "cat",
                    "matches": [{"id": key, "why": "instruction"}, {"id": session, "why": "t"}],
                },
                {
                    "answer": "The reconnect mission; p1 did the work.",
                    "matches": [{"id": key, "why": "brief says so"}, {"id": session, "why": "x"}],
                },
            ],
            calls,
        ),
    )
    monkeypatch.setattr(review, "gather_input", lambda key, n: ("user: reconnect", "fp"))
    result = asyncio.run(pulse_chat.ask("which mission fixed the websocket reconnect?"))

    # Stage 1 saw the mission as its own kind, with its instruction as the summary.
    stage1 = json.loads(calls[0]["messages"][-1]["content"])["catalog"]
    (entry,) = [e for e in stage1 if e["id"] == key]
    assert entry["kind"] == "mission"
    assert entry["summary"] == "fix the websocket reconnect storm"
    assert {e["kind"] for e in stage1} == {"mission", "session"}
    # Stage 2 verified the mission against its text, not a transcript it does not have.
    cands = json.loads(calls[1]["messages"][-1]["content"])["candidates"]
    (mc,) = [c for c in cands if c["id"] == key]
    assert mc["mission_brief"] == "fix the websocket reconnect storm"
    assert "transcript_tail" not in mc

    assert result["stage"] == "content"
    # `matches` stays session cards only; missions ride beside them, keyed by the BARE id the
    # `/mission?m=` deep link takes, and never carry the operator's instruction text.
    assert [m["id"] for m in result["matches"]] == [session]
    assert result["mission_matches"] == [
        {
            "id": mid,
            "title": "Stabilise terminal reconnects",
            "state": "draft",
            "project_id": "",
            "why": "brief says so",
        }
    ]


def test_invented_or_misshapen_mission_ids_are_dropped(configured_ai, monkeypatch):
    _setup(monkeypatch, [])
    mid = _mission("Real one", "do the thing")
    payload = {
        "answer": "found",
        "matches": [
            {"id": "mission:msn_" + "f" * 32, "why": "invented"},
            {"id": "mission:../../etc", "why": "junk"},
            {"id": mid, "why": "bare id was never in the catalog"},
            {"id": f"mission:{mid}", "why": "real"},
        ],
    }
    calls: list = []
    monkeypatch.setattr(review, "_TRANSPORT", _seq_transport([payload, payload], calls))
    result = asyncio.run(pulse_chat.ask("which one?"))
    assert [m["id"] for m in result["mission_matches"]] == [mid]
    assert result["matches"] == []


def test_missions_alone_are_not_an_empty_catalog(configured_ai, monkeypatch):
    _setup(monkeypatch, [])
    _mission("Only a mission", "something")
    calls: list = []
    monkeypatch.setattr(
        review, "_TRANSPORT", _seq_transport([{"answer": "none", "matches": []}], calls)
    )
    result = asyncio.run(pulse_chat.ask("anything?"))
    assert result["stage"] == "catalog"
    assert len(calls) == 1


def test_a_broken_missions_store_degrades_to_sessions_only(configured_ai, monkeypatch):
    _setup(monkeypatch, _sessions(2))
    _mission("Hidden by the failure", "x")

    def boom():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(pulse_chat, "build_mission_catalog", boom)
    calls: list = []
    monkeypatch.setattr(
        review, "_TRANSPORT", _seq_transport([{"answer": "none", "matches": []}], calls)
    )
    result = asyncio.run(pulse_chat.ask("anything?"))
    assert result["answer"] == "none"
    assert result["mission_matches"] == []
    stage1 = json.loads(calls[0]["messages"][-1]["content"])["catalog"]
    assert stage1 and all(e["kind"] == "session" for e in stage1)


def test_missions_matching_the_query_never_crowd_sessions_out_of_the_slice(
    configured_ai, monkeypatch
):
    # Missions have their own sub-quota: sixty keyword-hit missions still leave every session
    # in the Stage-1 catalog, and the missions themselves stop at MISSION_SLICE_MAX.
    _setup(monkeypatch, _sessions(3))
    for i in range(60):
        _mission(f"websocket mission {i}", "websocket reconnect")
    calls: list = []
    monkeypatch.setattr(
        review, "_TRANSPORT", _seq_transport([{"answer": "none", "matches": []}], calls)
    )
    asyncio.run(pulse_chat.ask("websocket"))
    stage1 = json.loads(calls[0]["messages"][-1]["content"])["catalog"]
    kinds = [e["kind"] for e in stage1]
    assert kinds.count("mission") == pulse_chat.MISSION_SLICE_MAX
    assert kinds.count("session") == 3


def test_an_operator_prompt_that_never_mentions_missions_still_works(configured_ai, monkeypatch):
    # An edited ask_catalog written before #1069 knows nothing about missions. The contract is
    # unchanged, so a reply naming only sessions is a normal answer with no mission matches.
    prompts.set_value("ask_catalog", "Find sessions. Reply with JSON {answer, matches}.")
    _setup(monkeypatch, _sessions(2))
    _mission("A mission", "x")
    session = f"claude:{_uuid(0)}"
    payload = {"answer": "p0", "matches": [{"id": session, "why": "w"}]}
    monkeypatch.setattr(review, "_TRANSPORT", _seq_transport([payload, payload]))
    monkeypatch.setattr(review, "gather_input", lambda key, n: ("tail", "fp"))
    result = asyncio.run(pulse_chat.ask("which?"))
    assert [m["id"] for m in result["matches"]] == [session]
    assert result["mission_matches"] == []


# ---- the grounding floor (#2016) ------------------------------------------------------

# The query HQ sent in #1950's acceptance run, and the mission it wrongly got back: the real
# row's title and instruction shape (no brief, the mailbox named, "mission" as a plain word).
UNRELATED_QUERY = "marcus-privat application mission"
EMAIL_TITLE = "Please setup email and email box, marcus@battlelabos.com through romania"
EMAIL_INSTRUCTION = (
    "Please setup email and email box, marcus@battlelabos.com through romania, ro-stalward "
    "and postal email edge server. Store the mailbox settings in nextcloud. Give me a "
    "comprehensive report in mission at the end."
)


def _collect(query: str, history: object = None, **kw) -> list[dict]:
    async def run():
        return [ev async for ev in pulse_chat.ask_events(query, history, **kw)]

    return asyncio.run(run())


def _names_mission(events: list[dict], mid: str) -> bool:
    return any(
        any(m["id"] == mid for m in ev.get("mission_matches") or [])
        for ev in events
        if ev["type"] == "answer"
    )


def test_kind_nouns_are_stopwords_so_the_query_is_two_grounding_tokens():
    # Hermes on the issue: the hyphenated project id stays ONE token; "mission" grounds nothing.
    assert pulse_chat._keywords(UNRELATED_QUERY) == ["marcus-privat", "application"]
    assert pulse_chat._keywords("which agent runs the zephyr project?") == [
        "runs",
        "zephyr",
    ]
    # With nothing to check against, the model's picks stand untouched (the same list object).
    matches = [("claude:x", "w")]
    assert pulse_chat._grounded(matches, {}, []) is matches


def test_a_mission_sharing_only_a_kind_noun_with_the_query_is_no_match(configured_ai, monkeypatch):
    """#1950's run: the email mission reached the slice because its instruction says "mission",
    the model picked it as the best of what it had, and Stage 2 confirmed it against its own
    text. Scripted here to pick it in BOTH stages — the floor never lets Stage 2 see it."""
    _setup(monkeypatch, _sessions(2))
    mid = _mission(EMAIL_TITLE, EMAIL_INSTRUCTION)
    key = f"mission:{mid}"
    calls: list = []
    pick = {
        "answer": "The email-setup mission covers marcus@battlelabos.com.",
        "matches": [{"id": key, "why": "Mission brief directly covers marcus@ email setup"}],
    }
    monkeypatch.setattr(review, "_TRANSPORT", _seq_transport([pick, pick], calls))
    monkeypatch.setattr(review, "gather_input", lambda key, n: ("tail", "fp"))

    events = _collect(UNRELATED_QUERY)

    # The mission was in the slice as padding, not a hit — and the slice was still sent.
    (catalog,) = [e for e in events if e["type"] == "progress" and e["step"] == "catalog"]
    assert catalog["missions"] == 1 and catalog["mission_hits"] == 0
    assert any(e["id"] == key for e in json.loads(calls[0]["messages"][-1]["content"])["catalog"])
    # One call: the floor emptied the Stage-1 list, so there was no provisional answer, no
    # content step and no Stage 2. No emitted answer names the mission, and the answer is the
    # server's line, not the model's sentence about the candidate it picked.
    assert len(calls) == 1
    assert [e["type"] for e in events] == ["progress", "answer"]
    assert not _names_mission(events, mid)
    final = events[-1]
    assert final["final"] is True and final["stage"] == "catalog"
    assert final["matches"] == [] and final["mission_matches"] == []
    assert final["answer"] == pulse_chat.NO_MATCH_ANSWER
    assert "marcus@" not in final["answer"]


def test_a_genuine_match_passes_the_floor_on_a_project_or_topic_word(configured_ai, monkeypatch):
    """Recall check: the same query, a session whose cwd carries the project id and a mission
    whose brief carries the topic word. Both grounded, both returned, Stage 2 runs as before."""
    sessions = _sessions(2)
    sessions[0].cwd = "/home/m/marcus-privat"
    _setup(monkeypatch, sessions)
    session = f"claude:{_uuid(0)}"
    mid = _mission("Job hunt", "write the application for the platform engineer position")
    key = f"mission:{mid}"
    calls: list = []
    pick = {
        "answer": "Your job-application mission; p0 drafted in marcus-privat.",
        "matches": [{"id": key, "why": "brief: the application"}, {"id": session, "why": "cwd"}],
    }
    monkeypatch.setattr(review, "_TRANSPORT", _seq_transport([pick, pick], calls))
    monkeypatch.setattr(review, "gather_input", lambda key, n: ("user: cover letter", "fp"))

    result = asyncio.run(pulse_chat.ask(UNRELATED_QUERY))
    assert len(calls) == 2 and result["stage"] == "content"
    assert [m["id"] for m in result["matches"]] == [session]
    assert [m["id"] for m in result["mission_matches"]] == [mid]
    assert result["answer"] == pick["answer"]


def test_a_stage2_failure_keeps_only_the_grounded_stage1_matches(configured_ai, monkeypatch):
    """The Stage-1 fallback (and the provisional answer before it) carries the grounded session
    and never the unrelated mission the model listed beside it. A partial drop keeps the model's
    answer: the remaining match is what it is about."""
    sessions = _sessions(2)
    sessions[0].first_user_message = "the websocket reconnect bug"
    _setup(monkeypatch, sessions)
    session = f"claude:{_uuid(0)}"
    mid = _mission(EMAIL_TITLE, EMAIL_INSTRUCTION)
    key = f"mission:{mid}"
    seen = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["n"] += 1
        if seen["n"] == 1:
            payload = {
                "answer": "p0 and the email mission",
                "matches": [{"id": session, "why": "title"}, {"id": key, "why": "mailbox"}],
            }
            return httpx.Response(
                200, json={"choices": [{"message": {"content": json.dumps(payload)}}]}
            )
        return httpx.Response(502)

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(review, "gather_input", lambda key, n: ("tail", "fp"))

    events = _collect("which session had the websocket reconnect bug?")
    assert seen["n"] == 2
    assert not _names_mission(events, mid)
    provisional, final = [e for e in events if e["type"] == "answer"]
    assert provisional["final"] is False and final["final"] is True
    for ev in (provisional, final):
        assert [m["id"] for m in ev["matches"]] == [session]
        assert ev["mission_matches"] == []
        assert ev["stage"] == "catalog"
        # The model's line named the dropped mission, so it went with it: the neutral server
        # line stands in, in the provisional answer and in the fallback alike.
        assert ev["answer"] == pulse_chat.GROUNDED_ANSWER
        assert "email" not in ev["answer"]
    # Stage 2 was asked about the grounded session only.
    (content,) = [e for e in events if e["type"] == "progress" and e["step"] == "content"]
    assert content["candidates"] == 1


def test_a_mixed_reply_naming_the_dropped_mission_gets_stage2s_line(configured_ai, monkeypatch):
    """Stage 1 lists a grounded session and the unrelated mission and its prose recommends the
    mission. Stage 2 sees the survivor only, so its line is the final answer; nothing emitted —
    rows or prose — carries the mission."""
    sessions = _sessions(2)
    sessions[0].first_user_message = "the websocket reconnect bug"
    _setup(monkeypatch, sessions)
    session = f"claude:{_uuid(0)}"
    mid = _mission(EMAIL_TITLE, EMAIL_INSTRUCTION)
    calls: list = []
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _seq_transport(
            [
                {
                    "answer": "Start with the email-setup mission; p0 is related.",
                    "matches": [
                        {"id": f"mission:{mid}", "why": "covers marcus@ email setup"},
                        {"id": session, "why": "title"},
                    ],
                },
                {"answer": "p0 fixed the reconnect bug.", "matches": [{"id": session, "why": "t"}]},
            ],
            calls,
        ),
    )
    monkeypatch.setattr(review, "gather_input", lambda key, n: ("user: reconnect", "fp"))

    events = _collect("which session had the websocket reconnect bug?")
    assert len(calls) == 2
    assert not _names_mission(events, mid)
    provisional, final = [e for e in events if e["type"] == "answer"]
    assert provisional["answer"] == pulse_chat.GROUNDED_ANSWER
    assert final["answer"] == "p0 fixed the reconnect bug." and final["stage"] == "content"
    assert [m["id"] for m in final["matches"]] == [session] and final["mission_matches"] == []
    for ev in (provisional, final):
        assert "email" not in ev["answer"]
    # Stage 2 never saw the dropped mission.
    cands = json.loads(calls[1]["messages"][-1]["content"])["candidates"]
    assert [c["id"] for c in cands] == [session]


def test_stage2_keeping_nothing_and_saying_nothing_is_the_no_match_line(configured_ai, monkeypatch):
    """Hermes on the issue: a verify that rejects every survivor with an empty answer must not
    fall back to the Stage-1 prose about the candidates it has just rejected."""
    sessions = _sessions(2)
    sessions[0].first_user_message = "the websocket reconnect bug"
    _setup(monkeypatch, sessions)
    session = f"claude:{_uuid(0)}"
    calls: list = []
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _seq_transport(
            [
                {"answer": "p0 looks like it.", "matches": [{"id": session, "why": "title"}]},
                {"answer": "", "matches": []},
            ],
            calls,
        ),
    )
    monkeypatch.setattr(review, "gather_input", lambda key, n: ("user: unrelated", "fp"))
    result = asyncio.run(pulse_chat.ask("which session had the websocket reconnect bug?"))
    assert len(calls) == 2
    assert result["stage"] == "content" and result["matches"] == []
    assert result["answer"] == pulse_chat.NO_MATCH_ANSWER


def test_a_follow_up_is_grounded_by_the_users_earlier_turns(configured_ai, monkeypatch):
    """ "And the second one?" shares no word with any card; the question it follows does."""
    sessions = _sessions(3)
    sessions[1].first_user_message = "websocket reconnect, another attempt"
    _setup(monkeypatch, sessions)
    target = f"claude:{_uuid(1)}"
    pick = {"answer": "p1", "matches": [{"id": target, "why": "the second reconnect session"}]}
    monkeypatch.setattr(review, "_TRANSPORT", _seq_transport([pick]))
    monkeypatch.setattr(
        review, "gather_input", lambda *a: (_ for _ in ()).throw(review.ReviewError("none"))
    )
    history = [
        {"role": "user", "content": "which sessions fixed the websocket reconnect?"},
        {"role": "assistant", "content": "p0 and p1."},
    ]
    with_history = asyncio.run(pulse_chat.ask("and the second one?", history))
    assert [m["id"] for m in with_history["matches"]] == [target]
    # The same pick with no history to ground it is refused: "second" is in no card.
    alone = asyncio.run(pulse_chat.ask("and the second one?"))
    assert alone["matches"] == [] and alone["answer"] == pulse_chat.NO_MATCH_ANSWER


def test_a_scoped_ask_is_never_filtered(configured_ai, monkeypatch):
    """A mission's own question (#1213): the operator chose the candidates, so "what is the
    decision?" keeps the model's pick even though no card says "decision"."""
    _setup(monkeypatch, _sessions(2))
    mid = _mission("Scoped", "x")
    target = f"claude:{_uuid(0)}"
    pick = {"answer": "p0 decided", "matches": [{"id": target, "why": "the decision"}]}
    monkeypatch.setattr(review, "_TRANSPORT", _seq_transport([pick]))
    monkeypatch.setattr(
        review, "gather_input", lambda *a: (_ for _ in ()).throw(review.ReviewError("none"))
    )
    result = asyncio.run(pulse_chat.ask("what is the decision?", scope=({target}, mid)))
    assert [m["id"] for m in result["matches"]] == [target]
    assert result["answer"] == "p0 decided"


def test_hq_find_sessions_reports_zero_rows_for_the_unrelated_mission(configured_ai, monkeypatch):
    """HQ's wrapper, through the real pipeline: the tool result HQ reads has no mission row,
    the turn learns no mission id (so no later tool can act on it), and the answer is the
    server's no-match line."""
    import uuid

    from agent_sessions import ask_orchestrator

    _setup(monkeypatch, _sessions(2))
    mid = _mission(EMAIL_TITLE, EMAIL_INSTRUCTION)
    pick = {
        "answer": "The email-setup mission.",
        "matches": [{"id": f"mission:{mid}", "why": "covers marcus@ email setup"}],
    }
    monkeypatch.setattr(review, "_TRANSPORT", _seq_transport([pick, pick]))
    turn = ask_orchestrator._Turn(
        cid="ask_" + "0" * 32,
        turn_id=str(uuid.uuid4()),
        question=UNRELATED_QUERY,
        working_keys=set(),
        progress=lambda ev: None,
    )
    out, meta = asyncio.run(ask_orchestrator._find_sessions(turn, {"query": UNRELATED_QUERY}))
    assert out == {"answer": pulse_chat.NO_MATCH_ANSWER, "sessions": [], "missions": []}
    assert meta == {"count": 0, "keys": [], "missions": []}
    assert turn.mission_ids == set() and turn.mission_matches == {}


def test_malformed_model_output_raises_review_error(configured_ai, monkeypatch):
    _setup(monkeypatch, _sessions(1))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json at all"}}]})

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
    with pytest.raises(review.ReviewError):
        asyncio.run(pulse_chat.ask("hello?"))


def test_stage2_skips_broken_candidate_keeps_others(configured_ai, monkeypatch):
    _setup(monkeypatch, _sessions(3))
    broken, good = f"claude:{_uuid(0)}", f"claude:{_uuid(1)}"
    calls: list = []
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _seq_transport(
            [
                {
                    "answer": "cat",
                    "matches": [{"id": broken, "why": "b"}, {"id": good, "why": "g"}],
                },
                {"answer": "refined", "matches": [{"id": good, "why": "confirmed"}]},
            ],
            calls,
        ),
    )

    def gather(key, n):
        if key == broken:
            raise review.ReviewError("nothing to review")
        return ("user: tail", "fp")

    monkeypatch.setattr(review, "gather_input", gather)
    result = asyncio.run(pulse_chat.ask("which?"))
    # The broken candidate never reached Stage 2, the good one did — and the ask succeeded.
    stage2_user = json.loads(calls[1]["messages"][-1]["content"])
    assert [c["id"] for c in stage2_user["candidates"]] == [good]
    assert result["stage"] == "content"
    assert [m["id"] for m in result["matches"]] == [good]


def test_total_stage2_failure_degrades_to_stage1(configured_ai, monkeypatch):
    _setup(monkeypatch, _sessions(2))
    target = f"claude:{_uuid(0)}"
    seen = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["n"] += 1
        if seen["n"] == 1:
            payload = {"answer": "stage1 answer", "matches": [{"id": target, "why": "w"}]}
            return httpx.Response(
                200, json={"choices": [{"message": {"content": json.dumps(payload)}}]}
            )
        return httpx.Response(502)  # Stage 2 call fails entirely

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(review, "gather_input", lambda key, n: ("tail", "fp"))
    result = asyncio.run(pulse_chat.ask("which?"))
    assert result["stage"] == "catalog"  # degraded, not errored
    assert result["answer"] == "stage1 answer"
    assert [m["id"] for m in result["matches"]] == [target]


def test_history_is_clamped(configured_ai, monkeypatch):
    _setup(monkeypatch, _sessions(1))
    calls: list = []
    monkeypatch.setattr(
        review, "_TRANSPORT", _seq_transport([{"answer": "a", "matches": []}], calls)
    )
    history = [{"role": "user", "content": f"turn {i} " + "x" * 5000} for i in range(20)] + [
        {"role": "tool", "content": "dropped"},
        {"bad": "shape"},
        "junk",
    ]
    asyncio.run(pulse_chat.ask("q?", history))
    messages = calls[0]["messages"]
    replayed = messages[1:-1]  # between the system prompt and the catalog user message
    assert len(replayed) == pulse_chat.HISTORY_TURNS_MAX
    assert all(len(m["content"]) <= pulse_chat.HISTORY_TURN_CHARS_MAX for m in replayed)
    assert all(m["role"] in ("user", "assistant") for m in replayed)
    # The newest turns won the clamp.
    assert replayed[-1]["content"].startswith("turn 19")


# ---- streamed ask events (#1171; HTTP routes retired in #2179) -------------------------


def _two_stage(monkeypatch, calls: list) -> str:
    sessions = _sessions(3)
    sessions[1].first_user_message = "reconnect storm"  # grounds the pick (#2016)
    _setup(monkeypatch, sessions)
    target = f"claude:{_uuid(1)}"
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _seq_transport(
            [
                {"answer": "first look", "matches": [{"id": target, "why": "title"}]},
                {"answer": "confirmed", "matches": [{"id": target, "why": "transcript"}]},
            ],
            calls,
        ),
    )
    monkeypatch.setattr(review, "gather_input", lambda key, n: ("user: reconnect", "fp"))
    return target


def test_events_stream_the_stage1_answer_before_stage2_runs(configured_ai, monkeypatch):
    calls: list = []
    target = _two_stage(monkeypatch, calls)

    async def collect():
        seen = []
        async for ev in pulse_chat.ask_events("reconnect?"):
            # How many model calls had been made when THIS event was handed out.
            seen.append((ev, len(calls)))
        return seen

    seen = asyncio.run(collect())
    kinds = [(ev["type"], ev.get("step") or ev.get("final")) for ev, _ in seen]
    assert kinds == [
        ("progress", "catalog"),
        ("answer", False),
        ("progress", "content"),
        ("answer", True),
    ]
    (catalog, n0), (early, n1), (content, n2), (final, n3) = seen
    assert n0 == 0 and catalog["sessions"] == 3
    # The whole point: the Stage-1 answer is out after ONE call, not after both.
    assert n1 == 1 and early["answer"] == "first look" and early["stage"] == "catalog"
    assert early["matches"][0]["id"] == target
    assert content["candidates"] == 1
    assert n3 == 2 and final["answer"] == "confirmed" and final["stage"] == "content"


def test_ask_is_the_last_event_without_its_event_fields(configured_ai, monkeypatch):
    _two_stage(monkeypatch, [])
    result = asyncio.run(pulse_chat.ask("reconnect?"))
    assert result["answer"] == "confirmed"
    assert "type" not in result and "final" not in result
    assert set(result) == {"answer", "matches", "mission_matches", "stage", "configured"}


def test_no_match_streams_one_final_answer_and_no_content_step(configured_ai, monkeypatch):
    _setup(monkeypatch, _sessions(2))
    monkeypatch.setattr(
        review, "_TRANSPORT", _seq_transport([{"answer": "Nothing like that.", "matches": []}])
    )

    async def collect():
        return [ev async for ev in pulse_chat.ask_events("q")]

    evs = asyncio.run(collect())
    assert [e["type"] for e in evs] == ["progress", "answer"]
    assert evs[-1]["final"] is True and evs[-1]["stage"] == "catalog"


def test_total_stage2_failure_streams_a_final_stage1_answer(configured_ai, monkeypatch):
    """The Stage-1 fallback holds for the stream too, and it is FINAL — the client's "checking
    transcripts…" must clear on it rather than wait for an answer that is not coming."""
    _setup(monkeypatch, _sessions(3))
    target = f"claude:{_uuid(1)}"
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _seq_transport([{"answer": "first look", "matches": [{"id": target, "why": "t"}]}]),
    )

    def boom(key, n):
        raise review.ReviewError("nothing to review")

    monkeypatch.setattr(review, "gather_input", boom)

    async def collect():
        return [ev async for ev in pulse_chat.ask_events("q")]

    evs = asyncio.run(collect())
    finals = [e for e in evs if e["type"] == "answer" and e["final"]]
    assert len(finals) == 1 and evs[-1] is finals[0]
    assert finals[0]["answer"] == "first look" and finals[0]["stage"] == "catalog"
    assert finals[0]["matches"][0]["id"] == target


def test_hqs_trusted_history_passes_whole_through_both_stages(configured_ai, monkeypatch):
    """#2137 P2 (Hermes on #2160): HQ's own stored conversation, already within its budget, is
    replayed whole by the catalog AND the refine stage when HQ passes its limits; the transient
    Ask's client history keeps the default clamp (`test_history_is_clamped`)."""
    _setup(monkeypatch, _sessions(1))
    target = f"claude:{_uuid(0)}"
    calls: list = []
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _seq_transport(
            [
                {"answer": "stage1", "matches": [{"id": target, "why": "w"}]},
                {"answer": "stage2", "matches": [{"id": target, "why": "w"}]},
            ],
            calls,
        ),
    )
    monkeypatch.setattr(review, "gather_input", lambda key, n: ("tail", "fp"))
    history = []
    for i in range(12):
        history += [
            {"role": "user", "content": f"p0 turn {i} " + "x" * 3000},  # grounded on p0
            {"role": "assistant", "content": f"answer {i}"},
        ]
    result = asyncio.run(pulse_chat.ask("q?", history, turns_max=None, chars_max=8000))
    assert result["stage"] == "content"
    for body in calls[:2]:  # the catalog request, then the refine request
        replayed = [m for m in body["messages"][1:-1] if m["role"] in ("user", "assistant")]
        assert len(replayed) == 24
        assert replayed[0]["content"].startswith("p0 turn 0") and len(replayed[0]["content"]) > 3000


@pytest.mark.parametrize(
    "refine, kept",
    [
        ("accepted", [111, 222]),  # the refined answer is the refine request's
        ("cut_off", [111]),  # rejected: the catalog answer stands, and so does its measurement
        ("invalid", [111]),
        ("failed", [111]),
        # Valid, but no answer of its own: the catalog line is kept, and so is its measurement.
        ("empty_answer", [111]),
    ],
)
def test_only_an_accepted_stage_is_measured(configured_ai, monkeypatch, refine, kept):
    """#2137 P2 (Hermes on #2160): HQ's context meter measures the request whose answer the ask
    KEEPS. A refine that is cut off, invalid or failed is rejected and the catalog answer stands:
    its measurement must stand too."""
    _setup(monkeypatch, _sessions(1))
    target = f"claude:{_uuid(0)}"
    n = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["i"] += 1
        if n["i"] == 1:
            payload = {"answer": "catalog answer", "matches": [{"id": target, "why": "w"}]}
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps(payload)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 111}})  # fmt: skip
        if refine == "failed":
            return httpx.Response(502)
        refined = json.dumps({"answer": "refined", "matches": [{"id": target, "why": "w"}]})
        empty = json.dumps({"answer": "", "matches": [{"id": target, "why": "w"}]})
        content = {"accepted": refined, "cut_off": '{"answer": "ref', "invalid": "not json",
                   "empty_answer": empty}[refine]  # fmt: skip
        finish = "length" if refine == "cut_off" else "stop"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 222 if refine == "accepted" else 999}})  # fmt: skip

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(review, "gather_input", lambda key, n: ("tail", "fp"))

    async def go():
        with pulse_chat.measure_kept() as got:
            result = await pulse_chat.ask("which?")
        return result, got

    result, got = asyncio.run(go())
    assert [g["tokens"] for g in got] == kept
    assert result["answer"] == ("refined" if refine == "accepted" else "catalog answer")
