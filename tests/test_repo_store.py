"""Shared artifact repositories on one instance (#2347 P1, ``docs/invariants/shared-repos.md``).

The store (`repo_store`): adding an artifact copies its manifest and no byte; a quota per
repository counts referenced bytes once and refuses with nothing stored; the host-wide cap and
garbage collection see repository revisions exactly as they see artifact revisions. The agent's
``artifact_publish(repository=…)`` goes through the one judge (`artifact_store.publish`) and is one
transaction with the artifact it makes. The operator API (``/api/repos``) is login + CSRF + Origin,
and an item opens through the artifact hosting routes, sandboxed.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_sessions import (
    agent_tools,
    artifact_broker,
    artifact_store,
    artifacts_mcp,
    files,
    prefs,
    repo_store,
    secrets_broker,
)
from agent_sessions.routes.artifacts import SANDBOX_CSP

SESSION = "claude:aaaaaaaa-0000-4000-8000-000000000001"
MANIFEST = types.SimpleNamespace(tools_mcp=agent_tools.CLAUDE_FLAG)
KEY = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENT_SESSIONS_FS_ROOT", str(home))
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))
    monkeypatch.setenv("AGENT_SESSIONS_PROJECTS", str(tmp_path / "projects.json"))
    files.reset_capabilities_for_test()
    prefs.set_artifacts_enabled(True)
    yield


@pytest.fixture
def proj(tmp_path) -> Path:
    p = tmp_path / "home" / "proj"
    p.mkdir()
    return p


@pytest.fixture
def origin(proj) -> artifact_store.Origin:
    return artifact_store.Origin(
        engine="claude", session_key=SESSION, phys_key=SESSION, cwd=str(proj)
    )


def publish(origin, proj, name: str, body: bytes | str, **args) -> dict:
    p = proj / name
    p.write_bytes(body.encode() if isinstance(body, str) else body)
    return artifact_store.publish(origin, {"path": name, "title": name, "why": "w", **args})


def blobs() -> dict[str, int]:
    """Every blob on disk: its sha256 → its inode."""
    root = artifact_store.content_dir() / "blobs"
    if not root.is_dir():
        return {}
    return {f.name: f.stat().st_ino for sub in root.iterdir() for f in sub.iterdir()}


def sha_of(aid: str) -> str:
    with artifact_store._con() as con:
        row = con.execute(
            "SELECT manifest FROM artifact_revisions WHERE artifact_id=?", (aid,)
        ).fetchone()
    (meta,) = json.loads(row["manifest"]).values()
    return meta["sha256"]


# ---- the store -----------------------------------------------------------------------------------


def test_adding_an_artifact_copies_its_manifest_and_no_byte(origin, proj):
    a = publish(origin, proj, "report.pdf", b"%PDF-1.4 " + b"x" * 5000)
    repo = repo_store.create("design-assets", "Shared design files")
    before = blobs()
    item = repo_store.add_artifact(repo["id"], a["id"])
    assert blobs() == before  # the same blobs, the same inodes: nothing copied
    assert item["kind"] == "file" and item["revision"] == 1 and item["entry"] == "report.pdf"
    assert item["added_by"] == {"kind": "operator"} and item["source_artifact"] == a["id"]
    with artifact_store._con() as con:
        art = con.execute("SELECT manifest FROM artifact_revisions").fetchone()["manifest"]
        rev = con.execute("SELECT manifest FROM repo_item_revisions").fetchone()["manifest"]
    assert json.loads(art) == json.loads(rev)
    (listed,) = repo_store.list_repos()
    assert listed["items"] == 1 and listed["bytes"] == 5009
    assert listed["quota_bytes"] == repo_store.QUOTA_DEFAULT == 2 * 2**30


def test_the_quota_counts_each_blob_once_and_refuses_with_nothing_stored(origin, proj, monkeypatch):
    monkeypatch.setattr(repo_store, "QUOTA_DEFAULT", 6000)
    repo = repo_store.create("small")
    a = publish(origin, proj, "a.bin", b"a" * 4000)
    b = publish(origin, proj, "b.bin", b"b" * 4000)
    repo_store.add_artifact(repo["id"], a["id"])
    # The same bytes again are the same blob: under quota, a second item is fine.
    repo_store.add_artifact(repo["id"], a["id"])
    assert repo_store.get(repo["id"])["bytes"] == 4000
    with pytest.raises(artifact_store.ArtifactRefused) as e:
        repo_store.add_artifact(repo["id"], b["id"])
    assert e.value.code == "quota"
    page = repo_store.items(repo["id"])
    assert page["total"] == 2 and {i["source_artifact"] for i in page["items"]} == {a["id"]}
    with artifact_store._con() as con:
        assert con.execute("SELECT count(*) FROM repo_item_revisions").fetchone()[0] == 2


def test_gc_keeps_a_blob_only_a_repository_references_and_a_repository_delete_frees_it(
    origin, proj
):
    a = publish(origin, proj, "keep.txt", "kept by the repository")
    sha = sha_of(a["id"])
    repo = repo_store.create("keep")
    item = repo_store.add_artifact(repo["id"], a["id"])
    assert artifact_store.delete(a["id"])
    artifact_store._collect_garbage()
    assert sha in blobs()  # only the repository names it now
    served = repo_store.resolve(item["id"], "")
    assert served.blob.read_text() == "kept by the repository"
    assert repo_store.delete(repo["id"])
    assert sha not in blobs()  # nothing names it: collected at once
    assert repo_store.list_repos() == []


def test_removing_an_item_releases_its_blobs(origin, proj):
    a = publish(origin, proj, "gone.txt", "removed with the item")
    sha = sha_of(a["id"])
    repo = repo_store.create("r1")
    item = repo_store.add_artifact(repo["id"], a["id"])
    assert repo_store.remove_item(repo["id"], item["id"])
    assert sha in blobs()  # the artifact still holds it
    assert repo_store.items(repo["id"])["total"] == 0
    assert artifact_store.delete(a["id"])
    assert sha not in blobs()
    assert repo_store.remove_item(repo["id"], item["id"]) is False


def test_the_global_cap_counts_repository_bytes(origin, proj, monkeypatch):
    a = publish(origin, proj, "big.bin", b"1" * 6000)
    repo = repo_store.create("holder")
    repo_store.add_artifact(repo["id"], a["id"])
    artifact_store.delete(a["id"])
    # The repository still holds 6000 bytes: they count toward the host-wide total once.
    assert artifact_store.usage()["bytes"] == 6000
    monkeypatch.setattr(artifact_store, "TOTAL_BYTES_MAX", 10_000)
    with pytest.raises(artifact_store.ArtifactRefused) as e:
        publish(origin, proj, "more.bin", b"2" * 6000)
    assert e.value.code == "store_full"
    # Bytes the repository already holds are counted once when they are published again (the
    # stat-size pre-check is deliberately conservative, so the cap leaves room for the copy).
    monkeypatch.setattr(artifact_store, "TOTAL_BYTES_MAX", 12_000)
    publish(origin, proj, "same.bin", b"1" * 6000)
    assert artifact_store.usage()["bytes"] == 6000


def test_a_revision_of_an_item_and_its_kind_and_pruning(origin, proj, monkeypatch):
    monkeypatch.setattr(repo_store, "REVISIONS_MAX", 2)
    repo = repo_store.create("revs")
    first = publish(origin, proj, "v.txt", "one")
    item = repo_store.add_artifact(repo["id"], first["id"])
    shas = []
    for body in ("two", "three"):
        nxt = publish(origin, proj, "v.txt", body)
        shas.append(sha_of(nxt["id"]))
        item = repo_store.add_artifact(repo["id"], nxt["id"], item_id=item["id"])
    assert item["revision"] == 3 and repo_store.items(repo["id"])["total"] == 1
    with artifact_store._con() as con:
        kept = [r[0] for r in con.execute("SELECT n FROM repo_item_revisions ORDER BY n")]
    assert kept == [2, 3]  # the oldest pruned, never the latest
    (proj / "site").mkdir()
    (proj / "site" / "index.html").write_text("<p>a site</p>")
    s = artifact_store.publish(origin, {"path": "site", "title": "S", "why": "w"})
    with pytest.raises(artifact_store.ArtifactRefused) as e:
        repo_store.add_artifact(repo["id"], s["id"], item_id=item["id"])
    assert e.value.code == "kind_mismatch"
    with pytest.raises(artifact_store.ArtifactRefused) as e:
        repo_store.add_artifact(repo["id"], first["id"], revision=9)
    assert e.value.code == "unknown_artifact"


def test_names_are_unique_slugs_never_an_ids_shape(origin):
    repo = repo_store.create("design-assets", "one")
    for bad in ("", "Design", "a b", "-x", "x-", "x" * 65, "1234-5678-9012-3456", None, 5):
        with pytest.raises(artifact_store.ArtifactRefused) as e:
            repo_store.create(bad)
        assert e.value.code == "repository_name", bad
    with pytest.raises(artifact_store.ArtifactRefused) as e:
        repo_store.create("design-assets")
    assert e.value.code == "repository_exists"
    other = repo_store.create("release-builds")
    with pytest.raises(artifact_store.ArtifactRefused) as e:
        repo_store.update(other["id"], name="design-assets")
    assert e.value.code == "repository_exists"
    renamed = repo_store.update(repo["id"], name="brand", description="two")
    assert renamed["name"] == "brand" and renamed["description"] == "two"
    assert repo_store.lookup("brand")["id"] == repo["id"]
    assert repo_store.lookup(repo["id"])["name"] == "brand"
    with pytest.raises(artifact_store.ArtifactRefused) as e:
        repo_store.lookup("design-assets")
    assert e.value.code == "unknown_repository"


def test_a_read_never_creates_the_store_and_a_version_1_store_gains_the_tables(origin, proj):
    assert repo_store.list_repos() == [] and not artifact_store.db_path().exists()
    publish(origin, proj, "a.txt", "x")
    con = sqlite3.connect(artifact_store.db_path())
    con.executescript(
        "DROP TABLE repo_item_revisions; DROP TABLE repo_items; DROP TABLE repositories;"
        " PRAGMA user_version=1;"
    )
    con.close()
    artifact_store.reset_for_tests()
    assert repo_store.list_repos() == []
    repo_store.create("after-upgrade")
    con = sqlite3.connect(artifact_store.db_path())
    assert con.execute("PRAGMA user_version").fetchone()[0] == artifact_store.SCHEMA_VERSION == 2
    con.close()


# ---- artifact_publish(repository=…): the one judge --------------------------------------------


def test_publishing_into_a_repository_is_one_judged_transaction(origin, proj):
    repo = repo_store.create("reports")
    out = publish(origin, proj, "r.md", "# report", repository="reports")
    assert out["repository"]["name"] == "reports" and out["repository"]["new_item"] is True
    item_id = out["repository"]["item_id"]
    (item,) = repo_store.items(repo["id"])["items"]
    assert item["id"] == item_id and item["added_by"] == {
        "kind": "session",
        "session_key": SESSION,
    }
    # The same artifact again (by id, into the same repository) is a new revision of its item.
    again = publish(
        origin, proj, "r.md", "# report v2", artifact_id=out["id"], repository=repo["id"]
    )
    assert again["repository"] == {
        "id": repo["id"],
        "name": "reports",
        "item_id": item_id,
        "revision": 2,
        "new_item": False,
    }
    # A new artifact is a new item.
    third = publish(origin, proj, "other.md", "# other", repository="reports")
    assert third["repository"]["item_id"] != item_id
    assert repo_store.items(repo["id"])["total"] == 2


@pytest.mark.parametrize(
    ("name", "body", "code"),
    [
        ("leak.txt", f"token={KEY}", "credential"),
        ("id_rsa", "anything", "credential"),
        (".env", "X=1", "hidden"),
        ("bundle.zip", "PK", "archive"),
    ],
)
def test_the_judge_refuses_on_the_repository_path_and_nothing_is_stored(
    origin, proj, name, body, code
):
    repo = repo_store.create("judged")
    with pytest.raises(artifact_store.ArtifactRefused) as e:
        publish(origin, proj, name, body, repository="judged")
    assert e.value.code == code
    assert repo_store.items(repo["id"])["total"] == 0
    assert artifact_store.list_artifacts()["total"] == 0


def test_an_unknown_repository_refuses_before_anything_is_copied(origin, proj, monkeypatch):
    copied = []
    monkeypatch.setattr(artifact_store, "_stage", lambda *a: copied.append(a))
    for ref in ("nope", "1234-5678-9012-3456"):
        with pytest.raises(artifact_store.ArtifactRefused) as e:
            publish(origin, proj, "a.txt", "x", repository=ref)
        assert e.value.code == "unknown_repository"
    assert copied == []
    with pytest.raises(artifact_store.ArtifactRefused) as e:
        publish(origin, proj, "a.txt", "x", repository="x" * 65)
    assert e.value.code == "bad_request"


def test_over_quota_on_publish_rolls_the_artifact_back_too(origin, proj, monkeypatch):
    monkeypatch.setattr(repo_store, "QUOTA_DEFAULT", 100)
    repo_store.create("tiny")
    before = blobs()
    with pytest.raises(artifact_store.ArtifactRefused) as e:
        publish(origin, proj, "big.bin", b"z" * 500, repository="tiny")
    assert e.value.code == "quota"
    assert artifact_store.list_artifacts()["total"] == 0
    assert blobs() == before
    assert not any((artifact_store.content_dir() / "tmp").glob("*.part"))


def test_a_repository_deleted_after_the_lookup_refuses_inside_the_commit(origin, proj, monkeypatch):
    repo = repo_store.create("vanishing")
    real_lookup = repo_store.lookup

    def lookup_then_gone(ref):
        found = real_lookup(ref)
        repo_store.delete(repo["id"])  # deleted while the publish stages its bytes
        return found

    monkeypatch.setattr(repo_store, "lookup", lookup_then_gone)
    with pytest.raises(artifact_store.ArtifactRefused) as e:
        publish(origin, proj, "a.txt", "x", repository="vanishing")
    assert e.value.code == "unknown_repository"
    assert artifact_store.list_artifacts()["total"] == 0
    assert blobs() == {}


# ---- the broker and the real tool -------------------------------------------------------------


@pytest.fixture
def broker(tmp_path, monkeypatch):
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv(secrets_broker.RUNTIME_ENV, str(runtime))
    monkeypatch.setattr(secrets_broker, "_ram_check", lambda path: True)
    monkeypatch.setattr(artifact_broker, "_server_running", True)
    return runtime


def _token(env: dict) -> str:
    return Path(env[artifact_broker.ENV_TOKEN_FILE]).read_text()


def test_the_broker_answers_which_repository_item_and_revision(broker, proj):
    repo = repo_store.create("agents")
    env = artifact_broker.launch_env(
        SESSION, SESSION, str(proj), engine="claude", manifest=MANIFEST
    )
    (proj / "f.txt").write_text("x")
    args = {"path": str(proj / "f.txt"), "title": "F", "why": "w", "repository": "agents"}
    out = artifact_broker.handle(_token(env), "publish", args)
    assert out["ok"] is True
    assert set(out["artifact"]) == {
        "id",
        "title",
        "kind",
        "revision",
        "url",
        "public_url",
        "visibility",
    }
    assert out["repository"]["id"] == repo["id"] and out["repository"]["revision"] == 1
    refused = artifact_broker.handle(_token(env), "publish", {**args, "repository": "missing"})
    assert refused == {"ok": False, "error": "unknown_repository"}
    plain = artifact_broker.handle(_token(env), "publish", {**args, "repository": None})
    assert plain["ok"] is True and "repository" not in plain


def test_the_tool_schema_offers_repository_on_every_channel(broker, proj):
    (publish_tool,) = [t for t in artifacts_mcp.TOOLS if t["name"] == "artifact_publish"]
    assert publish_tool["inputSchema"]["properties"]["repository"] == {
        "type": "string",
        "maxLength": 64,
    }
    assert "repository" not in publish_tool["inputSchema"]["required"]
    assert "repository" in artifacts_mcp.INSTRUCTIONS
    # The channels carry the same server and the same tool names (no new tool in P1).
    env = artifact_broker.launch_env(
        SESSION, SESSION, str(proj), engine="claude", manifest=MANIFEST
    )
    srv = agent_tools.artifacts_server(env)
    claude = json.loads(
        agent_tools.terminal_launch(MANIFEST, None, {}, artifacts_env=env).suffix[-1]
    )
    assert set(claude["mcpServers"]) == {"battlelab_artifacts"}
    assert any("battlelab_artifacts" in o for o in agent_tools.codex_overrides([srv]))
    assert (
        "battlelab_artifacts" in json.loads(agent_tools.opencode_config_content([srv], None))["mcp"]
    )
    copilot = json.loads(agent_tools.copilot_mcp_config([srv]))
    assert copilot["mcpServers"]["battlelab_artifacts"]["tools"] == sorted(
        t["name"] for t in artifacts_mcp.TOOLS
    )
    from agent_sessions import native_worker

    config = {"adapter": "claude-stream-json", "artifacts_env": env, "secrets_env": {}}
    assert "battlelab_artifacts" in [s.name for s in native_worker.tool_servers(config)]


def test_every_new_refusal_has_words():
    for code in ("unknown_repository", "quota", "repository_name", "repository_exists"):
        assert code in artifact_store.REFUSALS and artifacts_mcp.MEANING[code]
        assert repo_store.WORDS[code]


def test_the_real_tool_publishes_into_a_repository_over_the_socket(broker, proj):
    from test_artifacts import _serve

    repo_store.create("site-drops")
    (proj / "dist").mkdir()
    (proj / "dist" / "index.html").write_text("<h1>hi</h1>")
    env = artifact_broker.launch_env(
        SESSION, SESSION, str(proj), engine="claude", manifest=MANIFEST
    )
    stop = _serve(artifact_broker.socket_path())

    def call(i, args):
        return {
            "jsonrpc": "2.0",
            "id": i,
            "method": "tools/call",
            "params": {"name": "artifact_publish", "arguments": args},
        }

    try:
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            call(2, {"path": "dist", "title": "Hi", "why": "w", "repository": "site-drops"}),
            call(3, {"path": "dist", "title": "Hi", "why": "w", "repository": "nowhere"}),
        ]
        proc = subprocess.run(
            [sys.executable, "-I", "-m", "agent_sessions.artifacts_mcp"],
            input="".join(json.dumps(m) + "\n" for m in msgs),
            capture_output=True,
            text=True,
            cwd=proj,
            env={**os.environ, **env},
            timeout=60,
        )
    finally:
        stop["loop"].call_soon_threadsafe(stop["server"].close)
    replies = {r["id"]: r for r in map(json.loads, proc.stdout.splitlines())}
    ok = replies[2]["result"]["content"][0]["text"]
    assert replies[2]["result"]["isError"] is False
    assert "Added to repository site-drops as a new item" in ok
    bad = replies[3]["result"]
    assert bad["isError"] is True and "no repository with that name" in bad["content"][0]["text"]


# ---- the operator API and opening an item -----------------------------------------------------


@pytest.fixture
def client(auth_cfg):
    from agent_sessions.main import create_app

    return TestClient(create_app(auth_cfg), base_url="https://your-domain.example")


def _login(c, cfg):
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303


def _csrf(c, cfg) -> dict:
    return {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": cfg.origin}


def test_the_operator_api_needs_a_login_csrf_and_the_origin(client, auth_cfg, origin, proj):
    assert client.get("/api/repos").status_code == 401
    assert client.post("/api/repos", json={"name": "x"}).status_code in (401, 403)
    _login(client, auth_cfg)
    h = _csrf(client, auth_cfg)
    assert client.post("/api/repos", json={"name": "x"}).status_code == 403  # no CSRF
    wrong = {**h, "Origin": "https://evil.example"}
    assert client.post("/api/repos", json={"name": "x"}, headers=wrong).status_code == 403
    created = client.post("/api/repos", json={"name": "x", "description": "d"}, headers=h)
    assert created.status_code == 201
    rid = created.json()["id"]
    assert client.patch(f"/api/repos/{rid}", json={"name": "y"}).status_code == 403
    assert client.delete(f"/api/repos/{rid}").status_code == 403
    a = publish(origin, proj, "a.txt", "x")
    assert client.post(f"/api/repos/{rid}/items", json={"artifact_id": a["id"]}).status_code == 403
    assert repo_store.items(rid)["total"] == 0


def test_the_operator_api_creates_adds_lists_removes_and_deletes(client, auth_cfg, origin, proj):
    a = publish(origin, proj, "plan.md", "# plan")
    _login(client, auth_cfg)
    h = _csrf(client, auth_cfg)
    bad = client.post("/api/repos", json={"name": "Bad Name"}, headers=h)
    assert bad.status_code == 422 and bad.json()["code"] == "repository_name"
    assert "lowercase" in bad.json()["detail"]
    rid = client.post("/api/repos", json={"name": "plans"}, headers=h).json()["id"]
    dup = client.post("/api/repos", json={"name": "plans"}, headers=h)
    assert dup.status_code == 409 and dup.json()["code"] == "repository_exists"
    listed = client.get("/api/repos").json()
    assert [r["name"] for r in listed["repositories"]] == ["plans"]
    assert listed["quota_default"] == repo_store.QUOTA_DEFAULT
    renamed = client.patch(f"/api/repos/{rid}", json={"description": "all plans"}, headers=h)
    assert renamed.json()["description"] == "all plans" and renamed.json()["name"] == "plans"
    added = client.post(f"/api/repos/{rid}/items", json={"artifact_id": a["id"]}, headers=h)
    assert added.status_code == 201
    item = added.json()
    assert item["revision"] == 1 and item["url"].endswith(f"/artifacts/i/{item['id']}/")
    rev = client.post(
        f"/api/repos/{rid}/items",
        json={"artifact_id": a["id"], "revision": 1, "item_id": item["id"]},
        headers=h,
    )
    assert rev.json()["revision"] == 2
    unknown = client.post(
        f"/api/repos/{rid}/items", json={"artifact_id": "1234-5678-9012-3456"}, headers=h
    )
    assert unknown.status_code == 404 and unknown.json()["code"] == "unknown_artifact"
    extra = client.post(
        f"/api/repos/{rid}/items", json={"artifact_id": a["id"], "path": "/etc"}, headers=h
    )
    assert extra.status_code == 422
    page = client.get(f"/api/repos/{rid}/items").json()
    assert page["total"] == 1 and page["items"][0]["bytes"] == 6
    assert client.get(f"/api/repos/{rid}").json()["items"] == 1
    gone = client.delete(f"/api/repos/{rid}/items/{item['id']}", headers=h)
    assert gone.json() == {"removed": item["id"]}
    assert client.delete(f"/api/repos/{rid}/items/{item['id']}", headers=h).status_code == 404
    assert client.delete(f"/api/repos/{rid}", headers=h).json() == {"deleted": rid}
    assert client.get(f"/api/repos/{rid}").status_code == 404
    assert client.get("/api/repos/not-an-id/items").status_code == 404


def test_an_item_opens_sandboxed_behind_a_view_token(client, auth_cfg, origin, proj):
    a = publish(origin, proj, "page.html", "<h1>item page</h1>")
    repo = repo_store.create("pages")
    item = repo_store.add_artifact(repo["id"], a["id"])
    link = f"/artifacts/i/{item['id']}/"
    r = client.get(link, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/login?next={link}"
    _login(client, auth_cfg)
    r = client.get(link, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/artifacts/v/")
    served = client.get(r.headers["location"])
    assert served.status_code == 200 and "item page" in served.text
    assert served.headers["content-security-policy"] == SANDBOX_CSP
    # A pinned revision, the meta the viewer page reads, and an unknown one.
    pinned = client.get(f"/artifacts/i/{item['id']}@1/", follow_redirects=False)
    token = pinned.headers["location"].split("/")[3]
    meta = client.get(f"/artifacts/o/{token}/~meta").json()
    assert meta["title"] == "page.html" and meta["kind"] == "file" and meta["header"] is True
    assert client.get(f"/artifacts/i/{item['id']}@9/", follow_redirects=False).status_code == 404
    assert (
        client.get("/artifacts/i/1234-5678-9012-3456/", follow_redirects=False).status_code == 404
    )
    # An item token never reads as an artifact's, nor the reverse.
    from agent_sessions.routes import artifacts as routes

    as_artifact = routes.view_token(auth_cfg, item["id"], None)
    assert client.get(f"/artifacts/v/{as_artifact}/").status_code == 404
    as_item = routes.view_token(auth_cfg, a["id"], None, item=True)
    assert client.get(f"/artifacts/v/{as_item}/").status_code == 404
    # Still served once the artifact is gone: the repository holds its own revision.
    artifact_store.delete(a["id"])
    assert "item page" in client.get(r.headers["location"]).text
