"""The structured session assessment (#1020, Stage 2 of #1018).

Pinned here:

* **The contract** (`assessment.py`): versioned, bounded by construction, truncation never
  silent, evidence quotes kept only when they occur in the section they name, read-side
  re-validation of whatever a store holds.
* **Freshness is per source** (`assessment.project`): activity after the sources were read, a
  later review without a record, or a failed refresh each make a record STALE — kept whole, never
  erased, never labelled current.
* **The review writes it from the call it already makes**, keeps the last good one when a reply
  carries none, and records a failed refresh without touching any result.
* **The review's input keeps the user's messages** that fall outside the transcript tail — the
  corpus's middle-of-history correction reaches the model — while a short session's input and
  fingerprint are byte-identical to before, and a tool result never reads as a user message.
* **The decision pass reads explicit fields**, not a chronological prefix of the recap.
* **API projections**: the one-session lookup carries the projection, the list keeps its shape.
* **The PR #1357 review findings**: harness-injected Claude records are never operator messages,
  earlier-message membership is by turn position, a bad assessment keeps the summary, a stale
  record re-arms the decision pass, a reader-cut history never reports complete coverage, and a
  trivial quote or a "None." is not evidence.

Shape tests only. Nothing here measures model accuracy.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_sessions import assessment, metadata, orchestrator, prefs, review, transcript
from agent_sessions.main import create_app

SECRET = "sk-assessment-test-0001"  # noqa: S105 — test fixture value
BASE = "https://ai.test/v1"
KEY = "claude:00000000-0000-4000-8000-000000000001"
CORPUS = json.loads((Path(__file__).parent / "fixtures" / "assessment" / "corpus.json").read_text())
CASES = {c["id"]: c for c in CORPUS["cases"]}

FULL = {
    "task": "Implement the parser fix",
    "constraints": ["Keep production deployment on hold"],
    "current_state": "Implementation verified locally; CI blocked",
    "completed": ["Narrow fix implemented"],
    "remaining": ["Green CI"],
    "blocker": "CI failed twice with the same connection cause",
    "decision_needed": "When should CI be retried?",
    "evidence_refs": [],
}


def _record(fields=None, *, fp="fp-1", read_at=100.0, assessed_at=110.0, sources=None):
    norm = assessment.normalize(fields or FULL, sources)
    assert norm is not None
    return assessment.record(
        *norm,
        source_fingerprint=fp,
        source_read_at=read_at,
        latest_source_at=read_at - 5,
        assessed_at=assessed_at,
        coverage={"transcript": "tail", "operator_messages": "complete", "screen": True},
    )


# ---- the contract ------------------------------------------------------------------------------


def test_the_corpus_is_frozen_and_covers_every_named_phase_case():
    """The phase list names eight situations; each must have at least one corpus case."""
    assert len(CASES) == len(CORPUS["cases"]) == 17
    categories = " | ".join(c["category"] for c in CORPUS["cases"])
    for needed in (
        "recap/terminal disagreement",
        "current state after a long chronological opening",
        "changed user direction in the middle",
        "idle-complete vs blocked",
        "stale probe facts",
        "two sessions in one mission",
        "missing transcript",
        "shell screen-only",
    ):
        assert needed in categories, needed
    for c in CORPUS["cases"]:
        assert c["expected"]["must_not"], c["id"]


def test_a_late_failed_attempt_cannot_invalidate_a_concurrent_success(ai_prefs, monkeypatch):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def handler(request):
            nonlocal calls
            system = json.loads(request.content)["messages"][0]["content"]
            if "CHRONOLOGICAL" not in system:
                calls += 1
                if calls == 1:
                    entered.set()
                    await release.wait()
                    return httpx.Response(503)
            return _reply(FULL).handler(request)

        monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
        with _Session(TURNS, SCREEN):
            older = asyncio.create_task(review.run_review(KEY, {}))
            await asyncio.wait_for(entered.wait(), 5)
            try:
                fresh = await asyncio.wait_for(review.run_review(KEY, {}), 5)
                assert fresh["assessment"]["status"] == "current"
            finally:
                release.set()
            with pytest.raises(review.ReviewError):
                await older
        after = metadata.get(KEY)
        assert after.review_failed_at is None
        assert assessment.project(after.ai_assessment)["status"] == "current"

    asyncio.run(scenario())


def test_operator_evidence_is_limited_to_fragments_actually_sent():
    cut = "Keep the omitted production constraint out of the evidence."
    omitted = "This whole middle instruction was not sent to the reviewer."
    turns = [transcript.Turn("user", "Opening instruction retained. " + "x" * 800 + cut, "text")]
    turns += [transcript.Turn("user", omitted + str(i) + "y" * 130, "text") for i in range(10)]
    turns += [transcript.Turn("assistant", "padding " * 4000, "text")]
    turns += [transcript.Turn("user", "Newest instruction retained.", "text")]
    with _Session(turns):
        info = {}
        text, _ = review.gather_review_input(KEY, 6000, {}, info=info)
    omitted_quote = omitted + "4"
    assert cut not in text and omitted_quote not in text
    assert info["coverage"]["operator_messages"] == "partial"
    quotes = [cut, omitted_quote, "Opening instruction retained.", "Newest instruction retained."]
    fields, notes = assessment.normalize(
        {**FULL, "evidence_refs": [{"source": "operator", "quote": q} for q in quotes]},
        info["sources"],
    )
    assert [r["quote"] for r in fields["evidence_refs"]] == quotes[2:]
    assert notes["refs_dropped"] == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_read_at", 10**400),
        ("assessed_at", 10**400),
        ("latest_source_at", 10**400),
        ("coverage", {"transcript": []}),
        ("coverage", {"operator_messages": {}}),
    ],
)
def test_malformed_assessments_do_not_break_other_metadata_rows(field, value):
    bad = {**_record(), field: value}
    path = metadata._default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    other = "claude:00000000-0000-4000-8000-000000000002"
    path.write_text(json.dumps({KEY: {"ai_assessment": bad}, other: {"title": "Other session"}}))
    assert metadata.load()[other].title == "Other session"
    assert metadata.patch(other, title="Still writable").title == "Still writable"
    returned = metadata.patch(KEY, sticky=True)
    assert returned.ai_assessment == assessment.from_stored(bad)


@pytest.mark.parametrize("bad", [10**400, float("inf"), float("nan"), True, [], {}])
def test_invalid_failure_timestamps_are_ignored_on_read_and_refused_on_write(bad):
    path = metadata._default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({KEY: {"review_failed_at": bad}}))
    assert metadata.get(KEY).review_failed_at is None
    with pytest.raises(ValueError, match="review_failed_at"):
        metadata.patch(KEY, review_failed_at=bad)


@pytest.mark.parametrize(
    "decoy",
    [
        '{"debug":{"summary":"old state","title":"x","intervention_required":false},',
        '{"items":[{"summary":"old state","intervention_required":false}],',
        '{"summary":"real summary","debug":{"intervention_required":false},',
    ],
)
def test_nested_decoys_never_salvage_a_review_verdict(ai_prefs, monkeypatch, decoy):
    monkeypatch.setattr(
        review, "_TRANSPORT", _raw_reply(decoy + '"assessment":{"current_state":"cut', "length")
    )
    with _Session(TURNS, SCREEN), pytest.raises(review.ReviewError):
        asyncio.run(review.run_review(KEY, {}))
    assert metadata.get(KEY).reviewed_at is None


@pytest.mark.parametrize("finish", ["length", "stop"])
@pytest.mark.parametrize(
    "suffix",
    [
        ',,"intervention_required":true,"assessment":{"current_state":"cut',
        ',oops,"intervention_required":true,"assessment":{"current_state":"cut',
        ',"debug":],"intervention_required":true,"assessment":{"current_state":"cut',
        ',"reason":"bad\\q","intervention_required":true,"assessment":{"current_state":"cut',
        ",}",
    ],
)
def test_malformed_prefix_never_overwrites_the_last_good_verdict(
    ai_prefs, monkeypatch, suffix, finish
):
    before = metadata.patch(
        KEY,
        ai_summary="Last good summary",
        ai_title="Last good title",
        intervention_required=True,
        intervention_reason="Wait for the operator",
        ai_assessment=_record(),
        reviewed_at=1.0,
    )
    core = '{"summary":"new","title":"t","intervention_required":false'
    monkeypatch.setattr(review, "_TRANSPORT", _raw_reply(core + suffix, finish))
    with _Session(TURNS, SCREEN), pytest.raises(review.ReviewError):
        asyncio.run(review.run_review(KEY, {}))
    after = metadata.get(KEY)
    for field in (
        "ai_summary",
        "ai_title",
        "intervention_required",
        "intervention_reason",
        "ai_assessment",
        "reviewed_at",
    ):
        assert getattr(after, field) == getattr(before, field)
    assert after.review_failed_at is not None


def test_truncated_salvage_rejects_an_extra_object_after_the_root():
    content = '{"summary":"new","title":"t","intervention_required":false}'
    with pytest.raises(review.ReviewError, match="truncated"):
        review._salvaged_core(
            content + ' {"intervention_required":true}', review.ReviewError("truncated")
        )


def test_a_record_is_bounded_whatever_the_model_sends():
    huge = "x" * 50_000
    norm = assessment.normalize(
        {
            "task": huge,
            "constraints": [huge] * 40,
            "current_state": huge,
            "completed": [huge] * 40,
            "remaining": [huge] * 40,
            "blocker": huge,
            "decision_needed": huge,
            "evidence_refs": [{"source": "screen", "quote": "x" * 50}] * 40,
        },
        {"screen": huge},
    )
    fields, notes = norm
    text = sum(len(v) for v in fields.values() if isinstance(v, str))
    text += sum(len(i) for k in ("constraints", "completed", "remaining") for i in fields[k])
    text += sum(len(r["quote"]) for r in fields["evidence_refs"])
    assert text <= assessment.TOTAL_MAX
    assert len(fields["completed"]) == assessment.ITEMS_MAX
    assert len(fields["evidence_refs"]) == assessment.REFS_MAX
    # Never silent: every field that was cut is named, and a cut ends visibly.
    assert set(notes["truncated_fields"]) >= {"task", "current_state", "blocker", "completed"}
    assert fields["blocker"].endswith(assessment.ELLIPSIS)
    assert len(fields["blocker"]) == assessment.BLOCKER_MAX


def test_the_current_blocker_survives_any_amount_of_list_content():
    """The decision-bearing fields are scalars with their own caps: no list can crowd them out."""
    raw = {
        "current_state": "CI blocked",
        "completed": [f"step {i} " * 30 for i in range(200)],
        "remaining": [f"open {i} " * 30 for i in range(200)],
        "blocker": "Repeated CI connection failure under measured host load",
        "decision_needed": "When to retry CI",
    }
    fields, notes = assessment.normalize(raw)
    assert fields["blocker"] == raw["blocker"]
    assert fields["decision_needed"] == raw["decision_needed"]
    assert "blocker" not in notes["truncated_fields"]


def test_no_current_state_is_no_record_and_null_words_are_unknown():
    assert assessment.normalize({"blocker": "x"}) is None
    assert assessment.normalize({"current_state": "  "}) is None
    assert assessment.normalize("not an object") is None
    fields, _ = assessment.normalize(
        {"current_state": "Idle", "blocker": "none", "decision_needed": "null"}
    )
    assert fields["blocker"] is None and fields["decision_needed"] is None


def test_a_quote_is_kept_only_where_the_named_section_holds_it():
    sources = {
        "transcript": "assistant: Implemented the narrow fix; local checks passed.",
        "screen": "Two failures with the same cause.",
        "operator": "user: Keep production deployment on hold.",
    }
    raw = {
        "current_state": "Blocked",
        "evidence_refs": [
            {"source": "screen", "quote": "two failures with the   SAME cause"},  # kept
            {"source": "operator", "quote": "local checks passed"},  # wrong section
            {"source": "transcript", "quote": "deployed to production"},  # invented
            {"source": "url", "quote": "https://example.invalid/x"},  # not a section
            {"source": "screen", "quote": "Two failures" + " pad" * 80},  # too long: dropped
        ],
    }
    fields, notes = assessment.normalize(raw, sources)
    assert fields["evidence_refs"] == [
        {"source": "screen", "quote": "two failures with the SAME cause"}
    ]
    assert notes["refs_dropped"] == 4


def test_a_stored_record_is_revalidated_on_read():
    rec = _record()
    assert assessment.from_stored(rec) == rec
    assert assessment.from_stored({**rec, "schema_version": 2}) is None
    assert assessment.from_stored({**rec, "source_fingerprint": ""}) is None
    assert assessment.from_stored({**rec, "current_state": None}) is None
    # A hand-edited row past the caps reads clamped, and the clamp is recorded, not silent.
    edited = assessment.from_stored({**rec, "blocker": "b" * 10_000})
    assert len(edited["blocker"]) == assessment.BLOCKER_MAX
    assert "blocker" in edited["coverage"]["truncated_fields"]


# ---- freshness, per source ---------------------------------------------------------------------


def test_projection_status_is_decided_per_source():
    rec = _record()
    assert assessment.project(None)["status"] == "missing"
    cur = assessment.project(rec, review_fingerprint="fp-1", last_activity=90.0)
    assert cur["status"] == "current" and cur["stale_reasons"] == []
    assert cur["blocker"] == FULL["blocker"]
    assert cur["provenance"] == assessment.PROVENANCE

    moved = assessment.project(rec, review_fingerprint="fp-1", last_activity=150.0)
    assert moved["status"] == "stale"
    assert moved["stale_reasons"] == ["session_activity_since_assessment"]
    # Stale is labelled, never erased: the blocker that may still hold is still there.
    assert moved["blocker"] == FULL["blocker"]

    newer = assessment.project(rec, review_fingerprint="fp-2")
    assert newer["stale_reasons"] == ["newer_review_without_assessment"]

    failed = assessment.project(rec, review_fingerprint="fp-1", review_failed_at=200.0)
    assert failed["status"] == "stale" and failed["stale_reasons"] == ["refresh_failed"]
    assert failed["refresh_failed_at"] == 200.0
    # A failure from BEFORE the record does not stale it: this record is the newer news.
    older = assessment.project(rec, review_fingerprint="fp-1", review_failed_at=50.0)
    assert older["status"] == "current"


# ---- the review writes it ----------------------------------------------------------------------


@pytest.fixture
def ai_prefs(monkeypatch, tmp_path):
    monkeypatch.setattr(review, "_TRANSPORT", None)
    prefs.set_ai_review({"base_url": BASE, "api_key": SECRET, "model": "test-model"})
    return tmp_path


def _turns_of(case: dict) -> list:
    filler = case["sources"].get("filler") or {}
    out = []
    for role, kind, text in case["sources"]["turns"] or []:
        if text in filler:
            text = "\n".join([filler[text]["line"]] * int(filler[text]["repeat"]))
        out.append(transcript.Turn(role, text, kind, ts=1_800_000_000.0 + len(out)))
    return out


class _Session:
    """Replace the review's source readers with synthetic content (no store, no ring)."""

    def __init__(self, turns, screen="", key=KEY):
        self.turns, self.screen, self.key = turns, screen, key

    def __enter__(self):
        self._p = [
            patch.object(review, "_physical", return_value=self.key),
            patch.object(review, "_turns", side_effect=lambda *a, **k: list(self.turns)),
            patch.object(
                review.scrollback,
                "live_tail_text",
                side_effect=lambda _k, n: self.screen[-n:] if n > 0 else "",
            ),
            patch.object(review, "_pending_draft_section", return_value=""),
            patch.object(review, "_session_context", return_value=""),
        ]
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._p):
            p.stop()


def _reply(assessment_obj=None, *, status=200, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(json.loads(request.content))
        system = json.loads(request.content)["messages"][0]["content"]
        if "CHRONOLOGICAL" in system:
            content = {"recap": "Started.\nNow blocked on CI."}
        else:
            content = {
                "summary": "CI blocked",
                "title": "Retry CI",
                "intervention_required": True,
                "reason": "needs a retry decision",
            }
            if assessment_obj is not None:
                content["assessment"] = assessment_obj
        return httpx.Response(
            status, json={"choices": [{"message": {"content": json.dumps(content)}}]}
        )

    return httpx.MockTransport(handler)


SCREEN = "Two failures with the same cause. Awaiting the operator decision on when to retry."
TURNS = [
    transcript.Turn("user", "Fix the parser and keep deployment on hold.", "text", ts=1000.0),
    transcript.Turn("assistant", "Fixed; CI failed twice with one cause.", "text", ts=1001.0),
]


def test_the_review_stores_the_assessment_from_the_call_it_already_makes(ai_prefs, monkeypatch):
    calls: list = []
    obj = {
        **FULL,
        "evidence_refs": [
            {"source": "screen", "quote": "Awaiting the operator decision on when to retry"},
            {"source": "operator", "quote": "keep deployment on hold"},
        ],
    }
    monkeypatch.setattr(review, "_TRANSPORT", _reply(obj, calls=calls))
    with _Session(TURNS, SCREEN):
        out = asyncio.run(review.run_review(KEY, {}))
    # One tail call + one recap call: the assessment added no request.
    assert len(calls) == 2
    m = metadata.get(KEY)
    rec = m.ai_assessment
    assert rec["schema_version"] == assessment.SCHEMA_VERSION
    assert rec["source_fingerprint"] == m.review_fingerprint
    assert rec["latest_source_at"] == 1001.0
    assert rec["coverage"]["transcript"] == "complete"
    assert rec["coverage"]["refs_dropped"] == 0 and len(rec["evidence_refs"]) == 2
    assert out["assessment"]["status"] == "current"
    assert out["assessment"]["decision_needed"] == FULL["decision_needed"]
    # The tail prompt on the wire asked for it — whatever the operator's own wording.
    assert calls[0]["messages"][0]["content"].count(review.prompts.ASSESSMENT_CLAUSE) == 1


def test_a_reply_without_an_assessment_keeps_the_last_one_and_it_reads_stale(ai_prefs, monkeypatch):
    monkeypatch.setattr(review, "_TRANSPORT", _reply(FULL))
    with _Session(TURNS, SCREEN):
        asyncio.run(review.run_review(KEY, {}))
    first = metadata.get(KEY).ai_assessment
    monkeypatch.setattr(review, "_TRANSPORT", _reply(None))
    with _Session([*TURNS, transcript.Turn("assistant", "Still blocked.", "text")], SCREEN):
        out = asyncio.run(review.run_review(KEY, {}))
    m = metadata.get(KEY)
    assert m.ai_assessment == first
    assert out["assessment"]["status"] == "stale"
    assert out["assessment"]["stale_reasons"] == ["newer_review_without_assessment"]


def test_a_failed_refresh_keeps_every_result_and_marks_the_assessment_stale(ai_prefs, monkeypatch):
    monkeypatch.setattr(review, "_TRANSPORT", _reply(FULL))
    with _Session(TURNS, SCREEN):
        asyncio.run(review.run_review(KEY, {}))
    before = metadata.get(KEY)
    monkeypatch.setattr(review, "_TRANSPORT", _reply(FULL, status=503))
    with _Session(TURNS, SCREEN), pytest.raises(review.ReviewError):
        asyncio.run(review.run_review(KEY, {}))
    after = metadata.get(KEY)
    assert after.ai_assessment == before.ai_assessment
    assert after.ai_summary == before.ai_summary and after.reviewed_at == before.reviewed_at
    assert after.review_failed_at is not None
    view = assessment.project(
        after.ai_assessment,
        review_fingerprint=after.review_fingerprint,
        review_failed_at=after.review_failed_at,
    )
    assert view["status"] == "stale" and view["stale_reasons"] == ["refresh_failed"]
    assert view["blocker"] == FULL["blocker"]
    # The next success clears the failure stamp.
    monkeypatch.setattr(review, "_TRANSPORT", _reply(FULL))
    with _Session(TURNS, SCREEN):
        asyncio.run(review.run_review(KEY, {}))
    assert metadata.get(KEY).review_failed_at is None


def test_an_insufficient_reply_is_not_a_failed_refresh(ai_prefs, monkeypatch):
    def handler(request):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"insufficient": true}'}}]}
        )

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
    with _Session(TURNS, SCREEN), pytest.raises(review.InsufficientInputError):
        asyncio.run(review.run_review(KEY, {}))
    assert metadata.get(KEY).review_failed_at is None


# ---- the review's input keeps the user's messages ----------------------------------------------


def _gather(case_id: str, budget: int = 24_000):
    case = CASES[case_id]
    info: dict = {}
    with _Session(_turns_of(case), case["sources"].get("screen") or ""):
        review_text, review_fp = review.gather_review_input(KEY, budget, {}, info=info)
        mission_text, mission_fp = review.gather_input(KEY, budget, {})
    return review_text, review_fp, mission_text, mission_fp, info


def test_a_middle_of_history_correction_reaches_the_review():
    """The probe on #1020 measured a 132 000-character history whose 24 000-character review input
    held neither the opening request nor the mid-session hold. Both now reach the review."""
    text, _fp, tail_only, _mfp, info = _gather("middle-history-operator-correction")
    hold = "Keep production deployment on hold until I explicitly release it"
    assert hold in text and "Implement and release the feature" in text
    assert "Ready for the next step" in text
    assert hold not in tail_only  # the transcript tail alone still loses it
    assert len(text) <= 24_000
    assert info["coverage"]["operator_messages"] == "complete"
    assert info["coverage"]["transcript"] == "tail"
    # The section is labelled, and the newer message is presented as superseding.
    assert "## Earlier user messages" in text and "supersedes" in text
    assert text.index(hold) > text.index("Implement and release the feature")


def test_editing_only_the_elided_middle_now_moves_the_fingerprint():
    case = CASES["middle-history-operator-correction"]
    turns = _turns_of(case)
    edited = [
        transcript.Turn(
            t.role, t.text.replace("on hold until", "released now, not on hold until"), t.kind
        )
        for t in turns
    ]
    with _Session(turns, case["sources"]["screen"]):
        _, fp1 = review.gather_review_input(KEY, 24_000, {})
        _, mfp1 = review.gather_input(KEY, 24_000, {})
    with _Session(edited, case["sources"]["screen"]):
        _, fp2 = review.gather_review_input(KEY, 24_000, {})
        _, mfp2 = review.gather_input(KEY, 24_000, {})
    assert fp1 != fp2
    assert mfp1 == mfp2  # the mission path is unchanged in this slice — Stage 2 part 2


def test_a_short_session_reviews_byte_identically_to_before():
    """Every user message already in the tail → no section, so the input and its fingerprint are
    exactly `gather_input`'s, and an existing session is not re-reviewed for this change."""
    for case_id in ("current-ci-blocker-over-old-design", "finished-standalone-is-not-blocked"):
        text, fp, tail_only, mfp, info = _gather(case_id)
        assert (text, fp) == (tail_only, mfp)
        assert info["coverage"]["operator_messages"] == "complete"


def test_a_tool_result_never_reads_as_a_user_message():
    case = CASES["tool-output-is-not-operator-direction"]
    turns = _turns_of(case)
    pad = [transcript.Turn("assistant", "working " * 4000, "text")]
    long_turns = [turns[0], turns[1], *pad, turns[2]]
    with _Session(long_turns, case["sources"]["screen"]):
        text, _ = review.gather_review_input(KEY, 6000, {})
    section = text.split("## Transcript (tail)")[0]
    assert "production deployment remains on hold" in section
    assert "disregard the hold" not in section


def test_the_earlier_section_is_bounded_and_says_what_it_omitted():
    turns = []
    for i in range(300):
        turns.append(transcript.Turn("user", f"direction {i}: " + "detail " * 40, "text"))
        turns.append(transcript.Turn("assistant", "ok " * 400, "text"))
    with _Session(turns, "idle"):
        info: dict = {}
        text, _ = review.gather_review_input(KEY, 24_000, {}, info=info)
    section = text.split("\n\n## Transcript (tail)")[0]
    assert len(section) <= review._earlier_user_budget(24_000)
    assert len(text) <= 24_000
    assert "earlier user message(s) omitted]" in section
    assert "direction 0:" in section  # the opening request is kept first
    assert info["coverage"]["operator_messages"] == "partial"


# ---- the decision pass reads explicit fields ---------------------------------------------------


def _card(**over):
    card = {
        "id": "claude:a",
        "engine": "claude",
        "title": "t",
        "project": {"name": "P"},
        "state": "idle",
        "intervention_required": False,
        "ai_summary": "",
        "_ai_recap": "Planned the refactor across four modules.\n" * 20 + "Now waiting on review",
        "last_activity": 90.0,
    }
    card.update(over)
    return card


def test_the_digest_leads_with_the_assessments_explicit_fields():
    long_blocker = "b" * assessment.BLOCKER_MAX
    rec = _record({**FULL, "blocker": long_blocker})
    entry = orchestrator._digest_entry(
        _card(_ai_assessment=rec, _review_fingerprint="fp-1"), 1_000.0
    )
    keys = list(entry)
    assert keys.index("current_state") < keys.index("summary")
    assert entry["current_state"] == FULL["current_state"]
    # At the record's own cap — the digest never re-cuts a blocker the record kept.
    assert entry["blocker"] == long_blocker
    assert entry["decision_needed"] == FULL["decision_needed"]
    assert entry["constraints"] == FULL["constraints"]
    assert entry["context_source"] == "assessment"


def test_the_summary_is_never_a_prefix_of_the_recap():
    """#1018's measured bug: with no one-line summary the digest sent the recap's first 300
    characters — how the session STARTED — as what it was doing."""
    entry = orchestrator._digest_entry(_card(), 1_000.0)
    assert entry["summary"] == ""
    assert "Planned the refactor" not in json.dumps(entry)
    assert entry["current_state"] == "Now waiting on review"
    assert entry["context_source"] == "recap_last_line"


def test_a_stale_assessment_is_labelled_stale_in_the_digest():
    rec = _record()
    entry = orchestrator._digest_entry(
        _card(_ai_assessment=rec, _review_fingerprint="fp-1", last_activity=500.0), 1_000.0
    )
    assert entry["context_source"] == "assessment_stale"
    assert entry["blocker"] == FULL["blocker"]


# ---- API projections and retention -------------------------------------------------------------


def _client(cfg):
    return TestClient(create_app(cfg), base_url="https://testserver")


def _login(c, cfg):
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303


def test_the_session_lookup_projects_the_assessment_and_the_list_keeps_its_shape(
    auth_cfg, fake_jsonl
):
    sid = "claude:11111111-1111-1111-1111-111111111111"
    rec = _record(read_at=4_000_000_000.0, assessed_at=4_000_000_001.0)
    metadata.patch(sid, ai_assessment=rec, review_fingerprint="fp-1", ai_summary="CI blocked")
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    row = c.get(f"/api/sessions/{sid}").json()
    a = row["assessment"]
    assert a["status"] == "current" and a["blocker"] == FULL["blocker"]
    assert a["source"]["fingerprint"] == "fp-1"
    assert row["ai_summary"] == "CI blocked"  # the compatibility fields are untouched
    listed = c.get("/api/sessions").json()["sessions"]
    assert listed and all(r["assessment"] is None for r in listed)


def test_the_store_refuses_a_misshapen_record_and_clamps_a_hand_edited_one(tmp_path):
    with pytest.raises(ValueError):
        metadata.patch(KEY, ai_assessment={"schema_version": 1, "current_state": "x"})
    rec = _record()
    metadata.patch(KEY, ai_assessment=rec)
    path = metadata._default_path()
    raw = json.loads(path.read_text())
    raw[KEY]["ai_assessment"]["remaining"] = ["r" * 5000] * 50
    path.write_text(json.dumps(raw))
    got = metadata.get(KEY).ai_assessment
    assert len(got["remaining"]) == assessment.ITEMS_MAX
    assert all(len(i) <= assessment.ITEM_MAX for i in got["remaining"])
    raw[KEY]["ai_assessment"]["schema_version"] = 99
    path.write_text(json.dumps(raw))
    assert metadata.get(KEY).ai_assessment is None  # a future schema reads as unknown


def test_the_sweep_hashes_the_same_input_the_review_persists(ai_prefs, monkeypatch):
    """The background sweep's change detection must use the review's own gatherer. If it hashed
    the transcript-tail-only input instead, every long session's fingerprint would differ from the
    stored one forever, and each sweep would pay for a review of an unchanged session."""
    from agent_sessions import ai_review_loop

    class _Registry:
        def snapshot(self):
            return [{"id": KEY}]

    calls: list = []
    monkeypatch.setattr(review, "_TRANSPORT", _reply(FULL, calls=calls))
    monkeypatch.setattr(ai_review_loop, "CALL_SPACING_S", 0)
    prefs.set_ai_review({"enabled": True})
    case = CASES["middle-history-operator-correction"]
    with _Session(_turns_of(case), case["sources"]["screen"]):
        first, _ = asyncio.run(ai_review_loop.sweep(_Registry()))
        n = len(calls)
        ai_review_loop._LAST_ATTEMPT.clear()
        second, _ = asyncio.run(ai_review_loop.sweep(_Registry()))
    assert first == [KEY] and n == 2
    assert second == [] and len(calls) == n


# ---- independent-review findings on PR #1357 ----------------------------------------------------


def _claude_case_turns(tmp_path, case):
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in case["sources"]["claude_jsonl"]) + "\n")
    return transcript.claude_turns_from_jsonl(path)


def test_claude_harness_records_never_read_as_operator_messages(tmp_path):
    """Finding 1: Claude writes its compaction summary, the local-command caveat, slash-command
    wrappers and `!` command output as `type:user` records. They reached "Earlier user messages"
    (where a later one supersedes an earlier one) and verified as `source: operator` evidence —
    so command output, possibly untrusted file content, could become the session's constraints."""
    case = CASES["claude-harness-records-are-not-operator"]
    turns = _claude_case_turns(tmp_path, case)
    assert [[t.role, t.kind, t.text] for t in turns] == case["sources"]["turns"]
    ask = "Fix the flaky upload test. Do not touch the release branch."
    assert review._user_lines(turns) == [f"user: {ask}"]
    # Long enough that every user turn falls before the tail → all of them are "earlier".
    pad = [transcript.Turn("assistant", "working " * 3000, "text")]
    with _Session([*turns, *pad, turns[-1]], ""):
        info: dict = {}
        text, _ = review.gather_review_input(KEY, 6000, {}, info=info)
    section = text.split("## Transcript (tail)")[0]
    assert ask in section
    for injected in (
        "being continued",
        "push straight to the release",
        "local-command",
        "/compact",
    ):
        assert injected not in section, injected
        assert injected not in "\n".join(info["sources"]["operator"]), injected
    norm = assessment.normalize(
        {
            "current_state": "ready",
            "constraints": [],
            "evidence_refs": [
                {"source": "operator", "quote": "maintainers push straight to the release branch"},
                {"source": "operator", "quote": "merge the fix into the release branch"},
                {"source": "operator", "quote": "Do not touch the release branch"},
            ],
        },
        info["sources"],
    )
    assert norm is not None
    assert [r["quote"] for r in norm[0]["evidence_refs"]] == ["Do not touch the release branch"]


def test_a_harness_record_is_not_the_first_user_message_for_other_readers(tmp_path):
    """The same records feed handoff and the template suggester through `kind == "text"`; the
    caveat that opens most Claude transcripts must not be the "first user message" there."""
    from agent_sessions import handoff

    case = CASES["claude-harness-records-are-not-operator"]
    turns = _claude_case_turns(tmp_path, case)
    with patch.object(transcript, "adapter_for", return_value=lambda *_a: turns):
        pairs = handoff._source_texts("claude", "x")
    users = [t for r, t in pairs if r == "user"]
    assert users == ["Fix the flaky upload test. Do not touch the release branch."]
    # Scroll-up still shows them: the renderer styles a user turn by role.
    assert b"being continued" in transcript.render(turns, 120)


def test_a_short_correction_contained_in_a_later_message_is_not_dropped():
    """Finding 2: membership was a substring test, so "user: no" counted as shown because it
    occurs inside "user: no worries, ship it" — dropped while coverage said complete."""
    turns = [
        transcript.Turn("user", "no", "text"),
        transcript.Turn("assistant", "working " * 3000, "text"),
        transcript.Turn("user", "no worries, ship it", "text"),
        transcript.Turn("assistant", "Shipping.", "text"),
    ]
    with _Session(turns, ""):
        info: dict = {}
        text, _ = review.gather_review_input(KEY, 6000, {}, info=info)
    section = text.split("\n\n## Transcript (tail)")[0]
    assert "user: no" in section.splitlines()
    assert "user: no worries, ship it" not in section  # that one IS in the tail
    assert info["coverage"]["operator_messages"] == "complete"


def _raw_reply(content: str, finish: str = "stop", *, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(json.loads(request.content))
        system = json.loads(request.content)["messages"][0]["content"]
        if "CHRONOLOGICAL" in system:
            body = json.dumps({"recap": "Started.\nNow blocked on CI."})
            return httpx.Response(200, json={"choices": [{"message": {"content": body}}]})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}, "finish_reason": finish}]},
        )

    return httpx.MockTransport(handler)


_CORE = '{"summary": "CI blocked again", "title": "Retry CI", "intervention_required": true, '
_CORE += '"reason": "needs a retry decision", '


@pytest.mark.parametrize(
    ("content", "finish"),
    [
        # Cut by the token limit inside the assessment.
        (
            _CORE + '"assessment": {"task": "Implement the parser fix", "current_state": "Implem',
            "length",
        ),
        # A mis-escaped quote inside the assessment makes the whole reply invalid JSON.
        (
            _CORE + '"assessment": {"current_state": "agent said "done" twice", "blocker": null}}',
            "stop",
        ),
    ],
)
def test_a_bad_assessment_keeps_the_summary_and_needs_you(ai_prefs, monkeypatch, content, finish):
    """Finding 3: the assessment made the reply ~4x longer, so it is what a token limit cuts and
    where a quoting slip lands — and either failed the whole review, dropping a good summary and
    the needs-you flag. The summary contract now stands on its own; the assessment goes stale."""
    monkeypatch.setattr(review, "_TRANSPORT", _reply(FULL))
    with _Session(TURNS, SCREEN):
        asyncio.run(review.run_review(KEY, {}))
    first = metadata.get(KEY).ai_assessment
    monkeypatch.setattr(review, "_TRANSPORT", _raw_reply(content, finish))
    with _Session([*TURNS, transcript.Turn("assistant", "Retried; failed.", "text")], SCREEN):
        out = asyncio.run(review.run_review(KEY, {}))
    m = metadata.get(KEY)
    assert m.ai_summary == "CI blocked again" and m.ai_title == "Retry CI"
    assert m.intervention_required is True and m.intervention_reason == "needs a retry decision"
    assert m.review_failed_at is None
    assert m.ai_assessment == first
    assert out["assessment"]["status"] == "stale"
    assert out["assessment"]["stale_reasons"] == ["newer_review_without_assessment"]


@pytest.mark.parametrize(
    "content",
    [
        # The cut fell before the flag: no needs-you verdict, so no review.
        ('{"summary": "CI blocked", "title": "x", "intervention_req', "length"),
        # The assessment came FIRST: nothing before it to salvage.
        (
            '{"assessment": {"current_state": "x"}, "summary": "CI blocked", "intervention_req',
            "length",
        ),
        ("", "length"),
    ],
)
def test_an_unsalvageable_reply_still_fails_the_review(ai_prefs, monkeypatch, content):
    monkeypatch.setattr(review, "_TRANSPORT", _raw_reply(*content))
    with _Session(TURNS, SCREEN), pytest.raises(review.ReviewError):
        asyncio.run(review.run_review(KEY, {}))
    assert metadata.get(KEY).review_failed_at is not None


def test_the_decision_pass_reruns_when_an_assessment_goes_stale():
    """Finding 4: a failed refresh changes no review fingerprint, so the loop skipped the pass as
    "unchanged" while the digest it would send had switched to `assessment_stale`."""
    from agent_sessions import orchestrator_loop

    cfg = dict(prefs.get_orchestrator())
    rec = _record()
    card = _card(_ai_assessment=rec, _review_fingerprint="fp-1", last_activity=90.0)
    fp_current = orchestrator_loop.world_fingerprint([card], cfg, now=1_000.0)
    failed = {**card, "_review_failed_at": 200.0}
    assert orchestrator._digest_entry(failed, 1_000.0)["context_source"] == "assessment_stale"
    assert orchestrator_loop.world_fingerprint([failed], cfg, now=1_000.0) != fp_current
    moved = {**card, "last_activity": 150.0}  # same age bucket, activity after the read
    assert orchestrator_loop.world_fingerprint([moved], cfg, now=1_000.0) != fp_current


def test_a_newer_review_without_an_assessment_sends_its_recap_line_labelled():
    rec = _record()
    entry = orchestrator._digest_entry(
        _card(_ai_assessment=rec, _review_fingerprint="fp-2"), 1_000.0
    )
    assert entry["context_source"] == "assessment_stale"
    assert entry["current_state"] == FULL["current_state"]  # the older record, as itself
    assert entry["newer_recap_last_line"] == "Now waiting on review"
    # A current record sends no such line.
    current = orchestrator._digest_entry(
        _card(_ai_assessment=rec, _review_fingerprint="fp-1"), 1_000.0
    )
    assert "newer_recap_last_line" not in current


def test_a_reader_cut_history_never_reports_complete_coverage(tmp_path, monkeypatch):
    """Finding 5: the readers keep the newest `_TAIL_BYTES` / `DEFAULT_MAX_MESSAGES`. A history
    longer than that lost its opening request in the READER, while coverage — measured only on
    what the reader returned — said complete."""
    recs = [
        {"type": "user", "message": {"role": "user", "content": "Open request: do X."}},
        *[
            {"type": "assistant", "message": {"role": "assistant", "content": f"step {i}"}}
            for i in range(20)
        ],
    ]
    path = tmp_path / "long.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    whole = transcript.claude_turns_from_jsonl(path)
    assert not transcript.was_truncated(whole)
    cut = transcript.claude_turns_from_jsonl(path, max_messages=5)
    assert (
        transcript.was_truncated(cut) and "Open request" not in transcript.render(cut, 80).decode()
    )
    monkeypatch.setattr(transcript, "_TAIL_BYTES", 200)
    assert transcript.was_truncated(transcript.claude_turns_from_jsonl(path))

    tagged = transcript.tagged_turns(list(TURNS), True)
    with _Session(tagged, SCREEN):
        info: dict = {}
        with patch.object(review, "_turns", side_effect=lambda *a, **k: tagged):
            review.gather_review_input(KEY, 24_000, {}, info=info)
    assert info["coverage"]["transcript"] == "tail"
    assert info["coverage"]["operator_messages"] == "partial"
    with _Session(TURNS, SCREEN):
        info = {}
        review.gather_review_input(KEY, 24_000, {}, info=info)
    assert info["coverage"] == {
        **info["coverage"],
        "transcript": "complete",
        "operator_messages": "complete",
    }


def test_review_turns_keep_the_readers_truncation_flag(monkeypatch):
    tagged = transcript.tagged_turns(list(TURNS), True)
    monkeypatch.setattr(transcript, "adapter_for", lambda _e: lambda *_a: tagged)
    got = review._turns(KEY, {})
    assert got == TURNS and transcript.was_truncated(got)


def test_a_trivial_quote_is_not_evidence_and_negations_are_unknown():
    """Finding 6: a one-letter quote occurs in any section and so always "verified"; "None." and
    "No blocker" were stored as a blocker because the null check wanted the bare word."""
    sources = {"transcript": "a b c CI failed twice with one cause", "screen": "", "operator": ""}
    fields, notes = assessment.normalize(
        {
            "current_state": "idle",
            "evidence_refs": [
                {"source": "transcript", "quote": "a"},
                {"source": "transcript", "quote": "CI failed"},
                {"source": "transcript", "quote": "a b c"},
                {"source": "transcript", "quote": "CI failed twice"},
            ],
        },
        sources,
    )
    assert [r["quote"] for r in fields["evidence_refs"]] == ["a b c", "CI failed twice"]
    assert notes["refs_dropped"] == 2
    for null in ("None.", "No blocker", "n/a", "N/A.", "No blockers.", "none needed", "(none)"):
        got = assessment.normalize({"current_state": "idle", "blocker": null})
        assert got[0]["blocker"] is None, null
    for real in ("None of the tests pass", "No CI runner is available"):
        got = assessment.normalize({"current_state": "idle", "blocker": real})
        assert got[0]["blocker"] == real
