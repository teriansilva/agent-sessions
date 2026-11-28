"""\"Newer version available\" for MCP servers (#2431): a cache-only hint, never an install.

Against the real registry store with fixture registry answers (`mcp_updates._read` replaced, or
a local fixture origin behind the real egress proxy): the per-kind "latest" rules, the version
ordering and pre-release rule, the identity fence, the throttle and coalescing, the bounded
sweep, the route, and that nothing here ever resolves, reviews or installs.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_sessions import ask_mcp, mcp_gitsource, mcp_install, mcp_registry, mcp_updates


@pytest.fixture
def store(tmp_home, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MCP_SERVERS", str(tmp_path / "mcp-servers.json"))

    def refuse(*a, **kw):
        raise AssertionError("an update check must never resolve, review or install")

    for target, name in (
        (mcp_gitsource, "resolve"),
        (mcp_install, "review"),
        (mcp_install, "install"),
    ):
        monkeypatch.setattr(target, name, refuse)
    return tmp_path


class Registry:
    """Fixture answers by URL; counts reads, can hold them until released."""

    def __init__(self, monkeypatch):
        self.answers: dict[str, object] = {}
        self.reads: list[str] = []
        self.gate: threading.Event | None = None
        self.delay = 0.0
        self.now = 0
        self.peak = 0
        self._lock = threading.Lock()
        monkeypatch.setattr(mcp_updates, "_read", self.read)

    def read(self, kind, url):
        # Every read runs on the checker's own pool, never on a page load's or HQ's thread.
        assert threading.current_thread().name.startswith("mcp-updates"), "a read off the pool"
        with self._lock:
            self.reads.append(url)
            self.now += 1
            self.peak = max(self.peak, self.now)
        try:
            if self.gate is not None:
                assert self.gate.wait(10)
            if self.delay:
                time.sleep(self.delay)
            answer = self.answers.get(url)
            if isinstance(answer, Exception):
                raise answer
            if answer is None:
                return None
            return answer if isinstance(answer, bytes) else json.dumps(answer).encode()
        finally:
            with self._lock:
                self.now -= 1


@pytest.fixture
def registry(monkeypatch):
    return Registry(monkeypatch)


def _npm_url(pkg):
    return mcp_updates._lookup_url("npm", pkg)


def _server(name="pkg", kind="npm", package="@acme/x", version="1.0.0", installed=True):
    source = {"kind": kind, "package": package, "version": version}
    if kind == "go":  # a Go module comes only from a repository link's resolution (#2427)
        rec = mcp_registry.create(
            {"name": name},
            resolution={
                "id": "c" * 16,
                "source": source,
                "origin": {"url": "https://github.com/owner/repo", "commit": "1" * 40},
            },
        )
    else:
        rec = mcp_registry.create({"name": name, "source": source})
    if installed:
        rec = mcp_registry.set_installed(
            rec["id"], {"source": rec["source"], "command": "/bin/true", "args": []}
        )
    return rec


def _check(sid):
    fut = mcp_updates.start(sid)
    if fut is not None:
        fut.result(10)
    return mcp_updates.view(mcp_registry.get(sid))


# ---- ordering ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind, candidate, installed, offered",
    [
        ("npm", "1.1.0", "1.0.0", True),
        ("npm", "1.0.0", "1.0.0", False),
        ("npm", "0.9.0", "1.0.0", False),
        ("npm", "2.0.0-beta.1", "1.0.0", False),  # a pre-release only for a pre-release
        ("npm", "2.0.0-beta.2", "2.0.0-beta.1", True),
        ("npm", "2.0.0", "2.0.0-rc.1", True),
        ("npm", "1.0.0-rc.10", "1.0.0-rc.9", True),  # numeric identifiers compare as numbers
        ("pypi", "1.10", "1.9", True),
        ("pypi", "2.0rc1", "1.9", False),
        ("pypi", "1.0.post1", "1.0", True),
        ("pypi", "1.0", "1.0.0", False),  # PEP 440: the same version
        ("go", "v3.4.0", "v3.3.0", True),
        ("go", "v3.4.0", "v3.3.1-0.20261010175143-0123456789ab", True),
        ("npm", "not-a-version", "1.0.0", False),
    ],
)
def test_newer_is_strictly_greater_by_the_kinds_ordering(kind, candidate, installed, offered):
    assert mcp_updates.newer(kind, candidate, installed) is offered


def test_the_go_list_offers_only_tagged_releases_of_the_installed_major():
    listing = "\n".join(
        [
            "v3.3.0",
            "v3.4.0",
            "v3.5.0-0.20261111000000-abcdefabcdef",  # a pseudo-version
            "v3.6.0+incompatible",
            "v4.0.0",  # another module (…/v4)
            "v3.4.1-rc.1",
            "garbage",
        ]
    ).encode()
    # The greatest stable tag AND the greatest tag overall: a higher pre-release never hides
    # a stable one (Hermes on #2443).
    assert mcp_updates._go_candidates("example.org/m/v3", listing) == ["v3.4.0", "v3.4.1-rc.1"]
    assert mcp_updates._go_candidates("example.org/m", b"v0.9.0\nv1.2.0\nv2.0.0\n") == ["v1.2.0"]
    assert mcp_updates._go_candidates("example.org/m/v3", b"v4.0.0\n") == []


def test_the_go_module_path_is_case_encoded_for_the_proxy():
    url = mcp_updates._lookup_url("go", "github.com/BurntSushi/toml")
    assert url == "https://proxy.golang.org/github.com/!burnt!sushi/toml/@v/list"


def test_every_lookup_url_is_on_its_kinds_registry_hosts():
    from agent_sessions.mcp_egress import origin

    for kind, pkg in (("npm", "@acme/x"), ("pypi", "acme-x"), ("go", "example.org/m/v2")):
        assert origin(mcp_updates._lookup_url(kind, pkg)) in mcp_install._allowed(kind)


# ---- the per-kind "latest" and the view ----------------------------------------------------------


def test_a_newer_npm_release_is_available_and_equal_is_current(store, registry):
    rec = _server()
    registry.answers[_npm_url("@acme/x")] = {"dist-tags": {"latest": "1.2.0"}}
    view = _check(rec["id"])
    assert view["state"] == "available" and view["latest"] == "1.2.0"
    assert view["installed"] == "1.0.0" and view["checked_at"] and view["error"] is None
    other = _server(name="pkg_two", package="@acme/y", version="1.2.0")
    registry.answers[_npm_url("@acme/y")] = {"dist-tags": {"latest": "1.2.0"}}
    assert _check(other["id"])["state"] == "current"


def test_a_pypi_release_without_a_wheel_is_not_offered(store, registry):
    rec = _server(kind="pypi", package="acme-x", version="1.0")
    url = mcp_updates._lookup_url("pypi", "acme-x")
    registry.answers[url] = {
        "info": {"version": "1.1"},
        "urls": [{"packagetype": "sdist"}, {"packagetype": "bdist_wheel", "yanked": True}],
    }
    assert _check(rec["id"])["state"] == "current"
    wheel = {"packagetype": "bdist_wheel", "yanked": False}
    registry.answers[url] = {"info": {"version": "1.1"}, "urls": [wheel]}
    mcp_updates.forget(rec["id"])  # a new slot: the throttle held the first answer
    view = _check(rec["id"])
    assert view["state"] == "available" and view["latest"] == "1.1"


def test_a_go_module_reads_its_proxy_list(store, registry):
    rec = _server(kind="go", package="example.org/acme/mcp/v3", version="v3.3.0")
    registry.answers[mcp_updates._lookup_url("go", "example.org/acme/mcp/v3")] = (
        b"v3.2.0\nv3.3.0\nv3.4.0\nv4.0.0\n"
    )
    view = _check(rec["id"])
    assert view["state"] == "available" and view["latest"] == "v3.4.0"


def test_an_unreadable_registry_keeps_the_last_answer_and_says_so(store, registry, monkeypatch):
    rec = _server()
    registry.answers[_npm_url("@acme/x")] = {"dist-tags": {"latest": "1.2.0"}}
    assert _check(rec["id"])["state"] == "available"
    monkeypatch.setattr(mcp_updates, "THROTTLE_S", 0)
    registry.answers[_npm_url("@acme/x")] = mcp_updates.UpdateError("the npm answered 503")
    view = _check(rec["id"])
    assert view["state"] == "available" and view["latest"] == "1.2.0"
    assert view["error"] == "the npm answered 503" and view["tried_at"]
    registry.answers[_npm_url("@acme/x")] = {"dist-tags": {"latest": "1.2.0"}}
    assert _check(rec["id"])["error"] is None  # a success clears the failed attempt


def test_no_answer_yet_or_an_old_one_is_unknown_never_up_to_date(store, registry):
    rec = _server()
    assert mcp_updates.view(rec)["state"] == "unknown"
    registry.answers[_npm_url("@acme/x")] = mcp_updates.UpdateError("the npm could not be reached")
    view = _check(rec["id"])
    assert view["state"] == "unknown" and view["error"] == "the npm could not be reached"
    registry.answers[_npm_url("@acme/x")] = {"dist-tags": {"latest": "1.0.0"}}
    mcp_updates.forget(rec["id"])
    view = _check(rec["id"])
    assert view["state"] == "current"
    later = time.time() + mcp_updates.FRESH_S + 60
    assert mcp_updates.view(mcp_registry.get(rec["id"]), now=later)["state"] == "unknown"


def test_a_remote_or_uninstalled_server_has_no_hint(store, registry):
    remote = mcp_registry.create(
        {"name": "remote", "source": {"kind": "remote", "url": "https://mcp.example.test/x"}}
    )
    assert mcp_updates.view(remote) is None and mcp_updates.start(remote["id"]) is None
    pending = _server(name="pending", installed=False)
    assert mcp_updates.view(pending) is None and mcp_updates.start(pending["id"]) is None
    assert registry.reads == []


# ---- fences ------------------------------------------------------------------------------------


def test_newer_is_measured_against_the_installed_artifact(store, registry):
    """An edited, not yet installed source has no hint; installing it starts a new identity and
    the old answer is unreadable."""
    rec = _server()
    registry.answers[_npm_url("@acme/x")] = {"dist-tags": {"latest": "1.2.0"}}
    assert _check(rec["id"])["state"] == "available"
    edited = mcp_registry.update(
        rec["id"],
        {"source": {"kind": "npm", "package": "@acme/x", "version": "1.2.0"}},
        mcp_registry.get(rec["id"])["updated_at"],
    )
    assert mcp_updates.view(edited) is None  # 1.2.0 is chosen, not installed
    installed = mcp_registry.set_installed(
        rec["id"], {"source": edited["source"], "command": "/bin/true", "args": []}
    )
    view = mcp_updates.view(installed)
    assert view["state"] == "unknown" and view["installed"] == "1.2.0"  # the 1.0.0 answer is gone


def test_a_late_answer_for_an_old_identity_is_discarded(store, registry):
    rec = _server()
    registry.answers[_npm_url("@acme/x")] = {"dist-tags": {"latest": "9.0.0"}}
    registry.gate = threading.Event()
    fut = mcp_updates.start(rec["id"])
    assert mcp_updates.view(mcp_registry.get(rec["id"]))["checking"] is True
    mcp_registry.update(
        rec["id"],
        {"source": {"kind": "npm", "package": "@acme/x", "version": "2.0.0"}},
        mcp_registry.get(rec["id"])["updated_at"],
    )
    now = mcp_registry.get(rec["id"])
    mcp_registry.set_installed(rec["id"], {"source": now["source"], "command": "/bin/true"})
    registry.gate.set()
    fut.result(10)
    view = mcp_updates.view(mcp_registry.get(rec["id"]))
    assert view["state"] == "unknown" and view["latest"] is None and view["checking"] is False
    # Nothing of the old identity is written back: it would replace the new one's entry (and
    # its throttle slot).
    kept = json.loads(mcp_updates.store_path().read_text())["servers"].get(rec["id"])
    assert kept is None or kept["identity"] == ["npm", "@acme/x", "2.0.0"]


def test_an_answer_never_touches_the_server_record(store, registry):
    rec = _server()
    before = mcp_registry.get(rec["id"])
    registry.answers[_npm_url("@acme/x")] = {"dist-tags": {"latest": "1.2.0"}}
    _check(rec["id"])
    assert mcp_registry.get(rec["id"]) == before  # an open Settings save stays valid
    assert mcp_updates.store_path().exists()


def test_a_removed_servers_answer_goes_with_it(store, registry):
    rec = _server()
    registry.answers[_npm_url("@acme/x")] = {"dist-tags": {"latest": "1.2.0"}}
    registry.gate = threading.Event()
    fut = mcp_updates.start(rec["id"])
    mcp_registry.delete(rec["id"], mcp_registry.get(rec["id"])["updated_at"])
    registry.gate.set()
    fut.result(10)
    assert rec["id"] not in json.loads(mcp_updates.store_path().read_text())["servers"]


# ---- throttle, coalescing, the sweep ------------------------------------------------------------


def test_a_server_is_read_at_most_once_in_ten_minutes_failures_included(store, registry):
    rec = _server()
    registry.answers[_npm_url("@acme/x")] = mcp_updates.UpdateError("the npm answered 500")
    _check(rec["id"])
    assert mcp_updates.start(rec["id"]) is None  # the failed attempt holds the slot too
    assert mcp_updates.sweep() == 0
    assert len(registry.reads) == 1


def test_identical_lookups_across_servers_share_one_read(store, registry):
    a = _server(name="one")
    b = _server(name="two")  # the same package, a second entry (#2430's Duplicate)
    registry.answers[_npm_url("@acme/x")] = {"dist-tags": {"latest": "1.2.0"}}
    registry.gate = threading.Event()
    futs = [mcp_updates.start(a["id"]), mcp_updates.start(b["id"])]
    registry.gate.set()
    for f in futs:
        f.result(10)
    assert len(registry.reads) == 1
    for sid in (a["id"], b["id"]):
        assert mcp_updates.view(mcp_registry.get(sid))["latest"] == "1.2.0"


def test_the_sweep_is_bounded_and_the_list_stays_cache_only(store, registry, monkeypatch):
    """The 64-server limit, slow and failing registries: at most WORKERS reads at once, and the
    Library's list (every summary's ``update``) never reads the network meanwhile."""
    for i in range(mcp_registry.SERVERS_MAX):
        _server(name=f"pkg_{i}", package=f"@acme/p{i}")
        url = _npm_url(f"@acme/p{i}")
        registry.answers[url] = (
            mcp_updates.UpdateError("the npm answered 502")
            if i % 5 == 0
            else {"dist-tags": {"latest": "1.1.0"}}
        )
    registry.delay = 0.02
    done = []
    t = threading.Thread(target=lambda: done.append(mcp_updates.sweep()))
    t.start()
    snap = mcp_updates.snapshot()
    views = [mcp_updates.view(r, store=snap) for r in mcp_registry.list_servers()]
    assert len(views) == mcp_registry.SERVERS_MAX
    t.join(60)
    assert done == [mcp_registry.SERVERS_MAX]
    assert registry.peak <= mcp_updates.WORKERS
    assert mcp_updates.last_sweep["read"] == mcp_registry.SERVERS_MAX
    assert mcp_updates.last_sweep["seconds"] > 0
    states = [mcp_updates.view(r)["state"] for r in mcp_registry.list_servers()]
    assert states.count("available") == 51 and states.count("unknown") == 13


# ---- the real read through the egress proxy ------------------------------------------------------


def test_the_read_goes_through_the_egress_proxy_to_allowed_hosts_only(monkeypatch):
    class Origin(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/moved":
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/elsewhere")
                self.end_headers()
                return
            body = json.dumps({"dist-tags": {"latest": "2.0.0"}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_port
    monkeypatch.setattr(mcp_install, "_allowed", lambda kind: {("127.0.0.1", port)})
    try:
        body = mcp_updates._read("npm", f"http://127.0.0.1:{port}/pkg")
        assert json.loads(body)["dist-tags"]["latest"] == "2.0.0"
        with pytest.raises(mcp_updates.UpdateError, match="not allowed"):
            mcp_updates._read("npm", "https://registry.example.test/pkg")
        with pytest.raises(mcp_updates.UpdateError, match="answered 302"):
            mcp_updates._read("npm", f"http://127.0.0.1:{port}/moved")  # never followed
    finally:
        server.shutdown()


# ---- the route and HQ ---------------------------------------------------------------------------


def test_the_route_checks_in_the_background_and_the_list_carries_the_hint(
    store, registry, auth_cfg
):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    rec = _server()
    remote = mcp_registry.create(
        {"name": "remote", "source": {"kind": "remote", "url": "https://mcp.example.test/x"}}
    )
    registry.answers[_npm_url("@acme/x")] = {"dist-tags": {"latest": "1.2.0"}}
    client = TestClient(create_app(auth_cfg), base_url="https://testserver")
    url = f"/api/mcp/servers/{rec['id']}/updates/check"
    assert client.post(url).status_code in (401, 403)
    client.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    csrf = client.get("/api/config").json()["csrf"]
    guard = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    assert client.post(url, headers={"Origin": auth_cfg.origin}).status_code == 403
    registry.gate = threading.Event()
    r = client.post(url, headers=guard)
    assert r.status_code == 202 and r.json()["started"] is True
    assert r.json()["update"]["checking"] is True  # answered before the read finished
    registry.gate.set()
    for _ in range(100):
        if not mcp_updates._running:
            break
        time.sleep(0.05)
    again = client.post(url, headers=guard)
    assert again.status_code == 200 and again.json()["started"] is False  # throttled
    assert again.json()["update"]["latest"] == "1.2.0"
    rows = {s["id"]: s for s in client.get("/api/mcp/servers").json()["servers"]}
    assert rows[rec["id"]]["update"]["state"] == "available"
    assert rows[remote["id"]]["update"] is None
    detail = client.get(f"/api/mcp/servers/{rec['id']}").json()
    assert detail["server"]["update"]["latest"] == "1.2.0"
    assert len(registry.reads) == 1  # page loads never read
    no_version = client.post(f"/api/mcp/servers/{remote['id']}/updates/check", headers=guard)
    assert no_version.status_code == 422


@pytest.mark.parametrize(
    "update, available, words",
    [
        (None, None, None),
        (
            {"state": "available", "latest": "1.2.0", "installed": "1.0.0"},
            "1.2.0",
            "1.2.0 published; 1.0.0 installed",
        ),
        ({"state": "current", "installed": "1.0.0"}, None, "no newer release"),
        ({"state": "unknown", "installed": "1.0.0"}, None, "unknown"),
        (
            {"state": "unknown", "installed": "1.0.0", "error": "the npm answered 503"},
            None,
            "the last check failed (the npm answered 503)",
        ),
    ],
)
def test_hq_says_the_hint_in_words_and_never_up_to_date_without_an_answer(update, available, words):
    got, said = ask_mcp._update_said({"update": update})
    assert got == available
    if words is None:
        assert said is None
    else:
        assert words in said and "up to date" not in said


# ---- Hermes round 1 on PR #2443 ----------------------------------------------------------------


def test_a_higher_pre_release_never_hides_a_stable_update(store, registry):
    """One proxy read, two servers: the stable install is offered the stable v3.4.0 (not hidden
    by v3.4.1-rc.1), and a pre-release install is offered the rc."""
    module = "example.org/acme/mcp/v3"
    registry.answers[mcp_updates._lookup_url("go", module)] = b"v3.3.0\nv3.4.0\nv3.4.1-rc.1\n"
    stable = _server(name="stable", kind="go", package=module, version="v3.3.0")
    view = _check(stable["id"])
    assert view["state"] == "available" and view["latest"] == "v3.4.0"
    assert mcp_updates.pick("go", ["v3.4.0", "v3.4.1-rc.1"], "v3.4.1-rc.0") == "v3.4.1-rc.1"
    assert mcp_updates.pick("go", ["v3.4.1-rc.1"], "v3.3.0") is None
    assert mcp_updates.pick("pypi", ["2.0rc1", "1.9"], "1.8") == "1.9"


def test_a_late_settlement_never_replaces_a_newer_claims_answer_or_slot(store, registry):
    """read → the source is replaced and installed → a new claim settles → the OLD settlement
    lands: the new answer and the new claim's 10-minute slot are kept."""
    rec = _server()
    old_ident = mcp_updates.identity(rec)
    old_token = mcp_updates._claim(rec["id"], old_ident, time.time())
    edited = mcp_registry.update(
        rec["id"],
        {"source": {"kind": "npm", "package": "@acme/x", "version": "2.0.0"}},
        mcp_registry.get(rec["id"])["updated_at"],
    )
    now = mcp_registry.set_installed(
        rec["id"], {"source": edited["source"], "command": "/bin/true", "args": []}
    )
    new_ident = mcp_updates.identity(now)
    new_token = mcp_updates._claim(rec["id"], new_ident, time.time())
    assert new_token
    mcp_updates._settle(rec["id"], new_ident, new_token, latest="2.1.0")
    before = mcp_updates.view(mcp_registry.get(rec["id"]))
    assert before["state"] == "available" and before["latest"] == "2.1.0"
    mcp_updates._settle(rec["id"], old_ident, old_token, latest="9.0.0")  # the late one
    mcp_updates._settle(rec["id"], old_ident, old_token, error="the npm answered 503")
    after = mcp_updates.view(mcp_registry.get(rec["id"]))
    assert after == before
    kept = json.loads(mcp_updates.store_path().read_text())["servers"][rec["id"]]
    assert kept["identity"] == list(new_ident) and kept["claim"] == new_token
    assert mcp_updates.start(rec["id"]) is None  # the new claim's slot still holds
    assert registry.reads == []


def test_a_delayed_start_never_claims_over_a_newer_identity(store, registry):
    """old start reads 1.0.0 → the source is replaced and installed, and a fresh check settles →
    the OLD start reaches its claim: refused; the new answer and its 10-minute slot are kept
    (Hermes on #2443, review 9142)."""
    rec = _server()
    old_ident = mcp_updates.identity(rec)  # what the delayed start read
    edited = mcp_registry.update(
        rec["id"],
        {"source": {"kind": "npm", "package": "@acme/x", "version": "2.0.0"}},
        mcp_registry.get(rec["id"])["updated_at"],
    )
    mcp_registry.set_installed(
        rec["id"], {"source": edited["source"], "command": "/bin/true", "args": []}
    )
    registry.answers[_npm_url("@acme/x")] = {"dist-tags": {"latest": "2.1.0"}}
    fresh = _check(rec["id"])
    assert fresh["state"] == "available" and fresh["latest"] == "2.1.0"
    assert mcp_updates._claim(rec["id"], old_ident, time.time()) is None  # the delayed one
    assert mcp_updates.view(mcp_registry.get(rec["id"])) == fresh
    assert mcp_updates.start(rec["id"]) is None  # the new identity's slot still holds
    assert len(registry.reads) == 1
