"""Project session defaults (#1508): an optional ``launch_defaults`` on a project — what a NEW
session in it starts with — validated like the global ``agent_defaults``, and never read by a
launch path (only the New session form preselects from it)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_sessions import native_models, projects
from agent_sessions.main import create_app
from agent_sessions.native_models import ModelList

API = "codex-api"
MODELS = ModelList(
    status="ok",
    models=(
        {
            "id": "gpt-5.5-codex",
            "label": None,
            "description": None,
            "efforts": ["low", "high"],
            "is_default": True,
        },
        {
            "id": "gpt-5-mini",
            "label": None,
            "description": None,
            "efforts": [],
            "is_default": False,
        },
    ),
)


@pytest.fixture
def listing(monkeypatch):
    box = {"value": MODELS, "calls": 0}

    def models(prov):
        box["calls"] += 1
        return box["value"]

    monkeypatch.setattr(native_models, "models", models)
    return box


def _err(fn) -> str:
    with pytest.raises(projects.ProjectError) as e:
        fn()
    assert e.value.status == 422
    return str(e.value)


def test_absent_and_empty_store_nothing(tmp_home):
    p = projects.create("A", folders=["/a"])
    assert p.launch_defaults == {} and p.as_dict()["launch_defaults"] == {}
    p = projects.create("B", folders=["/b"], launch_defaults={})
    raw = json.loads(Path(projects._default_path()).read_text())["projects"][p.id]
    assert "launch_defaults" not in raw


def test_terminal_agent_model_is_stored_canonical(tmp_home):
    p = projects.create(
        "A", folders=["/a"], launch_defaults={"engine": "claude", "model": "opus", "bypass": False}
    )
    assert p.launch_defaults == {"engine": "claude", "model": "claude-opus-5", "bypass": False}
    assert projects.load()[p.id].launch_defaults == p.launch_defaults


def test_api_client_model_and_effort_against_its_own_list(tmp_home, listing):
    p = projects.create(
        "A",
        folders=["/a"],
        launch_defaults={"engine": API, "model": "gpt-5.5-codex", "effort": "high", "bypass": True},
    )
    assert p.launch_defaults == {
        "engine": API,
        "model": "gpt-5.5-codex",
        "effort": "high",
        "bypass": True,
    }
    assert "not offered" in _err(
        lambda: projects.create(
            "B", folders=["/b"], launch_defaults={"engine": API, "model": "nope"}
        )
    )
    assert "no effort" in _err(
        lambda: projects.create(
            "C",
            folders=["/c"],
            launch_defaults={"engine": API, "model": "gpt-5-mini", "effort": "high"},
        )
    )
    listing["value"] = ModelList(status="unavailable", reason="CLI missing")
    assert "unavailable" in _err(
        lambda: projects.create(
            "D", folders=["/d"], launch_defaults={"engine": API, "model": "gpt-5.5-codex"}
        )
    )


@pytest.mark.parametrize(
    ("block", "needle"),
    [
        ({"model": "opus"}, "needs the project's own agent"),
        ({"effort": "high"}, "needs the project's own agent"),
        ({"engine": "claude", "effort": "high"}, "an effort needs a model"),
        ({"engine": "claude", "model": "opus", "effort": "high"}, "offers no effort levels"),
        ({"engine": "claude", "model": "not-a-claude-model"}, "is not offered"),
        ({"engine": "claude", "model": "default"}, "invalid model id"),
        ({"engine": "opencode", "model": "anything"}, "does not take a model"),
        ({"engine": "no-such-engine"}, "can start a session"),
        ({"engine": "shell-but-not-real"}, "can start a session"),
        ({"bypass": "yes"}, "must be a boolean"),
        ({"bypass": 1}, "must be a boolean"),
        ({"engine": "claude", "sneaky": 1}, "unknown launch_defaults fields"),
        ([], "must be an object"),
    ],
)
def test_refusals(tmp_home, block, needle):
    assert needle in _err(lambda: projects.create("A", folders=["/a"], launch_defaults=block))


def test_update_replaces_clears_and_keeps_an_absent_stored_engine(tmp_home, monkeypatch):
    p = projects.create("A", folders=["/a"], launch_defaults={"engine": "claude", "bypass": True})
    p = projects.update(p.id, launch_defaults={"engine": "codex"})
    assert p.launch_defaults == {"engine": "codex"}  # replaced, bypass gone
    # The stored agent disappears from the roster: saving the same block still works…
    from agent_sessions import engines

    real = engines.ids_where
    monkeypatch.setattr(engines, "ids_where", lambda pred: [e for e in real(pred) if e != "codex"])
    p = projects.update(p.id, launch_defaults={"engine": "codex", "bypass": False})
    assert p.launch_defaults == {"engine": "codex", "bypass": False}
    # …but another project cannot newly pick it.
    q = projects.create("B", folders=["/b"])
    assert "can start a session" in _err(
        lambda: projects.update(q.id, launch_defaults={"engine": "codex"})
    )
    # {} clears; an unrelated patch leaves it alone.
    assert projects.update(p.id, name="A2").launch_defaults == {"engine": "codex", "bypass": False}
    assert projects.update(p.id, launch_defaults={}).launch_defaults == {}


def test_read_is_fail_soft_and_keeps_the_dependency_chain(tmp_home):
    p = projects.create("A", folders=["/a"])
    path = Path(projects._default_path())
    data = json.loads(path.read_text())
    data["projects"][p.id]["launch_defaults"] = {"model": "opus", "effort": "x", "bypass": "yes"}
    path.write_text(json.dumps(data))
    assert projects.load()[p.id].launch_defaults == {}  # a model without an engine is dropped
    data["projects"][p.id]["launch_defaults"] = {
        "engine": "gone-engine",
        "model": "m1",
        "effort": "high",
        "bypass": True,
    }
    path.write_text(json.dumps(data))
    assert projects.load()[p.id].launch_defaults == {
        "engine": "gone-engine",
        "model": "m1",
        "effort": "high",
        "bypass": True,
    }
    data["projects"][p.id]["launch_defaults"] = "garbage"
    path.write_text(json.dumps(data))
    assert projects.load()[p.id].launch_defaults == {}


def test_routes_accept_and_return_it(auth_cfg, fake_jsonl):
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    hdr = {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": auth_cfg.origin}
    r = c.post(
        "/api/projects",
        json={
            "name": "P",
            "default_folder": "/tmp/ld",
            "launch_defaults": {"engine": "claude", "model": "sonnet"},
        },
        headers=hdr,
    )
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    assert r.json()["launch_defaults"] == {"engine": "claude", "model": "claude-sonnet-5"}
    r = c.patch(f"/api/projects/{pid}", json={"launch_defaults": {"model": "opus"}}, headers=hdr)
    assert r.status_code == 422 and "own agent" in r.json()["detail"]
    r = c.patch(f"/api/projects/{pid}", json={"launch_defaults": {"bypass": False}}, headers=hdr)
    assert r.status_code == 200 and r.json()["launch_defaults"] == {"bypass": False}
    listed = c.get("/api/projects").json()["projects"]
    assert next(p for p in listed if p["id"] == pid)["launch_defaults"] == {"bypass": False}


def test_no_launch_path_reads_launch_defaults():
    """A project's defaults are a PRESELECTION (#1508): the store and its routes are the only
    readers in the server. A mission dispatch, an automation, Ask, a handoff or a playbook flow
    that read them would turn a form preference into a launch grant (``bypass``)."""
    src = Path(__file__).resolve().parent.parent / "src" / "agent_sessions"
    readers = sorted(
        str(f.relative_to(src))
        for f in src.rglob("*.py")
        if re.search(r"launch_defaults", f.read_text(encoding="utf-8"))
    )
    assert readers == ["projects.py", "routes/sessions.py"]
