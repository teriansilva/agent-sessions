"""Package sources for project MCP servers (#2163 P1b): exact, locked, reviewed, inert.

The unit tests drive `mcp_install` with a fake package tool that writes the lock a real one would.
``AGENT_SESSIONS_TEST_MCP_NETWORK=1`` additionally installs real packages from npm and PyPI and
checks the npm one end to end (network; never in CI)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from agent_sessions import mcp_install, mcp_registry

NPM_PKG = "@example/mcp-demo"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_SESSIONS_MCP_SERVERS", str(tmp_path / "mcp-servers.json"))
    return tmp_path


def _lock(packages: dict) -> dict:
    return {"name": "battlelab-mcp-server", "lockfileVersion": 3, "packages": packages}


def _good_lock(**top) -> dict:
    return _lock(
        {
            "": {"dependencies": {NPM_PKG: "1.2.3"}},
            f"node_modules/{NPM_PKG}": {
                "version": "1.2.3",
                "resolved": f"https://registry.npmjs.org/{NPM_PKG}/-/mcp-demo-1.2.3.tgz",
                "integrity": "sha512-AAAA",
                "bin": {"mcp-demo": "dist/index.js"},
                **top,
            },
            "node_modules/dep": {
                "version": "0.1.0",
                "resolved": "https://registry.npmjs.org/dep/-/dep-0.1.0.tgz",
                "integrity": "sha512-BBBB",
                "hasInstallScript": True,
            },
        }
    )


@pytest.fixture
def fake_npm(monkeypatch):
    """`_run` stands in for npm: review writes ``lock``; install lays down the package."""
    state = {"lock": _good_lock(), "calls": []}
    monkeypatch.setattr(mcp_install, "_find", lambda name: f"/usr/bin/{name}")

    def run(argv, cwd, what, kind, timeout=None):
        state["calls"].append((list(argv), Path(cwd)))
        if "--package-lock-only" in argv:
            (Path(cwd) / "package-lock.json").write_text(json.dumps(state["lock"]))
        elif argv[1] == "ci":
            pkg = Path(cwd) / "node_modules" / NPM_PKG
            (pkg / "dist").mkdir(parents=True)
            (pkg / "dist" / "index.js").write_text("// server\n")
            (pkg / "package.json").write_text(json.dumps({"bin": {"mcp-demo": "dist/index.js"}}))
        return ""

    monkeypatch.setattr(mcp_install, "_run", run)
    return state


def _npm_server(**source):
    return mcp_registry.create(
        {
            "name": "demo",
            "source": {"kind": "npm", "package": NPM_PKG, "version": "1.2.3", **source},
        }
    )


@pytest.mark.parametrize(
    "source, words",
    [
        ({"kind": "npm", "package": NPM_PKG, "version": "^1.2.3"}, "exact version"),
        ({"kind": "npm", "package": NPM_PKG, "version": "latest"}, "exact version"),
        ({"kind": "npm", "package": "Bad Name", "version": "1.0.0"}, "lowercase"),
        ({"kind": "pypi", "package": "nc", "version": ">=1.0"}, "exact version"),
        ({"kind": "pypi", "package": "nc", "version": "1.0", "url": "x"}, "unknown source field"),
    ],
)
def test_only_exact_versions_of_well_formed_packages_are_admitted(home, source, words):
    with pytest.raises(mcp_registry.RegistryError, match=words):
        mcp_registry.create({"name": "demo", "source": source})


def test_a_review_shows_what_will_run_and_installs_nothing(home, fake_npm):
    rec = _npm_server()
    facts = mcp_install.review(rec)
    assert facts["integrity"] == "sha512-AAAA" and facts["dependencies"] == 1
    assert facts["install_scripts_declared"] == ["dep"]  # declared, never run
    assert facts["entrypoint"] == "mcp-demo" and facts["registry"] == "registry.npmjs.org"
    ((argv, _),) = fake_npm["calls"]
    assert "--ignore-scripts" in argv and "--package-lock-only" in argv
    assert not (mcp_install.root() / rec["id"] / facts["review_id"]).exists()


@pytest.mark.parametrize(
    "mutate, words",
    [
        (
            lambda lk: lk["packages"]["node_modules/dep"].update(
                resolved="https://evil.example/dep.tgz"
            ),
            "outside registry.npmjs.org",
        ),
        (lambda lk: lk["packages"]["node_modules/dep"].pop("integrity"), "no digest"),
        (
            lambda lk: lk["packages"][f"node_modules/{NPM_PKG}"].update(version="1.2.4"),
            "different version",
        ),
        (
            lambda lk: lk["packages"][f"node_modules/{NPM_PKG}"].update(
                bin={"a": "a.js", "b": "b.js"}
            ),
            "choose one: a, b",
        ),
    ],
)
def test_a_review_refuses_a_lock_it_cannot_vouch_for(home, fake_npm, mutate, words):
    mutate(fake_npm["lock"])
    with pytest.raises(mcp_install.InstallError, match=words):
        mcp_install.review(_npm_server())


def test_install_takes_exactly_the_reviewed_lock_and_is_idempotent(home, fake_npm):
    rec = _npm_server()
    facts = mcp_install.review(rec)
    installed = mcp_install.install(rec, facts["review_id"])
    assert installed["command"] == "/usr/bin/node"
    assert installed["args"][0].endswith(f"{NPM_PKG}/dist/index.js")
    ci = fake_npm["calls"][-1][0]
    assert ci[1] == "ci" and "--ignore-scripts" in ci
    assert mcp_install.install(rec, facts["review_id"]) == installed  # no second install
    target = Path(installed["dir"])
    assert not os.access(target, os.W_OK)  # frozen
    rec = mcp_registry.set_installed(rec["id"], installed)
    assert mcp_install.command(rec) == ("/usr/bin/node", installed["args"])


def test_install_refuses_an_unknown_tampered_or_outdated_review(home, fake_npm):
    rec = _npm_server()
    facts = mcp_install.review(rec)
    with pytest.raises(mcp_install.InstallError, match="no such review"):
        mcp_install.install(rec, "0" * 16)
    stage = mcp_install._staging(rec["id"], facts["review_id"])
    (stage / "package-lock.json").write_text("{}")
    with pytest.raises(mcp_install.InstallError, match="lock changed"):
        mcp_install.install(rec, facts["review_id"])
    facts = mcp_install.review(rec)
    rec = mcp_registry.update(
        rec["id"],
        {"source": {"kind": "npm", "package": NPM_PKG, "version": "1.2.4"}},
        rec["updated_at"],
    )
    with pytest.raises(mcp_install.InstallError, match="source changed"):
        mcp_install.install(rec, facts["review_id"])


def test_an_install_belongs_to_its_source_not_to_a_settings_revision(home, fake_npm):
    rec = _npm_server()
    installed = mcp_install.install(rec, mcp_install.review(rec)["review_id"])
    rec = mcp_registry.set_installed(rec["id"], installed)
    rec = mcp_registry.update(
        rec["id"],
        {"settings": [{"key": "NC_HOST", "value": "https://cloud.example"}]},
        rec["updated_at"],
    )
    assert mcp_install.command(rec)[0] == "/usr/bin/node"  # a settings edit keeps the install
    rec = mcp_registry.update(
        rec["id"],
        {"source": {"kind": "npm", "package": NPM_PKG, "version": "1.2.4"}},
        rec["updated_at"],
    )
    with pytest.raises(mcp_registry.RegistryError, match="not installed"):
        mcp_install.command(rec)
    with pytest.raises(mcp_registry.Conflict):
        mcp_registry.set_installed(rec["id"], installed)  # an install for the old source


def test_the_tools_inherit_no_proxy_registry_or_credential_settings(home, monkeypatch, tmp_path):
    for k in ("HTTPS_PROXY", "NPM_TOKEN", "UV_INDEX_URL", "PIP_INDEX_URL", "npm_config_registry"):
        monkeypatch.setenv(k, "leak")
    monkeypatch.setenv("NO_PROXY", "*")
    env = mcp_install._env(tmp_path, "http://127.0.0.1:9")
    assert env["HOME"] == str(tmp_path)
    assert env["npm_config_registry"] == mcp_install.NPM_REGISTRY
    assert Path(env["npm_config_userconfig"]).read_text() == "" and env["UV_NO_CONFIG"] == "1"
    assert env["npm_config_userconfig"] != env["npm_config_globalconfig"]
    assert not {"NPM_TOKEN", "UV_INDEX_URL", "PIP_INDEX_URL"} & set(env)
    # The ONLY egress is BattleLab's own proxy (`mcp_egress`), for every tool and for git.
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        assert env[k] == "http://127.0.0.1:9", k
    assert env["npm_config_proxy"] == env["npm_config_https_proxy"] == "http://127.0.0.1:9"
    assert env["NO_PROXY"] == env["no_proxy"] == env["npm_config_noproxy"] == ""
    assert env["GIT_ALLOW_PROTOCOL"] == "https" and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert mcp_install._allowed("npm") == {("registry.npmjs.org", 443)}
    assert mcp_install._allowed("pypi") == {("pypi.org", 443), ("files.pythonhosted.org", 443)}


def test_pypi_hashed_requirements_yield_the_packages_digest():
    text = (
        "anyio==4.6.0 \\\n    --hash=sha256:" + "a" * 64 + "\n"
        "nextcloud-mcp-server==0.213.3 \\\n    --hash=sha256:" + "b" * 64 + " \\\n"
        "    --hash=sha256:" + "c" * 64 + "\n"
    )
    assert mcp_install._first_hash(text, "nextcloud-mcp-server") == "sha256:" + "b" * 64


def test_a_pypi_package_command_is_read_from_its_own_dist_info(tmp_path):
    site = tmp_path / "venv" / "lib" / "python3.13" / "site-packages"
    own = site / "nextcloud_mcp_server-0.213.3.dist-info"
    other = site / "nextcloud_client-0.213.3.dist-info"
    own.mkdir(parents=True)
    other.mkdir()
    (own / "entry_points.txt").write_text(
        "[console_scripts]\nnextcloud-mcp-server = nextcloud_mcp_server.cli:cli\n\n"
        "[nextcloud_mcp_server.plugins]\nsar = nextcloud_mcp_server.sar_plugin:plugin\n"
    )
    venv = tmp_path / "venv"
    assert mcp_install._console_scripts(venv, "nextcloud-mcp-server") == ["nextcloud-mcp-server"]
    assert mcp_install._console_scripts(venv, "Nextcloud.MCP_Server") == ["nextcloud-mcp-server"]
    assert mcp_install._console_scripts(venv, "nextcloud-client") == []
    assert mcp_install._console_scripts(venv, "nextcloud") == []


# ---- real packages (network; opt-in) ------------------------------------------------------------

network = pytest.mark.skipif(
    os.environ.get("AGENT_SESSIONS_TEST_MCP_NETWORK") != "1",
    reason="installs real packages from npm and PyPI (set AGENT_SESSIONS_TEST_MCP_NETWORK=1)",
)


@network
def test_a_real_pypi_server_installs_from_wheels_with_hashes(home):
    rec = mcp_registry.create(
        {
            "name": "nextcloud",
            "source": {"kind": "pypi", "package": "nextcloud-mcp-server", "version": "0.213.3"},
            "args": ["run", "--transport", "stdio"],
        }
    )
    facts = mcp_install.review(rec)
    assert facts["integrity"].startswith("sha256:") and facts["dependencies"] > 0
    installed = mcp_install.install(rec, facts["review_id"])
    assert installed["entrypoint"] == "nextcloud-mcp-server"
    assert os.access(installed["command"], os.X_OK)


# ---- egress: the registry and nothing else (Hermes on #2183) ---------------------------------


class _Counting:
    """A plain HTTP origin that counts every connection it accepts."""

    def __init__(self, routes=None):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.hits: list[str] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits.append(self.path)
                status, headers, body = (routes or {}).get(self.path.split("?")[0], (404, {}, b""))
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def _via_proxy(proxy_url: str, raw: bytes) -> bytes:
    import socket

    host, port = proxy_url.rsplit("//", 1)[1].split(":")
    with socket.create_connection((host, int(port)), timeout=5) as s:
        s.sendall(raw)
        out = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return out
            out += chunk
            if b"\r\n\r\n" in out and out.startswith(b"HTTP/1.1 403"):
                return out


def test_the_egress_proxy_refuses_every_other_host_before_connecting():
    from agent_sessions.mcp_egress import EgressProxy

    allowed, other = _Counting({"/ok": (200, {}, b"fine")}), _Counting()
    try:
        with EgressProxy({("127.0.0.1", allowed.port)}) as proxy:
            ok = _via_proxy(
                proxy.url,
                f"GET http://127.0.0.1:{allowed.port}/ok HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{allowed.port}\r\n\r\n".encode(),
            )
            assert ok.startswith(b"HTTP/1.0 200") and ok.endswith(b"fine")
            for raw in (
                f"GET http://127.0.0.1:{other.port}/private.tgz HTTP/1.1\r\n\r\n",
                f"CONNECT 127.0.0.1:{other.port} HTTP/1.1\r\n\r\n",
                f"GET https://127.0.0.1:{allowed.port}/ok HTTP/1.1\r\n\r\n",
                "CONNECT internal.example:443 HTTP/1.1\r\n\r\n",
            ):
                assert _via_proxy(proxy.url, raw.encode()).startswith(b"HTTP/1.1 403"), raw
            assert len(proxy.refused) == 4
        assert other.hits == [] and allowed.hits == ["/ok"]
    finally:
        allowed.close()
        other.close()


def _packument(name: str, registry: str, deps: dict | None = None) -> bytes:
    import base64
    import hashlib as h

    integrity = "sha512-" + base64.b64encode(h.sha512(name.encode()).digest()).decode()
    version = {
        "name": name,
        "version": "1.0.0",
        "dependencies": deps or {},
        "bin": {name: "index.js"},
        "dist": {"tarball": f"{registry}{name}/-/{name}-1.0.0.tgz", "integrity": integrity},
    }
    doc = {"name": name, "dist-tags": {"latest": "1.0.0"}, "versions": {"1.0.0": version}}
    return json.dumps(doc).encode()


@pytest.mark.skipif(
    not (shutil.which("npm") and shutil.which("node")), reason="needs a real npm on PATH"
)
def test_a_real_npm_review_reaches_only_its_registry(home, monkeypatch):
    """Hermes on #2183: npm fetched a URL dependency named in a published package's metadata
    while resolving, before the lock check. Through the egress proxy the private origin is never
    reached: a direct URL dependency, a transitive one, and a redirect off the registry."""
    private = _Counting({"/private.tgz": (200, {}, b"x")})
    routes: dict = {}
    registry = _Counting(routes)  # reads ``routes`` per request: filled once its port is known
    reg = f"http://127.0.0.1:{registry.port}/"
    evil = f"http://127.0.0.1:{private.port}/private.tgz"
    for name, deps in (
        ("honest", None),
        ("direct", {"evil": evil}),
        ("transitive", {"middle": "1.0.0"}),
        ("middle", {"evil": evil}),
        ("redirected", {"hop": "1.0.0"}),
    ):
        routes[f"/{name}"] = (
            200,
            {"Content-Type": "application/json"},
            _packument(name, reg, deps),
        )
    routes["/hop"] = (302, {"Location": f"http://127.0.0.1:{private.port}/hop"}, b"")
    monkeypatch.setattr(mcp_install, "NPM_REGISTRY", reg)
    try:
        honest = mcp_registry.create(
            {"name": "honest", "source": {"kind": "npm", "package": "honest", "version": "1.0.0"}}
        )
        facts = mcp_install.review(honest)  # the proxy forwards to the registry itself
        assert facts["entrypoint"] == "honest"
        said = {}
        for name in ("direct", "transitive", "redirected"):
            rec = mcp_registry.create(
                {"name": name, "source": {"kind": "npm", "package": name, "version": "1.0.0"}}
            )
            with pytest.raises(mcp_install.InstallError) as refused:
                mcp_install.review(rec)
            said[name] = str(refused.value)
        assert private.hits == []  # never reached: no URL dependency, no redirect target
        assert all("not from its registry" in w for w in said.values()), said
    finally:
        registry.close()
        private.close()
