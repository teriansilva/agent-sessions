"""An MCP server from a Go repository link (#2427): resolved by the Go module proxy's commit, built
from it with BattleLab's own toolchain.

The Go tool is a FAKE here (a script installed as the current toolchain, answering the exact
subcommands BattleLab runs from a fixture file, and recording each call's argv + environment), so
every decision BattleLab makes on its answers is pinned without the network. The toolchain's own
download is pinned against a local fixture origin standing in for go.dev / dl.google.com. A real
end-to-end run (the Codeberg link → v3.3.0 → a built binary that answers the MCP handshake) is
opt-in: ``AGENT_SESSIONS_TEST_MCP_NETWORK=1``.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agent_sessions import mcp_gitsource, mcp_gotool, mcp_install, mcp_registry, skill_repo
from test_skill_repo import _git, local, make_repo  # noqa: F401 — the fixture is used by name

LINK = "https://github.com/owner/repo"
MODULE = "example.org/acme/mcp/v2"
HOME_URL = "https://example.org/acme/mcp"
SUM = "h1:" + "A" * 43 + "="
GO_MOD = f"module {MODULE}\n\ngo 1.26.0\n\nrequire example.org/dep v1.0.0\n"
TOOLCHAIN = {"version": "1.30.0", "sha256": "f" * 64, "platform": "linux/amd64"}
#: What the checksum database vouches for (the wrapper's go.sum is written from it).
SUMDB = {
    "example.org/dep v1.0.0": "h1:" + "D" * 43 + "=",
    "example.org/dep v1.0.0/go.mod": "h1:" + "E" * 43 + "=",
}

FAKE_GO = r"""#!{python} -I
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
root = os.path.dirname(os.path.dirname(here))
cfg = json.load(open(os.path.join(root, "fake.json")))
args = sys.argv[1:]
with open(os.path.join(root, "calls.jsonl"), "a") as log:
    log.write(json.dumps({{"args": args, "env": dict(os.environ), "cwd": os.getcwd()}}) + "\n")
def out(doc):
    print(json.dumps(doc, indent=1)); sys.exit(1 if doc.get("Error") else 0)
if cfg.get("sleep_on") and args[:len(cfg["sleep_on"])] == cfg["sleep_on"]:
    import subprocess, time
    child = subprocess.Popen(["sleep", "300"])
    open(os.path.join(root, "child.pid"), "w").write(str(child.pid))
    time.sleep(300)
if args[:3] == ["list", "-m", "-json"]:
    out(cfg["list_m"])
if args[:3] == ["mod", "download", "-json"]:
    out(cfg["download"])
if args == ["mod", "download", "all"]:
    with open(os.path.join(os.getcwd(), "go.sum"), "w") as f:
        for key, value in cfg["sumdb"].items():
            f.write(key + " " + value + "\n")
    sys.exit(0)
if args[:3] == ["mod", "edit", "-json"]:
    text = open(args[3]).read()
    doc = {{"Module": {{"Path": cfg["module"]}}, "Go": cfg.get("go", "1.26.0")}}
    if "replace" in text: doc["Replace"] = [{{"Old": {{"Path": "x"}}, "New": {{"Path": "../x"}}}}]
    if "exclude" in text: doc["Exclude"] = [{{"Path": "y", "Version": "v1.0.0"}}]
    out(doc)
if args[:2] == ["list", "-f"]:
    print("\n".join(cfg["mains"])); sys.exit(0)
if args[:3] == ["list", "-m", "all"]:
    deps = ["dep%d v1.0.0" % i for i in range(cfg["deps"])]
    print("\n".join([cfg["module"]] + deps)); sys.exit(0)
if args[0] == "install":
    if cfg.get("sleep"):
        import subprocess, time
        child = subprocess.Popen(["sleep", "300"])
        open(os.path.join(root, "child.pid"), "w").write(str(child.pid))
        time.sleep(300)
    if cfg.get("install_fail"):
        sys.stderr.write(cfg["install_fail"]); sys.exit(1)
    parts = args[-1].split("@")[0].split("/")
    major = len(parts) > 1 and parts[-1][:1] == "v" and parts[-1][1:].isdigit()
    target = parts[-2] if major else parts[-1]
    path = os.path.join(os.environ["GOBIN"], target)
    open(path, "w").write("#!/bin/sh\necho fake-server\n"); os.chmod(path, 0o755); sys.exit(0)
if args[0] == "clean":
    sys.exit(0)
sys.stderr.write("fake go: unexpected " + " ".join(args)); sys.exit(2)
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_SESSIONS_MCP_SERVERS", str(tmp_path / "mcp-servers.json"))
    return tmp_path


@pytest.fixture
def fake_go(home, tmp_path):
    """A fake toolchain as the current one; returns its config (mutable) and its call log."""
    base = mcp_gotool.root()
    gobin = base / f"go{TOOLCHAIN['version']}" / "bin"
    gobin.mkdir(parents=True)
    (gobin / "go").write_text(FAKE_GO.format(python=sys.executable))
    (gobin / "go").chmod(0o755)
    (base / "current").write_text(json.dumps(TOOLCHAIN))
    moddir = tmp_path / "moddir"
    moddir.mkdir()
    (moddir / "go.mod").write_text(GO_MOD)
    cfg = {
        "module": MODULE,
        "list_m": {
            "Path": MODULE,
            "Version": "v2.1.0",
            "GoVersion": "1.26.0",
            "Time": "2026-10-10T17:51:43Z",
            "Origin": {"VCS": "git", "URL": HOME_URL, "Hash": "", "Ref": "refs/tags/v2.1.0"},
        },
        "download": {
            "Path": MODULE,
            "Version": "v2.1.0",
            "Sum": SUM,
            "GoMod": str(moddir / "go.mod"),
            "Dir": str(moddir),
        },
        "mains": [MODULE],
        "deps": 74,
        "sumdb": dict(SUMDB),
    }
    (moddir / "go.sum").write_text("".join(f"{k} {v}\n" for k, v in SUMDB.items()))

    def save():
        (base / "fake.json").write_text(json.dumps(cfg))

    save()
    moddir.chmod(0o555)  # read-only, like a module in the real module cache

    def calls() -> list[dict]:
        p = base / "calls.jsonl"
        return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

    return {"cfg": cfg, "save": save, "calls": calls, "moddir": moddir}


@pytest.fixture
def repos(home, monkeypatch, local):  # noqa: F811
    made: dict[str, object] = {}

    def build(files: dict, url: str = LINK):
        srv = make_repo(home / f"repo{len(made)}", files)
        made[skill_repo.normalize(url).lower()] = srv
        return srv

    def fetch(url, cancel):
        return skill_repo.fetch(f"file://{made[skill_repo.normalize(url).lower()]}", cancel=cancel)

    monkeypatch.setattr(mcp_gitsource, "_fetch", fetch)
    return build


def _write_module_file(moddir: Path, name: str, text: str) -> None:
    moddir.chmod(0o755)
    (moddir / name).write_text(text)
    moddir.chmod(0o555)


def _head(srv) -> str:
    return _git(srv, "rev-parse", "HEAD").strip()


# ---- resolve ------------------------------------------------------------------------------------


def test_a_go_repository_resolves_to_the_module_at_exactly_its_commit(repos, fake_go):
    srv = repos({"go.mod": GO_MOD, "main.go": "package main\nfunc main() {}\n"})
    fake_go["cfg"]["list_m"]["Origin"]["Hash"] = _head(srv)
    fake_go["save"]()
    out = mcp_gitsource.resolve(LINK)
    assert out["source"] == {"kind": "go", "package": MODULE, "version": "v2.1.0"}
    assert out["origin"] == {"url": LINK, "commit": _head(srv)}
    rel = out["facts"]["release"]
    assert rel["module"] == MODULE and rel["version"] == "v2.1.0"
    assert rel["home"] == "example.org/acme/mcp" and rel["mirror"] is True  # the link is a mirror
    assert rel["same_commit"] is True and rel["go"] == "1.26.0"
    (call,) = fake_go["calls"]()
    assert call["args"] == ["list", "-m", "-json", f"{MODULE}@{_head(srv)}"]


def test_a_repository_whose_go_mod_names_another_module_never_matches(repos, fake_go):
    """A lookalike: its go.mod names a real module, but the proxy's module is at another commit."""
    repos({"go.mod": GO_MOD})
    fake_go["cfg"]["list_m"]["Origin"]["Hash"] = "0" * 40
    fake_go["save"]()
    with pytest.raises(mcp_gitsource.LinkRefused) as e:
        mcp_gitsource.resolve(LINK)
    assert e.value.code == "provenance" and "not that module's code" in str(e.value)


def test_a_commit_the_proxy_does_not_serve_is_refused_in_words(repos, fake_go):
    repos({"go.mod": GO_MOD})
    fake_go["cfg"]["list_m"] = {"Path": MODULE, "Error": "unknown revision deadbeef"}
    fake_go["save"]()
    with pytest.raises(mcp_gitsource.LinkRefused) as e:
        mcp_gitsource.resolve(LINK)
    assert e.value.code == "unpublished" and "isn't in the module's own repository" in str(e.value)


@pytest.mark.parametrize(
    "files, code, words",
    [
        (
            {"go.mod": GO_MOD, "package.json": json.dumps({"name": "x", "version": "1.0.0"})},
            "both_manifests",
            "more than one kind of package (package.json and go.mod)",
        ),
        ({"go.mod": "go 1.26.0\n"}, "no_name", "go.mod names no package"),
        ({"go.mod": "module ./local/thing\n"}, "manifest", "not a module path"),
        ({"README.md": "x"}, "no_manifest", "and no go.mod"),
    ],
)
def test_a_go_repository_that_declares_no_single_module_is_refused(
    repos, fake_go, files, code, words
):
    repos(files)
    with pytest.raises(mcp_gitsource.LinkRefused) as e:
        mcp_gitsource.resolve(LINK)
    assert e.value.code == code and words in str(e.value)
    assert fake_go["calls"]() == []  # refused before the proxy is asked


# ---- review -------------------------------------------------------------------------------------


def _rec(sid: str = "b" * 16) -> dict:
    return {"id": sid, "source": {"kind": "go", "package": MODULE, "version": "v2.1.0"}}


def test_a_go_review_shows_what_will_be_built_and_pins_the_compiler(fake_go):
    facts = mcp_install.review(_rec())
    assert facts["integrity"] == SUM and facts["dependencies"] == 74
    # go install names the binary after the path, skipping the major suffix: …/mcp/v2 → mcp
    assert facts["command"] == MODULE and facts["entrypoint"] == "mcp"
    assert facts["go"] == "1.26.0" and facts["toolchain"] == TOOLCHAIN
    assert facts["install_scripts_declared"] == [] and facts["registry"] == "proxy.golang.org"
    lock = json.loads(
        (mcp_install._staging("b" * 16, facts["review_id"]) / "go.lock.json").read_text()
    )
    assert lock == {"module": MODULE, "version": "v2.1.0", "sum": SUM, "command": MODULE,
                    "toolchain": TOOLCHAIN}  # fmt: skip


@pytest.mark.parametrize(
    "directive", ["replace example.org/dep => ../dep", "exclude example.org/dep v1.0.0"]
)
def test_a_go_mod_go_install_refuses_is_refused_at_review(fake_go, directive):
    _write_module_file(fake_go["moddir"], "go.mod", GO_MOD + directive + "\n")
    with pytest.raises(mcp_install.InstallError, match="replaces or excludes modules"):
        mcp_install.review(_rec())
    assert not any(c["args"][:2] == ["list", "-f"] for c in fake_go["calls"]())  # nothing listed


@pytest.mark.parametrize(
    "mains, wanted, picked, refused",
    [
        ([f"{MODULE}/cmd/server"], None, f"{MODULE}/cmd/server", None),
        ([f"{MODULE}/cmd/server", f"{MODULE}/cmd/migrate"], None, None, "several commands"),
        (
            [f"{MODULE}/cmd/server", f"{MODULE}/cmd/migrate"],
            "migrate",
            f"{MODULE}/cmd/migrate",
            None,
        ),
        ([], None, None, "builds no command"),
        ([MODULE, f"{MODULE}/hack/gen"], None, MODULE, None),
    ],
)
def test_a_go_review_builds_exactly_one_command(fake_go, mains, wanted, picked, refused):
    fake_go["cfg"]["mains"] = mains
    fake_go["save"]()
    rec = _rec()
    if wanted:
        rec["source"]["entrypoint"] = wanted
    if refused:
        with pytest.raises(mcp_install.InstallError, match=refused):
            mcp_install.review(rec)
    else:
        assert mcp_install.review(rec)["command"] == picked


def test_a_go_work_in_the_repository_never_widens_the_graph(fake_go):
    """``go install pkg@version`` never uses a workspace; neither does any Go step here."""
    _write_module_file(fake_go["moddir"], "go.work", "go 1.26\nuse ../elsewhere\n")
    mcp_install.review(_rec())
    assert {c["env"].get("GOWORK") for c in fake_go["calls"]()} == {"off"}


# ---- install ------------------------------------------------------------------------------------


def test_a_go_install_builds_exactly_the_reviewed_module_in_a_bounded_env(fake_go, monkeypatch):
    hostile = {"GOFLAGS": "-mod=mod", "GOPRIVATE": "*", "GONOSUMDB": "*", "GOPROXY": "direct",
               "GOTOOLCHAIN": "auto", "CGO_ENABLED": "1", "GITHUB_TOKEN": "leak"}  # fmt: skip
    for k, v in hostile.items():
        monkeypatch.setenv(k, v)
    rec = _rec()
    facts = mcp_install.review(rec)
    inst = mcp_install.install(rec, facts["review_id"])
    assert Path(inst["command"]).name == "mcp" and Path(inst["command"]).parent.name == "bin"
    assert inst["args"] == [] and inst["integrity"] == SUM
    (build,) = [c for c in fake_go["calls"]() if c["args"][0] == "install"]
    assert build["args"] == ["install", "-p", "2", "-trimpath", f"{MODULE}@v2.1.0"]
    env = build["env"]
    for k, v in {"GOFLAGS": "-mod=readonly", "CGO_ENABLED": "0", "GOTOOLCHAIN": "local",
                 "GOVCS": "off", "GOWORK": "off", "GOENV": "off", "GOPROXY": "https://proxy.golang.org",
                 "GOSUMDB": "sum.golang.org", "GOPRIVATE": "", "GONOSUMDB": "", "GOINSECURE": "",
                 "GOMAXPROCS": "2", "GOMEMLIMIT": "1GiB"}.items():  # fmt: skip
        assert env.get(k) == v, k
    assert "GITHUB_TOKEN" not in env
    assert env["HTTPS_PROXY"].startswith("http://127.0.0.1:")  # the egress proxy, nothing else
    assert mcp_install._allowed("go") == {("proxy.golang.org", 443), ("sum.golang.org", 443)}


def test_a_go_install_refuses_a_different_checksum(fake_go):
    rec = _rec()
    facts = mcp_install.review(rec)
    fake_go["cfg"]["download"]["Sum"] = "h1:" + "B" * 43 + "="
    fake_go["save"]()
    with pytest.raises(mcp_install.InstallError, match="checksum differs"):
        mcp_install.install(rec, facts["review_id"])
    assert not any(c["args"][0] == "install" for c in fake_go["calls"]())


def test_a_toolchain_refreshed_after_the_review_needs_a_new_review(fake_go):
    rec = _rec()
    facts = mcp_install.review(rec)
    (mcp_gotool.root() / "current").write_text(json.dumps({**TOOLCHAIN, "sha256": "e" * 64}))
    with pytest.raises(mcp_install.InstallError, match="toolchain changed since the review"):
        mcp_install.install(rec, facts["review_id"])
    assert not any(c["args"][0] == "install" for c in fake_go["calls"]())


def test_a_build_needing_cgo_is_refused_in_words(fake_go):
    rec = _rec()
    facts = mcp_install.review(rec)
    fake_go["cfg"]["install_fail"] = "package x: build constraints exclude all Go files in /x"
    fake_go["save"]()
    with pytest.raises(mcp_install.InstallError, match="needs cgo"):
        mcp_install.install(rec, facts["review_id"])


# ---- checksum database, timeouts, cache recovery (#2427 round 2) -------------------------------


def test_a_go_sum_that_disagrees_with_the_checksum_database_is_refused(fake_go):
    """The module's own go.sum is never the evidence: a dependency hash it holds that differs from
    the database's (recorded in a wrapper module with an empty go.sum) refuses the review."""
    forged = {**SUMDB, "example.org/dep v1.0.0": "h1:" + "X" * 43 + "="}
    _write_module_file(
        fake_go["moddir"], "go.sum", "".join(f"{k} {v}\n" for k, v in forged.items())
    )
    with pytest.raises(mcp_install.InstallError, match="doesn't match the Go checksum database"):
        mcp_install.review(_rec())
    wrapper_runs = [c for c in fake_go["calls"]() if c["args"] == ["mod", "download", "all"]]
    assert wrapper_runs and Path(wrapper_runs[0]["cwd"]).name == "wrapper"
    assert wrapper_runs[0]["env"]["GOSUMDB"] == "sum.golang.org"
    assert not any(c["args"][:2] == ["list", "-f"] for c in fake_go["calls"]())


def test_a_timed_out_build_leaves_no_process_no_lock_and_no_directory(fake_go, monkeypatch):
    import fcntl

    rec = _rec()
    facts = mcp_install.review(rec)
    fake_go["cfg"]["sleep"] = True
    fake_go["save"]()
    monkeypatch.setattr(mcp_install, "GO_STEP_TIMEOUT_S", 3)
    with pytest.raises(mcp_install.InstallError, match="took longer than 3 s"):
        mcp_install.install(rec, facts["review_id"])
    child = int((mcp_gotool.root() / "child.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)  # the build's own child went with its process group
    assert not (mcp_install.root() / rec["id"] / facts["review_id"]).exists()
    for name in (".build.lock", ".lock"):
        fd = os.open(mcp_gotool.root() / name, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # free again
        finally:
            os.close(fd)


def test_the_go_caches_are_recovered_after_a_failed_job_too(fake_go, monkeypatch):
    rec = _rec()
    facts = mcp_install.review(rec)
    monkeypatch.setattr(mcp_install, "GO_CACHE_CEILING", 0)
    (mcp_install.root() / ".cache" / "go" / "build").mkdir(parents=True, exist_ok=True)
    (mcp_install.root() / ".cache" / "go" / "build" / "x").write_text("cached")
    fake_go["cfg"]["install_fail"] = "something broke"
    fake_go["save"]()
    with pytest.raises(mcp_install.InstallError):
        mcp_install.install(rec, facts["review_id"])
    assert any(c["args"][0] == "clean" for c in fake_go["calls"]())


# ---- the registry -------------------------------------------------------------------------------


def test_a_go_source_is_never_typed_in_only_resolved(home):
    with pytest.raises(mcp_registry.RegistryError, match="only from its repository link"):
        mcp_registry.create({"name": "fj", "source": _rec()["source"]})
    rec = mcp_registry.create(
        {"name": "fj"},
        resolution={
            "id": "c" * 16,
            "source": _rec()["source"],
            "origin": {"url": LINK, "commit": "1" * 40},
        },  # fmt: skip
    )
    assert rec["source"]["kind"] == "go" and rec["transport"] == "stdio"
    # Its own source resent unchanged is not a change; a typed-in new version is refused.
    mcp_registry.update(rec["id"], {"source": rec["source"], "label": "Forgejo"}, rec["updated_at"])
    rec = mcp_registry.get(rec["id"])
    with pytest.raises(mcp_registry.RegistryError, match="only from its repository link"):
        mcp_registry.update(
            rec["id"], {"source": {**rec["source"], "version": "v2.2.0"}}, rec["updated_at"]
        )


@pytest.mark.parametrize(
    "source, words",
    [
        ({"kind": "go", "package": "not a module", "version": "v1.0.0"}, "module path"),
        ({"kind": "go", "package": MODULE, "version": "latest"}, "exact Go module version"),
        ({"kind": "go", "package": MODULE, "version": "main"}, "exact Go module version"),
    ],
)
def test_only_an_exact_go_module_version_is_admitted(source, words):
    with pytest.raises(mcp_registry.RegistryError, match=words):
        mcp_install.validate_source(source)


# ---- the toolchain ------------------------------------------------------------------------------


def _tarball(version: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"#!/bin/sh\necho go version go" + version.encode() + b"\n"
        info = tarfile.TarInfo("go/bin/go")
        info.size, info.mode = len(data), 0o755
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def go_dev(home, monkeypatch):
    """A local stand-in for go.dev's release list and dl.google.com's files."""
    state: dict = {"releases": [], "files": {}, "hits": []}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            state["hits"].append(self.path)
            if self.path.startswith("/dl/"):
                body = json.dumps(state["releases"]).encode()
            elif self.path.startswith("/go/") and self.path[4:] in state["files"]:
                body = state["files"][self.path[4:]]
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setattr(mcp_gotool, "DL_INDEX", f"{base}/dl/?mode=json")
    monkeypatch.setattr(mcp_gotool, "DL_FILES", f"{base}/go/")
    monkeypatch.setattr(mcp_gotool, "TOOL_HOSTS", frozenset({("127.0.0.1", srv.server_address[1])}))
    monkeypatch.setattr(mcp_gotool, "_arch", lambda: "amd64")

    def publish(version: str, sha: str | None = None):
        name = f"go{version}.linux-amd64.tar.gz"
        data = _tarball(version)
        state["files"][name] = data
        state["releases"].insert(0, {
            "version": f"go{version}", "stable": True,
            "files": [{"filename": name, "os": "linux", "arch": "amd64", "kind": "archive",
                       "sha256": sha or hashlib.sha256(data).hexdigest(), "size": len(data)}],
        })  # fmt: skip

    yield {"publish": publish, "state": state}
    srv.shutdown()
    srv.server_close()


def test_the_toolchain_is_downloaded_verified_frozen_and_identified(go_dev):
    go_dev["publish"]("1.27.4")
    go = mcp_gotool.ensure("1.26.0")
    assert go == mcp_gotool.root() / "go1.27.4" / "bin" / "go" and os.access(go, os.X_OK)
    ident = mcp_gotool.identity()
    assert ident["version"] == "1.27.4" and ident["platform"] == "linux/amd64"
    assert (
        ident["sha256"]
        == hashlib.sha256(go_dev["state"]["files"]["go1.27.4.linux-amd64.tar.gz"]).hexdigest()
    )
    assert not os.access(go.parent, os.W_OK)  # frozen read-only
    hits = len(go_dev["state"]["hits"])
    assert mcp_gotool.ensure("1.27") == go and len(go_dev["state"]["hits"]) == hits  # reused


def test_a_tampered_toolchain_is_refused_and_nothing_is_kept(go_dev):
    go_dev["publish"]("1.27.4", sha="0" * 64)
    with pytest.raises(mcp_gotool.ToolchainError, match="does not match go.dev's checksum"):
        mcp_gotool.ensure(None)
    assert mcp_gotool.identity() == {}
    assert not list(mcp_gotool.root().glob("go*")) and not list(mcp_gotool.root().glob("fetch-*"))


def test_a_newer_go_directive_refreshes_once_and_is_refused_beyond_the_newest(go_dev):
    go_dev["publish"]("1.26.1")
    assert mcp_gotool.identity() == {} and mcp_gotool.ensure(None).parent.parent.name == "go1.26.1"
    go_dev["publish"]("1.27.4")
    assert mcp_gotool.ensure("1.27.0").parent.parent.name == "go1.27.4"
    assert not (mcp_gotool.root() / "go1.26.1").exists()  # one current toolchain
    with pytest.raises(mcp_gotool.ToolchainError, match="newer than the newest stable release"):
        mcp_gotool.ensure("1.99.0")


def test_the_toolchain_reads_reach_only_its_two_hosts():
    assert mcp_gotool.TOOL_HOSTS == {("go.dev", 443), ("dl.google.com", 443)}
    with pytest.raises(mcp_gotool.ToolchainError, match="not allowed"):
        mcp_gotool._get("https://evil.example/x", lambda b: None, 10, 1.0)


# ---- the real thing (network; opt-in, never in CI) ----------------------------------------------


@pytest.mark.skipif(
    os.environ.get("AGENT_SESSIONS_TEST_MCP_NETWORK") != "1",
    reason="downloads Go and builds a real server (set AGENT_SESSIONS_TEST_MCP_NETWORK=1)",
)
@pytest.mark.timeout(1800)  # a cold toolchain download plus a cold build: ~8 min
def test_the_codeberg_forgejo_mcp_link_resolves_builds_and_runs(home):
    out = mcp_gitsource.resolve("https://codeberg.org/goern/forgejo-mcp")
    assert out["source"]["kind"] == "go"
    assert out["source"]["package"] == "git.b4mad.industries/agentic-forges/forgejo-mcp/v3"
    assert out["facts"]["release"]["mirror"] is True
    rec = {"id": "d" * 16, "source": out["source"]}
    facts = mcp_install.review(rec)
    assert facts["integrity"].startswith("h1:") and facts["entrypoint"] == "forgejo-mcp"
    inst = mcp_install.install(rec, facts["review_id"])
    assert os.access(inst["command"], os.X_OK)
    # The built server speaks MCP: the handshake and its tool list, over stdio, against a local
    # stand-in forge (forgejo-mcp tests its instance's /api/v1/version at startup).
    import subprocess

    class Forge(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"version": "15.0.3"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    forge = ThreadingHTTPServer(("127.0.0.1", 0), Forge)
    threading.Thread(target=forge.serve_forever, daemon=True).start()
    argv = [
        inst["command"],
        *inst["args"],
        "-t",
        "stdio",
        "--url",
        f"http://127.0.0.1:{forge.server_port}",
    ]
    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(home)},
    )  # fmt: skip
    try:

        def send(doc):
            proc.stdin.write(json.dumps(doc) + "\n")
            proc.stdin.flush()

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "BattleLab check", "version": "1"}}})  # fmt: skip
        info = json.loads(proc.stdout.readline())["result"]["serverInfo"]
        assert info["version"] == "v3.3.0"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = json.loads(proc.stdout.readline())["result"]["tools"]
        assert {"list_repo_issues", "create_issue"} <= {t["name"] for t in tools}
    finally:
        proc.kill()
        proc.wait()
        forge.shutdown()


def test_a_go_job_waits_for_another_then_gives_up_in_words(fake_go, monkeypatch):
    import fcntl

    monkeypatch.setattr(mcp_gotool, "JOB_WAIT_S", 1.0)
    fd = os.open(mcp_gotool.root() / ".build.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # another build holds it
        with pytest.raises(mcp_gotool.ToolchainError, match="another Go build is still running"):
            mcp_install.review(_rec())
    finally:
        os.close(fd)
    assert not list((mcp_install.root() / ("b" * 16)).glob("reviews/*"))  # nothing staged
    mcp_install.review(_rec())  # free again: it runs


# ---- Hermes round 1 on PR #2433: route review, Stop, v10+ majors ---------------------------------


@pytest.fixture
def go_api(repos, fake_go, auth_cfg):
    """The real app (routes + operations) over the fake toolchain, signed in."""
    from fastapi.testclient import TestClient

    from agent_sessions import mcp_jobs
    from agent_sessions.main import create_app

    with TestClient(create_app(auth_cfg), base_url="https://testserver") as client:
        client.post(
            "/login",
            data={"username": "marcus", "password": "hunter2"},
            headers={"Origin": auth_cfg.origin},
            follow_redirects=False,
        )
        csrf = client.get("/api/config").json()["csrf"]
        yield {"client": client, "h": {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}}
    mcp_jobs._reset_for_tests()


def _op(api, url, body, wait=20):
    import time
    import uuid

    rid = str(uuid.uuid4())
    r = api["client"].post(url, json={**body, "request_id": rid}, headers=api["h"])
    assert r.status_code == 202, r.text
    deadline = time.monotonic() + wait
    while True:
        op = api["client"].get(f"/api/mcp/operations/{rid}").json()
        if op["state"] != "running":
            return op
        assert time.monotonic() < deadline, op
        time.sleep(0.05)


def _go_server(sid="c" * 16):
    return mcp_registry.create(
        {"name": "fj"},
        resolution={
            "id": sid,
            "source": _rec()["source"],
            "origin": {"url": LINK, "commit": "1" * 40},
        },
    )


def test_a_go_server_is_reviewed_again_from_its_page_after_a_compiler_change(go_api):
    """The page's Review is the recovery path: an install refused because the reviewed compiler
    was replaced succeeds after a new review through the route."""
    sid = _go_server()["id"]
    base = f"/api/mcp/servers/{sid}"
    first = _op(go_api, f"{base}/review", {})
    assert first["state"] == "done", first
    (mcp_gotool.root() / "current").write_text(json.dumps({**TOOLCHAIN, "sha256": "e" * 64}))
    refused = _op(go_api, f"{base}/install", {"review_id": first["result"]["review_id"]})
    assert refused["state"] == "failed" and "toolchain changed" in refused["error"]
    again = _op(go_api, f"{base}/review", {})
    assert again["state"] == "done" and again["result"]["toolchain"]["sha256"] == "e" * 64
    done = _op(go_api, f"{base}/install", {"review_id": again["result"]["review_id"]})
    assert done["state"] == "done", done
    assert mcp_install.command(mcp_registry.get(sid))[0].endswith("/bin/mcp")


def _resolve_started(go_api, srv):
    import uuid

    fake = srv
    rid = str(uuid.uuid4())
    r = go_api["client"].post(
        "/api/mcp/servers/resolve-link", json={"url": LINK, "request_id": rid},
        headers=go_api["h"],
    )  # fmt: skip
    assert r.status_code == 202, r.text
    return rid, fake


def _locks_free():
    import fcntl

    for name in (".build.lock", ".lock"):
        fd = os.open(mcp_gotool.root() / name, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)


@pytest.mark.parametrize("step", [["list", "-m", "-json"], ["mod", "download", "-json"]])
def test_stop_during_a_go_step_kills_it_and_keeps_nothing(go_api, repos, fake_go, step):
    """Stop while the proxy lookup (resolve) or the review's download runs: the step's process
    group dies at once, the op fails as stopped, no staged review stays, both locks are free."""
    import time

    srv = repos({"go.mod": GO_MOD, "main.go": "package main\nfunc main() {}\n"})
    fake_go["cfg"]["list_m"]["Origin"]["Hash"] = _head(srv)
    fake_go["cfg"]["sleep_on"] = step
    fake_go["save"]()
    rid, _ = _resolve_started(go_api, srv)
    pid_file = mcp_gotool.root() / "child.pid"
    deadline = time.monotonic() + 20
    while not pid_file.exists():
        assert time.monotonic() < deadline, "the Go step never started"
        time.sleep(0.05)
    child = int(pid_file.read_text())
    t0 = time.monotonic()
    stopped = go_api["client"].post(f"/api/mcp/operations/{rid}/stop", headers=go_api["h"])
    assert stopped.status_code == 200
    while True:
        op = go_api["client"].get(f"/api/mcp/operations/{rid}").json()
        if op["state"] != "running":
            break
        assert time.monotonic() - t0 < 5, "Stop did not end the Go step promptly"
        time.sleep(0.05)
    assert op["state"] == "failed" and op["error"].startswith("stopped"), op
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)  # the step's own child went with its process group
    assert not list(mcp_install.root().glob("*/reviews/*"))  # nothing staged is kept
    _locks_free()


def test_stop_while_waiting_for_the_build_slot_ends_the_wait(go_api, repos, fake_go):
    import fcntl
    import time

    srv = repos({"go.mod": GO_MOD, "main.go": "package main\nfunc main() {}\n"})
    fake_go["cfg"]["list_m"]["Origin"]["Hash"] = _head(srv)
    fake_go["save"]()
    fd = os.open(mcp_gotool.root() / ".build.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # another Go build holds the slot
        rid, _ = _resolve_started(go_api, srv)
        deadline = time.monotonic() + 20
        while (
            go_api["client"].get(f"/api/mcp/operations/{rid}").json()["progress"].get("step")
            != "release"
        ):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        time.sleep(1.0)  # now waiting for the slot
        t0 = time.monotonic()
        go_api["client"].post(f"/api/mcp/operations/{rid}/stop", headers=go_api["h"])
        while True:
            op = go_api["client"].get(f"/api/mcp/operations/{rid}").json()
            if op["state"] != "running":
                break
            assert time.monotonic() - t0 < 5, "Stop did not end the wait for the build slot"
            time.sleep(0.05)
    finally:
        os.close(fd)
    assert op["state"] == "failed" and op["error"].startswith("stopped"), op
    assert not any(c["args"][:1] == ["list"] for c in fake_go["calls"]())  # never started


@pytest.mark.parametrize("major", ["v9", "v10", "v19", "v20"])
def test_a_root_command_of_any_major_is_named_without_its_suffix(fake_go, major):
    """``go install`` names a root command after the element before ``/vN`` for EVERY major
    >= 2, two digits included; review records it and install finds it."""
    module = f"example.org/acme/mcp/{major}"
    fake_go["cfg"]["module"] = module
    fake_go["cfg"]["mains"] = [module]
    for key in ("list_m", "download"):
        fake_go["cfg"][key]["Path"] = module
    fake_go["save"]()
    _write_module_file(fake_go["moddir"], "go.mod", GO_MOD.replace(MODULE, module))
    rec = {"id": "b" * 16, "source": {"kind": "go", "package": module, "version": "v2.1.0"}}
    facts = mcp_install.review(rec)
    assert facts["entrypoint"] == "mcp"
    inst = mcp_install.install(rec, facts["review_id"])
    assert inst["command"].endswith("/bin/mcp") and os.access(inst["command"], os.X_OK)
