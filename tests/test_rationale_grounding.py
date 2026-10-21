"""Rationale de-echo (#753).

The rationale is the one line answering *why does this need me* — the grey text under the title
on a Pulse card, and the body of every bell row and Web Push. Measured on the live store on
2026-07-31: **31 of 200 rationales contained their own title**, and 4 opened literally
`Title says '…' — …`. The operator reads the redundant half and learns nothing they did not
already have from the line above it.

Cases below are verbatim from that store.
"""

from agent_sessions.orchestrator import _degabble, _validate_actions

TITLE = "iOS pipeline repaired, awaiting RTM re-queue decision"
ECHOED = (
    "Title says 'iOS pipeline repaired, awaiting RTM re-queue decision' — "
    "needs user decision on re-queue."
)


def test_the_title_says_preamble_is_stripped():
    assert _degabble(ECHOED, TITLE) == "Needs user decision on re-queue."


def test_a_title_quoted_mid_sentence_is_LEFT_ALONE():
    """Deliberately not removed, and this reversed during review.

    Removing it scored better on "does the reason still contain its title" (26 -> 2 against
    26 -> 22) and produced worse text, because in those rows the title IS the opening clause:
    `Awaiting user decision on PR #20 merge path after Hermes approval` became `after Hermes
    approval`. A redundant sentence is readable; a fragment is not. The metric rewarded
    shredding, so the metric was wrong.
    """
    line = f"Agent is blocked on {TITLE} and cannot proceed without a call."
    assert _degabble(line, TITLE) == line


def test_a_rationale_that_already_says_something_is_untouched():
    good = "Agent hit a 5-way VRAM tradeoff and cannot pick without a hardware call."
    assert _degabble(good, TITLE) == good


def test_stripping_never_empties_a_rationale():
    """A weak rationale is bad; an empty one is worse, and neither justifies dropping the
    action. Purely subtractive with a floor: if nothing meaningful survives, keep the original."""
    bare = f"Title says '{TITLE}' — done."
    assert _degabble(bare, TITLE) == bare
    assert _degabble(TITLE, TITLE) == TITLE


def test_a_short_title_is_never_used_as_a_needle():
    """A 3-char title would match inside ordinary words and shred the sentence."""
    line = "Agent is blocked on the CI run and needs a decision about the next step."
    assert _degabble(line, "CI") == line


def test_it_runs_on_the_real_validation_path():
    """Not just the helper — the field that reaches the ledger and the bell."""
    sid = "codex:019f980f-2435-7fd1-a86b-e38b25bff3ae"
    _, actions = _validate_actions(
        {
            "assessment": "x",
            "actions": [
                {
                    "session_id": sid,
                    "verb": "escalate",
                    "confidence": 0.8,
                    "rationale": ECHOED,
                }
            ],
        },
        {sid: {"id": sid, "title": TITLE, "age_hours": 1.0}},
    )
    assert actions[0]["rationale"] == "Needs user decision on re-queue."


def test_a_missing_rationale_does_not_explode():
    sid = "codex:019f980f-2435-7fd1-a86b-e38b25bff3ae"
    _, actions = _validate_actions(
        {"assessment": "x", "actions": [{"session_id": sid, "verb": "escalate"}]},
        {sid: {"id": sid, "title": TITLE, "age_hours": 1.0}},
    )
    assert actions[0]["rationale"] == ""


# --- the three regressions the review caught ------------------------------------------------


def test_an_apostrophe_inside_the_title_does_not_truncate_the_sentence():
    """Accepting any quote as the closer let the apostrophe in `Agent's` end the match, so the
    strip ate the start of the real sentence and returned `S build is blocked' — …`."""
    title = "Agent's build is blocked"
    line = f"Title says '{title}' — needs a deployment choice."
    assert _degabble(line, title) == "Needs a deployment choice."


def test_smart_quotes_pair_with_their_own_kind():
    title = "Deploy the gateway fix"
    assert (
        _degabble(f"Title says “{title}” — needs a maintenance window.", title)
        == "Needs a maintenance window."
    )


def test_a_title_inside_a_longer_word_is_left_alone():
    line = "Reauthentication is required before deployment can proceed."
    assert _degabble(line, "authentication") == line


def test_a_leading_clause_that_is_the_title_is_never_shredded():
    """The real shape from the live store — removing it leaves a dangling fragment."""
    line = "Awaiting user decision on PR #20 merge path after Hermes approval"
    assert _degabble(line, "Awaiting user decision on PR #20 merge path") == line


def test_casing_is_never_rewritten_on_an_untouched_rationale():
    """A subtractive transform must not edit what it keeps — `iOS` was becoming `IOS`."""
    line = "iOS deployment requires approval."
    assert _degabble(line, "Something unrelated entirely") == line


def test_casing_is_repaired_only_when_a_prefix_was_removed():
    title = "Awaiting the merge decision"
    assert _degabble(f"Title says '{title}' — needs user input on strategy.", title) == (
        "Needs user input on strategy."
    )


# --- second review round ---------------------------------------------------------------------


def test_a_later_quote_in_the_real_sentence_is_not_mistaken_for_the_closer():
    """Greedy arms ran to the LAST quote, so a quoted choice in the meaningful suffix closed the
    match and everything before it was eaten. Anchoring on the actual title removes the guess."""
    title = "Build is blocked"
    line = f"Title says '{title}' — choose 'blue' or 'green' before deployment tomorrow."
    assert _degabble(line, title) == "Choose 'blue' or 'green' before deployment tomorrow."


def test_the_same_holds_for_double_quotes():
    title = "Build is blocked"
    line = f'Title says "{title}" — choose "blue" or "green" now.'
    assert _degabble(line, title) == 'Choose "blue" or "green" now.'


def test_a_preamble_quoting_something_other_than_the_title_is_left_alone():
    """If the quoted span is not the title we were given, there is nothing to prove it is an
    echo — so nothing is removed."""
    line = "Title says 'a completely different string' — needs a decision."
    assert _degabble(line, "The actual session title") == line


def test_untouched_text_is_byte_identical_not_merely_equivalent():
    """Cleanup used to run unconditionally, so a rationale nothing matched still came back
    re-spaced or re-punctuated. A subtractive transform must not edit what it keeps."""
    assert (
        _degabble("iOS deployment  requires approval.", "Something unrelated entirely")
        == "iOS deployment  requires approval."
    )
    assert (
        _degabble("— iOS deployment requires approval.", "Something unrelated entirely")
        == "— iOS deployment requires approval."
    )


def test_an_empty_title_is_a_no_op():
    line = "Agent is blocked and needs a decision."
    assert _degabble(line, "") == line


def test_the_title_must_be_the_whole_quoted_span_not_a_prefix_of_it():
    """`'Build is blocked by CI'` against title `Build is blocked` used to leave `By CI' — …`.

    The parser now requires the quoted span to END where the title ends, and an unparseable
    preamble means the line is not touched at all.
    """
    for line, title in [
        ("Title says 'Build is blocked by CI' — needs a deployment choice.", "Build is blocked"),
        ("Title says 'Build is blockedness' — needs a deployment choice.", "Build is blocked"),
        ("Title says 'CI pipeline blocked' — needs a call now.", "CI"),
    ]:
        assert _degabble(line, title) == line


def test_an_unquoted_preamble_needs_an_explicit_separator():
    """Without one, `Title says Build is blocked by CI — …` would truncate to `by CI — …`."""
    line = "Title says Build is blocked by CI — needs a deployment choice."
    assert _degabble(line, "Build is blocked") == line
    ok = "Title says Build is blocked — needs a deployment choice."
    assert _degabble(ok, "Build is blocked") == "Needs a deployment choice."


# --- round five ------------------------------------------------------------------------------


def test_proper_nouns_in_the_retained_suffix_are_not_recased():
    """Capitalising unconditionally turned `iOS` into `IOS` and `eBay` into `EBay`.

    A word carrying its own internal capital is deliberately cased, so the repair only fires
    when the leading word is unambiguously all-lowercase.
    """
    t = "Build is blocked"
    for suffix in (
        "iOS deployment requires approval.",
        "eBay release ownership is unclear.",
        "macOS runner is offline entirely.",
    ):
        assert _degabble(f"Title says '{t}' — {suffix}", t) == suffix


def test_a_lowercase_suffix_is_still_capitalised():
    t = "Build is blocked"
    assert (
        _degabble(f"Title says '{t}' — needs a deployment choice.", t)
        == "Needs a deployment choice."
    )


def test_the_retained_suffix_is_not_re_spaced():
    """Collapsing internal whitespace also edits text that was KEPT — same class as the casing
    bug, found while fixing it rather than reported."""
    t = "Build is blocked"
    assert (
        _degabble(f"Title says '{t}' — needs  a  deployment choice.", t)
        == "Needs  a  deployment choice."
    )


def test_validation_compares_against_the_title_the_model_was_actually_shown():
    """`_digest_entry` sends a CLAMPED title; comparing against the raw card title meant a
    long title could be echoed perfectly and never recognised."""
    from agent_sessions.orchestrator import TITLE_MAX, _clamp, _validate_actions

    sid = "codex:019f980f-2435-7fd1-a86b-e38b25bff3ae"
    raw = "A" * 100
    shown = _clamp(raw, TITLE_MAX)
    assert shown != raw, "fixture must actually exercise clamping"
    _, actions = _validate_actions(
        {
            "assessment": "x",
            "actions": [
                {
                    "session_id": sid,
                    "verb": "escalate",
                    "confidence": 0.8,
                    "rationale": f"Title says '{shown}' — needs a deployment choice.",
                }
            ],
        },
        {sid: {"id": sid, "title": raw, "age_hours": 1.0}},
    )
    assert actions[0]["rationale"] == "Needs a deployment choice."
