"""Duplicate an MCP server (#2430): two logins for one service in one project.

Against the real registry, broker and launcher (the fixtures of `test_mcp_servers.py`): the copy
is made server-side from the stored record, remaps credentials only to existing Variables
secrets, copies no enablement or checks, shares the frozen install, and touches no network.
"""

# ruff: noqa: F811 — imported pytest fixtures
from __future__ import annotations

import json

import pytest

from agent_sessions import mcp_gitsource, mcp_install, mcp_registry, secrets_broker, variables
from test_mcp_servers import (  # noqa: F401 — fixtures are used by name
    LIB_KEY,
    _bind_shared,
    _check,
    _enable,
    _launch,
    _library_secret,
    _remote,
    env,
    http_server,
)

PERSONAL_KEY = "personal-key-7c6b5a4d3e2f"


@pytest.fixture
def no_network(monkeypatch):
    """Duplicate is metadata only: any resolver, review or install call fails the test."""

    def refuse(*a, **kw):
        raise AssertionError("duplicate must not resolve, download or build anything")

    for target, name in (
        (mcp_gitsource, "resolve"),
        (mcp_install, "review"),
        (mcp_install, "install"),
    ):
        monkeypatch.setattr(target, name, refuse)


def _dup(rec, **payload):
    return mcp_registry.duplicate(rec["id"], payload, mcp_registry.get(rec["id"])["updated_at"])


def test_a_copy_keeps_what_runs_and_drops_what_authorizes(env, http_server, no_network):
    _library_secret()
    variables.create({"name": "api_key_personal", "kind": "secret", "value": PERSONAL_KEY})
    _bind_shared(env["beta"])
    rec = _remote(http_server["url"], probe={"tool": "search", "arguments": {}})
    assert _check(rec["id"], env["beta"])["outcome"] == "passed"
    _enable(rec["id"], [env["beta"]])
    original = mcp_registry.get(rec["id"])
    copy = _dup(original, name="remote_personal", variables={"api_key": "api_key_personal"})
    assert copy["id"] != original["id"] and copy["name"] == "remote_personal"
    for field in ("source", "transport", "args", "probe", "installed"):
        assert copy[field] == original[field], field
    assert copy["settings"][0]["variable"] == "api_key_personal"
    assert copy["projects"] == [] and copy["project_settings"] == {}
    assert mcp_registry.checks(copy["id"]) == []
    assert mcp_registry.get(rec["id"]) == original  # the original is untouched


def test_two_logins_in_one_project_each_reach_their_own_server(env, http_server, no_network):
    """One project, two entries, two bound secrets: each launch's server gets its own value, and
    the copy reaches nobody until its OWN check passes."""
    _library_secret()
    variables.create({"name": "api_key_personal", "kind": "secret", "value": PERSONAL_KEY})
    _bind_shared(env["beta"])
    _bind_shared(env["beta"], "api_key_personal")
    work = _remote(http_server["url"])
    assert _check(work["id"], env["beta"])["outcome"] == "passed"
    _enable(work["id"], [env["beta"]])
    personal = _dup(work, name="remote_personal", variables={"api_key": "api_key_personal"})
    capability, token = _launch("claude:b1")
    assert [s["id"] for s in mcp_registry.launch_specs(env["beta"], capability)] == [work["id"]]
    with pytest.raises(mcp_registry.RegistryError, match="run a check"):
        _enable(personal["id"], [env["beta"]])  # no inherited check
    assert _check(personal["id"], env["beta"])["outcome"] == "passed"
    _enable(personal["id"], [env["beta"]])
    ids = {s["id"] for s in mcp_registry.launch_specs(env["beta"], capability)}
    assert ids == {work["id"], personal["id"]}
    got = {}
    for sid in (work["id"], personal["id"]):
        answer = secrets_broker.handle(token, "mcp", sid)
        assert answer["ok"] is True, answer
        got[sid] = json.loads(open(answer["path"]).read())["headers"]["Authorization"]
    assert got == {work["id"]: f"Bearer {LIB_KEY}", personal["id"]: f"Bearer {PERSONAL_KEY}"}


@pytest.mark.parametrize(
    "mapping, words",
    [
        ({"deploy_token": "api_key_personal"}, "names no credential variable deploy_token"),
        ({"api_key": "nope_missing"}, "no secret named nope_missing"),
        ({"api_key": "plain_one"}, "plain_one in Variables is not a secret"),
        ({"api_key": "Bad Name"}, "not a variable name"),
    ],
)
def test_a_remap_names_only_this_servers_credentials_and_existing_secrets(
    env, http_server, no_network, mapping, words
):
    _library_secret()
    variables.create({"name": "api_key_personal", "kind": "secret", "value": PERSONAL_KEY})
    variables.create({"name": "plain_one", "kind": "text", "value": "x"})
    rec = _remote(http_server["url"])
    before = mcp_registry.list_servers()
    with pytest.raises(mcp_registry.RegistryError, match=words):
        _dup(rec, name="copy_one", variables=mapping)
    assert mcp_registry.list_servers() == before  # nothing half-registered


def test_a_copy_needs_a_free_name_and_the_current_revision(env, http_server, no_network):
    _library_secret()
    rec = _remote(http_server["url"])
    with pytest.raises(mcp_registry.RegistryError, match="exists"):
        _dup(rec, name=rec["name"])
    with pytest.raises(mcp_registry.Conflict):
        mcp_registry.duplicate(rec["id"], {"name": "copy_two"}, rec["updated_at"] - 1)
    copy = _dup(rec, name="copy_two")  # an omitted mapping keeps the variable
    assert copy["settings"][0]["variable"] == "api_key"


def test_the_shared_install_outlives_either_entry(env, http_server, no_network, tmp_path):
    """The copy shares the frozen install; removing the original never removes it."""
    rec = mcp_registry.create(
        {"name": "pkg", "source": {"kind": "npm", "package": "@acme/x", "version": "1.0.0"}}
    )
    binary = tmp_path / "frozen" / "bin" / "x"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    mcp_registry.set_installed(
        rec["id"], {"source": rec["source"], "command": str(binary), "args": []}
    )
    copy = _dup(mcp_registry.get(rec["id"]), name="pkg_two")
    mcp_registry.delete(rec["id"], mcp_registry.get(rec["id"])["updated_at"])
    assert mcp_install.command(mcp_registry.get(copy["id"])) == (str(binary), [])


def test_updating_the_original_leaves_the_copy_on_its_install(
    env, http_server, no_network, tmp_path
):
    """A new version chosen for the original (P1b's Update…) makes the ORIGINAL not installed;
    the copy keeps naming the shared frozen install and still resolves its command."""
    rec = mcp_registry.create(
        {"name": "pkg", "source": {"kind": "npm", "package": "@acme/x", "version": "1.0.0"}}
    )
    binary = tmp_path / "frozen" / "bin" / "x"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    mcp_registry.set_installed(
        rec["id"], {"source": rec["source"], "command": str(binary), "args": []}
    )
    copy = _dup(mcp_registry.get(rec["id"]), name="pkg_two")
    mcp_registry.update(
        rec["id"],
        {"source": {"kind": "npm", "package": "@acme/x", "version": "2.0.0"}},
        mcp_registry.get(rec["id"])["updated_at"],
    )
    with pytest.raises(mcp_registry.RegistryError):
        mcp_install.command(mcp_registry.get(rec["id"]))  # the original awaits its new install
    assert mcp_install.command(mcp_registry.get(copy["id"])) == (str(binary), [])
    assert binary.exists()


def test_the_route_is_authenticated_guarded_and_answers_a_summary(env, http_server, auth_cfg):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    _library_secret()
    rec = _remote(http_server["url"])
    client = TestClient(create_app(auth_cfg), base_url="https://testserver")
    url = f"/api/mcp/servers/{rec['id']}/duplicate"
    body = {"name": "copy_route", "expected_updated_at": rec["updated_at"]}
    assert client.post(url, json=body).status_code in (401, 403)
    client.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    csrf = client.get("/api/config").json()["csrf"]
    assert client.post(url, json=body, headers={"Origin": auth_cfg.origin}).status_code == 403
    r = client.post(url, json=body, headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin})
    assert r.status_code == 201 and r.json()["name"] == "copy_route" and LIB_KEY not in r.text
    clash = client.post(url, json=body, headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin})
    assert clash.status_code in (409, 422)


def test_a_retried_duplicate_returns_its_copy_never_a_second_one(env, http_server, no_network):
    """Accepted but the response lost: the same request_id returns the copy it made; a name
    clash from another request is still refused, and a request_id never crosses originals."""
    import uuid

    _library_secret()
    rec = _remote(http_server["url"])
    other = mcp_registry.create(
        {"name": "other", "source": {"kind": "remote", "url": "https://mcp.example.test/x"}}
    )
    rid = str(uuid.uuid4())
    first = _dup(rec, name="copy_r", request_id=rid)
    again = _dup(rec, name="copy_r", request_id=rid)
    assert again["id"] == first["id"]
    assert [s["name"] for s in mcp_registry.list_servers()].count("copy_r") == 1
    with pytest.raises(mcp_registry.RegistryError, match="exists"):
        _dup(rec, name="copy_r", request_id=str(uuid.uuid4()))
    with pytest.raises(mcp_registry.Conflict, match="another server"):
        _dup(other, name="copy_r", request_id=rid)


def test_a_server_naming_one_variable_in_two_settings_remaps_both(env, http_server, no_network):
    _library_secret()
    variables.create({"name": "api_key_personal", "kind": "secret", "value": PERSONAL_KEY})
    rec = mcp_registry.create(
        {
            "name": "twice",
            "source": {"kind": "remote", "url": http_server["url"]},
            "settings": [
                {"key": "Authorization", "kind": "credential", "variable": "api_key",
                 "prefix": "Bearer "},
                {"key": "X-Api-Key", "kind": "credential", "variable": "api_key"},
            ],
        }
    )  # fmt: skip
    copy = _dup(rec, name="twice_personal", variables={"api_key": "api_key_personal"})
    assert {s["variable"] for s in copy["settings"]} == {"api_key_personal"}
