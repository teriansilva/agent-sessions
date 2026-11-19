"""A project's own MCP credentials through the Library page (#2163 P1c):
``PUT /api/mcp/servers/{sid}/projects/{pid}/credentials`` stores each value as that project's own
secret (resolution then prefers it over a shared one), accepts only the variables the server names,
is CSRF-guarded, and never answers with a value."""

from __future__ import annotations

import pytest

from agent_sessions import mcp_registry, projects, variables
from agent_sessions import template_vars as store

OWN = "atlas-own-key-9a8b7c6d5e"
SHARED = "library-key-0f1e2d3c4b5a"


@pytest.fixture
def client(tmp_home, tmp_path, monkeypatch, auth_cfg):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    monkeypatch.setenv("AGENT_SESSIONS_MCP_SERVERS", str(tmp_path / "mcp-servers.json"))
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    csrf = c.get("/api/config").json()["csrf"]
    c.headers.update({"X-CSRF-Token": csrf, "Origin": auth_cfg.origin})
    return c


def _server() -> dict:
    return mcp_registry.create(
        {
            "name": "remote1",
            "source": {"kind": "remote", "url": "https://mcp.example.test/mcp"},
            "settings": [
                {
                    "key": "Authorization",
                    "kind": "credential",
                    "variable": "api_key",
                    "prefix": "Bearer ",
                }
            ],
        }
    )


def _row(detail: dict, pid: str) -> dict:
    return next(r for r in detail["status"]["projects"] if r["project_id"] == pid)


def test_own_values_are_stored_for_the_project_and_never_returned(client):
    atlas = projects.create("Atlas").id
    beta = projects.create("Beta").id
    variables.create({"name": "api_key", "kind": "secret", "value": SHARED})
    store.bind_project(beta, [{"name": "api_key", "kind": "secret", "ref": "global"}])
    sid = _server()["id"]

    before = client.get(f"/api/mcp/servers/{sid}").json()
    assert _row(before, atlas)["credentials"] == "missing"
    assert _row(before, beta)["credentials"] == "shared"

    r = client.put(
        f"/api/mcp/servers/{sid}/projects/{atlas}/credentials", json={"values": {"api_key": OWN}}
    )
    assert r.status_code == 200, r.text
    assert OWN not in r.text and SHARED not in r.text
    assert _row(r.json(), atlas)["credentials"] == "own"
    # The other project is untouched: no fallback, no cross-project write.
    assert _row(r.json(), beta)["credentials"] == "shared"
    rec = mcp_registry.get(sid)
    resolved = mcp_registry.resolve(rec, atlas, with_values=True)
    assert resolved.headers["Authorization"] == "Bearer " + OWN
    assert resolved.sources == {"api_key": "own"}

    # A shared project switching to its own value replaces its ref.
    r = client.put(
        f"/api/mcp/servers/{sid}/projects/{beta}/credentials",
        json={"values": {"api_key": OWN + "-beta"}},
    )
    assert r.status_code == 200 and _row(r.json(), beta)["credentials"] == "own"
    for page in (client.get("/api/mcp/servers"), client.get(f"/api/mcp/servers/{sid}")):
        assert OWN not in page.text


def test_only_the_servers_own_variables_a_real_project_and_csrf(client, auth_cfg):
    atlas = projects.create("Atlas").id
    sid = _server()["id"]
    url = f"/api/mcp/servers/{sid}/projects/{atlas}/credentials"

    other = client.put(url, json={"values": {"deploy_token": OWN}})
    assert other.status_code == 422 and OWN not in other.text
    with pytest.raises(store.VariableNotFound):
        store._find(store._read(store.store_path())[0], "deploy_token", store.SCOPE_PROJECT, atlas)

    assert client.put(url, json={"values": {}}).status_code == 422
    assert client.put(url, json={"values": {"api_key": 7}}).status_code == 422
    assert client.put(url, json={"values": {"api_key": OWN}, "x": 1}).status_code == 422
    short = client.put(url, json={"values": {"api_key": "zq7x"}})
    assert short.status_code == 422 and "zq7x" not in short.json()["detail"]
    assert (
        client.put(
            f"/api/mcp/servers/{sid}/projects/no-such-project/credentials",
            json={"values": {"api_key": OWN}},
        ).status_code
        == 422
    )
    assert (
        client.put(
            f"/api/mcp/servers/{'0' * 16}/projects/{atlas}/credentials",
            json={"values": {"api_key": OWN}},
        ).status_code
        == 404
    )
    no_csrf = client.put(
        url, json={"values": {"api_key": OWN}}, headers={"X-CSRF-Token": "", "Origin": ""}
    )
    assert no_csrf.status_code == 403
    detail = client.get(f"/api/mcp/servers/{sid}").json()
    assert _row(detail, atlas)["credentials"] == "missing"
