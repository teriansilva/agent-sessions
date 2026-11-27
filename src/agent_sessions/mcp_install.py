"""Package sources for project MCP servers (#2163 P1b): npm and PyPI, exact, locked, inert.

**Review, then install from exactly what was reviewed.**

- `review` resolves the package at its EXACT version in a private staging directory and writes a
  lock, without installing anything: npm's ``package-lock.json`` (``--package-lock-only``, every
  package with its ``integrity``), or uv's hashed requirements (``uv pip compile
  --generate-hashes``). The operator sees what will run: the package, its version, digest,
  dependency count, the packages that DECLARE install scripts (which never run), the entry point.
- `install` installs from that lock only, into a new directory per review
  (``<AGENT_SESSIONS_HOME>/mcp/<server id>/<review id>/``), and records it with the server. npm:
  ``npm ci --ignore-scripts``. PyPI: a venv and ``uv pip install --require-hashes --only-binary
  :all:`` — wheels only, because an sdist would run its build backend.

**Go modules (#2427)**, from a repository link only (`mcp_gitsource`): the review downloads the
module at its exact version through the Go module proxy, verified against the Go checksum
database (its ``h1:`` hash is the lock), and finds the one command it builds; the install
downloads it again, compares the hash, and builds that command with BattleLab's own toolchain
(`mcp_gotool`) into the install's ``bin/``: ``go install -trimpath`` with cgo off, no toolchain
switch, no direct VCS fetch (``GOVCS=off``), only ``proxy.golang.org`` + ``sum.golang.org``
reachable. A Go build runs no repository code (no install hooks; ``go:generate`` never runs).

The registries are fixed (``registry.npmjs.org``, ``pypi.org``, the Go proxy and checksum
database); a lock entry resolved anywhere else is refused. Every tool runs as a literal argv
(no shell), with a whitelisted environment (no proxy or npm/uv/Go configuration from the
operator's environment or home: ``--no-config``, ``npm_config_userconfig=/dev/null``,
``GOENV=off``), bounded in time and output. An installed directory is made read-only. A
server's command is resolved from its install, which must match its CURRENT source.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

from . import mcp_jobs
from .mcp_egress import EgressProxy, origin
from .mcp_registry import RegistryError

log = logging.getLogger(__name__)

NPM_REGISTRY = "https://registry.npmjs.org/"
PYPI_INDEX = "https://pypi.org/simple"
PYPI_FILES = "https://files.pythonhosted.org/"
GO_PROXY = "https://proxy.golang.org"
GO_SUMDB = "https://sum.golang.org"
TOOL_TIMEOUT_S = 300
#: An install downloads every wheel or tarball of the closure (a server with heavy dependencies
#: takes minutes on a busy host); a review only resolves.
INSTALL_TIMEOUT_S = 1200
OUTPUT_MAX = 64 * 1024
DEPENDENCIES_MAX = 2000

_NPM_NAME = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]{0,213}$")
_NPM_VERSION = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$"
)
_PYPI_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$")
#: A Go module path: a dotted host, then path elements (``…/v3`` major suffixes included).
_GO_MODULE = re.compile(r"^[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)+(?:/[A-Za-z0-9._~-]+)+$")
#: A Go module version: a tag (``v3.3.0``) or a pseudo-version, never a branch or ``latest``.
_GO_VERSION = re.compile(r"^v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+incompatible)?$")
_PYPI_VERSION = re.compile(
    r"^[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?(?:\.dev[0-9]+)?$"
)
_ENTRY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REVIEW_ID = re.compile(r"^[0-9a-f]{16}$")


class InstallError(RegistryError):
    """An install or review refused or failed; the message is words, never tool output."""


# ---- validation ---------------------------------------------------------------------------------


def validate_source(raw: dict) -> dict:
    kind = raw.get("kind")
    unknown = set(raw) - {"kind", "package", "version", "entrypoint"}
    if unknown:
        raise RegistryError("unknown source field")
    package, version = raw.get("package"), raw.get("version")
    if kind == "go":  # #2427: ``package`` is the module path
        if not isinstance(package, str) or len(package) > 300 or not _GO_MODULE.fullmatch(package):
            raise RegistryError("a Go module path is a host name, then its path")
        if not isinstance(version, str) or not _GO_VERSION.fullmatch(version):
            raise RegistryError("an exact Go module version (a tag or a pseudo-version)")
    elif kind == "npm":
        if not isinstance(package, str) or not _NPM_NAME.fullmatch(package):
            raise RegistryError("an npm package name is lowercase (an optional @scope/ first)")
        if not isinstance(version, str) or not _NPM_VERSION.fullmatch(version):
            raise RegistryError("an exact version. Ranges and 'latest' are refused")
    elif kind == "pypi":
        if not isinstance(package, str) or not _PYPI_NAME.fullmatch(package):
            raise RegistryError("a PyPI package name is letters, digits, '.', '_' and '-'")
        if not isinstance(version, str) or not _PYPI_VERSION.fullmatch(version):
            raise RegistryError("an exact version. Ranges and 'latest' are refused")
    else:
        raise RegistryError("source kind is npm, pypi or go")
    out = {"kind": kind, "package": package, "version": version}
    entry = raw.get("entrypoint")
    if entry is not None:
        if not isinstance(entry, str) or not _ENTRY.fullmatch(entry):
            raise RegistryError("an entry point is a command name")
        out["entrypoint"] = entry
    return out


# ---- the tools ----------------------------------------------------------------------------------


def _home() -> Path:
    return Path(
        os.environ.get("AGENT_SESSIONS_HOME") or "~/.local/share/agent-sessions"
    ).expanduser()


def root() -> Path:
    return _home() / "mcp"


def _find(name: str) -> str:
    """An absolute path to ``name``: PATH first, then the per-user and system locations a
    service's PATH commonly misses."""
    found = shutil.which(name)
    candidates = [found] if found else []
    candidates += [
        str(Path.home() / ".local" / "bin" / name),
        f"/usr/local/bin/{name}",
        f"/usr/bin/{name}",
    ]
    for c in candidates:
        if c and os.path.isabs(c) and os.access(c, os.X_OK):
            return c
    raise InstallError(f"{name} is not installed on this machine")


def _allowed(kind: str) -> set[tuple[str, int]]:
    """The hosts a tool run for ``kind`` may reach (`mcp_egress`): its registry, nothing else."""
    if kind == "npm":
        return {origin(NPM_REGISTRY)}
    if kind == "go":
        return {origin(GO_PROXY), origin(GO_SUMDB)}
    return {origin(PYPI_INDEX), origin(PYPI_FILES)}


def _go_env(gobin: Path | None = None) -> dict[str, str]:
    """Go's settings for a build (#2427): every location under the MCP root, nothing from the
    operator's ``go env`` file or environment; the module proxy and checksum database fixed and
    nothing exempt from them; no cgo, no toolchain switch, no direct VCS fetch, no telemetry."""
    cache = root() / ".cache" / "go"
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {
        "GOENV": "off",
        "GOPATH": str(cache / "path"),
        "GOMODCACHE": str(cache / "mod"),
        "GOCACHE": str(cache / "build"),
        "GOFLAGS": "-mod=readonly",
        "CGO_ENABLED": "0",
        "GOTOOLCHAIN": "local",
        "GOPROXY": GO_PROXY,
        "GOSUMDB": "sum.golang.org",
        "GONOSUMDB": "",
        "GONOSUMCHECK": "",
        "GOPRIVATE": "",
        "GONOPROXY": "",
        "GOINSECURE": "",
        "GOVCS": "off",
        "GOTELEMETRY": "off",
        "GOWORK": "off",
        "GOMAXPROCS": "2",
        "GOMEMLIMIT": "1GiB",
    }
    if gobin is not None:
        env["GOBIN"] = str(gobin)
    return env


def _env(cwd: Path, proxy: str) -> dict[str, str]:
    """A whitelisted environment: no proxy, registry or credential settings are inherited; the
    only egress is ``proxy`` (`mcp_egress`), for npm, uv and any git npm starts (https only).
    npm's user and global config are two EMPTY files of the run's own (npm refuses one file for
    both). The download caches are shared under the MCP root: every download is verified against the
    lock's digests (``integrity`` / ``--require-hashes``), so a cache never decides content."""
    path = os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin"
    cache = root() / ".cache"
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in (".npmrc-user", ".npmrc-global"):
        (cwd / name).write_text("")
    return {
        "PATH": path,
        "HOME": str(cwd),  # never the operator's ~/.npmrc, pip.conf or uv config
        "LANG": os.environ.get("LANG") or "C.UTF-8",
        "npm_config_userconfig": str(cwd / ".npmrc-user"),
        "npm_config_globalconfig": str(cwd / ".npmrc-global"),
        "npm_config_registry": NPM_REGISTRY,
        "npm_config_ignore_scripts": "true",
        "npm_config_audit": "false",
        "npm_config_fund": "false",
        "npm_config_update_notifier": "false",
        "npm_config_cache": str(cache / "npm"),
        "UV_NO_CONFIG": "1",
        "UV_CACHE_DIR": str(cache / "uv"),
        "UV_PYTHON_DOWNLOADS": "never",
        **{k: proxy for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")},
        **{k: proxy for k in ("ALL_PROXY", "all_proxy")},
        "NO_PROXY": "",
        "no_proxy": "",
        "npm_config_proxy": proxy,
        "npm_config_https_proxy": proxy,
        "npm_config_noproxy": "",
        "GIT_ALLOW_PROTOCOL": "https",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _run(
    argv: list[str],
    cwd: Path,
    what: str,
    kind: str,
    timeout: int = TOOL_TIMEOUT_S,
    extra_env: dict[str, str] | None = None,
    home: Path | None = None,
) -> str:
    """One tool run: literal argv, whitelisted env, egress only to ``kind``'s registry
    (`mcp_egress`), bounded. Raises `InstallError` in words; the tool's own output is logged (it
    can name packages, never a credential: none is involved)."""
    with EgressProxy(_allowed(kind)) as proxy:
        try:
            proc = subprocess.Popen(  # noqa: S603 — a literal argv, never a shell
                argv,
                cwd=cwd,
                # ``home``: the run's writable home when ``cwd`` is not ours to write (a Go module
                # in the read-only module cache, #2427).
                env={**_env(home or cwd, proxy.url), **(extra_env or {})},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # Its own process group: a timeout ends the tool AND what it started (a Go build's
                # compiler processes), never only the parent.
                start_new_session=True,
            )
        except OSError as e:
            raise InstallError(f"{what} could not run ({e.strerror or type(e).__name__})") from None
        try:
            # Waited in short slices so a Stop kills the run (and its process group) at once,
            # not when the step's own bound runs out (#2427).
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                try:
                    stdout, stderr = proc.communicate(timeout=max(0.01, min(0.5, left)))
                    break
                except subprocess.TimeoutExpired:
                    if left <= 0.5:
                        raise
                    mcp_jobs.raise_if_stopped()
        except BaseException:
            _kill_group(proc)
            with contextlib.suppress(Exception):
                proc.communicate(timeout=5)
            if isinstance(sys.exc_info()[1], subprocess.TimeoutExpired):
                raise InstallError(f"{what} took longer than {timeout} s") from None
            raise
        proc = subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
        refused = proxy.refused
    out = proc.stdout.decode("utf-8", "replace")[-OUTPUT_MAX:]
    if refused:
        # Whatever the tool's exit, a refused fetch means the closure names somewhere other than
        # its registry: never a usable result.
        log.info("MCP install: %s tried to reach %s", what, ", ".join(sorted(set(refused))))
        raise InstallError(
            f"{what} failed: a dependency is fetched from {refused[0]}, not from its registry"
        )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")[-OUTPUT_MAX:]
        log.info("MCP install: %s failed (%s): %s", what, proc.returncode, err[-2000:])
        hint = _hint(err)
        raise InstallError(f"{what} failed{': ' + hint if hint else ''}")
    return out


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the tool's own process group. Never for a pid of 1 or less: ``killpg(1)`` is
    ``kill(-1)``, every process this user owns."""
    pid = proc.pid
    if not isinstance(pid, int) or pid <= 1:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def _hint(err: str) -> str:
    low = err.lower()
    if "cgo" in low or "build constraints exclude all go files" in low:
        return "it needs cgo (a C compiler), which BattleLab never uses for a build"
    if "unknown revision" in low or "invalid version" in low:
        return "that version isn't in the module's own repository"
    if "404" in low or "not found" in low or "no matching distribution" in low:
        return "the package or version was not found"
    if "no solution" in low or "only-binary" in low or "no wheels" in low or "has no wheel" in low:
        return "it has no wheel for this machine (packages that need a build are refused)"
    if "requires-python" in low or "requires python" in low:
        return "it needs a different Python version"
    if "enotfound" in low or "network" in low or "connect" in low:
        return "the registry could not be reached"
    return ""


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=False, mode=0o700)
    return path


def _staging(sid: str, review_id: str) -> Path:
    return root() / sid / "reviews" / review_id


# ---- review -------------------------------------------------------------------------------------


def review(rec: dict) -> dict:
    """Resolve ``rec``'s package source into a lock in a staging directory; return the facts the
    operator reviews, with a ``review_id`` that `install` takes. Installs nothing."""
    source = rec["source"]
    if source["kind"] not in ("npm", "pypi", "go"):
        raise InstallError("only a package source is reviewed")
    review_id = secrets.token_hex(8)
    stage = _private_dir(_staging(rec["id"], review_id))
    reviewers = {"npm": _review_npm, "pypi": _review_pypi, "go": _review_go}
    try:
        facts = reviewers[source["kind"]](source, stage)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    facts.update(
        {
            "review_id": review_id,
            "source": source,
            "reviewed_at": time.time(),
            "lock_digest": _digest(stage / facts["lock_file"]),
        }
    )
    (stage / "review.json").write_text(json.dumps(facts))
    return facts


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _review_npm(source: dict, stage: Path) -> dict:
    npm = _find("npm")
    _find("node")
    (stage / "package.json").write_text(
        json.dumps(
            {
                "name": "battlelab-mcp-server",
                "private": True,
                "dependencies": {source["package"]: source["version"]},
            }
        )
    )
    _run(
        [
            npm,
            "install",
            "--package-lock-only",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            "--save-exact",
            "--registry",
            NPM_REGISTRY,
        ],
        stage,
        "resolving the npm package",
        "npm",
    )
    lock = json.loads((stage / "package-lock.json").read_text())
    packages = lock.get("packages") or {}
    top = packages.get(f"node_modules/{source['package']}") or {}
    if top.get("version") != source["version"]:
        raise InstallError("npm resolved a different version than the one asked for")
    scripts = []
    for path, meta in packages.items():
        if not path:
            continue
        resolved = meta.get("resolved", "")
        if not resolved.startswith(NPM_REGISTRY) or not meta.get("integrity"):
            raise InstallError("a dependency resolves outside registry.npmjs.org or has no digest")
        if meta.get("hasInstallScript"):
            scripts.append(path.rsplit("node_modules/", 1)[-1])
    deps = len(packages) - 2  # the root and the package itself
    if deps > DEPENDENCIES_MAX:
        raise InstallError(f"more than {DEPENDENCIES_MAX} dependencies")
    bins = top.get("bin") or {}
    if isinstance(bins, str):
        bins = {source["package"].rsplit("/", 1)[-1]: bins}
    entry = _pick_entry(source, sorted(bins))
    return {
        "lock_file": "package-lock.json",
        "integrity": top.get("integrity", ""),
        "registry": "registry.npmjs.org",
        "dependencies": max(deps, 0),
        "install_scripts_declared": sorted(scripts),
        "entrypoints": sorted(bins),
        "entrypoint": entry,
        "license": top.get("license", ""),
        "runs": f"node …/{entry} (stdio)",
    }


def _review_pypi(source: dict, stage: Path) -> dict:
    uv = _find("uv")
    (stage / "requirements.in").write_text(f"{source['package']}=={source['version']}\n")
    _run(
        [
            uv,
            "pip",
            "compile",
            "--no-config",
            "--generate-hashes",
            "--only-binary",
            ":all:",
            "--index-url",
            PYPI_INDEX,
            "--python",
            _python(),
            "--quiet",
            "--output-file",
            "requirements.txt",
            "requirements.in",
        ],
        stage,
        "resolving the PyPI package",
        "pypi",
    )
    text = (stage / "requirements.txt").read_text()
    pins = re.findall(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\]+)", text, re.M)
    if not pins:
        raise InstallError("the resolution is empty")
    norm = re.sub(r"[-_.]+", "-", source["package"]).lower()
    top = next((v for n, v in pins if re.sub(r"[-_.]+", "-", n).lower() == norm), None)
    if top != source["version"]:
        raise InstallError("uv resolved a different version than the one asked for")
    if len(pins) - 1 > DEPENDENCIES_MAX:
        raise InstallError(f"more than {DEPENDENCIES_MAX} dependencies")
    digest = _first_hash(text, norm)
    return {
        "lock_file": "requirements.txt",
        "integrity": digest,
        "registry": "pypi.org",
        "dependencies": len(pins) - 1,
        "install_scripts_declared": [],
        "entrypoints": [],  # known once installed (console scripts live in the wheel)
        "entrypoint": source.get("entrypoint", ""),
        "license": "",
        "runs": "a console script of the package (stdio), wheels only",
    }


def _first_hash(text: str, norm: str) -> str:
    """The first ``--hash`` of the requirement block for the (normalized) package ``norm``."""
    blocks = re.split(r"\n(?=[A-Za-z0-9])", text)
    for block in blocks:
        m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)==", block)
        if m and re.sub(r"[-_.]+", "-", m.group(1)).lower() == norm:
            h = re.search(r"--hash=(sha256:[0-9a-f]{64})", block)
            return h.group(1) if h else ""
    return ""


def _python() -> str:
    import sys

    return sys.executable


def _pick_entry(source: dict, entries: list[str]) -> str:
    wanted = source.get("entrypoint")
    if wanted:
        if wanted not in entries:
            raise InstallError(f"the package has no command named {wanted}")
        return wanted
    if len(entries) == 1:
        return entries[0]
    if not entries:
        raise InstallError("the package declares no command to start")
    short = source["package"].rsplit("/", 1)[-1]
    if short in entries:
        return short
    raise InstallError("the package has several commands; choose one: " + ", ".join(entries))


# ---- install ------------------------------------------------------------------------------------


def install(rec: dict, review_id: object) -> dict:
    """Install the reviewed lock ``review_id`` for ``rec`` (whose source must be unchanged since);
    return the ``installed`` record (`mcp_registry.set_installed`)."""
    if not isinstance(review_id, str) or not _REVIEW_ID.fullmatch(review_id):
        raise InstallError("no such review")
    stage = _staging(rec["id"], review_id)
    try:
        facts = json.loads((stage / "review.json").read_text())
    except (OSError, ValueError):
        raise InstallError("no such review; review the package again") from None
    if facts["source"] != rec["source"]:
        raise InstallError("the source changed since it was reviewed; review it again")
    if _digest(stage / facts["lock_file"]) != facts["lock_digest"]:
        raise InstallError("the reviewed lock changed; review it again")
    target = root() / rec["id"] / review_id
    if (target / "installed.json").exists():
        return json.loads((target / "installed.json").read_text())  # idempotent
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)  # our own interrupted attempt
    # Installed IN PLACE: a venv's scripts carry their absolute interpreter path, so the
    # directory a server runs from is the one it was installed in.
    work = _private_dir(target)
    try:
        shutil.copy2(stage / facts["lock_file"], work / facts["lock_file"])
        if rec["source"]["kind"] == "npm":
            shutil.copy2(stage / "package.json", work / "package.json")
            installed = _install_npm(rec["source"], facts, work)
        elif rec["source"]["kind"] == "go":
            installed = _install_go(rec["source"], facts, work)
        else:
            installed = _install_pypi(rec["source"], facts, work)
        installed.update(
            {
                "review_id": review_id,
                "source": rec["source"],
                "lock_digest": facts["lock_digest"],
                "integrity": facts["integrity"],
                "installed_at": time.time(),
            }
        )
        for name in (".npmrc-user", ".npmrc-global"):
            (work / name).unlink(missing_ok=True)
        (work / "installed.json").write_text(json.dumps(installed))
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    _freeze(target)
    return installed


def _freeze(path: Path) -> None:
    """Read-only once complete: nothing (an agent included) rewrites an installed server in
    place by accident. The operator's own user can still change modes: not a sandbox."""
    for dirpath, _dirnames, filenames in os.walk(path, followlinks=False):
        for name in filenames:
            p = Path(dirpath) / name
            if not p.is_symlink():
                mode = p.stat().st_mode
                p.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    for dirpath, dirnames, _ in os.walk(path, topdown=False, followlinks=False):
        for name in dirnames:
            p = Path(dirpath) / name
            if not p.is_symlink():
                p.chmod(0o500)
    path.chmod(0o500)


def _install_npm(source: dict, facts: dict, work: Path) -> dict:
    npm, node = _find("npm"), _find("node")
    _run(
        [npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--registry", NPM_REGISTRY],
        work,
        "installing the npm package",
        "npm",
        INSTALL_TIMEOUT_S,
    )
    pkg_dir = work / "node_modules" / source["package"]
    meta = json.loads((pkg_dir / "package.json").read_text())
    bins = meta.get("bin") or {}
    if isinstance(bins, str):
        bins = {source["package"].rsplit("/", 1)[-1]: bins}
    entry = facts["entrypoint"]
    rel = bins.get(entry)
    if not isinstance(rel, str):
        raise InstallError("the installed package lacks the reviewed command")
    script = (pkg_dir / rel).resolve()
    if not script.is_relative_to(work.resolve()) or not script.is_file():
        raise InstallError("the package's command points outside its install")
    return {"dir": str(work), "command": node, "args": [str(script)], "entrypoint": entry}


def _install_pypi(source: dict, facts: dict, work: Path) -> dict:
    uv = _find("uv")
    _run(
        [uv, "venv", "--no-config", "--python", _python(), "--quiet", "venv"],
        work,
        "creating the environment",
        "pypi",
    )
    py = work / "venv" / "bin" / "python"
    _run(
        [
            uv,
            "pip",
            "install",
            "--no-config",
            "--python",
            str(py),
            "--require-hashes",
            "--only-binary",
            ":all:",
            "--no-deps",
            "--index-url",
            PYPI_INDEX,
            "--quiet",
            "-r",
            "requirements.txt",
        ],
        work,
        "installing the PyPI package",
        "pypi",
        INSTALL_TIMEOUT_S,
    )
    scripts = _console_scripts(work / "venv", source["package"])
    entry = _pick_entry(source, sorted(scripts))
    path = work / "venv" / "bin" / entry
    if not path.is_file():
        raise InstallError("the installed package lacks its command")
    return {"dir": str(work), "command": str(path), "args": [], "entrypoint": entry}


def _console_scripts(venv: Path, package: str) -> list[str]:
    norm = re.sub(r"[-_.]+", "_", package).lower()
    for info in venv.glob("lib/python*/site-packages/*.dist-info"):
        # ``<name>-<version>.dist-info``; a wheel's name part never holds a hyphen.
        name = info.name.removesuffix(".dist-info").split("-", 1)[0]
        if re.sub(r"[-_.]+", "_", name).lower() != norm:
            continue
        ep = info / "entry_points.txt"
        if not ep.is_file():
            return []
        out, section = [], ""
        for line in ep.read_text().splitlines():
            line = line.strip()
            if line.startswith("["):
                section = line.strip("[]")
            elif section == "console_scripts" and "=" in line:
                out.append(line.split("=", 1)[0].strip())
        return [s for s in out if _ENTRY.fullmatch(s)]
    return []


# ---- Go modules (#2427) -------------------------------------------------------------------------

#: One Go step's wall clock (a cold build of a mid-size server measured ~4 min here).
GO_STEP_TIMEOUT_S = 600
#: The shared Go caches are cleared above this (under the build lock: no job is using them).
GO_CACHE_CEILING = 2 * 1024**3
#: A build is refused with less free space than this on the MCP root's filesystem.
GO_FREE_MIN = 1024**3


def _go_json(go: Path, args: list[str], cwd: Path, what: str, timeout: int) -> dict:
    out = _run([str(go), *args], cwd, what, "go", timeout, _go_env())
    try:
        doc = json.loads(out)
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        raise InstallError(f"{what}: the Go tool's answer could not be read")
    if doc.get("Error"):
        log.info("MCP install: %s: %s", what, str(doc["Error"])[:500])
        raise InstallError(f"{what} failed: {_hint(str(doc['Error'])) or 'the Go tool refused it'}")
    return doc


def _go_download(go: Path, source: dict, cwd: Path) -> dict:
    """``go mod download -json`` of exactly the source: the module verified against the Go
    checksum database, its ``h1:`` hash, its go.mod and its source directory."""
    doc = _go_json(
        go,
        ["mod", "download", "-json", f"{source['package']}@{source['version']}"],
        cwd,
        "downloading the Go module",
        GO_STEP_TIMEOUT_S,
    )
    if doc.get("Path") != source["package"] or doc.get("Version") != source["version"]:
        raise InstallError("the Go module proxy answered for another module")
    if not isinstance(doc.get("Sum"), str) or not doc["Sum"].startswith("h1:"):
        raise InstallError("the Go module has no checksum")
    return doc


def _go_mod(go: Path, go_mod: Path, cwd: Path) -> dict:
    """The module's go.mod as ``go mod edit -json`` parses it (never executed), refused when it
    holds what ``go install <pkg>@<version>`` refuses: a ``replace`` or an ``exclude``. Review and
    install then see the same module graph (#2427 round 1)."""
    if not go_mod.is_file():
        raise InstallError("the Go module's go.mod could not be read")
    doc = _go_json(go, ["mod", "edit", "-json", str(go_mod)], cwd, "reading go.mod", 60)
    if doc.get("Replace") or doc.get("Exclude"):
        raise InstallError(
            "this module's go.mod replaces or excludes modules, which go install refuses; "
            "BattleLab builds only what go install builds"
        )
    return doc


#: A semantic-import major-version path element: ``v2`` … ``v9``, ``v10`` and up (never ``v0``,
#: ``v1`` or a leading zero).
_MAJOR = re.compile(r"^v(?:[2-9]|[1-9]\d+)$")


def _go_binary(import_path: str) -> str:
    """The executable ``go install`` writes: the import path's last element, skipping a major
    version suffix (``…/forgejo-mcp/v3`` → ``forgejo-mcp``)."""
    parts = import_path.split("/")
    if len(parts) > 1 and _MAJOR.fullmatch(parts[-1]):
        return parts[-2]
    return parts[-1]


def _pick_command(module: str, mains: list[str], wanted: str | None) -> str:
    """The ONE command a Go module builds: its root if it is a main package, else the single
    ``cmd/<name>``; ``wanted`` (an entry point) picks among several by name."""
    if wanted:
        named = [m for m in mains if _go_binary(m) == wanted]
        if len(named) != 1:
            raise InstallError(f"the module has no command named {wanted}")
        return named[0]
    if module in mains:
        return module
    depth = module.count("/") + 2
    under_cmd = [m for m in mains if m.startswith(module + "/cmd/") and m.count("/") == depth]
    if len(under_cmd) == 1:
        return under_cmd[0]
    if not mains:
        raise InstallError(
            "the module builds no command: no main package at its root or under cmd/"
        )
    shown = ", ".join(m.removeprefix(module + "/") for m in (under_cmd or mains)[:6])
    raise InstallError(
        f"the module builds several commands ({shown}), and BattleLab never picks one for you"
    )


def _go_room() -> None:
    if shutil.disk_usage(root()).free < GO_FREE_MIN:
        raise InstallError(
            "less than 1 GB is free where BattleLab keeps MCP servers; nothing was built"
        )


def _tree_size(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            with contextlib.suppress(OSError):
                total += (Path(dirpath) / name).lstat().st_size
    return total


def _scratch_home() -> Path:
    home = root() / ".cache" / "home"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    return home


def _trim_go_caches(go: Path) -> None:
    """Clear the shared Go caches above the ceiling. Called under `mcp_gotool.job()`: no review or
    build is using them. Installed binaries are never in a cache."""
    cache = root() / ".cache" / "go"
    if _tree_size(cache) <= GO_CACHE_CEILING:
        return
    with contextlib.suppress(InstallError):
        _run([str(go), "clean", "-cache", "-modcache"], root(), "clearing the Go caches", "go",
             120, _go_env(), home=_scratch_home())  # fmt: skip


_WRAPPER = "battlelab.invalid/review"


def _sum_lines(text: str) -> dict[tuple[str, str], str]:
    """``go.sum`` as ``{(path, version or version/go.mod): hash}``."""
    out: dict[tuple[str, str], str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2].startswith("h1:"):
            out[(parts[0], parts[1])] = parts[2]
    return out


def _authenticate_graph(go: Path, source: dict, need: str, moddir: Path, stage: Path) -> list[str]:
    """Every module ``go install <pkg>@<version>`` will use, with its hash checked against the Go
    checksum database (#2427 round 2), and the module's own go.sum shown to agree.

    Go checks a download against the main module's go.sum FIRST and asks sum.golang.org only for a
    missing entry, so the module's go.sum can't be the evidence. A throwaway wrapper module that
    requires exactly the module and starts with an EMPTY go.sum has nothing to trust: every hash
    it records was checked against the database. With replace/exclude refused (`_go_mod`), its
    build list is the module's own (minimal version selection). Returns the dependencies."""
    wrapper = stage / "wrapper"
    wrapper.mkdir(mode=0o700)
    requirement = f"{source['package']} {source['version']}"
    (wrapper / "go.mod").write_text(
        f"module {_WRAPPER}\n\ngo {need or '1.21'}\n\nrequire {requirement}\n"
    )
    (wrapper / "go.sum").write_text("")
    env = {**_go_env(), "GOFLAGS": "-mod=mod"}  # the wrapper's own go.mod/go.sum may be written
    _run([str(go), "mod", "download", "all"], wrapper, "checking every dependency against the "
         "Go checksum database", "go", GO_STEP_TIMEOUT_S, env)  # fmt: skip
    listed = _run([str(go), "list", "-m", "all"], wrapper, "reading the Go module's dependencies",
                  "go", GO_STEP_TIMEOUT_S, env)  # fmt: skip
    authentic = _sum_lines((wrapper / "go.sum").read_text())
    try:
        own = _sum_lines((moddir / "go.sum").read_text())
    except OSError:
        own = {}
    for key, value in own.items():
        if key in authentic and authentic[key] != value:
            raise InstallError(
                f"this module's go.sum doesn't match the Go checksum database for {key[0]} "
                f"{key[1]}; nothing was built"
            )
    deps = []
    for line in listed.splitlines():
        path = line.split(" ", 1)[0].strip()
        if path and path not in (_WRAPPER, source["package"]):
            deps.append(path)
    return deps


@contextlib.contextmanager
def _go_job():
    """One Go review or build (`mcp_gotool.job`, serialized per MCP root) with room checked first
    and the shared caches recovered AFTER it, whatever its outcome: success, failure, timeout or
    cancellation. Still under the build lock, so no job is using the caches."""
    from . import mcp_gotool

    with mcp_gotool.job():
        try:
            _go_room()
            yield
        finally:
            have = mcp_gotool._current()
            if have is not None:
                with contextlib.suppress(Exception):
                    _trim_go_caches(have[1])


def _review_go(source: dict, stage: Path) -> dict:
    from . import mcp_gotool

    with _go_job():
        go = mcp_gotool.ensure(None)
        with mcp_gotool.building():
            if not go.exists():
                raise InstallError("the Go toolchain changed meanwhile; review it again")
            doc = _go_download(go, source, stage)
            mod = _go_mod(go, Path(doc.get("GoMod") or ""), stage)
        need = (mod.get("Go") or "") if isinstance(mod.get("Go"), str) else ""
        go = mcp_gotool.ensure(need or None)  # the module's own go directive (refreshes once)
        moddir = Path(doc.get("Dir") or "")
        if not moddir.is_dir():
            raise InstallError("the Go module's source could not be read")
        with mcp_gotool.building():
            if not go.exists():
                raise InstallError("the Go toolchain changed meanwhile; review it again")
            toolchain = mcp_gotool.identity()
            modules = _authenticate_graph(go, source, need, moddir, stage)
            listed = _run(
                [str(go), "list", "-f", '{{if eq .Name "main"}}{{.ImportPath}}{{end}}', "./..."],
                moddir,
                "reading the Go module's commands",
                "go",
                GO_STEP_TIMEOUT_S,
                _go_env(),
                home=stage,
            )
    mains = sorted({line.strip() for line in listed.splitlines() if line.strip()})
    command = _pick_command(source["package"], mains, source.get("entrypoint"))
    deps = len(modules)
    if deps > DEPENDENCIES_MAX:
        raise InstallError(f"more than {DEPENDENCIES_MAX} dependencies")
    entry = _go_binary(command)
    if not _ENTRY.fullmatch(entry):
        raise InstallError("the module's command name is not one BattleLab runs")
    lock = {"module": source["package"], "version": source["version"], "sum": doc["Sum"],
            "command": command, "toolchain": toolchain}  # fmt: skip
    (stage / "go.lock.json").write_text(json.dumps(lock, sort_keys=True))
    version = toolchain.get("version", "")
    return {
        "lock_file": "go.lock.json",
        "integrity": doc["Sum"],
        "registry": "proxy.golang.org",
        "dependencies": deps,
        "install_scripts_declared": [],
        "entrypoints": sorted(_go_binary(m) for m in mains),
        "entrypoint": entry,
        "command": command,
        "go": need,
        "toolchain": toolchain,
        "license": "",
        "runs": f"{entry} (stdio), built with go{version}" if version else f"{entry} (stdio)",
    }


def _install_go(source: dict, facts: dict, work: Path) -> dict:
    from . import mcp_gotool

    bindir = work / "bin"
    bindir.mkdir(mode=0o700)
    with _go_job():
        with mcp_gotool.building():
            # The compiler is pinned like the source: exactly the reviewed toolchain, or a review.
            if not facts.get("toolchain") or mcp_gotool.identity() != facts["toolchain"]:
                raise InstallError("the Go toolchain changed since the review; review it again")
            go = mcp_gotool.ensure(None)
            doc = _go_download(go, source, work)
            if doc["Sum"] != facts["integrity"]:
                raise InstallError("the Go module's checksum differs from the reviewed one")
            _go_mod(go, Path(doc.get("GoMod") or ""), work)
            _run(
                [
                    str(go),
                    "install",
                    "-p",
                    "2",
                    "-trimpath",
                    f"{facts['command']}@{source['version']}",
                ],  # fmt: skip
                work,
                "building the Go module",
                "go",
                GO_STEP_TIMEOUT_S,
                _go_env(bindir),
            )
        binary = bindir / facts["entrypoint"]
        if not binary.is_file() or binary.is_symlink():
            raise InstallError("the build produced no command")
    return {"dir": str(work), "command": str(binary), "args": [], "entrypoint": facts["entrypoint"]}


# ---- the command a launch execs -----------------------------------------------------------------


def command(rec: dict) -> tuple[str, list[str]]:
    """The installed server's command line. The install must match the server's CURRENT source
    and still be there; otherwise it is not installed (a launch is refused ``not_installed``)."""
    inst = rec.get("installed")
    if not inst or inst.get("source") != rec["source"]:
        raise RegistryError("this server is not installed")
    cmd = inst.get("command")
    args = inst.get("args") or []
    if not isinstance(cmd, str) or not os.path.isabs(cmd) or not os.access(cmd, os.X_OK):
        raise RegistryError("this server's install is missing; install it again")
    for a in args:
        if not isinstance(a, str):
            raise RegistryError("this server's install is damaged")
    return cmd, list(args)
