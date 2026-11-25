"""HQ connectors, P1b (#1826): the read-only GitHub connector (`github-rest`).

Against a fake api.github.com that records every request:

* the bundled manifest: a fixed origin, reviewed members only, no other origin accepted;
* the transport: the kind's path table, GET only, refused BEFORE the token is fetched; GitHub's
  fixed headers, which can never displace the credential; rate limits are not bad tokens;
* the probe: the login, and a warning (never a refusal) for a write-scoped classic token;
* the four HQ tools end to end: search (the tool's kind decides issue vs PR), a repo, an issue's
  newest comments, a PR's head, CI, a directory, a file;
* CI from every source a read-only token can see — statuses, Actions runs, other apps' check
  runs — namespaced so a name shared across sources never collapses, never green when a source
  was capped or not visible;
* the #1750 guarantees carried over: the token reaches api.github.com only and nothing else holds
  it; a rebinding name connects only to the vetted address.
"""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

from agent_sessions import connector_http
from agent_sessions.connectors import hq, kinds, manifest, service, store
from agent_sessions.connectors.adapters import github

TOKEN = "ghp_SENTINEL_github_7c1d9e0f2a3b4c5d6e"
ORIGIN = "https://api.github.com"
HOST_IP = "140.82.112.6"
SHA = "c" * 40
PR_SHA = "d" * 40
CRED = "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def run(coro):
    return asyncio.run(coro)


def page_of(rows: list, key: str, request: httpx.Request, total: int | None = None) -> dict:
    per = int(request.url.params.get("per_page", 30))
    page = int(request.url.params.get("page", 1))
    return {"total_count": len(rows) if total is None else total,
            key: rows[(page - 1) * per : page * per]}  # fmt: skip


class GitHub:
    """api.github.com with one repo ``ops/app``: issue #1 (120 comments), PR #2, README, src/."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.token = TOKEN
        self.scopes: str | None = None
        self.statuses: list[dict] = [{"context": "ci/lint", "state": "success"}]
        self.runs: list[dict] = [
            {"name": "tests", "status": "completed", "conclusion": "success",
             "path": ".github/workflows/tests.yml", "workflow_id": 11}
        ]  # fmt: skip
        self.checks: list[dict] = []
        self.checks_status = 200
        self.comments = [{"user": {"login": "dev"}, "created_at": f"c{i}", "body": f"comment {i}"}
                         for i in range(1, 121)]  # fmt: skip

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"message": "Bad credentials"})
        p = request.url.path
        repo = {"name": "app", "owner": {"login": "ops"}, "description": "the app",
                "default_branch": "main", "open_issues_count": 3, "archived": False}  # fmt: skip
        if p == "/user":
            headers = {"x-oauth-scopes": self.scopes} if self.scopes is not None else {}
            return httpx.Response(200, json={"login": "commander"}, headers=headers)
        if p == "/user/repos":
            return httpx.Response(200, json=[repo])
        if p == "/search/repositories":
            return httpx.Response(200, json={"total_count": 1, "items": [repo]})
        if p == "/search/issues":
            q = request.url.params["q"]
            items = [
                {"number": 1, "title": "Flaky test", "state": "open",
                 "repository_url": f"{ORIGIN}/repos/ops/app"},
                {"number": 2, "title": "Fix flake", "state": "open", "pull_request": {},
                 "repository_url": f"{ORIGIN}/repos/ops/app"},
            ]  # fmt: skip
            want_pr = "is:pr" in q.split()
            return httpx.Response(200, json={
                "items": [i for i in items if ("pull_request" in i) == want_pr]})  # fmt: skip
        routes = {
            "/repos/ops/app": repo,
            "/repos/ops/app/branches/main": {"commit": {"sha": SHA}},
            "/repos/ops/app/issues/1": {"number": 1, "title": "Flaky test", "state": "open",
                                        "user": {"login": "dev"}, "body": "it flakes",
                                        "comments": len(self.comments)},
            "/repos/ops/app/issues/2": {"number": 2, "title": "Fix flake", "state": "open",
                                        "user": {"login": "dev"}, "body": "fixes #1",
                                        "comments": 0, "pull_request": {}},
            "/repos/ops/app/pulls/2": {"head": {"sha": PR_SHA}, "merged": False},
            "/repos/ops/app/contents": [
                {"name": "README.md", "type": "file", "size": 12},
                {"name": "src", "type": "dir", "size": 0},
            ],
            "/repos/ops/app/contents/README.md": {
                "type": "file", "encoding": "base64", "size": 12,
                "content": base64.b64encode(b"hello world\n").decode(),
            },
        }  # fmt: skip
        if p == "/repos/ops/app/issues/1/comments":
            return httpx.Response(200, json=page_of(self.comments, "x", request)["x"])
        if p.endswith("/status"):
            return httpx.Response(200, json=page_of(self.statuses, "statuses", request))
        if p == "/repos/ops/app/actions/runs":
            assert request.url.params["head_sha"] in (SHA, PR_SHA)
            return httpx.Response(200, json=page_of(self.runs, "workflow_runs", request))
        if p.endswith("/check-runs"):
            if self.checks_status != 200:
                return httpx.Response(
                    self.checks_status, json={"message": "Resource not accessible"}
                )
            return httpx.Response(200, json=page_of(self.checks, "check_runs", request))
        if p in routes:
            return httpx.Response(200, json=routes[p])
        return httpx.Response(404, json={"message": "Not Found"})


@pytest.fixture
def gh(monkeypatch):
    f = GitHub()
    lookups: list[str] = []

    async def fake_resolve(host, port):
        lookups.append(host)
        if host != "api.github.com":
            raise OSError("no such host")
        return [HOST_IP]

    monkeypatch.setattr(connector_http, "_resolve", fake_resolve)
    monkeypatch.setattr(connector_http, "_TRANSPORT", httpx.MockTransport(f.handler))
    f.lookups = lookups  # type: ignore[attr-defined]
    return f


def _ready(token: str = TOKEN) -> dict:
    inst = store.add("github")
    store.patch(inst["id"], {"token": token})
    assert run(service.run_test(inst["id"]))["status"] == "ok"
    return store.set_capability(inst["id"], True)


def _turn_with_refs(*calls) -> tuple[hq.TurnConnectors, list[dict]]:
    turn = hq.TurnConnectors()
    out = [run(hq.list_connectors(turn))[0]]
    for fn, args in calls:
        out.append(run(fn(turn, {"connector_index": 0, **args}))[0])
    return turn, out


# ---- the manifest --------------------------------------------------------------------------------


def test_the_bundled_github_manifest_is_fixed_origin_and_reviewed_members_only():
    m = manifest.bundled()["github"]
    assert m.adapter == "github-rest" and m.adapter in kinds.ADAPTER_KINDS
    assert m.origin_mode == "fixed" and m.fixed_origin == ORIGIN
    assert m.auth_scheme == "Bearer" and set(m.scopes) <= kinds.AUTH_SCOPES
    assert "checks:read" not in m.scopes  # a fine-grained token has no Checks permission
    assert set(m.reads) == kinds.ADAPTER_READS["github-rest"]


def test_a_github_connector_starts_at_its_fixed_origin_and_refuses_another():
    inst = store.add("github")
    assert inst["origin"] == ORIGIN
    view = service.catalog()["instances"][0]
    assert view["status"] == "needs_token"
    with pytest.raises(store.Refused) as e:
        store.patch(inst["id"], {"origin": "https://evil.example", "token": TOKEN})
    assert e.value.code == "fixed_origin"


# ---- the transport -------------------------------------------------------------------------------


def test_the_github_kind_reads_only_its_paths_and_is_refused_before_the_token(gh, monkeypatch):
    inst = store.add("github")
    store.patch(inst["id"], {"token": TOKEN})
    b = service._bound(store.read()["instances"][0])
    fetched: list[str] = []
    real = connector_http._token
    monkeypatch.setattr(connector_http, "_token", lambda bound: fetched.append("x") or real(bound))
    for method, path in (
        ("POST", "/repos/ops/app/issues"),
        ("GET", "/api/v1/user"),  # Forgejo's prefix is not GitHub's
        ("GET", "/admin"),
        ("GET", "/repos/../user"),
        ("GET", "/repos//ops"),
        ("GET", "/orgs/ops/members"),
    ):
        with pytest.raises(connector_http.ConnectorError) as e:
            run(connector_http._request(b, method, path))
        assert e.value.code == "refused", path
    assert fetched == [] and gh.requests == []
    # And the Forgejo kind cannot reach GitHub's paths either.
    fb = connector_http.Bound(inst["id"], b.generation, ORIGIN, "token", "Forgejo")
    with pytest.raises(connector_http.ConnectorError):
        run(connector_http._request(fb, "GET", "/user"))


def test_github_requests_carry_its_fixed_headers_and_the_token_goes_only_to_github(gh):
    _ready()
    rq = gh.requests[0]
    assert rq.url.host == HOST_IP and rq.headers["host"] == "api.github.com"
    assert rq.headers["authorization"] == f"Bearer {TOKEN}"
    assert rq.headers["accept"] == "application/vnd.github+json"
    assert rq.headers["x-github-api-version"] == "2022-11-28"
    assert rq.headers["accept-encoding"] == "identity"
    assert all(r.method == "GET" for r in gh.requests)
    assert set(gh.lookups) == {"api.github.com"}


def test_the_kind_headers_can_never_displace_the_credential_or_the_host_pin(gh, monkeypatch):
    monkeypatch.setitem(
        connector_http.HEADERS,
        "github-rest",
        {"Authorization": "Bearer stolen", "Host": "evil.example", "Accept": "x/y"},
    )
    _ready()
    rq = gh.requests[0]
    assert rq.headers["authorization"] == f"Bearer {TOKEN}"
    assert rq.headers["host"] == "api.github.com"


@pytest.mark.parametrize(
    ("status", "headers", "code"),
    [
        (403, {"x-ratelimit-remaining": "0"}, "rate_limited"),
        (429, {}, "rate_limited"),
        (403, {}, "rejected"),
        (403, {"x-ratelimit-remaining": "4999"}, "rejected"),
        (401, {"x-ratelimit-remaining": "0"}, "rejected"),
    ],
)
def test_a_used_up_rate_limit_is_not_a_bad_token(gh, monkeypatch, status, headers, code):
    inst = store.add("github")
    store.patch(inst["id"], {"token": TOKEN})
    monkeypatch.setattr(
        connector_http,
        "_TRANSPORT",
        httpx.MockTransport(lambda r: httpx.Response(status, json={}, headers=headers)),
    )
    out = run(service.run_test(inst["id"]))
    assert out["status"] == "failed" and out["code"] == code, out


def test_only_the_named_reply_headers_are_handed_back(gh):
    inst = store.add("github")
    store.patch(inst["id"], {"token": TOKEN})
    b = service._bound(store.read()["instances"][0])
    gh.scopes = "repo"
    seen: dict = {}
    run(connector_http._request(b, "GET", "/user", headers_out=seen))
    assert seen == {"x-oauth-scopes": "repo"}


# ---- the probe -----------------------------------------------------------------------------------


def test_the_probe_names_the_account_and_warns_about_a_write_scoped_classic_token(gh):
    inst = store.add("github")
    store.patch(inst["id"], {"token": TOKEN})
    out = run(service.run_test(inst["id"]))
    assert out["status"] == "ok" and out["login"] == "commander" and out["repos"] == 1
    assert "warning" not in out  # a fine-grained token sends no scope header
    gh.scopes = "read:org, repo, workflow"
    out = run(service.run_test(inst["id"]))
    assert out["status"] == "ok"  # warned, never refused (#1746 D3)
    assert "repo, workflow" in out["warning"]
    gh.scopes = "read:org, read:user, user:email"
    assert "warning" not in run(service.run_test(inst["id"]))


def test_any_scope_that_is_not_read_only_warns_even_without_repo(gh):
    """#1831 (Hermes): the rule is an allowlist of read-only scopes, so a write scope GitHub adds,
    or one a list forgot (`admin:repo_hook`), still warns."""
    inst = store.add("github")
    store.patch(inst["id"], {"token": TOKEN})
    for scope in ("admin:repo_hook", "write:repo_hook", "gist", "notifications", "repo:status"):
        gh.scopes = f"read:org, {scope}"
        out = run(service.run_test(inst["id"]))
        assert out["status"] == "ok" and scope in out["warning"], scope


# ---- the HQ tools end to end ---------------------------------------------------------------------


def test_hq_reads_github_end_to_end(gh):
    _ready()
    turn, (listing, repos, repo) = _turn_with_refs(
        (hq.search, {"kind": "repos", "query": "app"}),
        (hq.read, {"item_ref": "repo:ops/app"}),
    )
    assert listing["connectors"] == [
        {
            "connector_index": 0,
            "label": "GitHub",
            "host": "api.github.com",
            "category": "versioning",
            "search_kinds": ["repos", "issues", "pulls"],
        }
    ]
    assert repos["untrusted_connector_content"]["items"][0]["ref"] == "repo:ops/app"
    body = repo["untrusted_connector_content"]
    assert body["head_sha"] == SHA and body["open_pulls"] is None
    assert "api.github.com" in repo["provenance"]
    ci = run(hq.read(turn, {"connector_index": 0, "item_ref": f"ci:ops/app@{SHA}"}))[0]
    assert ci["untrusted_connector_content"]["state"] in ("success", "incomplete")
    listing_dir = run(hq.list_dir(turn, {"connector_index": 0, "item_ref": f"dir:ops/app@{SHA}:"}))[
        0
    ]
    names = {e["name"] for e in listing_dir["untrusted_connector_content"]["entries"]}
    assert names == {"README.md", "src"}
    f = run(hq.read(turn, {"connector_index": 0, "item_ref": f"file:ops/app@{SHA}:README.md"}))[0]
    assert f["untrusted_connector_content"]["text"] == "hello world\n"
    paths = [r.url.path for r in gh.requests]
    assert "/repos/ops/app/contents/README.md" in paths
    assert all(r.url.params.get("ref") == SHA for r in gh.requests if "/contents" in r.url.path)


def test_the_tools_kind_decides_issue_or_pr_whatever_the_query_says(gh):
    _ready()
    turn, (_, issues, pulls) = _turn_with_refs(
        (hq.search, {"kind": "issues", "query": "flake is:pr -is:issue type:pr"}),
        (hq.search, {"kind": "pulls", "query": "flake is:issue"}),
    )
    qs = [r.url.params["q"] for r in gh.requests if r.url.path == "/search/issues"]
    assert qs == ["flake is:issue", "flake is:pr"]
    assert [i["ref"] for i in issues["untrusted_connector_content"]["items"]] == ["issue:ops/app#1"]
    assert [i["ref"] for i in pulls["untrusted_connector_content"]["items"]] == ["issue:ops/app#2"]


def test_an_issue_reads_its_newest_comments_in_one_request(gh):
    _ready()
    turn, (_, _found) = _turn_with_refs((hq.search, {"kind": "issues", "query": "flake"}))
    out = run(hq.read(turn, {"connector_index": 0, "item_ref": "issue:ops/app#1"}))[0]
    body = out["untrusted_connector_content"]
    assert body["comments_total"] == 120
    assert body["comments"][-1]["body"] == "comment 120"
    asked = [r for r in gh.requests if r.url.path.endswith("/comments")]
    assert len(asked) == 1 and asked[0].url.params["page"] == "3"


def test_an_oversized_comment_page_still_reads_the_issue_and_says_so(gh):
    """#1831 (Hermes): a newest-comments page over the transport's body cap degrades to the issue
    alone with a note, never a failed read (the Forgejo adapter's rule)."""
    _ready()
    gh.comments = [
        {"user": {"login": "dev"}, "created_at": f"c{i}", "body": "x" * 60_000} for i in range(20)
    ]
    turn, _ = _turn_with_refs((hq.search, {"kind": "issues", "query": "flake"}))
    out = run(hq.read(turn, {"connector_index": 0, "item_ref": "issue:ops/app#1"}))[0]
    body = out["untrusted_connector_content"]
    assert body["body"] == "it flakes" and body["comments"] == []
    assert body["comments_note"] == "the comment thread is too long to read here"


def test_a_pr_mints_ci_and_dir_refs_at_its_head(gh):
    _ready()
    turn, (_, _found) = _turn_with_refs((hq.search, {"kind": "pulls", "query": "flake"}))
    out = run(hq.read(turn, {"connector_index": 0, "item_ref": "issue:ops/app#2"}))[0]
    body = out["untrusted_connector_content"]
    assert body["is_pull"] and body["head_sha"] == PR_SHA
    assert f"ci:ops/app@{PR_SHA}" in body["refs"]


def test_a_credential_shaped_issue_is_withheld_whole(gh):
    _ready()
    gh.comments = [{"user": {"login": "dev"}, "created_at": "now", "body": f"use {CRED}"}]
    turn, _ = _turn_with_refs((hq.search, {"kind": "issues", "query": "flake"}))
    out = run(hq.read(turn, {"connector_index": 0, "item_ref": "issue:ops/app#1"}))[0]
    assert CRED not in json.dumps(out)


# ---- CI from every source ------------------------------------------------------------------------


def _ci(gh) -> dict:
    b = service._bound(store.read()["instances"][0])
    return run(github.read_ci(b, "ops", "app", SHA))


def test_ci_keeps_a_green_status_and_a_failed_check_with_the_same_name_apart(gh):
    _ready()
    gh.statuses = [{"context": "build", "state": "success"}]
    gh.runs = []
    gh.checks = [{"name": "build", "status": "completed", "conclusion": "failure",
                  "app": {"slug": "circleci"}}]  # fmt: skip
    ci = _ci(gh)
    assert ci["state"] == "failure"
    assert {c["context"] for c in ci["checks"]} == {"status · build", "check · circleci · build"}


def test_an_actions_only_commit_reads_its_runs(gh):
    _ready()
    gh.statuses = []
    gh.runs = [
        {
            "name": "tests",
            "status": "completed",
            "conclusion": "failure",
            "path": ".github/workflows/tests.yml",
            "workflow_id": 11,
        },
        {
            "name": "lint",
            "status": "in_progress",
            "conclusion": None,
            "path": ".github/workflows/lint.yml",
            "workflow_id": 12,
        },
    ]
    ci = _ci(gh)
    assert ci["state"] == "failure" and ci["checks_total"] == 2
    assert ci["checks"][0] == {"context": "actions · tests · tests.yml", "state": "failure"}


def test_two_workflows_with_one_name_are_two_entries_and_a_failure_shows(gh):
    """#1831 (Hermes): runs are keyed by WORKFLOW, not display name. Two files both named "CI":
    the newer one's success must not hide the other's failure."""
    _ready()
    gh.statuses = []
    gh.runs = [
        {"name": "CI", "status": "completed", "conclusion": "success",
         "path": ".github/workflows/ci.yml", "workflow_id": 21},
        {"name": "CI", "status": "completed", "conclusion": "failure",
         "path": ".github/workflows/ci-nightly.yml", "workflow_id": 22},
        {"name": "CI", "status": "completed", "conclusion": "success",
         "path": ".github/workflows/ci-nightly.yml", "workflow_id": 22},  # older run, same workflow
    ]  # fmt: skip
    ci = _ci(gh)
    assert ci["state"] == "failure"
    assert {c["context"]: c["state"] for c in ci["checks"]} == {
        "actions · CI · ci-nightly.yml": "failure",
        "actions · CI · ci.yml": "success",
    }


def test_actions_check_runs_are_not_counted_twice(gh):
    _ready()
    gh.checks = [
        {"name": "tests (3.12)", "status": "completed", "conclusion": "success",
         "app": {"slug": "github-actions"}},
    ]  # fmt: skip
    ci = _ci(gh)
    assert ci["state"] == "success"
    assert {c["context"] for c in ci["checks"]} == {
        "status · ci/lint",
        "actions · tests · tests.yml",
    }


@pytest.mark.parametrize("refused", [403, 404])
def test_check_runs_a_token_cannot_see_never_read_as_green(gh, refused):
    _ready()
    gh.checks_status = refused
    ci = _ci(gh)
    assert ci["state"] == "incomplete" and "not visible" in ci["note"]
    gh.runs = [
        {"name": "tests", "status": "completed", "conclusion": "failure",
         "path": ".github/workflows/tests.yml", "workflow_id": 11}
    ]  # fmt: skip
    assert _ci(gh)["state"] == "failure"  # a failure seen stays a failure


def test_a_rate_limited_check_runs_read_is_an_error_not_an_unseen_source(gh, monkeypatch):
    """#1826 (Hermes): only a permission refusal makes check runs "not visible". A used-up rate
    limit is an outage of the whole read, reported as one, never quietly downgraded."""
    _ready()
    real = gh.handler

    def limited(request):
        if request.url.path.endswith("/check-runs"):
            gh.requests.append(request)
            return httpx.Response(403, json={}, headers={"x-ratelimit-remaining": "0"})
        return real(request)

    monkeypatch.setattr(connector_http, "_TRANSPORT", httpx.MockTransport(limited))
    with pytest.raises(connector_http.ConnectorError) as e:
        _ci(gh)
    assert e.value.code == "rate_limited"


def test_a_capped_ci_walk_is_never_green(gh, monkeypatch):
    _ready()
    monkeypatch.setattr(github, "CI_PAGES_MAX", 1)
    monkeypatch.setattr(github, "PAGE_MAX", 2)
    gh.statuses = [{"context": f"s{i}", "state": "success"} for i in range(5)]
    ci = _ci(gh)
    assert ci["state"] == "incomplete" and ci["checks_total_capped"] is True


def test_hq_says_which_ci_sources_it_could_not_see(gh):
    _ready()
    gh.checks_status = 403
    turn, _ = _turn_with_refs(
        (hq.search, {"kind": "repos", "query": "app"}), (hq.read, {"item_ref": "repo:ops/app"})
    )
    out = run(hq.read(turn, {"connector_index": 0, "item_ref": f"ci:ops/app@{SHA}"}))[0]
    body = out["untrusted_connector_content"]
    assert body["state"] == "incomplete" and "not visible" in body["sources_note"]


# ---- the #1750 guarantees ------------------------------------------------------------------------


def test_the_github_token_is_held_by_nothing_but_the_request_to_github(gh):
    _ready()
    turn, outs = _turn_with_refs(
        (hq.search, {"kind": "repos", "query": "app"}),
        (hq.read, {"item_ref": "repo:ops/app"}),
    )
    assert TOKEN not in json.dumps(outs)
    assert TOKEN not in json.dumps(service.catalog())
    assert TOKEN.encode() not in store.store_path().read_bytes()
    assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in gh.requests)
    assert {r.headers["host"] for r in gh.requests} == {"api.github.com"}


def test_a_rebinding_github_name_connects_only_to_the_vetted_address(gh, monkeypatch):
    answers = iter([[HOST_IP], ["169.254.169.254"], ["169.254.169.254"]])
    lookups: list[str] = []

    async def rebinding(host, port):
        lookups.append(host)
        return next(answers)

    inst = store.add("github")
    store.patch(inst["id"], {"token": TOKEN})
    monkeypatch.setattr(connector_http, "_resolve", rebinding)
    b = service._bound(store.read()["instances"][0])
    run(connector_http._request(b, "GET", "/user"))
    rq = gh.requests[-1]
    assert rq.url.host == HOST_IP and rq.headers["host"] == "api.github.com"
    assert rq.extensions.get("sni_hostname") == "api.github.com"
    assert lookups == ["api.github.com"]
