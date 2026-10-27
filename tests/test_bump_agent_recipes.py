"""`scripts/bump-agent-recipes` (#1741): the daily catalog bump, offline.

Every registry and GitHub answer comes from an `httpx.MockTransport`; no test touches the network.
The real `Http` client, `feed.entry`, `artifacts.extract` and `npm_closure.validate` run unchanged.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).parents[1]
RECIPES = ROOT / "release" / "recipes" / "linux-x64"
NOW = dt.datetime(2026, 10, 27, 12, tzinfo=dt.UTC)
REG = "https://registry.npmjs.org"


def _load():
    loader = importlib.machinery.SourceFileLoader(
        "bump_agent_recipes", str(ROOT / "scripts" / "bump-agent-recipes")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module  # dataclasses resolve their module by name
    loader.exec_module(module)
    return module


bump = _load()


# -- fixtures --------------------------------------------------------------------------------


def _tgz(files: dict[str, bytes], *, root: str = "package/") -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz", format=tarfile.USTAR_FORMAT) as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(root + name)
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def npm_tgz(name: str, version: str, deps: dict | None = None, **extra) -> bytes:
    meta = {"name": name, "version": version}
    if deps:
        meta["dependencies"] = deps
    meta.update(extra.pop("meta", {}))
    files = {"package.json": json.dumps(meta).encode()}
    files.update(extra.pop("files", {}))
    return _tgz(files)


def sri(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


def ago(hours: float) -> str:
    return (NOW - dt.timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def tarball_url(name: str, version: str) -> str:
    return f"{REG}/{name}/-/{name.rsplit('/', 1)[-1]}-{version}.tgz"


def body(data: bytes, headers: dict | None = None) -> httpx.Response:
    # A streamed body, like the network's; `content=` would arrive already consumed.
    headers = {"content-length": str(len(data)), **(headers or {})}
    return httpx.Response(200, stream=httpx.ByteStream(data), headers=headers)


class Registry:
    """Packuments + tarballs + GitHub, served through the real `Http` client."""

    def __init__(self):
        self.docs: dict[str, dict] = {}
        self.blobs: dict[str, bytes] = {}
        self.extra: dict[str, httpx.Response] = {}
        self.requested: list[str] = []

    def publish(
        self,
        name: str,
        version: str,
        data: bytes,
        *,
        hours: float = 200,
        deps: dict | None = None,
        integrity: str | None = None,
        **meta,
    ) -> None:
        doc = self.docs.setdefault(
            name, {"name": name, "dist-tags": {}, "versions": {}, "time": {"created": ago(9999)}}
        )
        url = tarball_url(name, version)
        entry = {"name": name, "version": version, "dist": {"tarball": url}}
        entry["dist"]["integrity"] = integrity or sri(data)
        if deps:
            entry["dependencies"] = deps
        entry.update(meta)
        doc["versions"][version] = entry
        doc["time"][version] = ago(hours)
        self.blobs[url] = data

    def latest(self, name: str, version: str) -> None:
        self.docs[name]["dist-tags"]["latest"] = version

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        assert request.url.host != "api.github.com", "the anonymous REST API is never used"
        if url in self.extra:
            return self.extra[url]
        if url in self.blobs:
            return body(self.blobs[url])
        for name, doc in self.docs.items():
            if url == f"{REG}/{name.replace('/', '%2F')}":
                return body(json.dumps(doc).encode())
        return httpx.Response(404)

    def http(self):
        return bump.Http(transport=httpx.MockTransport(self.handler))


class Runner:
    def __init__(self, reported: str | None = None):
        self.calls = []
        self.reported = reported

    def __call__(self, prefix: Path, entrypoint: str, flag: str, expected: str) -> str:
        path = prefix / entrypoint
        assert path.is_file(), "the version runs from the extracted, isolated prefix"
        self.calls.append((path, flag, expected))
        if self.reported is not None and self.reported != expected:
            raise bump.BumpError(f"`{flag}` did not report {expected}")
        return expected


def recipe(source: str, package: str, artifacts: list[tuple[str, str, bytes]], version: str):
    """A real recipe's manifest, re-pinned to fixture artifacts (primary first)."""
    doc = json.loads((RECIPES / f"{source}.json").read_text())
    install = doc["manifest"]["install"]
    install["version"] = version
    out = [
        {"destination": dest, "sha256": hashlib.sha256(data).hexdigest(), "url": url}
        for dest, url, data in artifacts
    ]
    install["digest"] = "sha256:" + out[0]["sha256"]
    doc["manifest"]["identity"]["version"] = version.removesuffix("-linux-x64")
    doc["recipe"]["artifacts"] = out
    assert install["package"] == package
    return doc


def write(tmp_path: Path, name: str, doc: dict, indent: int = 2) -> Path:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(doc, indent=indent, ensure_ascii=False) + "\n")
    return path


def go(reg: Registry, recipes: Path, *, runner=None, cooldown=72, rebuilt=None):
    rebuilt = rebuilt if rebuilt is not None else []
    http = reg.http()
    try:
        bumper = bump.Bumper(http, now=NOW, cooldown_hours=cooldown, runner=runner or Runner())
        return bump.run(bumper, recipes, rebuild=lambda: rebuilt.append(True))
    finally:
        http.close()


CLAUDE = "@anthropic-ai/claude-code-linux-x64"
CLAUDE_BIN = {"files": {"claude": b"#!/bin/sh\n"}}


def claude_setup(tmp_path, reg, *, new_hours=100, integrity=None):
    old = npm_tgz(CLAUDE, "1.0.0", **CLAUDE_BIN)
    new = npm_tgz(CLAUDE, "1.0.1", **CLAUDE_BIN)
    reg.publish(CLAUDE, "1.0.0", old, hours=500)
    reg.publish(CLAUDE, "1.0.1", new, hours=new_hours, integrity=integrity)
    reg.latest(CLAUDE, "1.0.1")
    doc = recipe(
        "claude",
        CLAUDE,
        [(f"node_modules/{CLAUDE}", tarball_url(CLAUDE, "1.0.0"), old)],
        "1.0.0",
    )
    return write(tmp_path, "claude", doc), new


# -- version pick ----------------------------------------------------------------------------


def _bumper(reg: Registry, cooldown: float = 72):
    return bump.Bumper(reg.http(), now=NOW, cooldown_hours=cooldown, runner=Runner())


def test_npm_pick_is_latest_bounded_cooled_down_stable_and_not_deprecated():
    reg = Registry()
    blob = b"x"
    reg.publish("pkg", "1.0.0", blob, hours=900)
    reg.publish("pkg", "1.0.1", blob, hours=100)
    reg.publish("pkg", "1.0.2", blob, hours=100, deprecated="broken")
    reg.publish("pkg", "1.0.3-beta.1", blob, hours=100)
    reg.publish("pkg", "1.0.4", blob, hours=10)
    reg.publish("pkg", "2.0.0", blob, hours=300)  # above `latest`: a dist-tag never chose it
    reg.latest("pkg", "1.0.4")
    rule = bump.Npm("pkg", "")
    assert _bumper(reg).pick_npm(rule, "1.0.0") == ("1.0.1", "1.0.4")
    # The cool-down is configurable: with none, `latest` itself is eligible.
    assert _bumper(reg, cooldown=0).pick_npm(rule, "1.0.0") == ("1.0.4", None)
    # Never a downgrade, never a re-pin of the current version.
    assert _bumper(reg).pick_npm(rule, "1.0.4") == (None, None)


def test_a_version_newer_than_the_cool_down_is_skipped():
    reg = Registry()
    reg.publish("pkg", "1.0.0", b"x", hours=900)
    reg.publish("pkg", "1.0.1", b"x", hours=71.9)
    reg.latest("pkg", "1.0.1")
    assert _bumper(reg).pick_npm(bump.Npm("pkg", ""), "1.0.0") == (None, "1.0.1")
    reg.docs["pkg"]["time"]["1.0.1"] = ago(72)
    assert _bumper(reg).pick_npm(bump.Npm("pkg", ""), "1.0.0") == ("1.0.1", None)


def test_codex_rule_picks_the_linux_x64_platform_build_of_latest():
    reg = Registry()
    for version in (
        "0.2.0",
        "0.2.0-darwin-arm64",
        "0.2.0-linux-x64",
        "0.3.0-alpha.1-linux-x64",
        "0.1.0-linux-x64",
    ):
        reg.publish("@openai/codex", version, b"x", hours=200)
    reg.latest("@openai/codex", "0.2.0")
    rule = bump.RULES["codex"]
    assert _bumper(reg).pick_npm(rule, "0.1.0-linux-x64") == ("0.2.0-linux-x64", None)


# -- one npm bump end to end -----------------------------------------------------------------


def test_npm_bump_rewrites_only_the_pins_and_rebuilds_the_catalog(tmp_path):
    reg = Registry()
    path, new = claude_setup(tmp_path, reg)
    before = json.loads(path.read_text())
    runner, rebuilt = Runner(), []
    report = go(reg, tmp_path, runner=runner, rebuilt=rebuilt)
    after = json.loads(path.read_text())
    sha = hashlib.sha256(new).hexdigest()
    assert [(o.id, o.old, o.new) for o in report.changed] == [("claude", "1.0.0", "1.0.1")]
    assert after["manifest"]["install"]["version"] == "1.0.1"
    assert after["manifest"]["install"]["digest"] == "sha256:" + sha
    assert after["manifest"]["identity"]["version"] == "1.0.1"
    assert after["recipe"]["artifacts"] == [
        {
            "destination": f"node_modules/{CLAUDE}",
            "sha256": sha,
            "url": tarball_url(CLAUDE, "1.0.1"),
        }
    ]
    # Nothing else in the manifest moved: launch, probe, verify, signin, store...
    for doc in (before, after):
        for key in ("version", "digest"):
            doc["manifest"]["install"].pop(key)
        doc["manifest"]["identity"].pop("version")
        doc["recipe"].pop("artifacts")
    assert before == after
    assert rebuilt == [True]
    [(entrypoint, flag, expected)] = runner.calls
    assert entrypoint.name == "claude" and flag == "--version" and expected == "1.0.1"


def test_an_integrity_mismatch_is_refused_and_nothing_changes(tmp_path):
    reg = Registry()
    path, _ = claude_setup(tmp_path, reg, integrity=sri(b"some other bytes"))
    original = path.read_bytes()
    runner, rebuilt = Runner(), []
    report = go(reg, tmp_path, runner=runner, rebuilt=rebuilt)
    [refused] = report.refused
    assert "dist.integrity" in refused.refused
    assert report.changed == []
    assert path.read_bytes() == original
    assert rebuilt == [] and runner.calls == []


def test_a_version_command_that_disagrees_is_refused(tmp_path):
    reg = Registry()
    path, _ = claude_setup(tmp_path, reg)
    original = path.read_bytes()
    report = go(reg, tmp_path, runner=Runner(reported="1.0.0"))
    assert [o.id for o in report.refused] == ["claude"]
    assert path.read_bytes() == original


FAKE_DOCKER = """#!{python}
import os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(" ".join(args[:3]) + "\\n")
if args[0] == "run":
    mount = next(a for a in args if a.startswith("type=bind,"))
    source = mount.split(",")[1].removeprefix("source=")
    entry = args[args.index("--entrypoint") + 1].removeprefix("/opt/agent/")
    os.execv(os.path.join(source, entry), [entry, args[-1]])
"""


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    """A `docker` that runs the entrypoint directly, logging every call."""
    import sys

    docker = tmp_path / "docker"
    docker.write_text(FAKE_DOCKER.format(python=sys.executable))
    docker.chmod(0o700)
    log = tmp_path / "docker.log"
    monkeypatch.setattr(bump, "DOCKER", str(docker))
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    return prefix, log


def _tool(prefix: Path, script: str) -> None:
    tool = prefix / "bin" / "tool"
    tool.parent.mkdir(exist_ok=True)
    tool.write_text("#!/bin/sh\n" + script)
    tool.chmod(0o700)


def test_the_version_runner_needs_the_new_version_and_always_removes_its_container(
    fake_docker,
):
    prefix, log = fake_docker
    _tool(prefix, "echo 'Tool 2.10.4 (build) and anything else'\n")
    assert bump.run_version(prefix, "bin/tool", "--version", "2.10.4") == "2.10.4"
    for wrong in ("2.10", "2.1.4", "10.4"):
        with pytest.raises(bump.BumpError):
            bump.run_version(prefix, "bin/tool", "--version", wrong)
    _tool(prefix, "echo 2.10.4\nexit 1\n")
    with pytest.raises(bump.BumpError, match="exit 1"):
        bump.run_version(prefix, "bin/tool", "--version", "2.10.4")
    calls = log.read_text().splitlines()
    runs = [c for c in calls if c.startswith("run ")]
    removals = [c for c in calls if c.startswith("rm --force agent-catalog-bump-")]
    assert len(runs) == len(removals) == 5


def test_a_hung_version_check_is_killed_by_container_name(fake_docker, monkeypatch):
    prefix, log = fake_docker
    _tool(prefix, "sleep 30\n")
    monkeypatch.setattr(bump, "VERSION_SECONDS", 0.5)
    with pytest.raises(bump.BumpError, match="did not finish"):
        bump.run_version(prefix, "bin/tool", "--version", "1.0.0")
    assert log.read_text().splitlines()[-1].startswith("rm --force agent-catalog-bump-")


def test_an_entrypoint_cannot_name_a_path_outside_the_prefix(fake_docker):
    prefix, _ = fake_docker
    for entrypoint in ("../../bin/sh", "bin/../../x", "a b", "/bin/sh"):
        with pytest.raises(bump.BumpError, match="plain relative path"):
            bump.run_version(prefix, entrypoint, "--version", "1.0.0")


def test_the_sandbox_has_no_network_no_writes_no_privileges_and_no_host_environment(tmp_path):
    argv = bump.sandbox_argv("n", tmp_path, "node_modules/x/bin", "--version")
    joined = " ".join(argv)
    assert argv[:3] == [bump.DOCKER, "run", "--rm"]
    for flag in (
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        f"--mount type=bind,source={tmp_path},target=/opt/agent,readonly",
        "--entrypoint /opt/agent/node_modules/x/bin",
    ):
        assert flag in joined
    assert argv[argv.index("--user") + 1] != "0:0" and not argv[
        argv.index("--user") + 1
    ].startswith("0:")
    assert "--privileged" not in argv and "--env-file" not in argv and "-v" not in argv
    envs = [argv[i + 1] for i, a in enumerate(argv) if a == "--env"]
    assert all(
        "=" in e and e.split("=")[0] in {"HOME", "NO_COLOR", "CI", "DISABLE_AUTOUPDATER"}
        for e in envs
    )
    assert bump.SANDBOX_IMAGE.split("@")[1].startswith("sha256:")
    assert argv[-2:] == [bump.SANDBOX_IMAGE, "--version"]


def _docker_with_image() -> bool:
    import shutil
    import subprocess

    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "image", "inspect", bump.SANDBOX_IMAGE],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


@pytest.mark.skipif(not _docker_with_image(), reason="needs docker and the pinned image cached")
def test_a_detached_child_of_the_version_command_dies_with_its_container(tmp_path):
    """Hermes on #1745: a `--version` that forks a detached child must not outlive the check
    (and so cannot wait for the later, credentialed step), nor write anything outside it."""
    import os

    prefix = tmp_path / "prefix"
    prefix.mkdir()
    _tool(
        prefix,
        "(setsid sleep 31337 </dev/null >/dev/null 2>&1 &)\n"
        "touch /opt/agent/planted 2>/dev/null\n"
        'touch "$HOME/../planted" 2>/dev/null\n'
        "echo 3.2.1\n",
    )
    assert bump.run_version(prefix, "bin/tool", "--version", "3.2.1") == "3.2.1"
    survivors = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if cmd[:2] == [b"sleep", b"31337"]:
            survivors.append(pid)
    assert survivors == []
    assert not (prefix / "planted").exists()


def test_a_second_run_is_a_no_op(tmp_path):
    reg = Registry()
    path, _ = claude_setup(tmp_path, reg)
    go(reg, tmp_path)
    bumped = path.read_bytes()
    stamp = path.stat().st_mtime_ns
    runner, rebuilt = Runner(), []
    report = go(reg, tmp_path, runner=runner, rebuilt=rebuilt)
    assert report.changed == [] and report.refused == []
    assert path.read_bytes() == bumped and path.stat().st_mtime_ns == stamp
    assert rebuilt == [] and runner.calls == []


def test_a_current_recipe_downloads_nothing(tmp_path):
    reg = Registry()
    claude_setup(tmp_path, reg, new_hours=1)
    report = go(reg, tmp_path)
    assert report.changed == [] and report.outcomes[0].held == "1.0.1"
    assert not [u for u in reg.requested if u.endswith(".tgz")]


def test_indentation_of_each_recipe_file_is_preserved(tmp_path):
    reg = Registry()
    path, _ = claude_setup(tmp_path, reg)
    write(tmp_path, "claude", json.loads(path.read_text()), indent=4)
    go(reg, tmp_path)
    assert path.read_text().startswith('{\n    "manifest"')


# -- the Kimi closure ------------------------------------------------------------------------

KIMI = "@moonshot-ai/kimi-code"
KIMI_BIN = {"files": {"dist/main.mjs": b"#!/usr/bin/env node\n"}}


def test_kimi_closure_is_re_resolved_with_exact_pins(tmp_path):
    reg = Registry()
    kimi_old = npm_tgz(KIMI, "1.0.0", {"a": "^1.0.0"}, **KIMI_BIN)
    new_deps = {"a": "^1.0.0", "b": "^2.0.0"}
    optional = {"opt": "^1.0.0"}
    kimi_new = npm_tgz(KIMI, "1.1.0", new_deps, meta={"optionalDependencies": optional}, **KIMI_BIN)
    reg.publish(KIMI, "1.0.0", kimi_old, deps={"a": "^1.0.0"}, hours=900)
    reg.publish(KIMI, "1.1.0", kimi_new, deps=new_deps, optionalDependencies=optional, hours=100)
    reg.latest(KIMI, "1.1.0")
    a = {v: npm_tgz("a", v) for v in ("1.0.0", "1.0.5", "2.0.0", "2.1.0")}
    for version, data in a.items():
        reg.publish("a", version, data, hours=1 if version == "2.1.0" else 300)
    b2 = npm_tgz("b", "2.0.0", {"a": "^2.0.0"})
    reg.publish("b", "2.0.0", b2, deps={"a": "^2.0.0"}, hours=300)
    doc = recipe(
        "kimi",
        KIMI,
        [
            (f"node_modules/{KIMI}", tarball_url(KIMI, "1.0.0"), kimi_old),
            ("node_modules/a", tarball_url("a", "1.0.0"), a["1.0.0"]),
        ],
        "1.0.0",
    )
    path = write(tmp_path, "kimi", doc)
    report = go(reg, tmp_path)
    assert [(o.id, o.new) for o in report.changed] == [("kimi", "1.1.0")]
    pins = {
        art["destination"]: art["url"].rsplit("/", 1)[1]
        for art in json.loads(path.read_text())["recipe"]["artifacts"]
    }
    assert list(pins) == [
        f"node_modules/{KIMI}",
        "node_modules/a",
        "node_modules/b",
        "node_modules/b/node_modules/a",
    ]
    # The existing pin still satisfies ^1.0.0, so it stays; b's own ^2 range nests a copy, and
    # 2.1.0 is inside the cool-down. Optional dependencies are never pinned or even fetched.
    assert pins["node_modules/a"] == "a-1.0.0.tgz"
    assert pins["node_modules/b"] == "b-2.0.0.tgz"
    assert pins["node_modules/b/node_modules/a"] == "a-2.0.0.tgz"
    assert not [u for u in reg.requested if "/opt" in u]


def test_a_pinned_dependency_whose_bytes_changed_is_refused(tmp_path):
    reg = Registry()
    kimi_old = npm_tgz(KIMI, "1.0.0", {"a": "^1.0.0"}, **KIMI_BIN)
    kimi_new = npm_tgz(KIMI, "1.1.0", {"a": "^1.0.0"}, **KIMI_BIN)
    reg.publish(KIMI, "1.0.0", kimi_old, deps={"a": "^1.0.0"}, hours=900)
    reg.publish(KIMI, "1.1.0", kimi_new, deps={"a": "^1.0.0"}, hours=100)
    reg.latest(KIMI, "1.1.0")
    pinned = npm_tgz("a", "1.0.0")
    reg.publish("a", "1.0.0", npm_tgz("a", "1.0.0", meta={"description": "swapped"}))
    doc = recipe(
        "kimi",
        KIMI,
        [
            (f"node_modules/{KIMI}", tarball_url(KIMI, "1.0.0"), kimi_old),
            ("node_modules/a", tarball_url("a", "1.0.0"), pinned),
        ],
        "1.0.0",
    )
    path = write(tmp_path, "kimi", doc)
    original = path.read_bytes()
    report = go(reg, tmp_path)
    assert "changed bytes" in report.refused[0].refused
    assert path.read_bytes() == original


def test_a_dependency_only_inside_the_cool_down_is_refused(tmp_path):
    reg = Registry()
    kimi_old = npm_tgz(KIMI, "1.0.0", **KIMI_BIN)
    kimi_new = npm_tgz(KIMI, "1.1.0", {"fresh": "^1.0.0"}, **KIMI_BIN)
    reg.publish(KIMI, "1.0.0", kimi_old, hours=900)
    reg.publish(KIMI, "1.1.0", kimi_new, deps={"fresh": "^1.0.0"}, hours=100)
    reg.latest(KIMI, "1.1.0")
    reg.publish("fresh", "1.0.0", npm_tgz("fresh", "1.0.0"), hours=2)
    doc = recipe(
        "kimi", KIMI, [(f"node_modules/{KIMI}", tarball_url(KIMI, "1.0.0"), kimi_old)], "1.0.0"
    )
    write(tmp_path, "kimi", doc)
    report = go(reg, tmp_path)
    assert "outside the cool-down" in report.refused[0].refused


# -- Antigravity's GitHub release ------------------------------------------------------------

REPO = "google-antigravity/antigravity-cli"
ASSET = "agy_cli_linux_x64.tar.gz"


def github_setup(
    tmp_path, reg, *, published_hours, redirect_host="release-assets.githubusercontent.com"
):
    old = _tgz({"antigravity": b"#!/bin/sh\n"}, root="")
    new = _tgz({"antigravity": b"#!/bin/sh\necho new\n"}, root="")
    doc = recipe(
        "antigravity",
        f"@{REPO}",
        [(".", f"https://github.com/{REPO}/releases/download/1.0.0/{ASSET}", old)],
        "1.0.0",
    )
    path = write(tmp_path, "antigravity", doc)
    reg.extra[f"https://github.com/{REPO}/releases/latest"] = httpx.Response(
        302, headers={"location": f"https://github.com/{REPO}/releases/tag/1.1.0"}
    )
    blob = f"https://{redirect_host}/github-production-release-asset/1/abc?sig=x"
    reg.extra[f"https://github.com/{REPO}/releases/download/1.1.0/{ASSET}"] = httpx.Response(
        302, headers={"location": blob}
    )
    stamp = (NOW - dt.timedelta(hours=published_hours)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    reg.extra[blob] = body(new, {"last-modified": stamp})
    return path, new


def test_github_release_is_read_from_the_redirect_and_honours_the_cool_down(tmp_path):
    reg = Registry()
    path, new = github_setup(tmp_path, reg, published_hours=100)
    runner = Runner()
    report = go(reg, tmp_path, runner=runner)
    assert [(o.id, o.new) for o in report.changed] == [("antigravity", "1.1.0")]
    after = json.loads(path.read_text())
    sha = hashlib.sha256(new).hexdigest()
    assert after["recipe"]["artifacts"] == [
        {
            "destination": ".",
            "sha256": sha,
            "url": f"https://github.com/{REPO}/releases/download/1.1.0/{ASSET}",
        }
    ]
    assert after["manifest"]["install"]["digest"] == "sha256:" + sha
    assert runner.calls[0][0].name == "antigravity"
    assert all(u.startswith("https://github.com/") or "release-assets" in u for u in reg.requested)


def test_a_fresh_github_release_waits(tmp_path):
    reg = Registry()
    path, _ = github_setup(tmp_path, reg, published_hours=5)
    original = path.read_bytes()
    report = go(reg, tmp_path)
    assert report.changed == [] and report.outcomes[0].held == "1.1.0"
    assert path.read_bytes() == original


def test_a_github_redirect_to_another_host_is_refused(tmp_path):
    reg = Registry()
    path, _ = github_setup(tmp_path, reg, published_hours=100, redirect_host="example.com")
    original = path.read_bytes()
    report = go(reg, tmp_path)
    assert [o.id for o in report.refused] == ["antigravity"]
    assert path.read_bytes() == original
    assert not [u for u in reg.requested if "example.com" in u]


# -- the real tree ---------------------------------------------------------------------------


def test_every_release_recipe_has_exactly_one_matching_upstream_rule():
    ids = set()
    for path in sorted(RECIPES.glob("*.json")):
        manifest = json.loads(path.read_text())["manifest"]
        ident, package = manifest["identity"]["id"], manifest["install"]["package"]
        rule = bump.RULES[ident]
        expected = rule.package if isinstance(rule, bump.Npm) else f"@{rule.repository}"
        assert package == expected, ident
        ids.add(ident)
    assert ids == set(bump.RULES)


def test_the_real_recipes_round_trip_byte_for_byte():
    for path in sorted(RECIPES.glob("*.json")):
        text = path.read_text()
        assert bump._dump(json.loads(text), text) == text, path.name


def test_the_pr_title_and_body():
    report = bump.Report(
        [
            bump.Outcome("claude", "Claude Code", "1.0.0", "1.0.1", "notes", reported="1.0.1"),
            bump.Outcome("codex", "Codex", "0.1.0-linux-x64", "0.2.0-linux-x64", reported="0.2.0"),
            bump.Outcome("gemini", "Gemini CLI", "0.1.0", held="0.2.0"),
            bump.Outcome("kimi", "Kimi Code", "1.0.0", refused="downloaded bytes differ"),
        ]
    )
    assert bump.title(report) == "chore(agents): catalog — claude 1.0.1, codex 0.2.0"
    text = bump.body(report, 72)
    assert "| Claude Code | 1.0.0 | **1.0.1** |" in text
    assert "- Gemini CLI: 0.2.0" in text
    assert "- Kimi Code: downloaded bytes differ" in text
    assert "## Security impact" in text and "## Test plan" in text


def test_unknown_agent_ids_are_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        bump.main(["--only", "nope"])
    assert exc.value.code == 2
