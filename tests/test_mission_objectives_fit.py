"""A mission's objectives fit the mission (#1061) — the store and producer halves.

Phase 1: `playbook_id` has THREE shapes — absent (the operator's default), a playbook id, and the
declined sentinel — and "you declined" is a different fact from "what you chose is gone".

Phase 2: a selection may parameterise a probe, under rules that keep every target
operator-authored: only forge/git probes, only `repo`/`branch`, only values that occur verbatim in
the instruction. The HTTP probes are never parameterised — a model-written `url` is the SSRF
surface the playbook boundary exists to prevent.

Phase 4: the non-settling answer says it will be asked again.
"""

from __future__ import annotations

import pytest

from agent_sessions import mission_objectives as mo
from agent_sessions import mission_questions as mq
from agent_sessions import missions, prefs


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "p.json"))
    missions.reset_schema_cache_for_test()
    prefs.set_mission_playbooks(
        {
            "default_id": "ship",
            "playbooks": [
                {
                    "id": "ship",
                    "label": "Ship",
                    "objectives": [
                        {
                            "key": "merged",
                            "title": "It is merged",
                            "probe": "forge_merged",
                            "gate": True,
                        },
                        {
                            "key": "live",
                            "title": "It is live",
                            "probe": "http_status",
                            "probe_args": {"url": "https://app.example.com/healthz"},
                            "gate": True,
                        },
                    ],
                }
            ],
        }
    )
    yield
    missions.reset_schema_cache_for_test()


# ---- Phase 1: three shapes, validated in the store --------------------------------------------


def test_absent_means_the_operators_default(store):
    mid = missions.create_mission("ship it")["id"]
    status, templates = missions.templates_for_mission(mid)
    assert status == "ok" and [t["key"] for t in templates] == ["merged", "live"]


def test_the_declined_sentinel_is_its_own_state_with_no_templates(store):
    mid = missions.create_mission("look into it", playbook_id=missions.PLAYBOOK_DECLINED)["id"]
    assert missions.templates_for_mission(mid) == ("declined", [])


def test_declined_is_not_the_same_fact_as_a_playbook_that_is_gone(store):
    gone = missions.create_mission("x", playbook_id="deleted-since")["id"]
    declined = missions.create_mission("y", playbook_id=missions.PLAYBOOK_DECLINED)["id"]
    assert missions.templates_for_mission(gone)[0] == "unknown_playbook"
    assert missions.templates_for_mission(declined)[0] == "declined"


@pytest.mark.parametrize(
    "bad", ["Ship", "a b", "../ship", ":other", ":", "-ship", "x" * 201, 7, ["ship"]]
)
def test_a_malformed_playbook_id_is_refused_at_creation(store, bad):
    with pytest.raises(missions.MissionError) as e:
        missions.create_mission("x", playbook_id=bad)  # type: ignore[arg-type]
    assert e.value.status == 422


def test_the_sentinel_can_never_be_a_real_playbook_id(store):
    """The leading colon is outside the playbook id alphabet, so no playbook can shadow it."""
    with pytest.raises(prefs.PlaybookError):
        prefs.set_mission_playbooks(
            {"default_id": "", "playbooks": [{"id": ":none", "label": "x", "objectives": []}]}
        )


# ---- Phase 2: operator-authored parameters only -----------------------------------------------

T = [
    {"key": "merged", "title": "It is merged", "probe": "forge_merged", "probe_args": None},
    {
        "key": "live",
        "title": "It is live",
        "probe": "http_status",
        "probe_args": {"url": "https://x/"},
    },
    {
        "key": "pinned",
        "title": "Main is green",
        "probe": "forge_checks",
        "probe_args": {"branch": "main"},
    },
]
INSTR = "merge devopsagent/alpha and devopsagent/beta into superstatus.io/agent-sessions if okay"


def rows_for(*items, instruction=INSTR):
    stats: dict = {}
    rows, dropped = mo._rows_from_reply(
        {"objectives": list(items)}, T, instruction=instruction, stats=stats
    )
    return rows, dropped, stats


def test_one_template_fitted_to_each_branch_the_instruction_names():
    rows, dropped, stats = rows_for(
        {"template_index": 0, "gate": True, "probe_args": {"branch": "devopsagent/alpha"}},
        {"template_index": 0, "gate": True, "probe_args": {"branch": "devopsagent/beta"}},
    )
    assert dropped == 0
    assert [(r["key"], r["probe_args"]) for r in rows] == [
        ("merged", {"branch": "devopsagent/alpha"}),
        ("merged--2", {"branch": "devopsagent/beta"}),
    ]
    assert stats == {"parameterised": 2}


def test_a_repo_the_instruction_names_is_accepted():
    rows, dropped, _ = rows_for(
        {"template_index": 0, "probe_args": {"repo": "superstatus.io/agent-sessions"}}
    )
    assert dropped == 0 and rows[0]["probe_args"] == {"repo": "superstatus.io/agent-sessions"}


@pytest.mark.parametrize(
    "args",
    [
        {"branch": "devopsagent/gamma"},  # not in the instruction: a guess
        {"branch": "devopsagent/alph"},  # a prefix of a real token is still not what was typed…
        {"branch": "a"},  # …nor a letter that occurs inside one ("and", "alpha")…
        {"branch": "merge and"},  # …and a phrase is never a target, even though it occurs
        {"branch": ""},
        {"branch": 7},
        {"url": "http://169.254.169.254/"},  # not a key a selection may set
        {"workflow": "deploy"},
        {},
        "devopsagent/alpha",
    ],
)
def test_anything_the_operator_did_not_write_refuses_the_row(args):
    rows, dropped, stats = rows_for({"template_index": 0, "probe_args": args})
    assert rows == [] and dropped == 1, args
    assert stats == {"parameterised": 0}


@pytest.mark.parametrize(
    "instruction",
    [
        "merge `devopsagent/alpha` when green",
        'merge "devopsagent/alpha", then stop',
        "merge (devopsagent/alpha).",
    ],
)
def test_a_value_wrapped_in_prose_punctuation_is_still_the_operators_token(instruction):
    rows, dropped, _ = rows_for(
        {"template_index": 0, "probe_args": {"branch": "devopsagent/alpha"}},
        instruction=instruction,
    )
    assert dropped == 0 and rows[0]["probe_args"] == {"branch": "devopsagent/alpha"}


def test_the_HTTP_probes_are_never_parameterised_even_with_an_instruction_value(monkeypatch):
    """A model-written request target is the SSRF surface itself — refused by KIND, however the
    instruction reads.

    The shared validator would also refuse `repo` on an HTTP probe today, and a test that relied on
    that would pass for the wrong reason: it would keep passing if the kind rule were deleted, right
    up until a schema change gave an HTTP probe an argument. So the validator is made permissive
    here, and the refusal has to come from the kind rule alone.
    """
    monkeypatch.setattr(mo.missions, "validate_probe_args", lambda kind, args: None)
    rows, dropped, _ = rows_for(
        {"template_index": 1, "probe_args": {"repo": "superstatus.io/agent-sessions"}},
        instruction="check superstatus.io/agent-sessions and http://169.254.169.254/",
    )
    assert rows == [] and dropped == 1


def test_a_value_the_operators_template_already_sets_is_not_overwritten():
    rows, dropped, _ = rows_for(
        {"template_index": 2, "probe_args": {"branch": "devopsagent/alpha"}}
    )
    assert rows == [] and dropped == 1


def test_an_unfilled_argument_beside_an_operator_one_is_merged_not_replaced():
    rows, dropped, _ = rows_for(
        {"template_index": 2, "probe_args": {"repo": "superstatus.io/agent-sessions"}}
    )
    assert dropped == 0
    assert rows[0]["probe_args"] == {"branch": "main", "repo": "superstatus.io/agent-sessions"}


def test_the_same_template_and_arguments_twice_is_one_objective_not_a_refusal():
    rows, dropped, stats = rows_for(
        {"template_index": 0, "probe_args": {"branch": "devopsagent/alpha"}},
        {"template_index": 0, "probe_args": {"branch": "devopsagent/alpha"}},
    )
    assert len(rows) == 1 and dropped == 0 and stats == {"parameterised": 1}


def test_without_parameters_nothing_changes():
    rows, dropped, stats = rows_for({"template_index": 0, "gate": True}, {"template_index": 1})
    assert [(r["key"], r["probe_args"]) for r in rows] == [
        ("merged", None),
        ("live", {"url": "https://x/"}),
    ]
    assert dropped == 0 and stats == {"parameterised": 0}


def test_the_model_is_told_which_templates_may_take_a_repo_or_branch_and_never_shown_a_value():
    text = mo._render_templates(T)
    assert "It is merged  [checks: forge_merged]  [may set: repo, branch]" in text
    assert "It is live  [checks: http_status]" in text and "may set" not in text.split("\n")[1]
    assert "https://x/" not in text and "main" not in text


@pytest.mark.anyio
async def test_propose_passes_the_instruction_and_reports_what_it_parameterised(store, monkeypatch):
    monkeypatch.setattr(mo.review, "_require_config", lambda: {"base_url": "x", "api_key": "y"})

    async def fake(_messages, **_kw):
        return {
            "objectives": [
                {"template_index": 0, "gate": True, "probe_args": {"branch": "devopsagent/alpha"}},
                {"template_index": 0, "gate": True, "probe_args": {"branch": "not-in-it"}},
            ]
        }

    monkeypatch.setattr(mo.review, "complete_json", fake)
    mid = missions.create_mission("please merge devopsagent/alpha when green")["id"]
    out = await mo.propose(mid)
    assert out["parameterised"] == 1 and out["dropped"] == 1
    [row] = [o for o in out["objectives"] if o["key"].startswith("merged")]
    assert row["probe_args"] == {"branch": "devopsagent/alpha"}


@pytest.mark.anyio
async def test_a_declined_checklist_is_said_as_a_choice_on_the_timeline(store, monkeypatch):
    monkeypatch.setattr(mo.review, "_require_config", lambda: {"base_url": "x", "api_key": "y"})

    async def fake(_messages, **_kw):
        return {"notes": [{"title": "Look around"}]}

    monkeypatch.setattr(mo.review, "complete_json", fake)
    mid = missions.create_mission("look around", playbook_id=missions.PLAYBOOK_DECLINED)["id"]
    await mo.propose(mid)
    texts = [e.get("text") or "" for e in missions.get_mission(mid)["events"]]
    assert any(t.startswith("checklist declined for this mission") for t in texts), texts
    assert not any("no objective templates: declined" in t for t in texts)


def test_a_bare_repo_does_not_match_the_owner_qualified_one_the_instruction_names():
    """`repo` matches as written: the instruction names `superstatus.io/agent-sessions`, and the
    bare `agent-sessions` is a different token — the operator did not write it."""
    rows, dropped, _ = rows_for({"template_index": 0, "probe_args": {"repo": "agent-sessions"}})
    assert rows == [] and dropped == 1


# ---- minted keys resolve back to their template (issue review) ---------------------------------


def test_a_minted_key_uses_the_reserved_separator_and_fits_the_key_limit():
    assert missions.minted_key("merged", 2) == "merged--2"
    long = "k" * missions.OBJECTIVE_KEY_MAX
    assert len(missions.minted_key(long, 12)) <= missions.OBJECTIVE_KEY_MAX


def test_template_for_key_prefers_an_exact_key_and_resolves_a_minted_one():
    tpl = {"merged": {"key": "merged"}, "merged-2": {"key": "merged-2"}}
    assert missions.template_for_key(tpl, "merged")["key"] == "merged"
    # a playbook's OWN `merged-2` is never mistaken for a minted row — that is why minting uses `--`
    assert missions.template_for_key(tpl, "merged-2")["key"] == "merged-2"
    assert missions.template_for_key(tpl, "merged--2")["key"] == "merged"
    assert missions.template_for_key(tpl, "merged--17")["key"] == "merged"
    assert missions.template_for_key(tpl, "gone--2") is None
    assert (
        missions.template_for_key(tpl, "merged--1") is None
    )  # never minted: the first use is bare
    assert missions.template_for_key(tpl, "note_1") is None


def test_a_truncated_minted_base_resolves_only_when_unambiguous():
    base = "k" * (missions.OBJECTIVE_KEY_MAX - 5)
    one = {base + "xyz": {"key": base + "xyz"}}
    assert (
        missions.template_for_key(one, missions.minted_key(base + "xyz", 2))["key"] == base + "xyz"
    )
    two = {base + "a": {"key": "a"}, base + "b": {"key": "b"}}
    assert missions.template_for_key(two, missions.minted_key(base + "a", 2)) is None


def test_a_playbook_write_refuses_the_minted_separator_in_an_objective_key(store):
    with pytest.raises(prefs.PlaybookError, match="may not contain"):
        prefs.set_mission_playbooks(
            {
                "default_id": "p",
                "playbooks": [
                    {
                        "id": "p",
                        "label": "P",
                        "objectives": [
                            {
                                "key": "merged--2",
                                "title": "x",
                                "probe": "forge_merged",
                                "gate": True,
                            }
                        ],
                    }
                ],
            }
        )


@pytest.mark.anyio
async def test_resetting_a_fitted_rows_direction_uses_the_template_it_was_minted_from(
    store, monkeypatch
):
    """The panel offers "reset to the playbook's direction" on every row of a mission with a
    playbook. On a minted row it used to answer "the mission's playbook has no objective merged-2" —
    offered, and refused."""
    prefs.set_mission_playbooks(
        {
            "default_id": "ship",
            "playbooks": [
                {
                    "id": "ship",
                    "label": "Ship",
                    "objectives": [
                        {
                            "key": "merged",
                            "title": "It is merged",
                            "probe": "forge_merged",
                            "gate": True,
                            "direction": "Merge it once review passes.",
                        }
                    ],
                }
            ],
        }
    )
    monkeypatch.setattr(mo.review, "_require_config", lambda: {"base_url": "x", "api_key": "y"})

    async def fake(_messages, **_kw):
        return {
            "objectives": [
                {"template_index": 0, "gate": True, "probe_args": {"branch": "devopsagent/alpha"}},
                {"template_index": 0, "gate": True, "probe_args": {"branch": "devopsagent/beta"}},
            ]
        }

    monkeypatch.setattr(mo.review, "complete_json", fake)
    mid = missions.create_mission("merge devopsagent/alpha and devopsagent/beta")["id"]
    await mo.propose(mid)
    keys = [o["key"] for o in missions.objectives(mid)]
    assert keys == ["merged", "merged--2"], keys

    missions.patch_objectives(
        mid, [{"op": "set_direction", "key": "merged--2", "direction": "Mine."}]
    )
    missions.patch_objectives(mid, [{"op": "reset_direction", "key": "merged--2"}])
    row = next(o for o in missions.objectives(mid) if o["key"] == "merged--2")
    assert (row["direction"], row["direction_source"]) == (
        "Merge it once review passes.",
        "template",
    )


# ---- Phase 4 -----------------------------------------------------------------------------------


def test_the_non_settling_answer_says_it_may_be_asked_again():
    """It is literally true that nothing else changes — and that is exactly why the same question
    comes back as a new episode. The consequence the card shows has to say so."""
    assert "may be asked again" in mq.CONSEQUENCE["note_answer"]
