"""#1626 review: which transcripts belong to which spoken turn (`voice_turns`, used by the worker's
bridge). STT results come back in order; a result for an already-delivered turn is LATE and is
delivered once, on its own — never lost, never merged into a later turn."""

from agent_sessions.voice_turns import Turns


def test_a_turn_waits_for_its_own_segments():
    t = Turns()
    t.segment_sent()
    t.segment_sent()
    target = t.target()
    assert t.transcript("hello") is None and not t.settled(target)
    assert t.transcript("there") is None and t.settled(target)
    assert t.flush(target) == "hello there"


def test_a_slow_segment_is_delivered_late_on_its_own():
    t = Turns()
    t.segment_sent()
    t.segment_sent()
    first = t.target()
    assert t.transcript("rerun the tests") is None
    assert t.flush(first) == "rerun the tests"  # the grace ran out: one segment still out
    t.segment_sent()  # the Commander speaks again
    second = t.target()
    assert t.transcript("on api rebase") == "on api rebase"  # late: delivered alone
    assert t.transcript("and the docs") is None
    assert t.flush(second) == "and the docs"  # not mixed with the late one


def test_a_turn_never_takes_its_neighbours_words():
    """Review 6845: two turns whose results both land before the first flush stay two turns."""
    t = Turns()
    t.segment_sent()
    first = t.target()
    t.segment_sent()
    second = t.target()
    assert t.transcript("first question") is None
    assert t.transcript("second question") is None
    assert t.flush(first) == "first question"
    assert t.flush(second) == "second question"


def test_both_orderings_of_two_stops_and_their_results():
    # Results before the stops' flushes, and a flush before its result (late).
    t = Turns()
    t.segment_sent()
    a = t.target()
    t.segment_sent()
    b = t.target()
    assert t.flush(a) == ""  # the grace ran out before A's result
    assert t.transcript("alpha") == "alpha"  # late, on its own
    assert t.transcript("bravo") is None
    assert t.flush(b) == "bravo"


def test_empty_segments_count():
    t = Turns()
    t.segment_sent()
    t.segment_empty()
    t.segment_sent()
    target = t.target()
    assert t.transcript("one") is None
    assert t.settled(target) and t.flush(target) == "one"


def test_a_turn_is_heard_in_a_language_only_when_all_its_words_were():
    """Second review of #1635: the language of a TURN, not of its last segment. A breath after
    German words (an empty segment) must not make the turn English; mixed words have none."""
    t = Turns()
    t.segment_sent()
    t.segment_sent()
    assert t.transcript("Confirm.", "de") is None
    t.segment_empty()  # a breath: no words, so no language
    assert t.flush(t.target()) == "Confirm." and t.flushed_language == "de"
    t.segment_sent()
    t.segment_sent()
    t.transcript("confirm", "en")
    t.transcript("bitte", "de")
    assert t.flush(t.target()) == "confirm bitte" and t.flushed_language is None
    t.segment_sent()
    t.transcript("confirm", None)  # not reported
    assert t.flush(t.target()) == "confirm" and t.flushed_language is None


def test_a_late_transcript_keeps_its_own_language():
    t = Turns()
    t.segment_sent()
    assert t.flush(t.target()) == ""  # the grace ran out
    assert t.transcript("confirm", "de") == "confirm" and t.late_language == "de"


def test_two_turns_whose_results_arrive_before_the_first_flush_keep_their_languages():
    """#1635 review: turn 1 ("confirm", heard as German) and turn 2 (English) both come back from
    STT before turn 1 flushes: turn 1 is still German, never the newer English."""
    t = Turns()
    t.segment_sent()
    first = t.target()
    t.segment_sent()
    second = t.target()
    t.transcript("confirm", "de")
    t.transcript("how are the tests", "en")
    assert t.flush(first) == "confirm" and t.flushed_language == "de"
    assert t.flush(second) == "how are the tests" and t.flushed_language == "en"
