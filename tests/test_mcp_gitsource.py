"""An MCP server from a repository link (#2353): `mcp_gitsource`, `mcp_jobs`' resolve kind and the
routes that bind a resolution.

Pinned here, against real git (a bare fixture repository behind #2145's test admission, as
`test_skill_repo.py` does) and fixture registry answers:

* npm and PyPI happy paths: the source is P1b's, unchanged; the origin is the link and the commit
  the fetch pinned; the only registry request is the release's own metadata;
* every refusal, in words, with nothing registered: no manifest, both manifests, no name, no or a
  dynamic version, a malformed manifest, a /tree/ or /blob/ link, a non-root or non-https link,
  an unpublished release, a PyPI release without a wheel, missing / malformed / mismatching
  repository metadata;
* identity: ONE parser on both sides, exact host (`github.com.evil.example`), exact repo
  (`repo-evil`), no path smuggling (`evil.example/github.com/owner/repo`); `.git`, a trailing
  `/`, `git+https`, `git://` and npm's `github:` forms match; a metadata URL is never requested;
* the registry read itself: a 404 is "unpublished", a redirect is never followed, the body is
  bounded, and only the registry's host is allowed;
* the routes: resolve → create persists source and origin together under the reserved id; an
  edited source is 409, a raw origin 422; an unknown, expired, restarted or reused resolution is
  refused; the same request id with another link is 409; Stop ends a read; an update that changes
  the source clears the origin.
"""

from __future__ import annotations

import http.server
import json
import threading
import time
import uuid

import pytest

from agent_sessions import mcp_gitsource, mcp_install, mcp_jobs, mcp_registry, skill_repo
from test_skill_repo import _git, local, make_repo  # noqa: F401 — the fixture is used by name

LINK = "https://github.com/owner/repo"
NPM_URL = "https://registry.npmjs.org/@acme%2Fmcp-server/1.4.0"
PYPI_URL = "https://pypi.org/pypi/acme-mcp/1.4.0/json"
PKG = json.dumps({"name": "@acme/mcp-server", "version": "1.4.0", "bin": {"acme-mcp": "x.js"}})
PYPROJECT = '[project]\nname = "acme-mcp"\nversion = "1.4.0"\n'


def _npm_meta(repository: object = "git+https://github.com/owner/repo.git", **over) -> dict:
    meta = {
        "name": "@acme/mcp-server",
        "version": "1.4.0",
        "license": "MIT",
        "dependencies": {"a": "^1", "b": "^2", "c": "^3"},
        "scripts": {"postinstall": "node setup.js", "test": "x"},
        "dist": {"integrity": "sha512-AbCdEf=="},
        **over,
    }
    if repository is not None:
        meta["repository"] = repository
    return meta


def _pypi_meta(urls: dict | None = None, wheel: bool = True) -> dict:
    files = [{"packagetype": "sdist", "filename": "acme_mcp-1.4.0.tar.gz"}]
    if wheel:
        files.append({"packagetype": "bdist_wheel", "filename": "acme_mcp-1.4.0-py3-none-any.whl"})
    return {
        "info": {
            "name": "acme-mcp",
            "version": "1.4.0",
            "license_expression": "MIT",
            "project_urls": urls if urls is not None else {"Source": LINK},
            "requires_dist": ["mcp>=1"],
        },
        "urls": files,
    }


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_SESSIONS_MCP_SERVERS", str(tmp_path / "mcp-servers.json"))
    return tmp_path


@pytest.fixture
def repos(home, monkeypatch, local):  # noqa: F811
    """`build(files)` makes a bare fixture repository served for LINK (and its spellings): the
    only test seam is the transport, the fetch and every read are #2145's own."""
    made: dict[str, object] = {}

    def build(files: dict, url: str = LINK):
        srv = make_repo(home / f"repo{len(made)}", files)
        made[skill_repo.normalize(url).lower()] = srv
        return srv

    def fetch(url, cancel):
        return skill_repo.fetch(f"file://{made[skill_repo.normalize(url).lower()]}", cancel=cancel)

    monkeypatch.setattr(mcp_gitsource, "_fetch", fetch)
    return build


@pytest.fixture
def registry(monkeypatch):
    """Fixture registry answers by URL (absent → 404), and every URL requested."""
    state: dict = {"answers": {}, "seen": []}

    def get(kind, url):
        state["seen"].append(url)
        answer = state["answers"].get(url)
        return json.loads(json.dumps(answer)) if answer is not None else None

    monkeypatch.setattr(mcp_gitsource, "_registry_get", get)
    return state


def _head(srv) -> str:
    return _git(srv, "rev-parse", "HEAD").strip()


# ---- happy paths --------------------------------------------------------------------------------


def test_an_npm_repository_resolves_to_its_published_release_at_a_pinned_commit(repos, registry):
    srv = repos({"package.json": PKG, "README.md": "# acme\n"})
    registry["answers"][NPM_URL] = _npm_meta()
    out = mcp_gitsource.resolve(LINK)
    assert out["source"] == {"kind": "npm", "package": "@acme/mcp-server", "version": "1.4.0"}
    assert out["origin"] == {"url": LINK, "commit": _head(srv)}
    facts = out["facts"]
    assert facts["repository"] == "github.com/owner/repo"
    assert facts["declared"] == {
        "manifest": "package.json",
        "kind": "npm",
        "package": "@acme/mcp-server",
        "version": "1.4.0",
    }
    assert facts["release"]["names"] == "github.com/owner/repo"
    assert facts["release"]["install_scripts"] == ["postinstall"]
    assert facts["release"]["direct_dependencies"] == 3
    # The release's own metadata is the ONLY registry read; the repository URL in it never is.
    assert registry["seen"] == [NPM_URL]
    assert list((skill_repo.root()).glob("fetch-*")) == []  # the fetched folder is gone


def test_a_python_repository_resolves_to_its_wheel_release(repos, registry):
    srv = repos({"pyproject.toml": PYPROJECT, "src/acme_mcp/__init__.py": ""})
    registry["answers"][PYPI_URL] = _pypi_meta({"Homepage": "https://acme.example", "Source": LINK})
    out = mcp_gitsource.resolve(LINK + ".git/")
    assert out["source"] == {"kind": "pypi", "package": "acme-mcp", "version": "1.4.0"}
    assert out["origin"] == {"url": LINK, "commit": _head(srv)}
    assert out["facts"]["release"]["registry"] == "pypi.org"
    assert registry["seen"] == [PYPI_URL]


def test_a_pyproject_without_a_project_table_is_not_a_second_declaration(repos, registry):
    repos({"package.json": PKG, "pyproject.toml": "[tool.black]\nline-length = 100\n"})
    registry["answers"][NPM_URL] = _npm_meta()
    assert mcp_gitsource.resolve(LINK)["source"]["kind"] == "npm"


# ---- refusals ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "files, code, words",
    [
        ({"README.md": "x"}, "no_manifest", "declares no package"),
        ({"pyproject.toml": "[tool.ruff]\n"}, "no_manifest", "declares no package"),
        ({"package.json": PKG, "pyproject.toml": PYPROJECT}, "both_manifests", "both an npm"),
        ({"package.json": '{"version": "1.0.0"}'}, "no_name", "names no package"),
        ({"package.json": '{"name": "acme"}'}, "no_version", "doesn't state a version"),
        (
            {"pyproject.toml": '[project]\nname = "acme-mcp"\ndynamic = ["version"]\n'},
            "no_version",
            "version is dynamic",
        ),
        ({"package.json": "{not json"}, "manifest", "not valid JSON"),
        ({"pyproject.toml": "[project\n"}, "manifest", "not valid TOML"),
        ({"package.json": '{"name": "acme", "version": "^1.0.0"}'}, "manifest", "exact version"),
    ],
)
def test_a_repository_that_declares_no_single_exact_package_is_refused(
    repos, registry, files, code, words
):
    repos(files)
    with pytest.raises(mcp_gitsource.LinkRefused, match=words) as e:
        mcp_gitsource.resolve(LINK)
    assert e.value.code == code
    assert registry["seen"] == []  # nothing is looked up for a repository that declares nothing
    assert e.value.facts["repository"] == "github.com/owner/repo" and e.value.facts["commit"]


def test_the_refusal_for_two_manifests_names_both(repos, registry):
    repos({"package.json": PKG, "pyproject.toml": PYPROJECT})
    with pytest.raises(mcp_gitsource.LinkRefused) as e:
        mcp_gitsource.resolve(LINK)
    assert [(f["manifest"], f["package"]) for f in e.value.facts["found"]] == [
        ("package.json", "@acme/mcp-server"),
        ("pyproject.toml", "acme-mcp"),
    ]


@pytest.mark.parametrize(
    "link, code",
    [
        ("https://github.com/owner/repo/tree/main/packages/server", "not_root"),
        ("https://github.com/owner/repo/blob/main/package.json", "not_root"),
        ("https://gitlab.com/group/repo/-/tree/main", "not_root"),
        ("https://github.com/owner", "not_root"),
        ("https://github.com/owner/repo/extra", "not_root"),
        ("http://github.com/owner/repo", "bad_url"),
        ("git@github.com:owner/repo.git", "bad_url"),
        ("https://user:pw@github.com/owner/repo", "bad_url"),
        ("https://github.com/owner/repo?tab=readme", "bad_url"),
    ],
)
def test_only_an_https_repository_root_link_is_read(home, registry, monkeypatch, link, code):
    fetched = []
    monkeypatch.setattr(mcp_gitsource, "_fetch", lambda url, cancel: fetched.append(url))
    with pytest.raises(mcp_gitsource.LinkRefused) as e:
        mcp_gitsource.resolve(link)
    assert e.value.code == code, str(e.value)
    assert fetched == [] and registry["seen"] == []


def test_an_unpublished_release_is_refused_and_nothing_is_built(repos, registry):
    repos({"package.json": PKG})
    with pytest.raises(mcp_gitsource.LinkRefused, match="no published release") as e:
        mcp_gitsource.resolve(LINK)
    assert e.value.code == "unpublished" and "never builds" in str(e.value)


def test_a_pypi_release_without_a_wheel_is_refused(repos, registry):
    repos({"pyproject.toml": PYPROJECT})
    registry["answers"][PYPI_URL] = _pypi_meta(wheel=False)
    with pytest.raises(mcp_gitsource.LinkRefused, match="wheels") as e:
        mcp_gitsource.resolve(LINK)
    assert e.value.code == "no_wheel"


@pytest.mark.parametrize(
    "repository, code",
    [
        (None, "no_metadata"),
        ("not a repository", "bad_metadata"),
        ({"type": "git"}, "bad_metadata"),
        ("ssh://git@github.com/owner/repo.git", "bad_metadata"),
        # Deceptive: another host whose name STARTS with the link's, a repository whose name
        # starts with the link's, and the link's host smuggled in as a path.
        ("https://github.com.evil.example/owner/repo", "mismatch"),
        ("https://github.com/owner/repo-evil", "mismatch"),
        ("https://evil.example/github.com/owner/repo", "mismatch"),
        ("https://github.com/other/repo", "mismatch"),
    ],
)
def test_registry_metadata_that_does_not_name_the_link_is_refused(
    repos, registry, repository, code
):
    repos({"package.json": PKG})
    registry["answers"][NPM_URL] = _npm_meta(repository)
    with pytest.raises(mcp_gitsource.LinkRefused) as e:
        mcp_gitsource.resolve(LINK)
    assert e.value.code == code, str(e.value)
    # Never requested, even to "check" it: only the release's metadata was read.
    assert registry["seen"] == [NPM_URL]


@pytest.mark.parametrize(
    "repository",
    [
        "git+https://github.com/owner/repo.git",
        "https://github.com/owner/repo/",
        "https://github.com/Owner/Repo",
        "github:owner/repo",
        "git://github.com/owner/repo.git",
        {"type": "git", "url": "https://github.com/owner/repo.git", "directory": "packages/x"},
        "https://github.com/owner/repo/tree/main/packages/server",
    ],
)
def test_the_usual_spellings_of_the_same_repository_match(repos, registry, repository):
    repos({"package.json": PKG})
    registry["answers"][NPM_URL] = _npm_meta(repository)
    assert mcp_gitsource.resolve(LINK)["facts"]["release"]["names"] == "github.com/owner/repo"


def test_pypi_project_urls_other_than_source_repository_or_homepage_do_not_count(repos, registry):
    repos({"pyproject.toml": PYPROJECT})
    registry["answers"][PYPI_URL] = _pypi_meta({"Documentation": LINK, "Issues": LINK + "/issues"})
    with pytest.raises(mcp_gitsource.LinkRefused) as e:
        mcp_gitsource.resolve(LINK)
    assert e.value.code == "no_metadata"


def test_identity_is_one_parser_with_an_exact_host():
    ident = mcp_gitsource.identity
    assert ident("https://github.com/Owner/Repo.git") == ("github.com", "owner", "repo")
    assert ident("github:owner/repo#v1.2.3") == ("github.com", "owner", "repo")
    # Unknown forges compare owner and repository exactly.
    assert ident("https://git.example/Owner/Repo") != ident("https://git.example/owner/repo")
    assert ident("https://GIT.example:8443/o/r")[0] == "git.example:8443"
    assert ident("https://git.example:443/o/r")[0] == "git.example"
    for bad in ("https://github.com/owner", "ftp://github.com/o/r", "https://a@github.com/o/r", ""):
        assert ident(bad) is None, bad


# ---- the registry read -------------------------------------------------------------------------


@pytest.fixture
def loopback_registry(monkeypatch):
    """A plain-http stand-in for a registry on loopback, admitted as the ONLY egress host."""
    state = {"routes": {}, "paths": []}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            state["paths"].append(self.path)
            status, headers, body = state["routes"].get(self.path, (404, {}, b""))
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    monkeypatch.setattr(mcp_install, "_allowed", lambda kind: {("127.0.0.1", port)})
    state["base"] = f"http://127.0.0.1:{port}"
    yield state
    srv.shutdown()


def test_the_registry_read_is_bounded_and_never_follows_a_redirect(loopback_registry, monkeypatch):
    st = loopback_registry
    base = st["base"]
    st["routes"]["/ok"] = (200, {}, json.dumps({"name": "x"}).encode())
    st["routes"]["/moved"] = (302, {"Location": f"{base}/ok"}, b"")
    st["routes"]["/text"] = (200, {}, b"<html>")
    assert mcp_gitsource._registry_get("npm", f"{base}/ok") == {"name": "x"}
    assert mcp_gitsource._registry_get("npm", f"{base}/gone") is None  # 404: unpublished
    st["paths"].clear()
    with pytest.raises(mcp_gitsource.LinkRefused, match="answered 302"):
        mcp_gitsource._registry_get("npm", f"{base}/moved")
    assert st["paths"] == ["/moved"]  # the redirect's target was never requested
    with pytest.raises(mcp_gitsource.LinkRefused, match="could not be read"):
        mcp_gitsource._registry_get("npm", f"{base}/text")
    monkeypatch.setattr(mcp_gitsource, "REGISTRY_BODY_MAX", 10)
    st["routes"]["/big"] = (200, {}, b"x" * 100)
    with pytest.raises(mcp_gitsource.LinkRefused, match="too large"):
        mcp_gitsource._registry_get("npm", f"{base}/big")
    # Only the registry's host: anything else is refused before a request is made.
    with pytest.raises(mcp_gitsource.LinkRefused, match="not allowed"):
        mcp_gitsource._registry_get("npm", "https://evil.example/x")


# ---- the operation and the routes ---------------------------------------------------------------


def _resolved(link_id=("github.com", "owner", "repo")) -> dict:
    return {
        "source": {"kind": "npm", "package": "@acme/mcp-server", "version": "1.4.0"},
        "origin": {"url": LINK, "commit": "3f2a9c1e5b7d40a8c6f2e19d3b4a7c58e0f1d26b"},
        "facts": {"repository": "/".join(link_id), "commit": "3f2a9c1e" + "0" * 32},
    }


@pytest.fixture
def api(home, tmp_home, auth_cfg, monkeypatch):
    """The real app, logged in, with the resolver and P1b's review as fixtures (a gate holds a
    resolve while a test needs it running)."""
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    mcp_jobs._reset_for_tests()
    state: dict = {"gate": threading.Event(), "links": [], "reviewed": []}
    state["gate"].set()

    def resolve(url, *, cancel=None, progress=mcp_gitsource._no_progress):
        state["links"].append(url)
        progress("commit")
        while not state["gate"].wait(0.05):
            if cancel is not None and cancel.is_set():
                raise mcp_gitsource.LinkRefused("stopped: the link was not read", "stopped")
        if "refused" in url:
            raise mcp_gitsource.LinkRefused(
                "this repository declares both an npm and a Python package",
                "both_manifests",
                {"repository": "github.com/owner/refused", "found": []},
            )
        return _resolved()

    def review(rec):
        state["reviewed"].append(rec["id"])
        return {"review_id": "0123456789abcdef", "source": rec["source"], "integrity": "sha512-A"}

    monkeypatch.setattr(mcp_gitsource, "resolve", resolve)
    monkeypatch.setattr(mcp_install, "review", review)
    with TestClient(create_app(auth_cfg), base_url="https://testserver") as client:
        assert client.post("/api/mcp/servers/resolve-link", json={}).status_code in (401, 403)
        client.post(
            "/login",
            data={"username": "marcus", "password": "hunter2"},
            headers={"Origin": auth_cfg.origin},
            follow_redirects=False,
        )
        csrf = client.get("/api/config").json()["csrf"]
        state["client"] = client
        state["h"] = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
        yield state
    mcp_jobs._reset_for_tests()


def _start(api, link=LINK, rid=None):
    rid = rid or str(uuid.uuid4())
    r = api["client"].post(
        "/api/mcp/servers/resolve-link", json={"url": link, "request_id": rid}, headers=api["h"]
    )
    return rid, r


def _poll(api, rid):
    deadline = time.monotonic() + 10
    while True:
        op = api["client"].get(f"/api/mcp/operations/{rid}").json()
        if op["state"] != "running":
            return op
        assert time.monotonic() < deadline, op
        time.sleep(0.05)


def _create(api, body):
    return api["client"].post("/api/mcp/servers", json=body, headers=api["h"])


def test_resolve_then_create_persists_source_and_origin_together(api):
    rid, r = _start(api)
    assert r.status_code == 202 and r.json()["kind"] == "resolve" and r.json()["server"] is None
    op = _poll(api, rid)
    assert op["state"] == "done", op
    res = op["result"]
    assert res["resolution_id"] == rid and res["review"]["review_id"] == "0123456789abcdef"
    assert res["disclaimer"] == "Installs the published release; not built from this commit."
    assert mcp_registry.list_servers() == []  # resolving registered nothing
    created = _create(
        api, {"name": "acme", "source": res["source"], "resolution_id": rid, "label": "Acme"}
    )
    assert created.status_code == 201, created.text
    rec = mcp_registry.get(created.json()["id"])
    assert rec["origin"] == res["origin"] and rec["source"] == res["source"]
    # The review was staged under the id the server took: Install installs exactly that lock.
    assert api["reviewed"] == [rec["id"]]
    assert created.json()["origin"] == res["origin"]
    # One resolution, one server.
    again = _create(api, {"name": "acme2", "resolution_id": rid})
    assert again.status_code == 409


def test_a_create_that_edits_the_resolved_source_or_sends_an_origin_is_refused(api):
    rid, _ = _start(api)
    res = _poll(api, rid)["result"]
    edited = {**res["source"], "version": "1.4.1"}
    r = _create(api, {"name": "acme", "source": edited, "resolution_id": rid})
    assert r.status_code == 409 and "resolve the link again" in r.json()["detail"]
    other = {"kind": "npm", "package": "@evil/server", "version": "1.4.0"}
    assert _create(api, {"name": "acme", "source": other, "resolution_id": rid}).status_code == 409
    raw = {"name": "acme", "source": res["source"], "origin": res["origin"]}
    assert _create(api, raw).status_code == 422
    assert _create(api, {**raw, "resolution_id": rid}).status_code == 422
    assert mcp_registry.list_servers() == []


def test_an_unknown_expired_or_restarted_resolution_is_refused(api, monkeypatch):
    body = {"name": "acme", "source": _resolved()["source"]}
    r = _create(api, {**body, "resolution_id": str(uuid.uuid4())})
    assert r.status_code == 404 and "resolve the link again" in r.json()["detail"]
    assert _create(api, {**body, "resolution_id": "nope"}).status_code == 422
    rid, _ = _start(api)
    _poll(api, rid)
    monkeypatch.setattr(mcp_jobs, "RESOLUTION_TTL_S", 0)
    time.sleep(0.01)
    assert _create(api, {**body, "resolution_id": rid}).status_code == 404  # expired
    monkeypatch.setattr(mcp_jobs, "RESOLUTION_TTL_S", 3600)
    rid, _ = _start(api)
    _poll(api, rid)
    mcp_jobs._reset_for_tests()  # what an app restart leaves: no operations at all
    assert _create(api, {**body, "resolution_id": rid}).status_code == 404
    assert mcp_registry.list_servers() == []


def test_the_same_request_id_with_another_link_is_refused_never_answered(api):
    api["gate"].clear()
    rid, first = _start(api)
    assert first.status_code == 202
    _, same = _start(api, LINK, rid)
    assert same.status_code == 202 and same.json()["id"] == rid
    _, other = _start(api, "https://github.com/owner/other", rid)
    assert other.status_code == 409
    api["gate"].set()
    assert _poll(api, rid)["state"] == "done"
    _, after = _start(api, "https://github.com/owner/other", rid)
    assert after.status_code == 409  # still bound to its link once done
    assert api["links"] == [LINK]
    # The same id is never reused for a server's review either.
    sid = mcp_registry.create(
        {"name": "xx", "source": {"kind": "npm", "package": "x", "version": "1.0.0"}}
    )["id"]
    url = f"/api/mcp/servers/{sid}/review"
    r = api["client"].post(url, json={"request_id": rid}, headers=api["h"])
    assert r.status_code == 409


def test_a_refused_link_fails_in_words_with_what_was_read(api):
    rid, _ = _start(api, "https://github.com/owner/refused")
    op = _poll(api, rid)
    assert op["state"] == "failed" and "both an npm and a Python" in op["error"]
    assert op["details"]["repository"] == "github.com/owner/refused"
    r = _create(api, {"name": "acme", "source": _resolved()["source"], "resolution_id": rid})
    assert r.status_code == 409 and mcp_registry.list_servers() == []


def test_stop_ends_a_running_read(api):
    api["gate"].clear()
    rid, _ = _start(api)
    assert api["client"].post(f"/api/mcp/operations/{rid}/stop").status_code == 403  # CSRF
    stopped = api["client"].post(f"/api/mcp/operations/{rid}/stop", headers=api["h"])
    assert stopped.status_code == 200
    op = _poll(api, rid)
    assert op["state"] == "failed" and op["error"].startswith("stopped")
    assert api["reviewed"] == []


def test_an_update_that_changes_the_source_clears_the_origin(api):
    rid, _ = _start(api)
    res = _poll(api, rid)["result"]
    rec = _create(api, {"name": "acme", "resolution_id": rid}).json()
    sid = rec["id"]
    kept = mcp_registry.update(sid, {"label": "Acme"}, mcp_registry.get(sid)["updated_at"])
    assert kept["origin"] == res["origin"]
    same = mcp_registry.update(sid, {"source": res["source"]}, kept["updated_at"])
    assert same["origin"] == res["origin"]  # the same source keeps it
    moved = {**res["source"], "version": "1.5.0"}
    r = api["client"].patch(
        f"/api/mcp/servers/{sid}",
        json={"source": moved, "expected_updated_at": same["updated_at"]},
        headers=api["h"],
    )
    assert r.status_code == 200 and "origin" not in r.json()
    assert "origin" not in mcp_registry.get(sid)
    # Provenance never comes back through an update.
    r = api["client"].patch(
        f"/api/mcp/servers/{sid}",
        json={"origin": res["origin"], "expected_updated_at": r.json()["updated_at"]},
        headers=api["h"],
    )
    assert r.status_code == 422


def test_run_reviews_the_resolved_source_under_the_reserved_id(home, monkeypatch):
    seen = {}
    monkeypatch.setattr(mcp_gitsource, "resolve", lambda url, **k: _resolved())

    def review(rec):
        seen.update(rec)
        return {"review_id": "f" * 16}

    monkeypatch.setattr(mcp_install, "review", review)
    op = mcp_jobs.Operation(id=str(uuid.uuid4()), server="", kind="resolve", link=LINK)
    op.reserved = "a" * 16
    out = mcp_gitsource.run(op)
    assert seen == {"id": "a" * 16, "source": _resolved()["source"]}
    assert op.bound == {"id": "a" * 16, **{k: _resolved()[k] for k in ("source", "origin")}}
    assert out["review"] == {"review_id": "f" * 16}

    def failing(rec):
        raise mcp_install.InstallError("it has no wheel for this machine")

    monkeypatch.setattr(mcp_install, "review", failing)
    op2 = mcp_jobs.Operation(id=str(uuid.uuid4()), server="", kind="resolve", link=LINK)
    with pytest.raises(mcp_gitsource.LinkRefused, match="review failed: it has no wheel"):
        mcp_gitsource.run(op2)
    assert op2.bound is None


# ---- Update from source (#2427) -----------------------------------------------------------------

NEW_COMMIT = "4b1d2e7f" + "1" * 32


def _register(api) -> dict:
    rid, _ = _start(api)
    _poll(api, rid)
    r = _create(api, {"name": "acme", "resolution_id": rid})
    assert r.status_code == 201, r.text
    rec = mcp_registry.get(r.json()["id"])
    return mcp_registry.set_installed(rec["id"], {"source": rec["source"], "command": "/bin/true"})


def _update_start(api, sid, rid=None):
    rid = rid or str(uuid.uuid4())
    r = api["client"].post(
        "/api/mcp/servers/resolve-link",
        json={"server_id": sid, "request_id": rid},
        headers=api["h"],
    )
    return rid, r


def _newer(monkeypatch, version="1.5.0", commit=NEW_COMMIT):
    def resolve(url, *, cancel=None, progress=mcp_gitsource._no_progress):
        out = _resolved()
        out["source"]["version"] = version
        out["origin"]["commit"] = commit
        return out

    monkeypatch.setattr(mcp_gitsource, "resolve", resolve)


def test_update_from_source_reads_the_servers_own_link_and_replaces_source_and_origin(
    api, monkeypatch
):
    rec = _register(api)
    _newer(monkeypatch)
    rid, r = _update_start(api, rec["id"])
    assert r.status_code == 202
    op = _poll(api, rid)
    assert op["state"] == "done", op
    res = op["result"]
    assert res["up_to_date"] is False and res["source"]["version"] == "1.5.0"
    assert res["previous"]["source"]["version"] == "1.4.0" and res["origin"]["commit"] == NEW_COMMIT
    assert api["reviewed"][-1] == rec["id"]  # the review is staged under the server's own id
    patched = api["client"].patch(
        f"/api/mcp/servers/{rec['id']}",
        json={"resolution_id": rid, "expected_updated_at": rec["updated_at"]},
        headers=api["h"],
    )
    assert patched.status_code == 200, patched.text
    now = mcp_registry.get(rec["id"])
    assert now["source"]["version"] == "1.5.0" and now["origin"]["commit"] == NEW_COMMIT
    with pytest.raises(mcp_registry.RegistryError, match="not installed"):
        mcp_install.command(now)  # the new source is installed (and checked) afresh
    again = api["client"].patch(
        f"/api/mcp/servers/{rec['id']}",
        json={"resolution_id": rid, "expected_updated_at": now["updated_at"]},
        headers=api["h"],
    )
    assert again.status_code == 409  # a resolution applies once


def test_an_up_to_date_server_says_so_and_nothing_can_be_applied(api):
    rec = _register(api)
    rid, _ = _update_start(api, rec["id"])
    res = _poll(api, rid)["result"]
    assert res["up_to_date"] is True and "review" not in res
    r = api["client"].patch(
        f"/api/mcp/servers/{rec['id']}",
        json={"resolution_id": rid, "expected_updated_at": rec["updated_at"]},
        headers=api["h"],
    )
    assert r.status_code == 409 and "already up to date" in r.json()["detail"]
    assert mcp_registry.get(rec["id"])["updated_at"] == rec["updated_at"]  # nothing written


def test_an_update_resolution_is_bound_to_its_own_server(api, monkeypatch):
    a = _register(api)
    b = mcp_registry.create(
        {"name": "other"},
        resolution={
            "id": "e" * 16,
            "source": _resolved()["source"],
            "origin": _resolved()["origin"],
        },
    )
    _newer(monkeypatch)
    rid, _ = _update_start(api, a["id"])
    _poll(api, rid)
    wrong = api["client"].patch(
        f"/api/mcp/servers/{b['id']}",
        json={"resolution_id": rid, "expected_updated_at": b["updated_at"]},
        headers=api["h"],
    )
    assert wrong.status_code == 409
    created = _create(api, {"name": "third", "resolution_id": rid})
    assert created.status_code == 409  # an update's resolution never registers a new server


def test_a_server_without_a_link_is_updated_by_version_not_from_source(api):
    rec = mcp_registry.create(
        {"name": "plain", "source": {"kind": "remote", "url": "https://mcp.example.test/mcp"}}
    )
    _, r = _update_start(api, rec["id"])
    assert r.status_code == 422 and "not added from a repository link" in r.json()["detail"]
    _, r = _update_start(api, "0" * 16)
    assert r.status_code == 404


def test_an_update_reviewed_against_an_older_server_is_refused(api, monkeypatch):
    """Fenced to what was reviewed: a source edit after the link was read makes applying the
    update a 409, never an older review overwriting a newer choice."""
    rec = _register(api)
    _newer(monkeypatch)
    rid, _ = _update_start(api, rec["id"])
    _poll(api, rid)
    edited = mcp_registry.update(
        rec["id"],
        {"source": {"kind": "npm", "package": "@acme/mcp-server", "version": "1.4.1"}},
        mcp_registry.get(rec["id"])["updated_at"],
    )
    r = api["client"].patch(
        f"/api/mcp/servers/{rec['id']}",
        json={"resolution_id": rid, "expected_updated_at": edited["updated_at"]},
        headers=api["h"],
    )
    assert r.status_code == 409 and "changed since its link was read" in r.json()["detail"]
    assert mcp_registry.get(rec["id"])["source"]["version"] == "1.4.1"


def test_two_competing_update_confirmations_apply_only_once(api, monkeypatch):
    """Two readings of the same server's link: the first applies; the second was read against
    the server before the first, so it is refused, never an older review overwriting a newer."""
    rec = _register(api)
    _newer(monkeypatch, version="1.5.0")
    first, _ = _update_start(api, rec["id"])
    _poll(api, first)
    _newer(monkeypatch, version="1.6.0", commit="5c2e3f80" + "2" * 32)
    second, _ = _update_start(api, rec["id"])
    _poll(api, second)
    ok = api["client"].patch(
        f"/api/mcp/servers/{rec['id']}",
        json={"resolution_id": first, "expected_updated_at": rec["updated_at"]},
        headers=api["h"],
    )
    assert ok.status_code == 200
    late = api["client"].patch(
        f"/api/mcp/servers/{rec['id']}",
        json={"resolution_id": second, "expected_updated_at": ok.json()["updated_at"]},
        headers=api["h"],
    )
    assert late.status_code == 409 and "changed since its link was read" in late.json()["detail"]
    assert mcp_registry.get(rec["id"])["source"]["version"] == "1.5.0"


def test_an_update_read_for_a_removed_server_never_applies_to_its_recreation(api, monkeypatch):
    rec = _register(api)
    _newer(monkeypatch)
    rid, _ = _update_start(api, rec["id"])
    _poll(api, rid)
    mcp_registry.delete(rec["id"], mcp_registry.get(rec["id"])["updated_at"])
    again = _register(api)  # the same link, registered anew: another server id
    r = api["client"].patch(
        f"/api/mcp/servers/{again['id']}",
        json={"resolution_id": rid, "expected_updated_at": again["updated_at"]},
        headers=api["h"],
    )
    assert r.status_code == 409
    gone = api["client"].patch(
        f"/api/mcp/servers/{rec['id']}",
        json={"resolution_id": rid, "expected_updated_at": rec["updated_at"]},
        headers=api["h"],
    )
    assert gone.status_code == 404


@pytest.mark.parametrize(
    "old, new",
    [
        ({"kind": "npm", "package": "@acme/mcp-server", "version": "1.4.0"},
         {"kind": "npm", "package": "@acme/mcp-server", "version": "1.5.0"}),
        ({"kind": "pypi", "package": "acme-mcp", "version": "1.4.0"},
         {"kind": "pypi", "package": "acme-mcp", "version": "1.5.0"}),
        ({"kind": "go", "package": "example.org/acme/mcp/v2", "version": "v2.1.0"},
         {"kind": "go", "package": "example.org/acme/mcp/v2", "version": "v2.2.0"}),
    ],
)  # fmt: skip
def test_an_applied_update_runs_nothing_until_its_new_install_is_checked(home, tmp_path, old, new):
    """After an update is applied, the old install and the old check authorize nothing, for every
    source kind; the old install directory itself stays (a running session may be using it)."""
    sid = "f" * 16
    origin_old = {"url": LINK, "commit": "1" * 40}
    rec = mcp_registry.create(
        {"name": "srv"}, resolution={"id": sid, "source": old, "origin": origin_old}
    )
    binary = tmp_path / "old-install" / "bin" / "srv"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    rec = mcp_registry.set_installed(sid, {"source": old, "command": str(binary), "args": []})
    assert mcp_install.command(rec) == (str(binary), [])
    mcp_registry.update(
        sid,
        {},
        rec["updated_at"],
        resolution={
            "id": sid,
            "update_of": sid,
            "source": new,
            "origin": {"url": LINK, "commit": "2" * 40},
            "previous": {"source": old, "origin": origin_old},
        },
    )
    now = mcp_registry.get(sid)
    assert now["source"] == new
    with pytest.raises(mcp_registry.RegistryError, match="not installed"):
        mcp_install.command(now)  # nothing runs until the new source is installed
    assert binary.exists()  # the old install is never removed under a running session
