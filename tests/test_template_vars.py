"""The template variables library and the ``source`` field (#1090, Phase 1).

What this pins:

* **A library field owns no value of its own** — ``source: "library"`` with a ``default`` is a
  422, so "what gets sent" never depends on which of two values a reader looked at.
* **Store v2 reads v1 unchanged** — every v1 field reads as ``source: "template"``, and the first
  write republishes the file as v2.
* **The name is the identity** — no rename through PATCH, and DELETE is a 409 carrying the
  dependants while any template still references the variable.
* **The variables store keeps the template store's discipline** — owner-only file, fenced
  edits, a damaged file quarantined rather than overwritten, a newer version never rewritten,
  and every response ``no-store``.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agent_sessions import template_vars, templates
from agent_sessions.main import create_app


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
    return c.get("/api/config").json()["csrf"]


def _hdr(cfg, csrf):
    return {"X-CSRF-Token": csrf, "Origin": cfg.origin}


def _template(**over):
    base = {
        "name": "Smoke test staging",
        "body": "Run {{test_cmd}} against {{staging_host}} for {{ticket}}.",
        "fields": [
            {"name": "test_cmd", "source": "library"},
            {"name": "staging_host", "source": "library"},
            {"name": "ticket", "required": True},
        ],
    }
    base.update(over)
    return base


def _var(c, cfg, csrf, name, value):
    r = c.post(
        "/api/template-variables",
        json={"name": name, "value": value},
        headers=_hdr(cfg, csrf),
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---- the source field ------------------------------------------------------------------------


def test_a_field_carries_its_source_and_defaults_to_template(tmp_home):
    rec = templates.create_template(_template())
    assert [(f["name"], f["source"]) for f in rec["fields"]] == [
        ("test_cmd", "library"),
        ("staging_host", "library"),
        ("ticket", "template"),
    ]


@pytest.mark.parametrize(
    "field, message",
    [
        ({"name": "x", "source": "library", "default": "mine"}, "takes its value from the library"),
        ({"name": "x", "source": "vault"}, "source must be"),
        ({"name": "x", "source": None}, "source must be"),
        ({"name": "x", "kind": "vault"}, "kind must be"),
        ({"name": "x", "kind": "secret", "default": "hunter22"}, "a secret field has no default"),
    ],
)
def test_bad_sources_are_refused(tmp_home, field, message):
    with pytest.raises(templates.TemplateError, match=message):
        templates.validate(_template(fields=[field], body="{{x}}"))


def test_a_v1_store_reads_as_template_fields_and_is_republished_current(tmp_home):
    store = templates.store_path()
    store.parent.mkdir(parents=True)
    v1_record = {
        "id": "old",
        "name": "Old",
        "description": "",
        "tags": [],
        "body": "Hi {{who}}",
        "fields": [{"name": "who", "label": "Who", "default": "you", "required": False}],
        "images": [],
        "created_at": 1.0,
        "updated_at": 1.0,
        "used_count": 0,
        "last_used_at": None,
    }
    store.write_text(json.dumps({"version": 1, "templates": [v1_record]}))
    [rec] = templates.list_templates()
    assert rec["fields"][0]["source"] == "template"
    assert rec["fields"][0]["kind"] == "text"
    # Reading never rewrote the file…
    assert json.loads(store.read_text())["version"] == 1
    # …the first accepted write publishes the current version, with nothing quarantined.
    templates.mark_used("old")
    doc = json.loads(store.read_text())
    assert doc["version"] == templates.STORE_VERSION
    assert doc["templates"][0]["fields"][0]["source"] == "template"
    assert doc["templates"][0]["fields"][0]["kind"] == "text"
    assert not list(store.parent.glob("templates.json.corrupt-*"))


# ---- the variables store ---------------------------------------------------------------------


def test_round_trip_with_used_by_and_an_owner_only_file(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    rec = _var(c, auth_cfg, csrf, "staging_host", "staging.acme.test")
    assert rec["name"] == "staging_host" and rec["value"] == "staging.acme.test"
    _var(c, auth_cfg, csrf, "test_cmd", "uv run pytest -q")
    t = templates.create_template(_template())

    r = c.get("/api/template-variables")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert [v["name"] for v in body["variables"]] == ["staging_host", "test_cmd"]
    assert body["variables"][0]["used_by"] == [{"id": t["id"], "name": t["name"]}]
    assert body["limits"]["variables_max"] == template_vars.VARIABLES_MAX

    path = template_vars.store_path()
    assert path == tmp_home / ".config" / "agent-sessions" / "template-variables.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert template_vars.values() == {
        "staging_host": "staging.acme.test",
        "test_cmd": "uv run pytest -q",
    }


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"name": "Bad", "value": "x"}, "a variable name is"),
        ({"name": "ok", "value": ""}, "value is required"),
        ({"name": "ok", "value": "x" * (template_vars.VALUE_MAX + 1)}, "too long"),
        ({"name": "ok", "value": "a\x1b[2~b"}, "control character"),
        ({"name": "ok", "value": "x", "updated_at": 1}, "unknown fields"),
    ],
)
def test_create_is_validated_server_side(auth_cfg, tmp_home, payload, message):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post("/api/template-variables", json=payload, headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 422 and message in r.json()["detail"]
    assert r.headers["cache-control"] == "no-store"
    assert not template_vars.store_path().exists()


def test_duplicate_names_and_the_cap_are_refused(auth_cfg, tmp_home, monkeypatch):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    _var(c, auth_cfg, csrf, "a", "1")
    r = c.post(
        "/api/template-variables", json={"name": "a", "value": "2"}, headers=_hdr(auth_cfg, csrf)
    )
    assert r.status_code == 422 and "already exists" in r.json()["detail"]
    monkeypatch.setattr(template_vars, "VARIABLES_MAX", 1)
    r = c.post(
        "/api/template-variables", json={"name": "b", "value": "2"}, headers=_hdr(auth_cfg, csrf)
    )
    assert r.status_code == 422 and "too many" in r.json()["detail"]


def test_patch_is_fenced_and_cannot_rename(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    rec = _var(c, auth_cfg, csrf, "host", "a.test")
    ok = c.patch(
        "/api/template-variables/host",
        json={"value": "b.test", "expected_updated_at": rec["updated_at"]},
        headers=_hdr(auth_cfg, csrf),
    )
    assert ok.status_code == 200 and ok.json()["value"] == "b.test"
    assert ok.json()["updated_at"] > rec["updated_at"]

    stale = c.patch(
        "/api/template-variables/host",
        json={"value": "c.test", "expected_updated_at": rec["updated_at"]},
        headers=_hdr(auth_cfg, csrf),
    )
    assert stale.status_code == 409 and stale.json()["current"]["value"] == "b.test"

    rename = c.patch(
        "/api/template-variables/host",
        json={"name": "other", "value": "c.test", "expected_updated_at": ok.json()["updated_at"]},
        headers=_hdr(auth_cfg, csrf),
    )
    assert rename.status_code == 422 and "cannot be renamed" in rename.json()["detail"]
    assert template_vars.values() == {"host": "b.test"}

    missing = c.patch(
        "/api/template-variables/nope",
        json={"value": "x", "expected_updated_at": 1.0},
        headers=_hdr(auth_cfg, csrf),
    )
    assert missing.status_code == 404


def test_delete_is_refused_while_a_template_references_the_variable(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    host = _var(c, auth_cfg, csrf, "staging_host", "staging.acme.test")
    t = templates.create_template(_template())
    # A template field of the same name that is NOT a library field is not a dependant.
    templates.create_template(
        _template(name="Unrelated", body="{{staging_host}}", fields=[{"name": "staging_host"}])
    )

    r = c.delete(
        f"/api/template-variables/staging_host?expected_updated_at={host['updated_at']}",
        headers=_hdr(auth_cfg, csrf),
    )
    assert r.status_code == 409
    assert r.headers["cache-control"] == "no-store"
    assert r.json()["dependants"] == [{"id": t["id"], "name": t["name"]}]
    assert "still used by 1 template" in r.json()["detail"]
    assert "staging_host" in template_vars.values()

    # Switch the dependant back to a template field, and the delete goes through.
    templates.update_template(
        t["id"],
        _template(fields=[{"name": "staging_host"}, {"name": "test_cmd"}, {"name": "ticket"}]),
        t["updated_at"],
    )
    r = c.delete(
        f"/api/template-variables/staging_host?expected_updated_at={host['updated_at']}",
        headers=_hdr(auth_cfg, csrf),
    )
    assert r.status_code == 204
    assert template_vars.values() == {}


def test_delete_is_fenced(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    _var(c, auth_cfg, csrf, "host", "a.test")
    r = c.delete(
        "/api/template-variables/host?expected_updated_at=1.0", headers=_hdr(auth_cfg, csrf)
    )
    assert r.status_code == 409 and r.json()["current"]["value"] == "a.test"
    assert template_vars.values() == {"host": "a.test"}


def test_writes_need_csrf_and_reads_need_a_session(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    assert c.get("/api/template-variables").status_code == 401
    _login(c, auth_cfg)
    r = c.post(
        "/api/template-variables",
        json={"name": "a", "value": "1"},
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 403
    assert not template_vars.store_path().exists()


def test_a_damaged_store_is_quarantined_before_the_first_write(tmp_home):
    path = template_vars.store_path()
    path.parent.mkdir(parents=True)
    path.write_text('{"version": 1, "variables": [{"name": "Bad!"}, ')
    assert template_vars.list_variables() == []
    template_vars.create_variable({"name": "a", "value": "1"})
    [kept] = list(path.parent.glob("template-variables.json.corrupt-*"))
    assert kept.read_text().startswith('{"version": 1')
    assert template_vars.values() == {"a": "1"}


def test_a_refused_write_leaves_a_damaged_store_untouched(tmp_home):
    path = template_vars.store_path()
    path.parent.mkdir(parents=True)
    raw = '{"version": 1, "variables": "nope"}'
    path.write_text(raw)
    with pytest.raises(template_vars.VariableError):
        template_vars.create_variable({"name": "Bad", "value": "1"})
    assert path.read_text() == raw
    assert not list(path.parent.glob("template-variables.json.corrupt-*"))


def test_a_newer_store_is_refused_and_never_rewritten(auth_cfg, tmp_home):
    path = template_vars.store_path()
    path.parent.mkdir(parents=True)
    raw = json.dumps({"version": template_vars.STORE_VERSION + 1, "vars": {"a": {"kind": "x"}}})
    path.write_text(raw)
    assert template_vars.list_variables() == []
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/template-variables", json={"name": "a", "value": "1"}, headers=_hdr(auth_cfg, csrf)
    )
    assert r.status_code == 409 and "nothing was written" in r.json()["detail"]
    assert path.read_text() == raw


# ---- fail-closed delete (Hermes on #1095) ------------------------------------------------------


def _delete_refused_without_writing(c, cfg, csrf, host):
    before = template_vars.store_path().read_bytes()
    r = c.delete(
        f"/api/template-variables/host?expected_updated_at={host['updated_at']}",
        headers=_hdr(cfg, csrf),
    )
    assert r.status_code == 409, r.text
    assert "nothing was deleted" in r.json()["detail"]
    assert r.headers["cache-control"] == "no-store"
    assert template_vars.store_path().read_bytes() == before


def test_delete_refuses_when_the_template_library_cannot_be_read(auth_cfg, tmp_home, monkeypatch):
    """The real reader's failure path: ``templates._read`` degrades an OSError to "no records,
    damaged". Read leniently, that was "no dependants" and the delete went through."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    host = _var(c, auth_cfg, csrf, "host", "a.test")
    templates.create_template(
        _template(body="{{host}}", fields=[{"name": "host", "source": "library"}])
    )
    store = templates.store_path()
    real_read_text = type(store).read_text

    def read_text(self, *a, **kw):
        if self == store:
            raise PermissionError(13, "Permission denied", str(self))
        return real_read_text(self, *a, **kw)

    # A scoped patch: `monkeypatch.undo()` would also undo conftest's env pins and point the
    # stores at the real home.
    with monkeypatch.context() as m:
        m.setattr(type(store), "read_text", read_text)
        _delete_refused_without_writing(c, auth_cfg, csrf, host)
    # Readable again: the template still has its variable.
    assert template_vars.values() == {"host": "a.test"}


def test_delete_refuses_when_the_template_library_is_a_newer_version(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    host = _var(c, auth_cfg, csrf, "host", "a.test")
    store = templates.store_path()
    store.write_text(
        json.dumps({"version": templates.STORE_VERSION + 1, "templates": [{"id": "future"}]})
    )
    _delete_refused_without_writing(c, auth_cfg, csrf, host)


def test_delete_refuses_when_a_template_record_is_damaged(auth_cfg, tmp_home):
    """A record the validator skips may be exactly the one that uses the variable."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    host = _var(c, auth_cfg, csrf, "host", "a.test")
    templates.create_template(_template(name="Fine", body="x", fields=[]))
    store = templates.store_path()
    doc = json.loads(store.read_text())
    doc["templates"].append({"id": "Bad Id", "name": "uses host", "body": "{{host}}"})
    store.write_text(json.dumps(doc))
    _delete_refused_without_writing(c, auth_cfg, csrf, host)


def test_delete_with_no_template_library_at_all_goes_through(auth_cfg, tmp_home):
    """A missing template file is a complete answer — no templates, so no dependants."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    host = _var(c, auth_cfg, csrf, "host", "a.test")
    assert not templates.store_path().exists()
    r = c.delete(
        f"/api/template-variables/host?expected_updated_at={host['updated_at']}",
        headers=_hdr(auth_cfg, csrf),
    )
    assert r.status_code == 204


def test_used_by_stays_lenient_for_display(tmp_home):
    """Display may degrade (a damaged library names fewer dependants); only DELETE fails closed."""
    template_vars.create_variable({"name": "host", "value": "a.test"})
    store = templates.store_path()
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("{not json")
    [v] = template_vars.list_variables()
    assert v["used_by"] == []


def test_listing_reads_the_template_store_once_however_many_variables(tmp_home, monkeypatch):
    """``used_by`` for N variables is one read of the template library, not N (#1095 review:
    24 s at the caps when it was one read per variable)."""
    for i in range(5):
        template_vars.create_variable({"name": f"v{i}", "value": "x"})
    templates.create_template(
        _template(
            body="{{v1}} {{v3}}",
            fields=[{"name": "v1", "source": "library"}, {"name": "v3", "source": "library"}],
        )
    )
    reads = []
    real = templates._read

    def counting(path):
        reads.append(path)
        return real(path)

    monkeypatch.setattr(templates, "_read", counting)
    listed = template_vars.list_variables()
    assert len(reads) == 1
    used = {v["name"]: [d["name"] for d in v["used_by"]] for v in listed}
    assert used == {
        "v0": [],
        "v1": ["Smoke test staging"],
        "v2": [],
        "v3": ["Smoke test staging"],
        "v4": [],
    }
