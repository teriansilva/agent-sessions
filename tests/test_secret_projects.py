"""Binding a library secret to projects, and its access log (#1786 P3)."""

import pytest

from agent_sessions import projects, secrets_broker, variables
from agent_sessions import template_vars as store


@pytest.fixture
def two_projects(tmp_home):
    a = projects.create("Atlas iOS")
    b = projects.create("Beta")
    return a.id, b.id


def _secret(name="asc_api_key"):
    return variables.create({"name": name, "kind": "secret", "value": "s3cr3t-value-1786"})


def test_a_secret_is_bound_to_exactly_the_chosen_projects(two_projects):
    a, b = two_projects
    row = _secret()
    assert row["projects"] == []
    row = variables.set_projects(row["id"], [a, b], row["updated_at"])
    assert row["projects"] == sorted([a, b])
    assert sorted(
        r["project_id"] for r in store.project_bindings(a) + store.project_bindings(b)
    ) == sorted([a, b])
    row = variables.set_projects(row["id"], [b], row["updated_at"])
    assert row["projects"] == [b]
    assert store.project_bindings(a) == []
    listed = next(v for v in variables.library()["variables"] if v["id"] == row["id"])
    assert listed["projects"] == [b]


def test_unknown_projects_and_text_variables_are_refused(two_projects):
    row = _secret()
    with pytest.raises(store.VariableError):
        variables.set_projects(row["id"], ["no-such-project"], row["updated_at"])
    text = variables.create({"name": "host", "value": "a.test"})
    with pytest.raises(store.VariableError):
        variables.set_projects(text["id"], [two_projects[0]], text["updated_at"])


def test_a_projects_own_value_is_never_overwritten_by_a_binding(two_projects):
    a, _ = two_projects
    row = _secret()
    store.bind_project(
        a, [{"name": "asc_api_key", "kind": "secret", "value": "project-own-value-1"}]
    )
    with pytest.raises(store.VariableError, match="its own value"):
        variables.set_projects(row["id"], [a], row["updated_at"])


def test_a_binding_a_playbook_deployment_holds_is_never_removed_here(two_projects, monkeypatch):
    from agent_sessions import variable_references

    a, _ = two_projects
    row = _secret()
    row = variables.set_projects(row["id"], [a], row["updated_at"])
    monkeypatch.setattr(variable_references, "deployment_projects", lambda name: [a])
    with pytest.raises(store.VariableError, match="playbook deployment"):
        variables.set_projects(row["id"], [], row["updated_at"])
    assert [r["project_id"] for r in store.project_bindings(a)] == [a]


def test_a_stale_revision_is_a_conflict(two_projects):
    row = _secret()
    with pytest.raises(store.VariableConflict):
        variables.set_projects(row["id"], [two_projects[0]], row["updated_at"] - 1)


def test_the_access_log_is_empty_until_an_agent_asks_and_never_holds_values(two_projects):
    row = _secret()
    assert variables.access_log(row["id"]) == []
    conn = secrets_broker._connect()
    secrets_broker._audit(
        conn, None, var_id=row["id"], name="asc_api_key", outcome="refused", reason="access_off"
    )
    conn.close()
    [entry] = variables.access_log(row["id"])
    assert entry["outcome"] == "refused" and entry["reason"] == "access_off"
    assert "s3cr3t" not in str(entry)


def test_the_routes_are_authenticated_and_no_store(auth_cfg, two_projects):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    client = TestClient(create_app(auth_cfg), base_url="https://testserver")
    row = _secret()
    assert client.get(f"/api/variables/{row['id']}/access-log").status_code == 401
    client.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    csrf = client.get("/api/config").json()["csrf"]
    headers = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    no_csrf = client.put(
        f"/api/variables/{row['id']}/projects",
        json={"project_ids": [two_projects[0]], "expected_updated_at": row["updated_at"]},
        headers={"Origin": auth_cfg.origin},
    )
    assert no_csrf.status_code == 403
    ok = client.put(
        f"/api/variables/{row['id']}/projects",
        json={"project_ids": [two_projects[0]], "expected_updated_at": row["updated_at"]},
        headers=headers,
    )
    assert ok.status_code == 200 and ok.json()["projects"] == [two_projects[0]]
    assert "no-store" in ok.headers["cache-control"]
    log = client.get(f"/api/variables/{row['id']}/access-log")
    assert log.status_code == 200 and log.json() == {"entries": []}


def test_a_deleted_projects_binding_never_blocks_changes_and_stays_removable(two_projects):
    a, b = two_projects
    row = _secret()
    row = variables.set_projects(row["id"], [a], row["updated_at"])
    projects.delete(a)
    row = variables.set_projects(row["id"], [a, b], row["updated_at"])  # keeping A is no change
    assert row["projects"] == sorted([a, b])
    row = variables.set_projects(row["id"], [b], row["updated_at"])
    assert row["projects"] == [b]
    with pytest.raises(store.VariableError, match="unknown projects"):
        variables.set_projects(row["id"], [a, b], row["updated_at"])  # re-adding it is refused


def test_a_secret_file_binds_like_a_secret(two_projects):
    a, _ = two_projects
    row = variables.create({"name": "asc_key", "kind": "secret_file"})
    row = variables.put_file(row["id"], b"file-bytes-1786", "AuthKey.p8", row["updated_at"])
    row = variables.set_projects(row["id"], [a], row["updated_at"])
    [binding] = store.project_bindings(a)
    assert binding["name"] == "asc_key" and binding["kind"] == "secret_file"


def test_a_change_moves_the_revision_so_a_second_stale_writer_conflicts(two_projects):
    a, b = two_projects
    row = _secret()
    stale = row["updated_at"]
    after = variables.set_projects(row["id"], [a], stale)
    assert after["updated_at"] > stale
    with pytest.raises(store.VariableConflict):
        variables.set_projects(row["id"], [b], stale)


def test_a_binding_never_pushes_a_project_past_its_limit(two_projects, monkeypatch):
    a, _ = two_projects
    monkeypatch.setattr(store, "PROJECT_VARIABLES_MAX", 1)
    first, second = _secret("one_secret"), _secret("two_secret")
    variables.set_projects(first["id"], [a], first["updated_at"])
    with pytest.raises(store.VariableError, match="too many bindings"):
        variables.set_projects(second["id"], [a], second["updated_at"])


def test_the_access_log_is_one_records_own(two_projects):
    mine, other = _secret("mine_secret"), _secret("other_secret")
    conn = secrets_broker._connect()
    secrets_broker._audit(
        conn, None, var_id=other["id"], name="other_secret", outcome="refused", reason="access_off"
    )
    conn.close()
    assert variables.access_log(mine["id"]) == []
    assert len(variables.access_log(other["id"])) == 1
