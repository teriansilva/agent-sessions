"""Format 5 role steps (#1654, epic #1613 P2).

An agent step names a ROLE — required skills by exact id and an optional minimum grade — never an
engine or model. Formats 1–4 keep loading exactly as before (their actor is a read-only hint, their
bytes are never rewritten), and only format ≤4 steps take a deployment assignment.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from agent_sessions import playbooks
from agent_sessions.playbooks import authoring, flow_document, review, schema, store
from agent_sessions.playbooks.errors import PlaybookFormatError

FIXTURES = Path(__file__).parent / "fixtures" / "playbooks"

PLAN_ACTOR = 'actor = { kind = "agent", engine = "claude", model = "default" }'
IMPLEMENT_ACTOR = 'actor = { kind = "agent", engine = "claude" }'


@pytest.fixture
def dev(tmp_path: Path) -> Path:
    dst = tmp_path / "src" / "forge-workflow"
    shutil.copytree(FIXTURES / "forge-workflow", dst)
    return dst


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"test anchor {old!r} not in {path.name}"
    path.write_text(text.replace(old, new, 1))


def _flow(b: Path) -> Path:
    return b / "flows" / "dev.toml"


def _manifest(b: Path) -> Path:
    return b / "playbook.toml"


def _refused(bundle: Path, *needles: str) -> str:
    with pytest.raises(PlaybookFormatError) as e:
        playbooks.load_bundle(bundle)
    msg = str(e.value)
    for needle in needles:
        assert needle in msg, msg
    return msg


def _to_format5(
    b: Path, *, plan_role: str = 'role = { skills = ["forgejo-pr-flow"], min_grade = "strong" }'
) -> None:
    _edit(_manifest(b), "format = 1\n", "format = 5\n")
    _edit(_flow(b), "format = 1\n", "format = 5\n")
    _edit(_flow(b), PLAN_ACTOR, 'actor = { kind = "agent" }\n' + plan_role)
    _edit(_flow(b), IMPLEMENT_ACTOR, 'actor = { kind = "agent" }')
    # Format 5 implies a different instance; "session" becomes the named-specialist constraint.
    _edit(_flow(b), 'constraint = "session"', 'constraint = "specialist"')


def _steps(bundle: dict, flow: str = "dev") -> dict:
    return {s["id"]: s for s in bundle["flows"][flow]["steps"]}


# --- format 5 -----------------------------------------------------------------------------------


def test_format5_role_round_trips_with_skills_and_grade(dev):
    _to_format5(dev)
    loaded = playbooks.load_bundle(dev)
    steps = _steps(loaded)
    assert loaded["flows"]["dev"]["format"] == 5
    assert steps["plan"]["actor"] == {"kind": "agent"}
    assert steps["plan"]["role"] == {"skills": ["forgejo-pr-flow"], "min_grade": "strong"}
    # An agent step with no role table still has a role: no requirements, any grade.
    assert steps["implement"]["role"] == {"skills": [], "min_grade": None}
    # A non-agent step has no role at all.
    assert steps["review"]["role"] is None


@pytest.mark.parametrize("field", ["engine", "model"])
def test_format5_agent_step_refuses_an_engine_or_model(dev, field):
    _to_format5(dev)
    value = '"claude"' if field == "engine" else '"default"'
    _edit(
        _flow(dev),
        'actor = { kind = "agent" }\nrole',
        f'actor = {{ kind = "agent", {field} = {value} }}\nrole',
    )
    _refused(dev, f"actor.{field}", "names a role, not an engine or model")


def test_a_format4_file_with_a_role_is_refused(dev):
    _edit(_flow(dev), PLAN_ACTOR, PLAN_ACTOR + '\nrole = { skills = ["code-review"] }')
    _refused(dev, "role")


@pytest.mark.parametrize(
    "role, needle",
    [
        ('role = { skills = ["code.review"] }', "skills"),
        ('role = { skills = ["Code-Review"] }', "skills"),
        ('role = { skills = ["a", "a"] }', "duplicate"),
        ('role = { min_grade = "genius" }', "must be one of"),
        ('role = { skills = ["a"], model = "x" }', "model"),
    ],
)
def test_format5_role_values_are_closed(dev, role, needle):
    _to_format5(dev, plan_role=role)
    _refused(dev, needle)


def test_format5_moves_step_skills_into_the_role(dev):
    _to_format5(dev)
    _edit(
        _flow(dev),
        'actor = { kind = "agent" }\nrole',
        'skills = [{ id = "x" }]\nactor = { kind = "agent" }\nrole',
    )
    _refused(dev, "role.skills")


def test_only_an_agent_step_has_a_role(dev):
    _to_format5(dev)
    _edit(
        _flow(dev),
        'actor = { kind = "external", label = "Reviewer bot" }',
        'actor = { kind = "external", label = "Reviewer bot" }\nrole = { skills = ["x"] }',
    )
    _refused(dev, "only an agent step has a role")


def test_format5_flows_need_a_format5_bundle(dev):
    _to_format5(dev)
    _edit(_manifest(dev), "format = 5\n", "format = 4\n")
    _refused(dev, "playbook.toml: format", "require bundle format 5")


def test_distinct_from_constraints_follow_the_flow_format(dev):
    _to_format5(dev)
    steps = _steps(playbooks.load_bundle(dev))
    assert steps["review"]["distinct_from"] == [{"step": "implement", "constraint": "specialist"}]
    # The instance floor is implied in format 5: "session" is not a format 5 constraint.
    _edit(_flow(dev), 'constraint = "specialist"', 'constraint = "session"')
    _refused(dev, "constraint")


def test_format4_keeps_its_own_distinct_vocabulary(dev):
    _edit(
        _flow(dev),
        IMPLEMENT_ACTOR,
        IMPLEMENT_ACTOR + '\ndistinct_from = [{ step = "plan", constraint = "specialist" }]',
    )
    _refused(dev, "constraint")


# --- formats 1–4: read-only, never rewritten ---------------------------------------------------


def _hashes(root: Path) -> dict:
    return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}


def test_a_legacy_bundle_loads_unchanged_and_is_never_rewritten(dev):
    before = _hashes(dev)
    loaded = playbooks.load_bundle(dev)
    steps = _steps(loaded)
    assert steps["plan"]["actor"] == {"kind": "agent", "engine": "claude", "model": "default"}
    # The read normaliser: a role view with NO skills and the recorded agent as a non-binding hint.
    assert steps["plan"]["role"] == {
        "skills": [],
        "min_grade": None,
        "legacy_hint": {"engine": "claude", "model": "default"},
    }
    assert schema.binding_role(steps["plan"]) is None
    assert loaded["flows"]["dev"]["format"] == 1
    assert _hashes(dev) == before


def test_the_legacy_view_never_promotes_advisory_skills(dev):
    _edit(
        _flow(dev),
        PLAN_ACTOR,
        PLAN_ACTOR + '\nskills = [{ id = "forgejo-pr-flow", required = true }, { id = "notes" }]',
    )
    before = _hashes(dev)
    plan = _steps(playbooks.load_bundle(dev))["plan"]
    assert plan["role"]["skills"] == []  # not even the required reference: only an upgrade asks
    assert [s["id"] for s in plan["skills"]] == ["forgejo-pr-flow", "notes"]
    assert _hashes(dev) == before


# --- deployment: format 5 takes no assignment ---------------------------------------------------


def test_review_refuses_an_assignment_for_a_role_step(dev):
    _to_format5(dev)
    bundle = playbooks.load_bundle(dev)
    with pytest.raises(store.StoreError) as e:
        review._assignments(bundle, {"dev:plan": {"engine": "claude", "model": "default"}})
    assert "roles are filled at mission assembly" in str(e.value)
    rows, _facts = review._assignments(bundle, {})
    assert rows == []


def test_review_still_assigns_legacy_agent_steps(dev):
    bundle = playbooks.load_bundle(dev)
    rows, _facts = review._assignments(bundle, {})
    assert {row["step"] for row in rows} == {"dev:plan", "dev:implement"}


def test_flow_document_renders_a_role_and_names_no_agent(dev):
    from agent_sessions.playbooks import binding

    _to_format5(dev)
    bundle = playbooks.load_bundle(dev)
    supplied = [
        {"name": name, "kind": kind, "value": value}
        for name, kind, value in [
            ("repo_name", "text", "demo"),
            ("repo", "text", "example/demo"),
            ("forge_url", "text", "https://forge.example.com"),
            ("forge_token", "secret", "test-only-secret"),
            ("deploy_url", "text", "https://example.com/health"),
        ]
    ]
    resolved = binding.resolve(bundle, None, supplied)
    text = flow_document.render(
        bundle, resolved.text, [], binding.targets(bundle, resolved)
    ).data.decode("utf-8")
    assert "Role: needs `forgejo-pr-flow` · at least Specialist." in text
    assert "Role: needs no skills · at least any grade." in text
    assert "Actor: agent" not in text


# --- one skill-id grammar -----------------------------------------------------------------------


def test_one_skill_grammar_and_playbook_ids_unchanged():
    assert store.skill_id("a") == "a"
    assert store.skill_id("9-lives") == "9-lives"
    assert store.local_id("skill", "a") == "a"
    for bad in ("code.review", "Code", "a_b", "-a", "a" * 65, "", None):
        with pytest.raises(store.StoreError):
            store.skill_id(bad)
    # Playbook ids keep their own, narrower grammar.
    for bad in ("a", "9-lives"):
        with pytest.raises(store.StoreError):
            store.local_id("playbook", bad)
    assert schema.SKILL_REF_RE.pattern == r"^[a-z0-9][a-z0-9-]{0,63}$"


def test_authoring_vocabulary_offers_roles(monkeypatch):
    from agent_sessions.gallery import skills as skill_library

    monkeypatch.setattr(
        skill_library,
        "listing",
        lambda: {
            "skills": [
                {"id": "code-review", "files": {"SKILL.md": "---\nname: Code review\n---\nbody"}},
                {"id": "broken", "error": "unreadable"},
                {"id": "Bad.Id", "files": {}},
                {"id": "mail-triage", "files": {"SKILL.md": "no frontmatter"}},
            ]
        },
    )
    v = authoring.vocabulary()
    assert v["grades"] == ["fast", "strong", "frontier"]
    assert v["distinct_role"] == ["engine", "model", "specialist"]
    assert v["skills"] == [
        {"id": "code-review", "label": "Code review"},
        {"id": "mail-triage", "label": "mail-triage"},
    ]


# --- explicit upgrade, then Revert to format 4 (byte for byte) ----------------------------------


@pytest.fixture
def local_store(monkeypatch):
    from agent_sessions import fileedit
    from agent_sessions.playbooks import deployments, loader

    assert fileedit.install_lease_signal_handler()
    monkeypatch.setattr(loader, "BUNDLED_ROOT", FIXTURES)
    previous = deployments.set_registry(deployments._StoredDeployments())
    yield
    deployments.set_registry(previous)


def _legacy_files(new_id: str) -> dict:
    from agent_sessions.playbooks.tree import read_tree

    tree = read_tree(FIXTURES / "forge-workflow")
    out = {p: data.decode("utf-8") for p, data in tree.files.items()}
    out["playbook.toml"] = out["playbook.toml"].replace(
        'id = "forge-workflow"', f'id = "{new_id}"', 1
    )
    return out


def _upgraded(files: dict) -> dict:
    out = dict(files)
    out["playbook.toml"] = out["playbook.toml"].replace("format = 1\n", "format = 5\n", 1)
    flow = out["flows/dev.toml"].replace("format = 1\n", "format = 5\n", 1)
    flow = flow.replace(
        PLAN_ACTOR, 'actor = { kind = "agent" }\nrole = { skills = ["forgejo-pr-flow"] }', 1
    )
    flow = flow.replace(IMPLEMENT_ACTOR, 'actor = { kind = "agent" }', 1)
    out["flows/dev.toml"] = flow.replace('constraint = "session"', 'constraint = "specialist"', 1)
    return out


def test_revert_restores_the_exact_format4_bytes_and_keeps_the_format5_tree(local_store):
    legacy = _legacy_files("team-flow")
    created = store.create_playbook(legacy)
    upgraded = store.update_playbook("team-flow", created["revision"], _upgraded(legacy))
    assert upgraded["documents"]["playbook.toml"]["format"] == 5
    before = {e["name"] for e in store.recovery_entries()}

    reverted = store.revert_format("team-flow", upgraded["revision"])

    assert reverted["files"] == legacy
    assert reverted["revision"] == created["revision"]
    assert reverted["documents"]["playbook.toml"]["format"] == 1
    # The format 5 tree it replaced is retained, never dropped.
    after = {e["name"] for e in store.recovery_entries()}
    assert len(after - before) == 1
    assert reverted["reverted_from"] in before


def test_revert_is_fenced_by_the_loaded_revision(local_store):
    legacy = _legacy_files("team-flow")
    created = store.create_playbook(legacy)
    store.update_playbook("team-flow", created["revision"], _upgraded(legacy))
    with pytest.raises(store.StoreError) as e:
        store.revert_format("team-flow", created["revision"])  # stale: not the current revision
    assert e.value.status == 409


def test_revert_refuses_a_playbook_that_is_not_format5(local_store):
    created = store.create_playbook(_legacy_files("team-flow"))
    with pytest.raises(store.StoreError) as e:
        store.revert_format("team-flow", created["revision"])
    assert e.value.status == 409 and "not in format 5" in str(e.value)


def test_revert_without_a_retained_format4_copy_is_a_404(local_store):
    created = store.create_playbook(_upgraded(_legacy_files("team-flow")))
    assert store.get_playbook("team-flow")["revertible"] is False
    with pytest.raises(store.StoreError) as e:
        store.revert_format("team-flow", created["revision"])
    assert e.value.status == 404


def test_an_upgrade_is_revertible_and_later_format5_edits_keep_the_predecessor(local_store):
    legacy = _legacy_files("team-flow")
    created = store.create_playbook(legacy)
    assert store.get_playbook("team-flow")["revertible"] is False
    upgraded = store.update_playbook("team-flow", created["revision"], _upgraded(legacy))
    assert upgraded["revertible"] is True
    edited_files = dict(_upgraded(legacy))
    edited_files["README.md"] = edited_files.get("README.md", "") + "\nEdited after the upgrade.\n"
    edited = store.update_playbook("team-flow", upgraded["revision"], edited_files)
    assert edited["revertible"] is True and store.get_playbook("team-flow")["revertible"] is True
    reverted = store.revert_format("team-flow", edited["revision"])
    assert reverted["files"] == legacy and reverted["revertible"] is False


def test_revert_never_resurrects_a_deleted_book_under_a_reused_id(local_store):
    # Hermes 6947 #1: legacy `team-flow`, deleted; a DIFFERENT format 5 `team-flow` is created.
    old = store.create_playbook(_legacy_files("team-flow"))
    store.delete_playbook("team-flow", old["revision"])
    assert any(e["playbook_id"] == "team-flow" for e in store.recovery_entries())
    fresh = store.create_playbook(_upgraded(_legacy_files("team-flow")))
    assert store.get_playbook("team-flow")["revertible"] is False
    with pytest.raises(store.StoreError) as e:
        store.revert_format("team-flow", fresh["revision"])
    assert e.value.status == 404
    assert store.get_playbook("team-flow")["revision"] == fresh["revision"]


def test_revert_restores_the_immediate_predecessor_of_same_second_copies(local_store, monkeypatch):
    # Hermes 6947 #2: copies retained within one second share a timestamp; random suffixes are
    # not save order. Pin the clock so every retained copy has the same stamp.
    import time as real_time

    fixed = real_time.gmtime(1_800_000_000)

    class _Clock:
        def __getattr__(self, name):
            return getattr(real_time, name)

        @staticmethod
        def gmtime(*a):
            return fixed

    legacy = _legacy_files("team-flow")
    second = dict(legacy)
    second["README.md"] = second.get("README.md", "") + "\nSecond legacy edit.\n"
    with monkeypatch.context() as m:
        m.setattr(store, "time", _Clock())
        for attempt in range(6):  # several orders of random suffixes, each must restore `second`
            pid = f"same-second-{attempt}"
            a = store.create_playbook(_legacy_files(pid))
            files2 = {**second, "playbook.toml": a["files"]["playbook.toml"]}
            b = store.update_playbook(pid, a["revision"], files2)
            c = store.update_playbook(pid, b["revision"], _upgraded(files2))
            reverted = store.revert_format(pid, c["revision"])
            assert reverted["files"] == files2 and reverted["revision"] == b["revision"]


def test_the_card_projects_a_role_and_legacy_steps_stay_unchanged(dev):
    from agent_sessions.playbooks import loader

    legacy = loader.card(playbooks.load_bundle(dev))
    legacy_steps = {s["id"]: s for s in legacy["flows"][0]["steps"]}
    assert legacy_steps["plan"]["role"]["legacy_hint"] == {"engine": "claude", "model": "default"}
    assert "role" not in legacy_steps["review"]
    _to_format5(dev)
    card = loader.card(playbooks.load_bundle(dev))
    steps = {s["id"]: s for s in card["flows"][0]["steps"]}
    assert steps["plan"]["role"] == {"skills": ["forgejo-pr-flow"], "min_grade": "strong"}
    assert steps["plan"]["actor"] == {"kind": "agent"}
    assert "role" not in steps["review"]


def test_an_upgrade_that_cannot_record_its_predecessor_publishes_nothing(local_store, monkeypatch):
    # Hermes 6955 #1: a format 5 book without its pointer could never be reverted.
    import errno
    import os

    legacy = _legacy_files("team-flow")
    created = store.create_playbook(legacy)
    real = os.setxattr

    def setxattr(fd, name, *a, **kw):
        if name == store.LEGACY_XATTR:
            raise OSError(errno.ENOTSUP, "Operation not supported")
        return real(fd, name, *a, **kw)

    with monkeypatch.context() as m:
        m.setattr(os, "setxattr", setxattr)
        with pytest.raises(store.StoreError) as e:
            store.update_playbook("team-flow", created["revision"], _upgraded(legacy))
    assert e.value.status == 503 and "nothing was saved" in str(e.value)
    still = store.get_playbook("team-flow")
    assert still["revision"] == created["revision"] and still["files"] == legacy


def test_an_upgrade_whose_predecessor_cannot_be_retained_publishes_nothing(
    local_store, monkeypatch
):
    # Hermes 6958 #1: the copy the pointer names exists BEFORE anything publishes.
    legacy = _legacy_files("team-flow")
    created = store.create_playbook(legacy)
    with monkeypatch.context() as m:
        m.setattr(store, "_retain", lambda *a: None)
        with pytest.raises(store.StoreError) as e:
            store.update_playbook("team-flow", created["revision"], _upgraded(legacy))
    assert e.value.status == 503 and "nothing was saved" in str(e.value)
    still = store.get_playbook("team-flow")
    assert still["revision"] == created["revision"] and still["files"] == legacy


def test_an_upgrade_stays_revertible_when_the_displaced_tree_is_not_retained(
    local_store, monkeypatch
):
    # Hermes 6958 #1: a failed (or crash-interrupted) retention of the displaced live tree no
    # longer strands the pointer: it names the copy retained before publication.
    legacy = _legacy_files("team-flow")
    created = store.create_playbook(legacy)
    real, calls = store._retain, []

    def retain(*a):
        calls.append(a[1])
        return real(*a) if len(calls) == 1 else None  # 1st: the predecessor copy; 2nd: displaced

    with monkeypatch.context() as m:
        m.setattr(store, "_retain", retain)
        upgraded = store.update_playbook("team-flow", created["revision"], _upgraded(legacy))
    assert len(calls) == 2 and upgraded.get("recovery_durable") is False
    assert upgraded["revertible"] is True
    reverted = store.revert_format("team-flow", upgraded["revision"])
    assert reverted["files"] == legacy and reverted["revision"] == created["revision"]


def test_an_unreadable_skills_library_never_takes_the_authoring_schema_down(monkeypatch):
    # Reviewer finding: a damaged/unsafe/busy library raised out of `vocabulary()`, so the editor,
    # the New Project wizard and HQ's playbook_schema tool all failed with a 500.
    from agent_sessions.gallery import skills as skill_library
    from agent_sessions.plugins import provenance

    def broken():
        raise provenance.ProvenanceError("/secret/path/skills-library is writable by other users")

    monkeypatch.setattr(skill_library, "listing", broken)
    vocab = authoring.vocabulary()
    assert vocab["skills"] == [] and "grades" in vocab
    assert "cannot be read" in vocab["skills_unavailable"]
    assert "/secret/path" not in vocab["skills_unavailable"]  # no exception text reaches HQ
