"""The action registry is an ADAPTER over today's automations (#2248 P1).

* every stored config shape yields the IDENTICAL scope, digest and consent lines through its
  executor as through `automations` itself, and the digests are pinned: no receipt changes;
* a manifest carrying code, a command, a URL template or an unknown field is refused, and so is one
  naming an executor outside the closed table;
* every executor provides validate, scope, describe, widened and run, delegating;
* the registry, `automations.ACTION_KINDS` / `WEBHOOK_ACTIONS` and `automation_runner._ACTIONS`
  agree;
* `GET /api/actions` lists the five with their facts, requirements and use, behind login.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import tomllib
from importlib import resources
from pathlib import Path

import pytest

from agent_sessions import automation_runner
from agent_sessions import automations as model
from agent_sessions.actions import executors, registry
from agent_sessions.actions import manifest as mf
from agent_sessions.routes import automations as auto_routes

SESSION = "opencode:ses_0123456789abcdefABCDEFghij"
TPIN = {
    "template": {
        "id": "tpl_1",
        "digest": "d" * 64,
        "library": {"repo": "text", "token": "secret"},
        "name": "Review PR",
    },
    "checklist": {"id": "ship", "default": False, "label": "Ship a change", "digest": "c" * 64},
    "cwd": {"path": "/w/p", "real": "/srv/p"},
}
SPEC_PIN = {
    "specialist": {
        "id": "reviewer",
        "version": 3,
        "name": "Reviewer",
        "effort": "high",
        "engine": "some-api",
        "model": None,
    },
    "cwd": {"path": "/w/p", "real": "/srv/p"},
}
WEEKLY = {
    "kind": "schedule",
    "cadence": {"kind": "weekly", "days": ["mon", "fri"], "time": "07:30"},
    "tz": "Europe/Berlin",
}
HOOK = {
    "kind": "webhook",
    "webhook_id": "0a1b2c3d4e5f6a7b",
    "when": [{"path": "action", "op": "is", "value": "opened"}],
}

#: One of every stored config shape, with the pins it is approved under, and the scope digest it
#: had BEFORE the registry existed. A different digest here means a receipt would change.
SHAPES = {
    "mission, template, legacy checklist": (
        {
            "name": "m",
            "trigger": WEEKLY,
            "action": {
                "kind": "start_mission",
                "project_id": "p1",
                "instruction": {"template_id": "tpl_1", "values": {"pr": "12"}},
                "checklist_id": "ship",
                "autonomy": "dispatch",
            },
            "policy": {"expires_at": 2_000_000_000},
        },
        TPIN,
        "be54d4e684ca5d1fa74764d12d871350",
    ),
    "mission, text, propose": (
        {
            "name": "m2",
            "trigger": {"kind": "manual"},
            "action": {
                "kind": "start_mission",
                "project_id": "p1",
                "instruction": {"text": "Audit the dependencies"},
            },
        },
        {"template": None, "checklist": None, "cwd": TPIN["cwd"]},
        "430ac4091897a7ec94da00e48c41c074",
    ),
    "session, once, bypass": (
        {
            "name": "s",
            "trigger": {"kind": "once", "at": "2099-01-01T09:00", "tz": "UTC"},
            "action": {
                "kind": "start_session",
                "engine": "some-agent",
                "folder": "/w/p",
                "bypass": True,
                "message": {"text": "review the open PRs"},
            },
            "policy": {"concurrency": "allow", "max_concurrent": 2},
        },
        {"template": None, "checklist": None, "cwd": TPIN["cwd"]},
        "2fc5725d01fc986e6c02199adafbe154",
    ),
    "session, webhook, mapped template": (
        {
            "name": "s2",
            "trigger": HOOK,
            "action": {
                "kind": "start_session",
                "engine": "some-api",
                "folder": "/w/p",
                "message": {
                    "template_id": "tpl_1",
                    "values": {"repo": "shop"},
                    "from_delivery": {"pr": {"path": "number", "type": "number"}},
                },
            },
        },
        {"template": TPIN["template"], "checklist": None, "cwd": TPIN["cwd"]},
        "5d53c5c89e060632dfeda6b9f93db6da",
    ),
    "send, interval": (
        {
            "name": "t",
            "trigger": {
                "kind": "schedule",
                "cadence": {"kind": "interval", "every": 15, "unit": "minutes"},
                "tz": "UTC",
            },
            "action": {
                "kind": "send_to_session",
                "session_key": SESSION,
                "message": {"text": "go"},
            },
        },
        {"template": None, "checklist": None},
        "00ffe2eff955d077fb112dc5c7ecc4c3",
    ),
    "send, webhook": (
        {
            "name": "t2",
            "trigger": {"kind": "webhook", "webhook_id": "1b2c3d4e5f6a7b8c"},
            "action": {
                "kind": "send_to_session",
                "session_key": SESSION,
                "message": {"text": "a delivery came"},
            },
        },
        {"template": None, "checklist": None},
        "8e4202e69b710abf19704182471a0b55",
    ),
    "review loop, legacy webhook": (
        {
            "name": "r",
            "trigger": {"kind": "webhook", "scheme": "forgejo"},
            "action": {
                "kind": "forge_review_loop",
                "project_id": "p1",
                "repo": "acme/shop",
                "reviewer": {"specialist_id": "reviewer", "version": 3},
                "reviewer_login": "review-bot",
                "legs": {"issues": False, "pulls": True},
                "max_rounds": {"issue": 2, "pr": 4},
                "return_to_origin": False,
            },
        },
        SPEC_PIN,
        "e4eb91cb2471d8644d0dd4a68dcefd31",
    ),
    "review loop, webhook from the list": (
        {
            "name": "r2",
            "trigger": {"kind": "webhook", "webhook_id": "2c3d4e5f6a7b8c9d"},
            "action": {
                "kind": "forge_review_loop",
                "project_id": "p1",
                "repo": "acme/shop",
                "reviewer": {"specialist_id": "reviewer", "version": 3},
                "reviewer_login": "review-bot",
            },
        },
        SPEC_PIN,
        "f85ac32c081fb0b21f3a8658a5e73fd6",
    ),
    "tell HQ, template": (
        {
            "name": "h",
            "trigger": {
                "kind": "schedule",
                "cadence": {"kind": "daily", "time": "06:00"},
                "tz": "UTC",
            },
            "action": {
                "kind": "tell_hq",
                "message": {"template_id": "tpl_1", "values": {"pr": "12"}},
            },
        },
        {"template": TPIN["template"], "checklist": None},
        "5d1fdc62d8b163a5aca60402b4ee6667",
    ),
    "tell HQ, webhook text": (
        {
            "name": "h2",
            "trigger": HOOK,
            "action": {"kind": "tell_hq", "message": {"text": "a PR was opened"}},
        },
        {"template": None, "checklist": None},
        "ad3a723da1278d417bb64c846a74c56a",
    ),
}


def _stored(raw: dict) -> dict:
    return model.validate_config(copy.deepcopy(raw))


# ---- the adapter changes nothing --------------------------------------------------------------


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_every_stored_shape_yields_the_identical_scope_digest_and_lines(shape):
    raw, pins, _golden = SHAPES[shape]
    config = _stored(raw)
    ex = registry.get(config["action"]["kind"]).executor
    assert ex.validate(copy.deepcopy(raw["action"])) == model.validate_action(
        copy.deepcopy(raw["action"])
    )
    scope = ex.scope(config, pins)
    assert scope == model.scope_of(config, pins)
    assert auto_routes.scope_digest(scope) == auto_routes.scope_digest(model.scope_of(config, pins))
    preview = model.message_preview(config)
    assert ex.describe(scope, preview) == model.describe(scope, preview)
    assert ex.widened(scope, scope) == model.widened(scope, scope) == []
    assert ex.widened(None, scope) == model.widened(None, scope)
    narrower = {**scope, "max_runs_per_day": scope["max_runs_per_day"] + 1}
    assert ex.widened(scope, narrower) == model.widened(scope, narrower) != []


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_scope_digest_of_every_stored_shape_is_pinned(shape):
    raw, pins, golden = SHAPES[shape]
    config = _stored(raw)
    scope = registry.get(config["action"]["kind"]).executor.scope(config, pins)
    assert auto_routes.scope_digest(scope) == golden, (
        "this automation's consent receipt would no longer match: the registry must not change a "
        "stored scope"
    )


def test_the_shapes_cover_every_action_and_every_trigger_kind():
    configs = [_stored(raw) for raw, _p, _g in SHAPES.values()]
    # The five built-in kinds; the plugin kind's shapes are pinned in `test_action_plugins.py`.
    assert {c["action"]["kind"] for c in configs} == set(model.ACTION_KINDS) - {"plugin"}
    assert {c["trigger"]["kind"] for c in configs} == set(model.TRIGGER_KINDS)


def test_an_executor_refuses_another_kinds_action():
    raw, pins, _g = SHAPES["tell HQ, template"]
    config = _stored(raw)
    ex = registry.get("start_session").executor
    with pytest.raises(model.AutomationError):
        ex.validate(copy.deepcopy(raw["action"]))
    with pytest.raises(model.AutomationError):
        ex.scope(config, pins)
    scope = model.scope_of(config, pins)
    with pytest.raises(model.AutomationError):
        ex.describe(scope)
    with pytest.raises(model.AutomationError):
        ex.widened(scope, scope)
    with pytest.raises(model.AutomationError):
        asyncio.run(ex.run({}, config, pins, registry=None))


def test_every_executor_provides_the_five_functions():
    for kind, ex in executors.EXECUTORS.items():
        assert ex.id == kind
        for fn in ("validate", "scope", "describe", "widened", "run"):
            assert callable(getattr(ex, fn)), (kind, fn)
        assert asyncio.iscoroutinefunction(ex.run), kind


def test_run_delegates_to_the_runners_own_action(monkeypatch):
    seen = []

    async def fake(run, config, pins, *, registry):
        seen.append((run, config["action"]["kind"], pins, registry))
        return "ok", "delegated"

    raw, pins, _g = SHAPES["tell HQ, template"]
    config = _stored(raw)
    monkeypatch.setitem(automation_runner._ACTIONS, "tell_hq", fake)
    got = asyncio.run(
        registry.get("tell_hq").executor.run({"id": "r1"}, config, pins, registry="reg")
    )
    assert got == ("ok", "delegated")
    assert seen == [({"id": "r1"}, "tell_hq", pins, "reg")]


# ---- parity -----------------------------------------------------------------------------------


def test_the_registry_the_model_and_the_runner_agree():
    from agent_sessions.actions import plugins

    assert (*registry.builtin_kinds(), "plugin") == model.ACTION_KINDS
    assert tuple(automation_runner._ACTIONS) == model.ACTION_KINDS
    assert registry.plugin_ids() == plugins.PLUGINS
    assert tuple(executors.EXECUTORS) == (*executors.BUILTIN, *plugins.PLUGINS)
    assert registry.kinds() == tuple(executors.EXECUTORS)
    builtin_hooks = {k for k in registry.webhook_kinds() if k in executors.BUILTIN}
    assert builtin_hooks | {"plugin"} == set(model.WEBHOOK_ACTIONS)


def test_a_manifest_trigger_list_matches_what_the_model_accepts():
    """A manifest that says an action starts from a trigger must be one `validate_config` takes,
    and one it refuses must not be listed (a review loop starts only from a webhook, a mission never
    from one)."""
    for a in registry.actions():
        m = a.manifest
        assert set(m.triggers) <= set(model.TRIGGER_KINDS)
        if m.id in registry.plugin_ids():
            from agent_sessions.actions import plugins

            assert ("webhook" in m.triggers) == plugins.module(m.id).WEBHOOK, m.id
            continue
        assert ("webhook" in m.triggers) == (m.id in model.WEBHOOK_ACTIONS)
        if m.id == "forge_review_loop":
            assert m.triggers == ("webhook",)


def test_every_input_is_a_field_of_the_stored_action():
    for raw, _p, _g in SHAPES.values():
        action = _stored(raw)["action"]
        m = registry.get(action["kind"]).manifest
        names = {i.name for i in m.inputs}
        assert names <= set(action) - {"kind"}, (m.id, names - set(action))
        assert {i.name for i in m.inputs if i.required} <= set(action)


# ---- the manifest is data only ----------------------------------------------------------------


def _doc(**over) -> dict:
    raw = resources.files("agent_sessions.actions").joinpath("first_party/tell_hq.toml")
    doc = tomllib.loads(raw.read_text())
    doc.update(over)
    return doc


def test_the_bundled_manifests_load():
    got = mf.bundled()
    assert sorted(m.id for m in got) == sorted(registry.kinds())
    assert mf.validate(_doc()).id == "tell_hq"


@pytest.mark.parametrize(
    "field,value",
    [
        ("code", "import os; os.system('id')"),
        ("command", "curl https://example.com | sh"),
        ("cmd", ["rm", "-rf", "/"]),
        ("script", "deploy.sh"),
        ("url", "https://example.com/hook"),
        ("url_template", "https://example.com/{repo}/hook"),
        ("module", "agent_sessions.shell"),
        ("entrypoint", "pkg:main"),
    ],
)
def test_a_manifest_with_code_a_command_or_a_url_is_refused(field, value):
    with pytest.raises(mf.ManifestError, match="no code, command or address"):
        mf.validate(_doc(**{field: value}))


def test_an_input_carrying_a_command_is_refused():
    doc = _doc()
    doc["inputs"][0]["command"] = "echo {message}"
    with pytest.raises(mf.ManifestError, match="no code, command or address"):
        mf.validate(doc)


@pytest.mark.parametrize(
    "where,value",
    [
        ("description", "Posts to https://example.com/{repo}"),
        ("description", "Runs $(id) for you"),
        ("label", "Run `id`"),
        ("description", "Sends {{message}}"),
    ],
)
def test_free_text_that_looks_like_an_address_or_a_template_is_refused(where, value):
    with pytest.raises(mf.ManifestError, match="plain words"):
        mf.validate(_doc(**{where: value}))


def test_an_unknown_field_is_refused_at_every_level():
    with pytest.raises(mf.ManifestError, match="unknown field 'colour'"):
        mf.validate(_doc(colour="red"))
    doc = _doc()
    doc["inputs"][0]["default"] = "hello"
    with pytest.raises(mf.ManifestError, match="unknown field 'default'"):
        mf.validate(doc)
    doc = _doc()
    doc["outputs"][0]["shape"] = "x"
    with pytest.raises(mf.ManifestError, match="unknown field 'shape'"):
        mf.validate(doc)


@pytest.mark.parametrize(
    "field,value",
    [
        ("category", "anything"),
        ("effect", "write-everything"),
        ("requires", ["root"]),
        ("triggers", ["loop"]),
        ("contract", 2),
        ("contract", 0),
        ("id", "Not An Id"),
    ],
)
def test_values_outside_the_closed_sets_are_refused(field, value):
    with pytest.raises(mf.ManifestError):
        mf.validate(_doc(**{field: value}))


def test_an_unknown_executor_is_refused():
    manifests = mf.bundled()
    bad = [
        mf.validate({**_doc(), "id": "deploy", "executor": "deploy"}) if m.id == "tell_hq" else m
        for m in manifests
    ]
    with pytest.raises(registry.RegistryError, match="unknown executor 'deploy'"):
        registry.build(bad)


def test_every_executor_needs_exactly_one_manifest():
    manifests = mf.bundled()
    with pytest.raises(registry.RegistryError, match="no manifest describes"):
        registry.build([m for m in manifests if m.id != "tell_hq"])
    with pytest.raises(registry.RegistryError, match="two manifests"):
        registry.build([*manifests, manifests[0]])
    renamed = mf.validate({**_doc(), "id": "say_hello"})
    with pytest.raises(registry.RegistryError, match="named for its executor"):
        registry.build([renamed if m.id == "tell_hq" else m for m in manifests])


def test_a_manifest_that_is_not_toml_or_too_large_is_refused():
    with pytest.raises(mf.ManifestError, match="not valid TOML"):
        mf.parse(b"id = ")
    with pytest.raises(mf.ManifestError, match="too large"):
        mf.parse(b"#" * (mf.MANIFEST_MAX_BYTES + 1))


# ---- the gallery ------------------------------------------------------------------------------


def test_the_gallery_lists_the_five_with_their_facts_requirements_and_use(monkeypatch):
    rows = [
        {"config": _stored(SHAPES["tell HQ, template"][0])},
        {"config": _stored(SHAPES["tell HQ, webhook text"][0])},
        {"config": _stored(SHAPES["send, interval"][0])},
        {"config": None},  # an unreadable stored config uses nothing
    ]
    monkeypatch.setattr("agent_sessions.automations_store.list_all", lambda: rows)
    monkeypatch.setitem(registry._CHECKS, "api_agent", lambda: (False, "no API agent"))
    monkeypatch.setitem(registry._CHECKS, "forge", lambda: (True, "Forgejo is set up"))
    g = registry.gallery()
    by = {a["id"]: a for a in g["actions"]}
    assert list(by) == list(registry.kinds())
    assert by["tell_hq"]["used_by"] == 2 and by["send_to_session"]["used_by"] == 1
    assert by["start_mission"]["used_by"] == 0
    assert by["tell_hq"]["effect"] == "notify" and by["tell_hq"]["requires"] == []
    assert by["start_session"]["available"] is False
    assert by["start_session"]["requires"] == [
        {
            "id": "api_agent",
            "words": mf.REQUIREMENTS["api_agent"],
            "ok": False,
            "detail": "no API agent",
            "setup": "agents",
        }
    ]
    # A requirement that depends on the wizard's choice is not checked here: never "missing".
    assert by["send_to_session"]["requires"][0]["ok"] is None
    assert by["send_to_session"]["available"] is True
    assert by["forge_review_loop"]["available"] is True
    assert [c["id"] for c in g["categories"]] == list(mf.CATEGORIES)
    assert [e["level"] for e in g["effects"]] == [1, 2, 3, 4, 5]


def test_the_gallery_survives_an_unreadable_store(monkeypatch):
    def boom():
        raise RuntimeError("locked")

    monkeypatch.setattr("agent_sessions.automations_store.list_all", boom)
    g = registry.gallery()
    assert all(a["used_by"] is None for a in g["actions"])


def test_the_gallery_route_needs_a_login():
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from agent_sessions.routes import actions as routes

    def refused():
        raise HTTPException(401)

    app = FastAPI()
    routes.register(app, logged_in=refused)
    assert TestClient(app).get(routes.ACTIONS_API).status_code == 401

    app = FastAPI()
    routes.register(app, logged_in=lambda: "op")
    r = TestClient(app).get(routes.ACTIONS_API)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert [a["id"] for a in r.json()["actions"]] == list(registry.kinds())


# ---- the browser fixture is the server's own answer -------------------------------------------

FIXTURE = Path(__file__).resolve().parents[1] / "web/e2e/fixtures/actions-gallery.json"


def test_the_committed_browser_fixture_is_what_the_server_sends(monkeypatch):
    """`web/e2e/fixtures/actions-gallery.json` feeds the gallery and wizard-picker specs. It is
    `registry.gallery()` with every requirement met and fixed counts, so the browser tests can
    never pass against a shape or wording the server does not send. Regenerate deliberately with
    `AGENT_SESSIONS_WRITE_FIXTURES=1`."""
    monkeypatch.setitem(registry._CHECKS, "api_agent", lambda: (True, "an API agent is ready"))
    monkeypatch.setitem(registry._CHECKS, "forge", lambda: (True, "Forgejo is set up"))
    # Every requirement met; the browser specs flip one to meet the needs-setup state.
    monkeypatch.setitem(registry._CHECKS, "messenger", lambda: (True, "a messenger can send"))
    monkeypatch.setitem(registry._CHECKS, "artifacts", lambda: (True, "artifacts are on"))
    used = {"start_session": 3, "send_to_session": 1, "forge_review_loop": 2, "tell_hq": 4}
    monkeypatch.setattr(registry, "_used_by", lambda: used)
    text = json.dumps(registry.gallery(), indent=2, ensure_ascii=False) + "\n"
    if os.environ.get("AGENT_SESSIONS_WRITE_FIXTURES") == "1":
        FIXTURE.write_text(text)
    assert FIXTURE.read_text() == text, (
        "web/e2e/fixtures/actions-gallery.json is not what the server sends; regenerate it with "
        "AGENT_SESSIONS_WRITE_FIXTURES=1 and review the diff"
    )
