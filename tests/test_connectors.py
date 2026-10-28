"""HQ connectors, P1a (#1750): the read-only Forgejo connector and its boundaries.

The acceptance tests of #1750, by number:

1. a token never leaks — route responses, the model request bodies of an HQ turn that used the
   connector, the HQ store, captured logs, and a launched agent's environment and argv;
2. an origin move needs a new token, refused INSIDE the lock;
3. destination vetting — link-local / metadata refused, DNS rebinding pinned, no redirects;
4. credential-shaped content is withheld whole, a registered secret is redacted;
5. generation vs credential revision; 5a a stale probe records nothing; 5b TLS for tokens;
   5c the advertised reads end to end, and forged refs refused;
6. refs come from this turn; 7. disable applies at once; 8. an unreadable store refuses and is
   never rewritten.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_sessions import ask_store, connector_http, prefs, review, template_secrets
from agent_sessions.connectors import hq, kinds, manifest, service, store
from agent_sessions.connectors import secrets as creds
from agent_sessions.connectors.adapters import forgejo
from agent_sessions.main import create_app
from test_ask_orchestrator import App, Script, answer, call

TOKEN = "fjtok-SENTINEL-3b9c1e77d04a5f6182"
TOKEN2 = "fjtok-SECOND-0aa1bb2cc3dd4ee5ff6"
ORIGIN = "https://git.example"
HOST_IP = "10.0.0.5"
SHA = "a" * 40
PR_SHA = "b" * 40
CRED = "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
FIXTURES = Path(__file__).parent / "fixtures" / "connectors"


# ---- a fake Forgejo ------------------------------------------------------------------------------


class Forge:
    """A Forgejo API: one repo ``ops/app`` with an issue #1 and a PR #2, a README and src/."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.token = TOKEN
        self.comment = "looks good, ship it"
        self.on_request = None  # a hook a test can use to interleave work

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.on_request is not None:
            self.on_request(request)
        if request.headers.get("authorization") != f"token {self.token}":
            return httpx.Response(401, json={"message": "unauthorized"})
        p, q = request.url.path, request.url.params
        repo = {
            "name": "app",
            "owner": {"login": "ops"},
            "full_name": "ops/app",
            "description": "the app",
            "default_branch": "main",
            "open_issues_count": 1,
            "open_pr_counter": 1,
        }
        routes = {
            "/api/v1/user": {"login": "commander"},
            "/api/v1/repos/search": {"ok": True, "data": [repo]},
            "/api/v1/repos/ops/app": repo,
            "/api/v1/repos/ops/app/branches/main": {"name": "main", "commit": {"id": SHA}},
            "/api/v1/repos/ops/app/issues/1": {
                "number": 1,
                "title": "Flaky test",
                "state": "open",
                "body": "it flakes",
                "user": {"login": "dev"},
                "pull_request": None,
            },
            "/api/v1/repos/ops/app/issues/1/comments": [
                {
                    "user": {"login": "hermes"},
                    "created_at": "2026-10-28T10:00:00Z",
                    "body": self.comment,
                },
            ],
            "/api/v1/repos/ops/app/issues/2": {
                "number": 2,
                "title": "Fix flake",
                "state": "open",
                "body": "fixes #1",
                "user": {"login": "dev"},
                "pull_request": {"merged": False},
            },
            "/api/v1/repos/ops/app/issues/2/comments": [],
            "/api/v1/repos/ops/app/pulls/2": {"head": {"sha": PR_SHA}, "merged": False},
            f"/api/v1/repos/ops/app/commits/{SHA}/status": {
                "state": "success",
                "statuses": [{"context": "ci/test", "status": "success"}],
            },
            f"/api/v1/repos/ops/app/commits/{PR_SHA}/status": {
                "state": "failure",
                "statuses": [{"context": "ci/test", "status": "failure"}],
            },
            "/api/v1/repos/ops/app/contents": [
                {"name": "README.md", "type": "file", "path": "README.md", "size": 12},
                {"name": "src", "type": "dir", "path": "src", "size": 0},
            ],
            "/api/v1/repos/ops/app/contents/README.md": {
                "type": "file",
                "encoding": "base64",
                "size": 12,
                "content": base64.b64encode(b"hello world\n").decode(),
            },
            "/api/v1/repos/ops/app/contents/src": [
                {"name": "main.py", "type": "file", "path": "src/main.py", "size": 9},
            ],
            "/api/v1/repos/ops/app/contents/src/main.py": {
                "type": "file",
                "encoding": "base64",
                "size": 9,
                "content": base64.b64encode(b"print(1)\n").decode(),
            },
        }
        if p == "/api/v1/repos/issues/search":
            kind = q.get("type")
            rows = [
                {
                    "number": 1,
                    "title": "Flaky test",
                    "state": "open",
                    "pull_request": None,
                    "repository": {"owner": "ops", "name": "app"},
                },
                {
                    "number": 2,
                    "title": "Fix flake",
                    "state": "open",
                    "pull_request": {"merged": False},
                    "repository": {"owner": "ops", "name": "app"},
                },
            ]
            want_pull = kind == "pulls"
            return httpx.Response(
                200, json=[r for r in rows if (r["pull_request"] is not None) == want_pull]
            )
        if p.startswith("/api/v1/repos/ops/app/contents") and q.get("ref") not in (SHA, PR_SHA):
            return httpx.Response(404, json={"message": "no such ref"})
        if p in routes:
            return httpx.Response(200, json=routes[p])
        return httpx.Response(404, json={"message": "not found"})


@pytest.fixture
def forge(monkeypatch):
    f = Forge()
    resolved: dict[str, list[str]] = {"git.example": [HOST_IP]}

    async def fake_resolve(host, port):
        if host not in resolved:
            raise OSError("no such host")
        return resolved[host]

    monkeypatch.setattr(connector_http, "_resolve", fake_resolve)
    monkeypatch.setattr(connector_http, "_TRANSPORT", httpx.MockTransport(f.handler))
    f.resolved = resolved  # type: ignore[attr-defined]
    return f


def _ready(origin: str = ORIGIN, token: str = TOKEN) -> dict:
    """An instance through the store API: added, configured, tested, enabled."""
    inst = store.add("forgejo")
    store.patch(inst["id"], {"origin": origin, "token": token})
    result = asyncio.run(service.run_test(inst["id"]))
    assert result["status"] == "ok", result
    return store.set_capability(inst["id"], True)


def _turn() -> hq.TurnConnectors:
    return hq.TurnConnectors()


def run(coro):
    return asyncio.run(coro)


# ---- the manifest ------------------------------------------------------------------------------


def test_the_bundled_manifest_chooses_only_reviewed_members():
    m = manifest.bundled()["forgejo"]
    assert m.adapter in kinds.ADAPTER_KINDS and m.auth_kind in kinds.AUTH_KINDS
    assert set(m.reads) <= kinds.READ_OPS and m.origin_mode in kinds.ORIGIN_MODES
    assert m.category in kinds.CATEGORIES and m.publisher in kinds.PUBLISHERS


@pytest.mark.deploy_shape
def test_the_installed_package_ships_the_bundled_manifest():
    from importlib import resources

    data = resources.files("agent_sessions.connectors").joinpath("first_party/forgejo.toml")
    assert manifest.parse(data.read_bytes()).id == "forgejo"


def _vectors() -> list[Path]:
    return sorted(FIXTURES.glob("*.toml"))


def test_there_are_refusal_vectors():
    assert len(_vectors()) >= 10


@pytest.mark.parametrize("path", _vectors(), ids=lambda p: p.stem)
def test_each_refusal_vector_is_refused_for_its_reason(path):
    text = path.read_text()
    reason = text.splitlines()[0].removeprefix("# refuse:").strip()
    with pytest.raises(manifest.ManifestError) as e:
        manifest.parse(text.encode())
    assert reason in str(e.value)


# ---- 1. a token never leaks --------------------------------------------------------------------


def test_1_a_token_never_leaks_anywhere(auth_cfg, forge, monkeypatch, caplog, tmp_path):
    caplog.set_level(logging.DEBUG)
    prefs.set_ai_review(
        {
            "egress_acknowledged": True,
            "enabled": True,
            "base_url": "https://ai.test/v1",
            "api_key": "sk-ask-test",
            "model": "m",
        }
    )
    app = App(auth_cfg)
    c, hdr = app.c, app.hdr
    seen: list[str] = []

    def keep(r):
        seen.append(r.text)
        return r

    inst = keep(c.post("/api/connectors/instances", json={"manifest_id": "forgejo"}, headers=hdr))
    assert inst.status_code == 201
    iid = inst.json()["id"]
    r = keep(
        c.patch(
            f"/api/connectors/instances/{iid}", json={"origin": ORIGIN, "token": TOKEN}, headers=hdr
        )
    )
    assert r.status_code == 200 and r.json()["token_set"] is True
    r = keep(c.post(f"/api/connectors/instances/{iid}/test", json={}, headers=hdr))
    assert r.json()["status"] == "ok", r.text
    r = keep(
        c.post(f"/api/connectors/instances/{iid}/capabilities", json={"read": True}, headers=hdr)
    )
    assert r.json()["enabled"]["read"] is True
    listing = keep(c.get("/api/connectors"))
    assert listing.headers["cache-control"] == "no-store"
    assert "disclosure" in listing.json() and "ai.test" in listing.json()["disclosure"]
    # An HQ turn that reads through the connector.
    script = Script(
        call("list_connectors", {}),
        call("connector_search", {"connector_index": 0, "query": "app", "kind": "repos"}),
        call("connector_read", {"connector_index": 0, "item_ref": "repo:ops/app"}),
        answer("ops/app is at aaaa."),
    )
    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(script.handler))
    turn = app.ask(app.new(), "what is on ops/app?").json()["turn"]
    assert turn["status"] == "done", turn
    assert any("untrusted_connector_content" in json.dumps(b) for b in script.bodies)
    # The forge saw the token — the only place it may go.
    assert any(rq.headers.get("authorization") == f"token {TOKEN}" for rq in forge.requests)
    # Every other surface holds no trace of it.
    assert all(TOKEN not in s for s in seen)
    assert TOKEN not in json.dumps(script.bodies)
    on_disk = b"".join(p.read_bytes() for p in ask_store.root().rglob("*") if p.is_file())
    assert TOKEN.encode() not in on_disk
    assert TOKEN.encode() not in store.store_path().read_bytes()  # only its envelope is stored
    assert TOKEN not in caplog.text
    assert all(TOKEN not in v for v in os.environ.values())
    # A launched agent: its argv and environment.
    from agent_sessions import sessionlock, webterm

    lock = sessionlock.acquire("claude:connector-leak")
    captured: dict = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"], captured["env"] = argv, kwargs.get("env") or {}
        raise OSError("stop before pumping")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    class FakeWS:
        async def close(self, code=None):
            pass

    asyncio.run(webterm.run(FakeWS(), ["dtach"], cwd=str(tmp_path), lock=lock))
    lock.transfer()
    assert captured["argv"]
    assert all(TOKEN not in a for a in captured["argv"])
    assert all(TOKEN not in v for v in captured["env"].values())


def test_1_the_token_is_in_the_redaction_set_and_retired_on_removal(forge):
    inst = _ready()
    assert TOKEN in template_secrets.redaction_values()
    assert template_secrets.redact_text(f"leaked {TOKEN}") == f"leaked {template_secrets.REDACTED}"
    # Cold: the set was never computed in this process, so only the store's own retire keeps it.
    template_secrets._reset_for_tests()
    store.remove(inst["id"])
    # Removed from the store, still redacted for its TTL (it may sit in transcripts).
    assert TOKEN in template_secrets.redaction_values()


def test_1_an_unreadable_connector_store_makes_redaction_fail_closed():
    store.store_path().parent.mkdir(parents=True, exist_ok=True)
    store.store_path().write_text("{not json")
    with pytest.raises(template_secrets.RedactionUnavailable):
        template_secrets.redaction_values()


# ---- 2. an origin move needs a new token -------------------------------------------------------


def test_2_an_origin_change_without_a_token_is_refused():
    inst = store.add("forgejo")
    store.patch(inst["id"], {"origin": ORIGIN, "token": TOKEN})
    for body in (
        {"origin": "https://evil.example"},
        {"origin": "https://evil.example", "token": ""},
        {"origin": "https://evil.example", "token": creds.MASK},
    ):
        with pytest.raises(store.Refused) as e:
            store.patch(inst["id"], body)
        assert e.value.code == "origin_needs_token"
    assert store.read()["instances"][0]["origin"] == ORIGIN
    # With a new token, or clearing it, the move is allowed.
    moved = store.patch(inst["id"], {"origin": "https://other.example", "token": TOKEN2})
    assert moved["origin"] == "https://other.example"
    cleared = store.patch(inst["id"], {"origin": "https://third.example", "token": None})
    assert cleared["envelopes"] == {}


def test_2_the_check_runs_inside_the_lock_against_the_stored_instance(monkeypatch):
    """A save that interleaves after any pre-check: instance holds NO token when the origin-only
    patch is validated, then a token lands before it is applied. The in-lock check sees the token
    and refuses, so the old token is never paired with the new origin."""
    inst = store.add("forgejo")
    store.patch(inst["id"], {"origin": ORIGIN})
    real_locked = store._locked
    fired = {"done": False}

    import contextlib

    @contextlib.contextmanager
    def racing():
        if not fired["done"]:
            fired["done"] = True
            # The racing save commits first: a token for the CURRENT origin.
            with real_locked() as (path, doc):
                pass
            store.patch(inst["id"], {"token": TOKEN})
        with real_locked() as pair:
            yield pair

    monkeypatch.setattr(store, "_locked", racing)
    with pytest.raises(store.Refused) as e:
        store.patch(inst["id"], {"origin": "https://evil.example"})
    assert e.value.code == "origin_needs_token"
    after = store.read()["instances"][0]
    assert after["origin"] == ORIGIN and "token" in after["envelopes"]


# ---- 3. destination vetting --------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin", ["https://169.254.169.254", "https://[fe80::1]", "https://[fd00:ec2::254]"]
)
def test_3_a_link_local_or_metadata_origin_is_refused_at_save(origin):
    inst = store.add("forgejo")
    with pytest.raises(store.Refused) as e:
        store.patch(inst["id"], {"origin": origin, "token": TOKEN})
    assert e.value.code == "refused_destination"


def test_3_a_name_resolving_to_metadata_is_refused_before_any_request(forge):
    inst = store.add("forgejo")
    store.patch(inst["id"], {"origin": "https://meta.example", "token": TOKEN})
    forge.resolved["meta.example"] = ["169.254.169.254"]
    out = run(service.run_test(inst["id"]))
    assert out["status"] == "failed" and out["code"] == "refused_destination"
    assert forge.requests == []


def test_3_a_rebinding_name_connects_only_to_the_vetted_address(forge, monkeypatch):
    answers = iter([[HOST_IP], ["169.254.169.254"], ["169.254.169.254"]])
    lookups: list[str] = []

    async def rebinding(host, port):
        lookups.append(host)
        return next(answers)

    inst = store.add("forgejo")
    store.patch(inst["id"], {"origin": ORIGIN, "token": TOKEN})
    monkeypatch.setattr(connector_http, "_resolve", rebinding)
    b = service._bound(store.read()["instances"][0])
    run(connector_http._request(b, "GET", "/api/v1/user"))
    rq = forge.requests[-1]
    assert rq.url.host == HOST_IP  # connected to the vetted address, never by name again
    assert rq.headers["host"] == "git.example"
    assert rq.extensions.get("sni_hostname") == "git.example"
    assert lookups == ["git.example"]


def test_3_a_redirect_is_not_followed(forge, monkeypatch):
    def redirect(request):
        forge.requests.append(request)
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})

    monkeypatch.setattr(connector_http, "_TRANSPORT", httpx.MockTransport(redirect))
    inst = store.add("forgejo")
    store.patch(inst["id"], {"origin": ORIGIN, "token": TOKEN})
    out = run(service.run_test(inst["id"]))
    assert out["status"] == "failed" and out["code"] == "redirect"
    assert len(forge.requests) == 1


def test_3_the_transport_only_reads_api_paths(forge):
    inst = store.add("forgejo")
    store.patch(inst["id"], {"origin": ORIGIN, "token": TOKEN})
    b = service._bound(store.read()["instances"][0])
    for method, path in (
        ("POST", "/api/v1/user"),
        ("GET", "/admin"),
        ("GET", "/api/v1/../admin"),
        ("GET", "/api/v1//x"),
    ):
        with pytest.raises(connector_http.ConnectorError) as e:
            run(connector_http._request(b, method, path))
        assert e.value.code == "refused"
    assert forge.requests == []


# ---- 4. credential-shaped content --------------------------------------------------------------


def test_4_a_credential_shaped_comment_is_withheld_and_a_secret_redacted(forge):
    _ready()
    planted = "hunter2-PLANTED-template-value-9f3k"
    template_secrets.register_typed([planted])
    forge.comment = f"here you go: {CRED}"
    t = _turn()
    run(hq.list_connectors(t))
    run(hq.search(t, {"connector_index": 0, "query": "flaky", "kind": "issues"}))
    out, _ = run(hq.read(t, {"connector_index": 0, "item_ref": "issue:ops/app#1"}))
    comment = out["untrusted_connector_content"]["comments"][0]["body"]
    assert comment == ask_store.WITHHELD
    forge.comment = f"the build printed {planted} in its summary"
    out, _ = run(hq.read(t, {"connector_index": 0, "item_ref": "issue:ops/app#1"}))
    comment = out["untrusted_connector_content"]["comments"][0]["body"]
    assert planted not in comment and template_secrets.REDACTED in comment


def test_4_results_are_labelled_as_untrusted_data(forge):
    _ready()
    t = _turn()
    run(hq.list_connectors(t))
    out, _ = run(hq.search(t, {"connector_index": 0, "query": "app", "kind": "repos"}))
    assert "untrusted content from Forgejo at git.example" in out["provenance"]


# ---- 5. generation vs credential revision ------------------------------------------------------


def test_5_a_manifest_bump_needs_only_a_retest(forge, monkeypatch):
    inst = _ready()
    real = manifest.bundled()
    bumped = {"forgejo": real["forgejo"].__class__(**{**real["forgejo"].__dict__, "version": "2"})}
    monkeypatch.setattr(store, "_bundled", lambda: bumped)
    monkeypatch.setattr(manifest, "bundled", lambda: bumped)
    cur = store.read()["instances"][0]
    assert not store.readable(cur)  # not tested at this manifest version: HQ cannot read
    with pytest.raises(store.Refused) as e:
        store.set_capability(inst["id"], True)
    assert e.value.code == "needs_test"
    # The re-test commits the bump (generation + 1, tested cleared) and tests the new generation.
    assert run(service.run_test(inst["id"]))["status"] == "ok"
    after = store.read()["instances"][0]
    assert after["generation"] == cur["generation"] + 1
    assert after["credential_revision"] == cur["credential_revision"]
    assert after["manifest_version"] == "2" and after["tested"]["generation"] == after["generation"]
    # The token still opened (no re-entry), and read is back without enabling again.
    assert store.token_for(inst["id"], after["generation"])[1] == TOKEN
    assert store.readable(after)


def test_5_an_origin_change_needs_reentry_and_the_old_envelope_stays_shut():
    inst = store.add("forgejo")
    store.patch(inst["id"], {"origin": ORIGIN, "token": TOKEN})
    before = store.read()["instances"][0]
    old_env = before["envelopes"]["token"]
    store.patch(inst["id"], {"origin": "https://other.example", "token": TOKEN2})
    after = store.read()["instances"][0]
    assert after["credential_revision"] == before["credential_revision"] + 1
    assert after["generation"] == before["generation"] + 1 and after["tested"] is None
    assert creds.open_(inst["id"], after["credential_revision"], old_env) is None
    assert (
        creds.open_(inst["id"], after["credential_revision"], after["envelopes"]["token"]) == TOKEN2
    )
    # An envelope copied onto another instance does not open there either.
    other = store.add("forgejo")
    assert (
        creds.open_(other["id"], after["credential_revision"], after["envelopes"]["token"]) is None
    )


# ---- 5a. a stale probe -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change", [{"token": TOKEN2}, {"origin": "https://other.example", "token": TOKEN2}]
)
def test_5a_a_probe_that_settles_after_a_change_records_nothing(forge, change):
    inst = store.add("forgejo")
    store.patch(inst["id"], {"origin": ORIGIN, "token": TOKEN})
    forge.resolved["other.example"] = [HOST_IP]
    injected = {"done": False}

    def between(request):
        # The PATCH lands between the probe's request and its settlement.
        if not injected["done"]:
            injected["done"] = True
            store.patch(inst["id"], change)

    forge.on_request = between
    out = run(service.run_test(inst["id"]))
    assert out["status"] == "stale"
    cur = store.read()["instances"][0]
    assert cur["tested"] is None and cur["last_test"] is None
    with pytest.raises(store.Refused) as e:
        store.set_capability(inst["id"], True)
    assert e.value.code == "needs_test"


# ---- 5b. TLS for tokens ------------------------------------------------------------------------


def test_5b_plaintext_is_refused_for_a_lan_or_public_host_and_allowed_for_loopback(forge):
    inst = store.add("forgejo")
    for origin in ("http://git.example", "http://172.16.1.10", "http://10.0.0.5:3000"):
        with pytest.raises(store.Refused) as e:
            store.patch(inst["id"], {"origin": origin, "token": TOKEN})
        assert e.value.code == "https_required"
    ok = store.patch(inst["id"], {"origin": "http://127.0.0.1:3000", "token": TOKEN})
    assert ok["origin"] == "http://127.0.0.1:3000"


def test_5b_a_loopback_name_resolving_elsewhere_is_refused_at_request_time(forge):
    inst = store.add("forgejo")
    store.patch(inst["id"], {"origin": "http://localhost:3000", "token": TOKEN})
    forge.resolved["localhost"] = ["172.16.1.10"]
    out = run(service.run_test(inst["id"]))
    assert out["status"] == "failed" and out["code"] == "https_required"
    assert forge.requests == []
    forge.resolved["localhost"] = ["127.0.0.1"]
    assert run(service.run_test(inst["id"]))["status"] == "ok"


# ---- 5c. the advertised reads, end to end; forged refs -----------------------------------------


def test_5c_search_repo_ci_dir_file_and_pr_head_ci(forge):
    _ready()
    t = _turn()
    listed, _ = run(hq.list_connectors(t))
    assert listed == {
        "connectors": [{"connector_index": 0, "label": "Forgejo", "host": "git.example"}]
    }
    out, _ = run(hq.search(t, {"connector_index": 0, "query": "app", "kind": "repos"}))
    assert out["untrusted_connector_content"]["items"][0]["ref"] == "repo:ops/app"
    repo, _ = run(hq.read(t, {"connector_index": 0, "item_ref": "repo:ops/app"}))
    refs = repo["untrusted_connector_content"]["refs"]
    assert refs == [f"ci:ops/app@{SHA}", f"dir:ops/app@{SHA}:"]
    ci, _ = run(hq.read(t, {"connector_index": 0, "item_ref": refs[0]}))
    assert ci["untrusted_connector_content"]["state"] == "success"
    root, _ = run(hq.list_dir(t, {"connector_index": 0, "item_ref": refs[1]}))
    entries = {e["name"]: e["ref"] for e in root["untrusted_connector_content"]["entries"]}
    assert entries == {
        "README.md": f"file:ops/app@{SHA}:README.md",
        "src": f"dir:ops/app@{SHA}:src/",
    }
    sub, _ = run(hq.list_dir(t, {"connector_index": 0, "item_ref": entries["src"]}))
    main_py = sub["untrusted_connector_content"]["entries"][0]["ref"]
    assert main_py == f"file:ops/app@{SHA}:src/main.py"
    f, meta = run(hq.read(t, {"connector_index": 0, "item_ref": main_py}))
    assert f["untrusted_connector_content"]["text"] == "print(1)\n" and meta["length"] == 9
    readme, _ = run(hq.read(t, {"connector_index": 0, "item_ref": entries["README.md"]}))
    assert readme["untrusted_connector_content"]["text"] == "hello world\n"
    # search → a PR → its head CI
    prs, _ = run(hq.search(t, {"connector_index": 0, "query": "flake", "kind": "pulls"}))
    pr_ref = prs["untrusted_connector_content"]["items"][0]["ref"]
    assert pr_ref == "issue:ops/app#2"
    pr, _ = run(hq.read(t, {"connector_index": 0, "item_ref": pr_ref}))
    body = pr["untrusted_connector_content"]
    assert body["is_pull"] is True and body["refs"][0] == f"ci:ops/app@{PR_SHA}"
    head_ci, _ = run(hq.read(t, {"connector_index": 0, "item_ref": body["refs"][0]}))
    assert head_ci["untrusted_connector_content"]["state"] == "failure"
    # An issue mints nothing.
    run(hq.search(t, {"connector_index": 0, "query": "flaky", "kind": "issues"}))
    issue, _ = run(hq.read(t, {"connector_index": 0, "item_ref": "issue:ops/app#1"}))
    assert "refs" not in issue["untrusted_connector_content"]


def test_5c_forged_refs_are_refused_and_nothing_is_read(forge):
    _ready()
    earlier = _turn()
    run(hq.list_connectors(earlier))
    run(hq.search(earlier, {"connector_index": 0, "query": "app", "kind": "repos"}))
    run(hq.read(earlier, {"connector_index": 0, "item_ref": "repo:ops/app"}))
    t = _turn()
    run(hq.list_connectors(t))
    forged = [
        ({"connector_index": 0, "item_ref": "repo:ops/app"}, "unknown_ref"),  # an earlier turn's
        ({"connector_index": 0, "item_ref": "file:ops/app@main:README.md"}, "unknown_ref"),
        ({"connector_index": 1, "item_ref": "repo:ops/app"}, "unknown_connector"),
        ({"connector_index": 0, "item_ref": f"file:ops/app@{SHA}:../etc/passwd"}, "unknown_ref"),
        ({"connector_index": 0, "item_ref": f"file:ops/app@{SHA}:/etc/passwd"}, "unknown_ref"),
        ({"connector_index": 0, "item_ref": f"dir:ops/app@{SHA}:src/../.."}, "unknown_ref"),
    ]
    before = len(forge.requests)
    for args, code in forged:
        fn = hq.list_dir if args["item_ref"].startswith("dir:") else hq.read
        with pytest.raises(hq.Refused) as e:
            run(fn(t, args))
        assert e.value.code == code, args
    assert len(forge.requests) == before
    # Even a traversal path a hostile forge returned is never minted as a ref.
    assert not forgejo.valid_path("../x") and not forgejo.valid_path("/x")
    assert not forgejo.valid_path("a//b") and forgejo.valid_path("a/b.txt")


def test_5c_a_ref_never_pins_anything_but_a_commit_sha(forge, monkeypatch):
    """A forge that answers a branch head with a branch name instead of a sha mints no ref."""
    _ready()
    real = forge.handler

    def branchy(request):
        if request.url.path.endswith("/branches/main"):
            forge.requests.append(request)
            return httpx.Response(200, json={"name": "main", "commit": {"id": "main"}})
        return real(request)

    monkeypatch.setattr(connector_http, "_TRANSPORT", httpx.MockTransport(branchy))
    t = _turn()
    run(hq.list_connectors(t))
    run(hq.search(t, {"connector_index": 0, "query": "app", "kind": "repos"}))
    with pytest.raises(hq.Refused) as e:
        run(hq.read(t, {"connector_index": 0, "item_ref": "repo:ops/app"}))
    assert e.value.code == "connector_bad_reply"
    assert not any(r.startswith(("ci:", "dir:", "file:")) for r in t.refs[0])


# ---- 6. refs come from this turn ---------------------------------------------------------------


def test_6_an_unlisted_index_or_ref_makes_no_request(forge):
    _ready()
    t = _turn()
    before = len(forge.requests)
    with pytest.raises(hq.Refused) as e:
        run(hq.search(t, {"connector_index": 0, "query": "x", "kind": "repos"}))
    assert e.value.code == "unknown_connector"  # list_connectors was not called this turn
    run(hq.list_connectors(t))
    with pytest.raises(hq.Refused) as e:
        run(hq.read(t, {"connector_index": 0, "item_ref": "issue:ops/app#1"}))
    assert e.value.code == "unknown_ref"
    assert len(forge.requests) == before


# ---- 7. disable applies at once ----------------------------------------------------------------


def test_7_disabling_between_two_calls_makes_the_second_a_tool_error(forge):
    inst = _ready()
    t = _turn()
    run(hq.list_connectors(t))
    run(hq.search(t, {"connector_index": 0, "query": "app", "kind": "repos"}))
    store.set_capability(inst["id"], False)
    before = len(forge.requests)
    with pytest.raises(hq.Refused) as e:
        run(hq.read(t, {"connector_index": 0, "item_ref": "repo:ops/app"}))
    assert e.value.code == "connector_disabled"
    assert len(forge.requests) == before


def test_7_a_changed_connector_is_a_tool_error_too(forge):
    inst = _ready()
    t = _turn()
    run(hq.list_connectors(t))
    run(hq.search(t, {"connector_index": 0, "query": "app", "kind": "repos"}))
    store.patch(inst["id"], {"token": TOKEN2})
    forge.token = TOKEN2
    run(service.run_test(inst["id"]))
    with pytest.raises(hq.Refused) as e:
        run(hq.read(t, {"connector_index": 0, "item_ref": "repo:ops/app"}))
    assert e.value.code == "connector_changed"


def test_7_the_hq_tool_reports_a_disabled_connector_to_the_model(auth_cfg, forge, monkeypatch):
    inst = _ready()
    prefs.set_ai_review(
        {
            "egress_acknowledged": True,
            "enabled": True,
            "base_url": "https://ai.test/v1",
            "api_key": "sk-ask-test",
            "model": "m",
        }
    )
    app = App(auth_cfg)
    script = Script(
        call("list_connectors", {}),
        call("connector_search", {"connector_index": 0, "query": "app", "kind": "repos"}),
        answer("done"),
    )
    real = hq.search

    async def disable_first(turn, args):
        store.set_capability(inst["id"], False)
        return await real(turn, args)

    monkeypatch.setattr(hq, "search", disable_first)
    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(script.handler))
    assert app.ask(app.new(), "repos?").json()["turn"]["status"] == "done"
    results = script.tool_results(2)
    assert "not enabled for reading" in results[-1]["content"]


# ---- 8. an unreadable store --------------------------------------------------------------------


def test_8_an_unreadable_store_refuses_every_route_and_tool_and_is_never_rewritten(auth_cfg):
    path = store.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    damaged = b'{"version": 1, "instances": [{"id": "nope"}], "audit": []}'
    path.write_bytes(damaged)
    app = App(auth_cfg)
    c, hdr = app.c, app.hdr
    responses = [
        c.get("/api/connectors"),
        c.post("/api/connectors/instances", json={"manifest_id": "forgejo"}, headers=hdr),
        c.patch("/api/connectors/instances/c_000000000000", json={"token": TOKEN}, headers=hdr),
        c.post("/api/connectors/instances/c_000000000000/test", json={}, headers=hdr),
        c.post(
            "/api/connectors/instances/c_000000000000/capabilities",
            json={"read": False},
            headers=hdr,
        ),
        c.delete("/api/connectors/instances/c_000000000000", headers=hdr),
    ]
    assert [r.status_code for r in responses] == [503] * 6, [r.text for r in responses]
    assert all(r.json()["code"] == "store_unreadable" for r in responses)
    with pytest.raises(hq.Refused) as e:
        run(hq.list_connectors(_turn()))
    assert e.value.code == "store_unreadable"
    assert path.read_bytes() == damaged


# ---- the API's own boundaries ------------------------------------------------------------------


def test_the_api_requires_login_csrf_and_origin_and_strict_bodies(auth_cfg):
    anon = TestClient(create_app(auth_cfg), base_url="https://testserver")
    assert anon.get("/api/connectors").status_code == 401
    app = App(auth_cfg)
    c, hdr = app.c, app.hdr
    assert c.post("/api/connectors/instances", json={"manifest_id": "forgejo"}).status_code == 403
    bad_origin = {**hdr, "Origin": "https://invalid.example"}
    assert (
        c.post(
            "/api/connectors/instances", json={"manifest_id": "forgejo"}, headers=bad_origin
        ).status_code
        == 403
    )
    assert (
        c.post(
            "/api/connectors/instances", json={"manifest_id": "forgejo", "x": 1}, headers=hdr
        ).status_code
        == 422
    )
    assert (
        c.post("/api/connectors/instances", json={"manifest_id": "github"}, headers=hdr).status_code
        == 422
    )
    iid = c.post("/api/connectors/instances", json={"manifest_id": "forgejo"}, headers=hdr).json()[
        "id"
    ]
    assert (
        c.patch(
            f"/api/connectors/instances/{iid}", json={"origin": ORIGIN, "extra": 1}, headers=hdr
        ).status_code
        == 422
    )
    big = {"origin": ORIGIN, "token": "x" * (20 * 1024)}
    assert c.patch(f"/api/connectors/instances/{iid}", json=big, headers=hdr).status_code == 413
    # Enabling before a test is refused; disabling always works.
    assert (
        c.post(
            f"/api/connectors/instances/{iid}/capabilities", json={"read": True}, headers=hdr
        ).status_code
        == 409
    )
    assert (
        c.post(
            f"/api/connectors/instances/{iid}/capabilities", json={"read": False}, headers=hdr
        ).status_code
        == 200
    )
    assert c.delete(f"/api/connectors/instances/{iid}", headers=hdr).status_code == 200
    assert c.delete(f"/api/connectors/instances/{iid}", headers=hdr).status_code == 404
    audit = json.loads(store.store_path().read_text())["audit"]
    assert [a["op"] for a in audit][-1] == "removed"


def test_the_store_is_private_and_never_the_real_config():
    import pwd
    import stat

    store.add("forgejo")
    assert stat.S_IMODE(os.stat(store.store_path()).st_mode) == 0o600
    real = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config" / "agent-sessions"
    assert not str(store.store_path()).startswith(str(real))


# ---- Hermes review 7360 (#1782) ----------------------------------------------------------------


def test_7360_1_a_disable_while_resolving_sends_nothing(forge, monkeypatch):
    """Read is disabled after the tool checked it but before the token is fetched (the name is
    still resolving): the token fetch refuses, and no request leaves."""
    inst = _ready()
    t = _turn()
    run(hq.list_connectors(t))
    real = connector_http._resolve

    async def disabling(host, port):
        store.set_capability(inst["id"], False)
        return await real(host, port)

    monkeypatch.setattr(connector_http, "_resolve", disabling)
    before = len(forge.requests)
    with pytest.raises(hq.Refused) as e:
        run(hq.search(t, {"connector_index": 0, "query": "app", "kind": "repos"}))
    assert e.value.code == "connector_disabled"
    assert len(forge.requests) == before
    with pytest.raises(store.Refused):
        store.token_for(inst["id"], store.read()["instances"][0]["generation"])


def test_7360_1_a_disable_while_a_request_is_out_drops_its_result(forge):
    """The in-flight settlement fence: the request already left, but its result never reaches
    the model and mints no ref."""
    inst = _ready()
    t = _turn()
    run(hq.list_connectors(t))
    forge.on_request = lambda request: store.set_capability(inst["id"], False)
    with pytest.raises(hq.Refused) as e:
        run(hq.search(t, {"connector_index": 0, "query": "app", "kind": "repos"}))
    assert e.value.code == "connector_disabled"
    assert t.refs.get(0, set()) == set()


def test_7360_1_the_connection_test_still_works_before_read_is_enabled(forge):
    inst = store.add("forgejo")
    store.patch(inst["id"], {"origin": ORIGIN, "token": TOKEN})
    assert run(service.run_test(inst["id"]))["status"] == "ok"


def _many_repos(n: int):
    rows = [
        {"name": f"r{i:02d}", "owner": {"login": "ops"}, "description": "x" * 400} for i in range(n)
    ]
    return lambda request: httpx.Response(200, json={"ok": True, "data": rows})


def _shown(result: dict, key: str) -> set[str]:
    return {i["ref"] for i in result["untrusted_connector_content"][key] if i["ref"]}


def test_7360_2_accepted_refs_are_exactly_the_shown_ones(forge, monkeypatch):
    _ready()
    monkeypatch.setattr(connector_http, "_TRANSPORT", httpx.MockTransport(_many_repos(20)))
    monkeypatch.setattr(hq, "RESULT_BUDGET", 1500)  # room for a few rows only
    t = _turn()
    run(hq.list_connectors(t))
    out, meta = run(hq.search(t, {"connector_index": 0, "query": "r", "kind": "repos"}))
    shown = _shown(out, "items")
    assert 0 < len(shown) < 20 and meta["count"] == len(shown)
    assert t.refs[0] == shown  # nothing trimmed away is usable
    # Repeated calls keep the invariant.
    out2, _ = run(hq.search(t, {"connector_index": 0, "query": "r", "kind": "repos"}))
    assert t.refs[0] == shown | _shown(out2, "items")


def test_7360_2_past_the_cap_a_ref_is_neither_shown_nor_accepted(forge, monkeypatch):
    _ready()
    monkeypatch.setattr(connector_http, "_TRANSPORT", httpx.MockTransport(_many_repos(20)))
    monkeypatch.setattr(hq, "REFS_MAX", 5)
    t = _turn()
    run(hq.list_connectors(t))
    out, _ = run(hq.search(t, {"connector_index": 0, "query": "r", "kind": "repos"}))
    items = out["untrusted_connector_content"]["items"]
    shown = _shown(out, "items")
    assert len(shown) == 5 and t.refs[0] == shown
    assert sum(1 for i in items if i["ref"] is None) == len(items) - 5


def test_7360_3_a_thread_over_the_body_cap_still_reads_the_issue(forge, monkeypatch):
    real = forge.handler
    huge = [{"user": {"login": "x"}, "created_at": "t", "body": "y" * 30_000} for _ in range(50)]

    def long_thread(request):
        if request.url.path == "/api/v1/repos/ops/app/issues/1/comments":
            forge.requests.append(request)
            return httpx.Response(200, json=huge)
        return real(request)

    _ready()
    monkeypatch.setattr(connector_http, "_TRANSPORT", httpx.MockTransport(long_thread))
    t = _turn()
    run(hq.list_connectors(t))
    run(hq.search(t, {"connector_index": 0, "query": "flaky", "kind": "issues"}))
    out, _ = run(hq.read(t, {"connector_index": 0, "item_ref": "issue:ops/app#1"}))
    body = out["untrusted_connector_content"]
    assert body["body"] == "it flakes" and body["comments"] == []
    assert body["comments_note"] == "the comment thread is too long to read here"


def test_7360_4_search_finds_closed_items(forge, monkeypatch):
    real = forge.handler

    def with_closed(request):
        if request.url.path == "/api/v1/repos/issues/search":
            forge.requests.append(request)
            rows = [
                {"number": 9, "title": "Old flake", "state": "closed", "pull_request": None,
                 "repository": {"owner": "ops", "name": "app"}},
            ]  # fmt: skip
            state = request.url.params.get("state", "open")
            return httpx.Response(200, json=rows if state == "all" else [])
        return real(request)

    _ready()
    monkeypatch.setattr(connector_http, "_TRANSPORT", httpx.MockTransport(with_closed))
    t = _turn()
    run(hq.list_connectors(t))
    out, _ = run(hq.search(t, {"connector_index": 0, "query": "flake", "kind": "issues"}))
    assert _shown(out, "items") == {"issue:ops/app#9"}


def test_7360_5_a_stalled_resolver_is_inside_the_total_deadline(forge, monkeypatch):
    inst = _ready()

    async def stalled(host, port):
        await asyncio.sleep(1.0)
        return [HOST_IP]

    monkeypatch.setattr(connector_http, "_resolve", stalled)
    monkeypatch.setattr(connector_http, "TOTAL_TIMEOUT_S", 0.05)
    b = connector_http.Bound(inst["id"], inst["generation"], ORIGIN, "token", "Forgejo")
    with pytest.raises(connector_http.ConnectorError) as e:
        run(connector_http._request(b, "GET", "/api/v1/user"))
    assert e.value.code == "timeout"


def test_7360_a_malformed_audit_op_is_unreadable_not_a_crash():
    store.add("forgejo")
    doc = json.loads(store.store_path().read_text())
    doc["audit"][-1]["op"] = []
    store.store_path().write_text(json.dumps(doc))
    with pytest.raises(store.StoreUnreadable):
        store.read()


def test_7_the_tool_layer_refuses_a_disabled_connector_before_any_lookup(forge):
    """The first of two fences (the second is the token fetch): checked at every call."""
    inst = _ready()
    t = _turn()
    run(hq.list_connectors(t))
    store.set_capability(inst["id"], False)
    with pytest.raises(hq.Refused) as e:
        hq._bound_now(t.listed[0])
    assert e.value.code == "connector_disabled"


# ---- the re-review of 540e7ade (lost before posting; its probes recovered) ---------------------


def test_reflected_secrets_in_structural_fields_never_reach_the_model(forge, monkeypatch):
    """A forge can reflect a secret into `state` or `created`, not only into text fields."""
    planted = "AKIAreview7360abcdef"
    template_secrets.register_typed([planted])
    real = forge.handler

    def handler(request):
        p = request.url.path
        if p == "/api/v1/repos/ops/app/issues/1/comments":
            forge.requests.append(request)
            return httpx.Response(
                200, json=[{"user": {"login": "dev"}, "created_at": TOKEN, "body": "clean"}]
            )
        if p == "/api/v1/repos/ops/app/issues/1":
            forge.requests.append(request)
            return httpx.Response(
                200,
                json={"number": 1, "title": "t", "state": planted, "body": "b",
                      "user": {"login": "dev"}, "pull_request": None},
            )  # fmt: skip
        if p == f"/api/v1/repos/ops/app/commits/{SHA}/status":
            forge.requests.append(request)
            return httpx.Response(
                200, json={"state": planted, "statuses": [{"context": "c", "status": TOKEN}]}
            )
        return real(request)

    _ready()
    monkeypatch.setattr(connector_http, "_TRANSPORT", httpx.MockTransport(handler))
    t = _turn()
    run(hq.list_connectors(t))
    run(hq.search(t, {"connector_index": 0, "query": "flaky", "kind": "issues"}))
    run(hq.search(t, {"connector_index": 0, "query": "app", "kind": "repos"}))
    repo, _ = run(hq.read(t, {"connector_index": 0, "item_ref": "repo:ops/app"}))
    issue, _ = run(hq.read(t, {"connector_index": 0, "item_ref": "issue:ops/app#1"}))
    ci, _ = run(
        hq.read(
            t, {"connector_index": 0, "item_ref": repo["untrusted_connector_content"]["refs"][0]}
        )
    )
    text = json.dumps([issue, ci], ensure_ascii=False)
    assert planted not in text and TOKEN not in text


def test_a_login_echoing_the_token_is_never_returned_or_stored(forge, monkeypatch):
    real = forge.handler

    def handler(request):
        if request.url.path == "/api/v1/user":
            forge.requests.append(request)
            return httpx.Response(200, json={"login": TOKEN})
        return real(request)

    monkeypatch.setattr(connector_http, "_TRANSPORT", httpx.MockTransport(handler))
    inst = store.add("forgejo")
    store.patch(inst["id"], {"origin": ORIGIN, "token": TOKEN})
    res = run(service.run_test(inst["id"]))
    assert res["status"] == "ok" and res["login"] == ""
    assert TOKEN not in json.dumps(service.catalog())
    assert TOKEN not in store.store_path().read_text()


def _paged_status(rows: list[dict], state: str = "success", cap: int = 50):
    """Forgejo's commit-status route: PAGED, 30 rows a page by default, at most ``cap`` (the
    instance's ``MAX_RESPONSE_ITEMS``, 50 by default)."""

    def page_of(request):
        q = request.url.params
        limit = min(int(q.get("limit", 30)), cap)
        page = max(int(q.get("page", 1)), 1)
        chunk = rows[(page - 1) * limit : page * limit]
        return httpx.Response(200, json={"state": state, "statuses": chunk})

    return page_of


def test_many_ci_checks_are_shrunk_to_fit_not_refused(forge, monkeypatch):
    real = forge.handler

    def handler(request):
        if request.url.path.endswith("/status"):
            forge.requests.append(request)
            rows = [{"context": f"{i:02d}" + "c" * 198, "status": "success"} for i in range(50)]
            return _paged_status(rows)(request)
        return real(request)

    _ready()
    monkeypatch.setattr(connector_http, "_TRANSPORT", httpx.MockTransport(handler))
    t = _turn()
    run(hq.list_connectors(t))
    run(hq.search(t, {"connector_index": 0, "query": "app", "kind": "repos"}))
    repo, _ = run(hq.read(t, {"connector_index": 0, "item_ref": "repo:ops/app"}))
    out, _ = run(
        hq.read(
            t, {"connector_index": 0, "item_ref": repo["untrusted_connector_content"]["refs"][0]}
        )
    )
    from agent_sessions import ask_orchestrator

    assert len(json.dumps(out, ensure_ascii=False)) <= ask_orchestrator.TOOL_RESULT_MAX
    body = out["untrusted_connector_content"]
    assert body["state"] == "success" and body["checks_total"] == 50
    assert 0 < len(body["checks"]) < 50 and "checks_note" in body


def test_a_thread_longer_than_shown_says_so_with_its_true_total(forge, monkeypatch):
    real = forge.handler
    many = [{"user": {"login": "x"}, "created_at": "t", "body": "small"} for _ in range(60)]

    def handler(request):
        if request.url.path == "/api/v1/repos/ops/app/issues/1/comments":
            forge.requests.append(request)
            return httpx.Response(200, json=many)
        return real(request)

    _ready()
    monkeypatch.setattr(connector_http, "_TRANSPORT", httpx.MockTransport(handler))
    t = _turn()
    run(hq.list_connectors(t))
    run(hq.search(t, {"connector_index": 0, "query": "flaky", "kind": "issues"}))
    out, _ = run(hq.read(t, {"connector_index": 0, "item_ref": "issue:ops/app#1"}))
    body = out["untrusted_connector_content"]
    assert body["comments_total"] == 60
    assert body["comments_note"] == f"showing the newest {len(body['comments'])} of 60"


def test_a_disable_after_the_token_fetch_drops_the_result(forge, monkeypatch):
    """The defined in-flight boundary: the token fetch is the last check before a request leaves.
    A disable after it cannot recall that one request, but its result never reaches the model and
    mints no ref, and the next request is refused before any token is fetched."""
    inst = _ready()
    t = _turn()
    run(hq.list_connectors(t))
    real_token = connector_http._token

    def token_then_disable(bound):
        tok = real_token(bound)
        store.set_capability(inst["id"], False)
        return tok

    monkeypatch.setattr(connector_http, "_token", token_then_disable)
    before = len(forge.requests)
    with pytest.raises(hq.Refused) as e:
        run(hq.search(t, {"connector_index": 0, "query": "app", "kind": "repos"}))
    assert e.value.code == "connector_disabled"
    assert len(forge.requests) - before <= 1  # at most the one already in flight
    assert t.refs.get(0, set()) == set()


def test_more_checks_than_the_adapter_keeps_report_their_true_total(forge, monkeypatch):
    """Hermes on #1782 (review 7369): a commit with more than 50 contexts says how many it has."""
    real = forge.handler

    def handler(request):
        if request.url.path.endswith("/status"):
            forge.requests.append(request)
            rows = [{"context": f"c{i}", "status": "success"} for i in range(70)]
            return _paged_status(rows)(request)
        return real(request)

    _ready()
    monkeypatch.setattr(connector_http, "_TRANSPORT", httpx.MockTransport(handler))
    t = _turn()
    run(hq.list_connectors(t))
    run(hq.search(t, {"connector_index": 0, "query": "app", "kind": "repos"}))
    repo, _ = run(hq.read(t, {"connector_index": 0, "item_ref": "repo:ops/app"}))
    ref = repo["untrusted_connector_content"]["refs"][0]
    out, _ = run(hq.read(t, {"connector_index": 0, "item_ref": ref}))
    body = out["untrusted_connector_content"]
    assert body["checks_total"] == 70 and len(body["checks"]) == 50
    assert body["checks_note"] == "showing 50 of 70 checks"


def test_a_status_list_past_the_page_bound_reports_a_lower_bound(forge, monkeypatch):
    """More contexts than `STATUS_PAGES_MAX` pages hold: the total is said to be a lower bound."""
    real = forge.handler
    rows = [{"context": f"c{i}", "status": "success"} for i in range(400)]

    def handler(request):
        if request.url.path.endswith("/status"):
            forge.requests.append(request)
            return _paged_status(rows)(request)
        return real(request)

    _ready()
    monkeypatch.setattr(connector_http, "_TRANSPORT", httpx.MockTransport(handler))
    t = _turn()
    run(hq.list_connectors(t))
    run(hq.search(t, {"connector_index": 0, "query": "app", "kind": "repos"}))
    repo, _ = run(hq.read(t, {"connector_index": 0, "item_ref": "repo:ops/app"}))
    out, _ = run(
        hq.read(
            t, {"connector_index": 0, "item_ref": repo["untrusted_connector_content"]["refs"][0]}
        )
    )
    body = out["untrusted_connector_content"]
    assert body["checks_total"] == 300 and body["checks_total_is_lower_bound"] is True
    assert body["checks_note"] == "showing 50 of 300+ checks"
    pages = [r for r in forge.requests if r.url.path.endswith("/status")]
    assert len(pages) == 6 and all(r.url.params["limit"] == "50" for r in pages)


def _ci_read(forge, monkeypatch, status_handler) -> tuple[dict, list]:
    real = forge.handler

    def handler(request):
        if request.url.path.endswith("/status"):
            forge.requests.append(request)
            return status_handler(request)
        return real(request)

    _ready()
    monkeypatch.setattr(connector_http, "_TRANSPORT", httpx.MockTransport(handler))
    t = _turn()
    run(hq.list_connectors(t))
    run(hq.search(t, {"connector_index": 0, "query": "app", "kind": "repos"}))
    repo, _ = run(hq.read(t, {"connector_index": 0, "item_ref": "repo:ops/app"}))
    ref = repo["untrusted_connector_content"]["refs"][0]
    out, _ = run(hq.read(t, {"connector_index": 0, "item_ref": ref}))
    pages = [r for r in forge.requests if r.url.path.endswith("/status")]
    return out["untrusted_connector_content"], pages


@pytest.mark.parametrize("cap", [20, 30])
def test_a_forge_with_a_smaller_page_cap_still_reports_the_true_total(forge, monkeypatch, cap):
    """Hermes review 7374: an instance whose `MAX_RESPONSE_ITEMS` is below the `limit` asked for
    answers short pages that are not the last page."""
    rows = [{"context": f"c{i}", "status": "success"} for i in range(70)]
    body, _pages = _ci_read(forge, monkeypatch, _paged_status(rows, cap=cap))
    assert body["checks_total"] == 70 and "checks_total_is_lower_bound" not in body
    assert body["checks_note"] == "showing 50 of 70 checks"


def test_a_short_final_page_ends_the_walk_without_a_note(forge, monkeypatch):
    rows = [{"context": f"c{i}", "status": "success"} for i in range(20)]
    body, pages = _ci_read(forge, monkeypatch, _paged_status(rows, cap=20))
    assert body["checks_total"] == 20 and "checks_note" not in body
    assert len(pages) == 2  # page 2 came back empty: the end


def test_a_forge_that_ignores_the_page_parameter_does_not_inflate_the_count(forge, monkeypatch):
    rows = [{"context": f"c{i}", "status": "success"} for i in range(30)]
    body, pages = _ci_read(
        forge,
        monkeypatch,
        lambda request: httpx.Response(200, json={"state": "success", "statuses": rows}),
    )
    assert body["checks_total"] == 30 and "checks_note" not in body
    assert len(pages) == 2


def test_distinct_contexts_sharing_a_long_prefix_are_counted_apart(forge, monkeypatch):
    """Hermes review 7378: a context's identity is its FULL text, not the 200 characters shown."""
    prefix = "p" * 200
    rows = [{"context": f"{prefix}{i:02d}", "status": "success"} for i in range(70)]
    body, pages = _ci_read(forge, monkeypatch, _paged_status(rows))
    assert body["checks_total"] == 70 and len(pages) == 2
    assert body["checks_note"].endswith(" of 70 checks")  # long names: fewer fit, same total


def test_a_failure_on_a_later_page_makes_the_commit_red_and_shows_first(forge, monkeypatch):
    """Hermes review 7378: Forgejo's `state` is per PAGE; the combined state is computed here,
    over every context, and the failing checks are the ones kept."""
    rows = [{"context": f"ok{i}", "status": "success"} for i in range(50)] + [
        {"context": f"bad{i}", "status": "failure"} for i in range(20)
    ]
    body, _ = _ci_read(forge, monkeypatch, _paged_status(rows, state="success"))
    assert body["state"] == "failure"
    assert [c["state"] for c in body["checks"][:20]] == ["failure"] * 20


def test_a_capped_walk_never_claims_success(forge, monkeypatch):
    rows = [{"context": f"c{i}", "status": "success"} for i in range(400)]
    body, _ = _ci_read(forge, monkeypatch, _paged_status(rows, state="success"))
    assert body["state"] == "incomplete" and body["checks_total_is_lower_bound"] is True


def test_a_capped_walk_that_saw_a_failure_reports_it(forge, monkeypatch):
    rows = [{"context": "broken", "status": "failure"}] + [
        {"context": f"c{i}", "status": "success"} for i in range(399)
    ]
    body, _ = _ci_read(forge, monkeypatch, _paged_status(rows, state="success"))
    assert body["state"] == "failure" and body["checks"][0]["context"] == "broken"


def test_contexts_sharing_a_very_long_prefix_are_still_distinct(forge, monkeypatch):
    """Hermes review 7381: no length bound on a context's identity. 70 contexts share a
    10,001-character prefix; the 20 on page 2 fail."""
    prefix = "q" * 10_001
    rows = [{"context": f"{prefix}ok{i}", "status": "success"} for i in range(50)] + [
        {"context": f"{prefix}bad{i}", "status": "failure"} for i in range(20)
    ]
    body, pages = _ci_read(forge, monkeypatch, _paged_status(rows, state="success"))
    assert len(pages) == 2 and body["checks_total"] == 70
    assert body["state"] == "failure"
