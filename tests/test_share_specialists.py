"""Share bundles carry specialists (#1714 P2): a specialist travels with its pinned skills, its
agent/model/effort only as a suggestion that the preview settles against this install, and the
import creates a new, inert version 1 that its own validator accepts — pinned to the skill copies
this import made, or reused by provenance."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from agent_sessions import engines, fileedit, model_choice, model_grades, specialists
from agent_sessions import specialists_store as pool
from agent_sessions.gallery import library, skills
from agent_sessions.gallery import storage as gallery_storage
from agent_sessions.playbooks import loader
from agent_sessions.sharebundle import format as fmt
from agent_sessions.sharebundle import kinds, service

AGENT = "claude"  # a terminal agent with a tool channel; its catalog offers MODEL
MODEL = "claude-opus-5"


def _install(root: Path, monkeypatch) -> Path:
    monkeypatch.setenv("AGENT_SESSIONS_HOME", str(root / "app"))
    monkeypatch.setenv("AGENT_SESSIONS_PLAYBOOKS_DIR", str(root / "playbooks"))
    monkeypatch.setenv("AGENT_SESSIONS_SPECIALISTS_DB", str(root / "specialists.db"))
    pool.reset_schema_cache_for_test()
    empty = root / "bundled"
    empty.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(loader, "BUNDLED_ROOT", empty)
    return root


@pytest.fixture
def startable(monkeypatch):
    """Which agents can start here (no binaries on the test host): all of them by default."""
    allowed: set[str] | None = None

    def can_start(prov):
        return prov is not None and (allowed is None or prov.engine_id in allowed)

    monkeypatch.setattr(engines.registry, "can_start", can_start)

    def only(*ids: str) -> None:
        nonlocal allowed
        allowed = set(ids)

    return only


@pytest.fixture
def install(tmp_path, monkeypatch, startable):
    assert fileedit.install_lease_signal_handler()
    return _install(tmp_path / "one", monkeypatch)


def _skill(sid: str, body: str = "") -> str:
    return library.create_skill(sid, {"SKILL.md": body or f"# {sid}\n\nDo {sid} well.\n"})[
        "revision"
    ]


def _specialist(title: str = "Implementer", pins: tuple[str, ...] = ("tdd",), **over) -> str:
    lib = specialists.library_revisions()
    draft = {
        "title": title,
        "summary": "Builds a phase to its acceptance criteria.",
        "skills": [{"id": s, "revision": lib[s]} for s in pins],
        "engine": AGENT,
        "model": MODEL,
        "effort": None,
        "capacity": 2,
        **over,
    }
    return pool.create(specialists.check_draft(draft))["id"]


def _export(*items: tuple[str, str]) -> bytes:
    _, data = service.export([{"kind": k, "id": i} for k, i in items])
    return data


def _plan(preview: dict) -> dict:
    return {i["key"]: i for i in preview["items"]}


def _keys(preview: dict) -> list[str]:
    return [i["key"] for i in preview["items"] if i["status"] not in ("refused", "reused")]


def _skill_dirs() -> list[str]:
    root = gallery_storage.skills_root()
    if not root.exists():
        return []
    return sorted(n.name for n in root.iterdir() if not n.name.startswith("."))


def _bundle_doc(data: bytes, sid: str) -> dict:
    bundle = fmt.read(data)
    return json.loads(bundle.item(f"specialist:{sid}").files[kinds.SPECIALIST_FILE])


def _second(tmp_path, monkeypatch) -> None:
    _install(tmp_path / "two", monkeypatch)


# ---- export ---------------------------------------------------------------------------------


def test_a_specialist_exports_with_its_pinned_skills_and_only_the_job(install):
    _skill("tdd")
    _skill("forgejo-pr-flow")
    _skill("unrelated")
    sid = _specialist(pins=("tdd", "forgejo-pr-flow"))
    data = _export(("specialist", sid))
    bundle = fmt.read(data)
    assert [i.key for i in bundle.items] == [
        "skill:tdd",
        "skill:forgejo-pr-flow",
        f"specialist:{sid}",
    ]
    item = bundle.item(f"specialist:{sid}")
    assert sorted(item.depends_on) == ["skill:forgejo-pr-flow", "skill:tdd"]
    assert set(item.files) == {kinds.SPECIALIST_FILE}
    doc = _bundle_doc(data, sid)
    # The job, and the agent/model/effort ONLY as a suggestion: no grade, notes, version history,
    # revisions, reservations or project names.
    assert doc == {
        "format": 1,
        "title": "Implementer",
        "summary": "Builds a phase to its acceptance criteria.",
        "capacity": 2,
        "skills": ["tdd", "forgejo-pr-flow"],
        "suggests": {"engine": AGENT, "model": MODEL, "effort": None},
    }


def test_the_current_version_is_exported(install):
    _skill("tdd")
    sid = _specialist()
    spec = pool.get(sid)
    body = {k: v for k, v in spec["current"].items() if k not in ("version", "created_at")}
    pool.update(sid, {**body, "title": "Implementer two", "capacity": 3}, expect_revision=1)
    doc = _bundle_doc(_export(("specialist", sid)), sid)
    assert (doc["title"], doc["capacity"]) == ("Implementer two", 3)


def test_a_pin_the_library_no_longer_holds_refuses_the_export(install):
    _skill("tdd")
    rev = _skill("code-review")
    stale = pool.create(
        {
            "title": "Reviewer",
            "summary": "",
            "skills": [{"id": "code-review", "revision": "0" * 64}],
            "engine": AGENT,
            "model": MODEL,
            "effort": None,
            "capacity": 1,
        }
    )["id"]
    with pytest.raises(service.ShareError, match="code-review changed since version 1") as exc:
        _export(("specialist", stale))
    assert "save a new version" in exc.value.detail
    gone = pool.create(
        {
            "title": "Gone",
            "summary": "",
            "skills": [{"id": "missing-skill", "revision": rev}],
            "engine": AGENT,
            "model": MODEL,
            "effort": None,
            "capacity": 1,
        }
    )["id"]
    with pytest.raises(service.ShareError, match="missing-skill is no longer in your skills"):
        _export(("specialist", gone))
    rows = {r["id"]: r for r in kinds.catalog() if r["kind"] == "specialist"}
    assert rows[stale]["exportable"] is False and "save a new version" in rows[stale]["reason"]
    assert rows[gone]["exportable"] is False


def test_the_pinned_bytes_are_proved_at_export_not_the_listing(install, monkeypatch):
    _skill("tdd")
    sid = _specialist()
    real = kinds._skill_export

    def edited(item_id):  # the tree changed between the pool read and the skill read
        out = real(item_id)
        out["files"] = {"SKILL.md": b"# tdd\n\nSomething else.\n"}
        return out

    monkeypatch.setattr(kinds, "_skill_export", edited)
    with pytest.raises(service.ShareError, match="tdd changed since version 1"):
        _export(("specialist", sid))


def test_an_archived_specialist_is_not_offered_or_exported(install):
    _skill("tdd")
    sid = _specialist()
    pool.set_archived(sid, True, expect_revision=1)
    assert not [r for r in kinds.catalog() if r["kind"] == "specialist"]
    with pytest.raises(service.ShareError, match="archived"):
        _export(("specialist", sid))


# ---- import: settled in the preview ---------------------------------------------------------


def test_round_trip_creates_an_inert_version_1_pinned_to_the_imported_skills(
    tmp_path, monkeypatch, install
):
    _skill("tdd")
    sid = _specialist()
    data = _export(("specialist", sid))
    _second(tmp_path, monkeypatch)
    monkeypatch.setattr(model_grades, "get", lambda: {AGENT: {MODEL: "strong"}})
    preview = service.preview(data)
    item = _plan(preview)[f"specialist:{sid}"]
    assert item["status"] == "new"
    view = item["specialist"]
    assert view["settled"] == {"engine": AGENT, "model": MODEL, "effort": None}
    assert view["settle_reason"] is None
    assert view["grade"] == "strong"  # derived HERE, from this install's map
    result = service.confirm(preview["token"], _keys(preview))
    assert {i["status"] for i in result["items"]} == {"created"}
    copy = pool.get(sid)
    assert copy["current"]["version"] == 1
    assert copy["current"]["skills"] == [{"id": "tdd", "revision": skills.get("tdd")["revision"]}]
    assert (copy["current"]["engine"], copy["current"]["model"]) == (AGENT, MODEL)
    assert pool.active() == []  # inert: nothing reserved, nothing launched
    assert service.imports()[f"specialist:{sid}"]["bundle"] == preview["bundle"]["digest"]


def test_conflicts_are_renamed_and_pins_follow_the_renamed_skill(install):
    _skill("tdd")
    sid = _specialist()
    data = _export(("specialist", sid))
    pool_before = pool.get(sid)
    preview = service.preview(data)
    plan = _plan(preview)
    assert plan["skill:tdd"]["dest"] == "tdd-2"
    assert plan[f"specialist:{sid}"]["dest"] == f"{sid}-2"
    assert plan[f"specialist:{sid}"]["uses"] == [{"from": "tdd", "to": "tdd-2"}]
    result = service.confirm(preview["token"], _keys(preview))
    assert {i["status"] for i in result["items"]} == {"created"}
    copy = pool.get(f"{sid}-2")
    assert copy["current"]["skills"] == [
        {"id": "tdd-2", "revision": skills.get("tdd-2")["revision"]}
    ]
    assert pool.get(sid) == pool_before  # nothing existing touched


def test_a_suggestion_that_does_not_hold_here_must_be_settled_before_anything_is_created(
    tmp_path, monkeypatch, install, startable
):
    _skill("tdd")
    sid = _specialist()
    data = _export(("specialist", sid))
    _second(tmp_path, monkeypatch)
    other = next(
        p.engine_id
        for p in engines.all_providers()
        if p.manifest
        and p.manifest.identity.kind == "agent"
        and p.manifest.tools_mcp is not None
        and p.manifest.runtime == "pty"
        and p.engine_id != AGENT
    )
    startable(other)  # the suggested agent is not installed here
    preview = service.preview(data)
    view = _plan(preview)[f"specialist:{sid}"]["specialist"]
    assert view["settled"] is None
    assert f"Suggested {AGENT} · {MODEL}" in view["settle_reason"]
    with pytest.raises(service.ShareError) as exc:
        service.confirm(preview["token"], _keys(preview))
    assert exc.value.extra["reason"] == "settle"
    assert f"specialist:{sid}" in exc.value.extra["items"]
    assert _skill_dirs() == []  # nothing created
    assert pool.ids() == []
    choice = {"engine": other, "model": "default", "effort": None}
    result = service.confirm(preview["token"], _keys(preview), {f"specialist:{sid}": choice})
    assert {i["status"] for i in result["items"]} == {"created"}
    assert (pool.get(sid)["current"]["engine"], pool.get(sid)["current"]["model"]) == (
        other,
        "default",
    )


def test_an_invalid_choice_is_refused_before_anything_is_written(tmp_path, monkeypatch, install):
    _skill("tdd")
    sid = _specialist()
    data = _export(("specialist", sid))
    _second(tmp_path, monkeypatch)
    preview = service.preview(data)
    bad = {"engine": AGENT, "model": "no-such-model-9", "effort": None}
    with pytest.raises(service.ShareError) as exc:
        service.confirm(preview["token"], _keys(preview), {f"specialist:{sid}": bad})
    assert exc.value.extra["reason"] == "settle"
    assert exc.value.status == 422
    assert pool.ids() == [] and _skill_dirs() == []
    # No journal was written: the same preview takes a corrected choice.
    result = service.confirm(
        preview["token"], _keys(preview), {f"specialist:{sid}": {**bad, "model": MODEL}}
    )
    assert {i["status"] for i in result["items"]} == {"created"}


def test_an_api_agent_settles_model_and_effort_like_a_save(tmp_path, monkeypatch, install):
    api = next(
        (p for p in engines.all_providers() if p.manifest and p.manifest.runtime == "api"), None
    )
    if api is None or api.manifest.tools_mcp is None:
        pytest.skip("no API client with a tool channel registered")

    def choice(prov, model, effort):
        if effort not in (None, "high"):
            raise model_choice.ModelRefused("not_offered", f"offers no effort {effort!r}")
        return model, effort

    monkeypatch.setattr(model_choice, "select_api_choice", choice)
    _skill("tdd")
    sid = _specialist(engine=api.engine_id, model="m-1", effort="high")
    data = _export(("specialist", sid))
    _second(tmp_path, monkeypatch)
    preview = service.preview(data)
    view = _plan(preview)[f"specialist:{sid}"]["specialist"]
    assert view["settled"] == {"engine": api.engine_id, "model": "m-1", "effort": "high"}
    with pytest.raises(service.ShareError) as exc:
        service.confirm(
            preview["token"],
            _keys(preview),
            {f"specialist:{sid}": {"engine": api.engine_id, "model": "m-1", "effort": "max"}},
        )
    assert "effort" in exc.value.extra["items"][f"specialist:{sid}"]
    service.confirm(preview["token"], _keys(preview))
    assert pool.get(sid)["current"]["effort"] == "high"


def test_a_specialist_no_agent_here_could_run_is_refused_in_the_preview(
    tmp_path, monkeypatch, install, startable
):
    _skill("tdd")
    sid = _specialist()
    data = _export(("specialist", sid))
    _second(tmp_path, monkeypatch)
    no_channel = [
        p.engine_id
        for p in engines.all_providers()
        if p.manifest and p.manifest.identity.kind == "agent" and p.manifest.tools_mcp is None
    ]
    startable(*no_channel)  # only agents without a tool channel can start here
    plan = _plan(service.preview(data))
    assert plan[f"specialist:{sid}"]["status"] == "refused"
    assert "can load skills" in plan[f"specialist:{sid}"]["reason"]
    assert plan["skill:tdd"]["status"] == "new"  # the skill alone still imports


def test_deselecting_a_pinned_skill_refuses_the_specialist(tmp_path, monkeypatch, install):
    _skill("tdd")
    sid = _specialist()
    data = _export(("specialist", sid))
    _second(tmp_path, monkeypatch)
    preview = service.preview(data)
    result = service.confirm(preview["token"], [f"specialist:{sid}"])
    row = next(i for i in result["items"] if i["key"] == f"specialist:{sid}")
    assert row["status"] == "refused"
    assert "needs skill tdd, which isn't being imported" in row["reason"]
    assert pool.ids() == []


def test_a_retry_must_send_the_same_choices_and_never_makes_a_second_copy(
    tmp_path, monkeypatch, install
):
    _skill("tdd")
    sid = _specialist()
    data = _export(("specialist", sid))
    _second(tmp_path, monkeypatch)
    preview = service.preview(data)
    choice = {f"specialist:{sid}": {"engine": AGENT, "model": "default", "effort": None}}
    first = service.confirm(preview["token"], _keys(preview), choice)
    monkeypatch.setattr(kinds, "publish", lambda *a: pytest.fail("published twice"))
    assert service.confirm(preview["token"], _keys(preview), choice) == first
    with pytest.raises(service.ShareError) as exc:
        service.confirm(preview["token"], _keys(preview))  # the suggestion instead of the choice
    assert exc.value.extra["reason"] == "selection"
    assert pool.ids() == [sid]


def test_a_crash_after_creation_reconciles_to_created_by_digest(tmp_path, monkeypatch, install):
    _skill("tdd")
    sid = _specialist()
    data = _export(("specialist", sid))
    _second(tmp_path, monkeypatch)
    preview = service.preview(data)
    real = service._record_outcome

    class Crash(BaseException):
        pass

    def crash_on_specialist(journal, key, **outcome):
        if key.startswith("specialist:"):
            raise Crash()
        return real(journal, key, **outcome)

    monkeypatch.setattr(service, "_record_outcome", crash_on_specialist)
    with pytest.raises(Crash):
        service.confirm(preview["token"], _keys(preview))
    assert pool.ids() == [sid]  # created before the crash
    monkeypatch.setattr(service, "_record_outcome", real)
    monkeypatch.setattr(kinds, "publish", lambda *a: pytest.fail("created twice"))
    result = service.confirm(preview["token"], _keys(preview))
    assert {i["status"] for i in result["items"]} == {"created"}


def test_a_specialist_already_at_the_destination_is_never_adopted(tmp_path, monkeypatch, install):
    _skill("tdd")
    sid = _specialist()
    data = _export(("specialist", sid))
    _second(tmp_path, monkeypatch)
    preview = service.preview(data)
    real = kinds._specialist_publish

    def other_first(dest, body):  # someone else takes the id between plan and publish
        pool.create({**body, "title": "Someone else's"}, sid=dest)
        return real(dest, body)

    monkeypatch.setattr(kinds, "_specialist_publish", other_first)
    result = service.confirm(preview["token"], _keys(preview))
    row = next(i for i in result["items"] if i["key"] == f"specialist:{sid}")
    assert row["status"] == "failed" and "nothing was overwritten" in row["reason"]
    assert pool.get(sid)["current"]["title"] == "Someone else's"


def test_a_choice_that_stops_holding_before_apply_fails_that_item_and_writes_nothing(
    tmp_path, monkeypatch, install
):
    _skill("tdd")
    sid = _specialist()
    data = _export(("specialist", sid))
    _second(tmp_path, monkeypatch)
    preview = service.preview(data)
    real = specialists.check_draft

    def gone(draft, **kw):
        raise specialists.DraftRefused(f"{AGENT} cannot start a session on this host")

    monkeypatch.setattr(specialists, "check_draft", gone)
    result = service.confirm(preview["token"], _keys(preview))
    row = next(i for i in result["items"] if i["key"] == f"specialist:{sid}")
    assert row["status"] == "failed"
    assert row["reason"].startswith("Nothing was written:")
    assert pool.ids() == []
    monkeypatch.setattr(specialists, "check_draft", real)
    again = service.confirm(preview["token"], _keys(preview))  # nothing journaled: tried again
    assert {i["status"] for i in again["items"]} == {"created"}


# ---- skills reused by provenance ------------------------------------------------------------


def test_a_skill_imported_from_the_same_version_is_reused_not_copied(
    tmp_path, monkeypatch, install
):
    _skill("tdd")
    sid = _specialist()
    data = _export(("specialist", sid))
    _second(tmp_path, monkeypatch)
    first = service.preview(data)
    service.confirm(first["token"], _keys(first))
    revision = skills.get("tdd")["revision"]
    # The same file again: the skill came from this very content and is unchanged.
    again = service.preview(data)
    plan = _plan(again)
    assert plan["skill:tdd"]["status"] == "reused"
    assert plan["skill:tdd"]["dest"] == "tdd"
    assert plan[f"specialist:{sid}"]["dest"] == f"{sid}-2"
    result = service.confirm(again["token"], _keys(again))
    statuses = {i["key"]: i["status"] for i in result["items"]}
    assert statuses == {"skill:tdd": "reused", f"specialist:{sid}": "created"}
    assert _skill_dirs() == ["tdd"]  # no second copy
    assert pool.get(f"{sid}-2")["current"]["skills"] == [{"id": "tdd", "revision": revision}]


def test_a_same_named_skill_without_provenance_or_since_changed_is_never_reused(
    tmp_path, monkeypatch, install
):
    _skill("tdd")
    sid = _specialist()
    data = _export(("specialist", sid))
    _second(tmp_path, monkeypatch)
    _skill("tdd")  # same name AND same content, but never imported: no provenance
    plan = _plan(service.preview(data))
    assert plan["skill:tdd"]["status"] == "renamed"
    # Imported, then replaced by other content under the same id: not the same version any more.
    preview = service.preview(data)
    service.confirm(preview["token"], _keys(preview))
    assert service.imports()["skill:tdd-2"]
    shutil.rmtree(gallery_storage.skills_root() / "tdd-2")
    _skill("tdd-2", "# tdd-2\n\nEdited since.\n")
    plan = _plan(service.preview(data))
    assert plan["skill:tdd"]["status"] == "renamed"
    assert plan["skill:tdd"]["dest"] == "tdd-3"


def test_a_reused_skill_changed_after_the_preview_never_gets_pinned(tmp_path, monkeypatch, install):
    _skill("tdd")
    sid = _specialist()
    data = _export(("specialist", sid))
    _second(tmp_path, monkeypatch)
    first = service.preview(data)
    service.confirm(first["token"], _keys(first))
    again = service.preview(data)
    shutil.rmtree(gallery_storage.skills_root() / "tdd")
    _skill("tdd", "# tdd\n\nSomething else.\n")
    with pytest.raises(service.ShareError) as exc:
        service.confirm(again["token"], _keys(again))
    assert exc.value.extra["reason"] == "changed"
    assert pool.ids() == [sid]


def test_a_reused_skill_that_moves_during_apply_holds_its_dependent(tmp_path, monkeypatch, install):
    _skill("tdd")
    sid = _specialist()
    data = _export(("specialist", sid))
    _second(tmp_path, monkeypatch)
    first = service.preview(data)
    service.confirm(first["token"], _keys(first))
    again = service.preview(data)
    real = service._fresh

    def then_edit(record, journal):  # the library moves after the freshness check
        out = real(record, journal)
        shutil.rmtree(gallery_storage.skills_root() / "tdd")
        _skill("tdd", "# tdd\n\nSomething else.\n")
        return out

    monkeypatch.setattr(service, "_fresh", then_edit)
    result = service.confirm(again["token"], _keys(again))
    row = next(i for i in result["items"] if i["key"] == f"specialist:{sid}")
    assert row["status"] == "stale"
    assert pool.ids() == [sid]


# ---- hostile specialist items ---------------------------------------------------------------


def _bundle_with(doc: object, *, deps: tuple[str, ...] = ("tdd",), raw: bytes | None = None):
    items = [
        {
            "kind": "skill",
            "id": s,
            "title": s,
            "files": {"SKILL.md": f"# {s}\n".encode()},
            "depends_on": [],
        }
        for s in deps
    ]
    data = raw if raw is not None else json.dumps(doc).encode()
    items.append(
        {
            "kind": "specialist",
            "id": "imp",
            "title": "Imp",
            "files": {kinds.SPECIALIST_FILE: data},
            "depends_on": [f"skill:{s}" for s in deps],
        }
    )
    return fmt.write(items, created_at="2026-10-29T00:00:00Z", app_version="test")


GOOD = {
    "format": 1,
    "title": "Imp",
    "summary": "",
    "capacity": 1,
    "skills": ["tdd"],
    "suggests": {"engine": AGENT, "model": MODEL, "effort": None},
}


@pytest.mark.parametrize(
    "doc, needle",
    [
        ({**GOOD, "notes": "project secrets"}, "fields of format 1"),
        ({**GOOD, "format": 2}, "newer BattleLab"),
        ({**GOOD, "capacity": 99}, "capacity must be"),
        ({**GOOD, "capacity": True}, "capacity must be"),
        ({**GOOD, "title": ""}, "title is required"),
        ({**GOOD, "title": "x" * 61}, "longer than"),
        ({**GOOD, "skills": ["tdd", "tdd"]}, "distinct"),
        ({**GOOD, "suggests": {"engine": AGENT}}, "suggestion"),
        ({**GOOD, "suggests": {**GOOD["suggests"], "engine": "a\nb"}}, "not a name"),
        ({**GOOD, "grade": "frontier"}, "fields of format 1"),
    ],
)
def test_a_malformed_specialist_is_refused_per_item(install, doc, needle):
    plan = _plan(service.preview(_bundle_with(doc)))
    assert plan["specialist:imp"]["status"] == "refused"
    assert needle in plan["specialist:imp"]["reason"]
    assert plan["skill:tdd"]["status"] == "new"


def test_ambiguous_json_and_floats_are_refused(install):
    raw = b'{"format": 1, "format": 1, "title": "Imp", "summary": "", "capacity": 1, ' + (
        b'"skills": ["tdd"], "suggests": {"engine": null, "model": null, "effort": null}}'
    )
    assert _plan(service.preview(_bundle_with(None, raw=raw)))["specialist:imp"]["status"] == (
        "refused"
    )
    raw = json.dumps({**GOOD, "capacity": 1.0}).encode()
    assert _plan(service.preview(_bundle_with(None, raw=raw)))["specialist:imp"]["status"] == (
        "refused"
    )


def test_a_pin_the_file_does_not_carry_is_refused_never_matched_by_name(install):
    _skill("tdd")  # a local tdd exists, but the specialist's pins must travel with it
    data = _bundle_with({**GOOD, "skills": ["tdd", "other"]})
    plan = _plan(service.preview(data))
    assert plan["specialist:imp"]["status"] == "refused"
    assert "pins skill other, which this file doesn't carry" in plan["specialist:imp"]["reason"]


def test_an_unknown_specialist_id_shape_is_refused(install):
    items = [
        {
            "kind": "specialist",
            "id": "under_score",
            "title": "x",
            "files": {kinds.SPECIALIST_FILE: json.dumps({**GOOD, "skills": []}).encode()},
            "depends_on": [],
        }
    ]
    data = fmt.write(items, created_at="2026-10-29T00:00:00Z", app_version="test")
    plan = _plan(service.preview(data))
    assert plan["specialist:under_score"]["status"] == "refused"


def test_a_suggestion_with_no_agent_asks_for_one(install):
    data = _bundle_with(
        {**GOOD, "skills": [], "suggests": dict.fromkeys(GOOD["suggests"])}, deps=()
    )
    preview = service.preview(data)
    view = _plan(preview)["specialist:imp"]["specialist"]
    assert view["settled"] is None and "suggests no agent" in view["settle_reason"]
    with pytest.raises(service.ShareError) as exc:
        service.confirm(preview["token"], ["specialist:imp"])
    assert exc.value.extra["items"]["specialist:imp"] == "Choose the agent it runs on."


@pytest.mark.parametrize(
    "settle",
    [
        [],
        {"skill:tdd": {"engine": AGENT, "model": MODEL, "effort": None}},
        {"specialist:imp": {"engine": AGENT, "model": MODEL}},
        {"specialist:imp": {"engine": "", "model": MODEL, "effort": None}},
        {"specialist:imp": {"engine": AGENT, "model": 7, "effort": None}},
        {"specialist:nope": {"engine": AGENT, "model": MODEL, "effort": None}},
    ],
)
def test_malformed_choices_are_refused(install, settle):
    preview = service.preview(_bundle_with(GOOD))
    with pytest.raises(service.ShareError) as exc:
        service.confirm(preview["token"], _keys(preview), settle)
    assert exc.value.status == 422
    assert pool.ids() == []


def test_a_reused_skill_whose_provenance_is_gone_holds_its_dependent(
    tmp_path, monkeypatch, install
):
    _skill("tdd")
    sid = _specialist()
    data = _export(("specialist", sid))
    _second(tmp_path, monkeypatch)
    first = service.preview(data)
    service.confirm(first["token"], _keys(first))
    again = service.preview(data)
    assert _plan(again)["skill:tdd"]["status"] == "reused"
    monkeypatch.setattr(service, "imports", lambda: {})  # the record no longer names it
    result = service.confirm(again["token"], _keys(again))
    statuses = {i["key"]: i["status"] for i in result["items"]}
    assert statuses[f"specialist:{sid}"] == "stale"
    assert pool.ids() == [sid]


def test_reused_is_reported_on_a_lost_response_replay_and_in_the_receipt(
    tmp_path, monkeypatch, install
):
    _skill("tdd")
    sid = _specialist()
    data = _export(("specialist", sid))
    _second(tmp_path, monkeypatch)
    first = service.preview(data)
    service.confirm(first["token"], _keys(first))
    again = service.preview(data)
    result = service.confirm(again["token"], _keys(again))
    replay = service.confirm(again["token"], _keys(again))
    assert replay == result
    assert {i["key"]: i["status"] for i in replay["items"]}["skill:tdd"] == "reused"
    record = service.state.read_json(f"preview-{again['token']}.json")
    journal = service.state.read_json(f"journal-{again['token']}.json")
    receipt = service._receipt(again["token"], record, journal)
    assert {i["key"]: i["status"] for i in receipt["result"]["items"]}["skill:tdd"] == "reused"


# ---- review round 1 (independent) -----------------------------------------------------------


@pytest.mark.parametrize(
    "damage",
    ["x", ["x"], {"skills": ["tdd"], "settled": "x"}, {"skills": ["tdd"], "settled": ["a"]}],
)
def test_a_damaged_specialist_view_is_a_503_never_a_500(tmp_path, monkeypatch, install, damage):
    _skill("tdd")
    sid = _specialist()
    data = _export(("specialist", sid))
    _second(tmp_path, monkeypatch)
    preview = service.preview(data)
    name = f"preview-{preview['token']}.json"
    record = service.state.read_json(name)
    for item in record["items"]:
        if item["kind"] == "specialist":
            item["specialist"] = damage
    service.state.write_json(name, record)
    with pytest.raises(service.state.StateError):
        service.confirm(preview["token"], _keys(preview))
    assert pool.ids() == []


def test_a_damaged_view_on_a_retry_is_a_503_never_a_500(tmp_path, monkeypatch, install):
    _skill("tdd")
    sid = _specialist()
    data = _export(("specialist", sid))
    _second(tmp_path, monkeypatch)
    preview = service.preview(data)
    real = specialists.check_draft
    monkeypatch.setattr(
        specialists,
        "check_draft",
        lambda *a, **k: (_ for _ in ()).throw(specialists.DraftRefused("gone")),
    )
    service.confirm(preview["token"], _keys(preview))  # the skill lands, the specialist fails
    monkeypatch.setattr(specialists, "check_draft", real)
    name = f"preview-{preview['token']}.json"
    record = service.state.read_json(name)
    for item in record["items"]:
        if item["kind"] == "specialist":
            item["specialist"]["settled"] = "x"
    service.state.write_json(name, record)
    with pytest.raises(service.state.StateError):
        service.confirm(preview["token"], _keys(preview))
    assert pool.ids() == []


def test_a_long_specialist_id_is_renamed_within_the_stores_length(install):
    _skill("tdd")
    _specialist(title="a" * 45)
    long_id = _specialist(title="a" * 45)  # the collision suffix makes a 47-character id
    assert len(long_id) == 47
    preview = service.preview(_export(("specialist", long_id)))
    entry = _plan(preview)[f"specialist:{long_id}"]
    assert entry["status"] == "renamed"
    assert len(entry["dest"]) <= 48 and entry["dest"].endswith("-2")
    service.confirm(preview["token"], _keys(preview))
    assert entry["dest"] in pool.ids()


def test_a_skills_only_import_never_depends_on_the_specialist_store(tmp_path, monkeypatch, install):
    _skill("tdd")
    data = _export(("skill", "tdd"))
    _second(tmp_path, monkeypatch)

    def locked():
        raise pool.StoreError("the specialist store is unavailable: locked", status=503)

    monkeypatch.setattr(pool, "ids", locked)
    preview = service.preview(data)
    result = service.confirm(preview["token"], _keys(preview))
    assert {i["status"] for i in result["items"]} == {"created"}


def test_a_record_made_before_specialists_were_bound_stays_fresh(tmp_path, monkeypatch, install):
    _skill("tdd")
    data = _export(("skill", "tdd"))
    _second(tmp_path, monkeypatch)
    _specialist_pool_member = pool.create(
        {
            "title": "Here already",
            "summary": "",
            "skills": [],
            "engine": AGENT,
            "model": MODEL,
            "effort": None,
            "capacity": 1,
        }
    )
    assert _specialist_pool_member
    preview = service.preview(data)
    record = service.state.read_json(f"preview-{preview['token']}.json")
    assert "specialist" not in record["bindings"]
    result = service.confirm(preview["token"], _keys(preview))
    assert {i["status"] for i in result["items"]} == {"created"}


def test_one_check_per_distinct_suggestion_per_preview(install, monkeypatch):
    calls = []
    real = specialists.runs_on

    def counted(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(specialists, "runs_on", counted)
    items = [
        {
            "kind": "specialist",
            "id": f"imp-{n}",
            "title": "Imp",
            "files": {kinds.SPECIALIST_FILE: json.dumps({**GOOD, "skills": []}).encode()},
            "depends_on": [],
        }
        for n in range(5)
    ]
    service.preview(fmt.write(items, created_at="2026-10-29T00:00:00Z", app_version="test"))
    assert len(calls) == 1


def test_the_catalog_says_an_unreadable_pin_cannot_be_read(install, monkeypatch):
    _skill("tdd")
    sid = _specialist()
    real = kinds._skill_listing
    monkeypatch.setattr(
        kinds,
        "_skill_listing",
        lambda: [{**s, "error": "unreadable"} if s["id"] == "tdd" else s for s in real()],
    )
    row = next(r for r in kinds.catalog() if r["id"] == sid)
    assert "can't be read" in row["reason"]


# ---- Hermes review 7429 -------------------------------------------------------------------------


def test_two_identical_skills_keep_two_identities_when_one_is_reused(
    tmp_path, monkeypatch, install
):
    same = {"SKILL.md": b"# same\n"}
    items = [
        {"kind": "skill", "id": s, "title": s, "files": same, "depends_on": []} for s in ("a", "b")
    ]
    items.append(
        {
            "kind": "specialist",
            "id": "imp",
            "title": "Imp",
            "files": {kinds.SPECIALIST_FILE: json.dumps({**GOOD, "skills": ["a", "b"]}).encode()},
            "depends_on": ["skill:a", "skill:b"],
        }
    )
    data = fmt.write(items, created_at="2026-10-29T00:00:00Z", app_version="test")
    first = service.preview(data)
    service.confirm(first["token"], ["skill:a"])  # only `a` arrives first
    again = service.preview(data)
    plan = _plan(again)
    assert plan["skill:a"]["status"] == "reused" and plan["skill:a"]["dest"] == "a"
    assert plan["skill:b"]["status"] == "new" and plan["skill:b"]["dest"] == "b"
    result = service.confirm(again["token"], _keys(again))
    statuses = {i["key"]: i["status"] for i in result["items"]}
    assert statuses == {"skill:a": "reused", "skill:b": "created", "specialist:imp": "created"}
    assert [k["id"] for k in pool.get("imp")["current"]["skills"]] == ["a", "b"]


def test_a_skill_changed_after_validation_never_gets_pinned(tmp_path, monkeypatch, install):
    _skill("tdd")
    sid = _specialist()
    data = _export(("specialist", sid))
    _second(tmp_path, monkeypatch)
    preview = service.preview(data)
    real = specialists.check_draft

    def then_edit(draft, **kw):  # validated, then the skill moves before the create
        out = real(draft, **kw)
        shutil.rmtree(gallery_storage.skills_root() / "tdd")
        _skill("tdd", "# tdd\n\nSomething else.\n")
        return out

    monkeypatch.setattr(specialists, "check_draft", then_edit)
    result = service.confirm(preview["token"], _keys(preview))
    row = next(i for i in result["items"] if i["key"] == f"specialist:{sid}")
    assert row["status"] == "failed"
    assert "changed or can't be read during the import" in row["reason"]
    assert pool.ids() == []


def test_a_library_writer_waits_for_the_specialist_create(tmp_path, monkeypatch, install):
    import threading

    _skill("tdd")
    sid = _specialist()
    data = _export(("specialist", sid))
    _second(tmp_path, monkeypatch)
    preview = service.preview(data)
    real = pool.create
    writer: dict = {}

    def create_while_a_writer_tries(body, **kw):
        t = threading.Thread(target=lambda: writer.update(rev=_skill("other")), daemon=True)
        t.start()
        t.join(0.5)
        writer["blocked"] = t.is_alive()  # the library writer is fenced out of the create
        writer["thread"] = t
        return real(body, **kw)

    monkeypatch.setattr(pool, "create", create_while_a_writer_tries)
    result = service.confirm(preview["token"], _keys(preview))
    assert {i["status"] for i in result["items"]} == {"created"}
    assert writer["blocked"] is True
    writer["thread"].join(10)
    assert writer.get("rev")  # and it lands once the create is done
