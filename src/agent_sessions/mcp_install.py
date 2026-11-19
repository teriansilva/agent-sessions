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

The registries are fixed (``registry.npmjs.org``, ``pypi.org``); a lock entry resolved anywhere
else is refused. Every tool runs as a literal argv (no shell), with a whitelisted environment
(no proxy or npm/uv configuration from the operator's environment or home: ``--no-config``,
``npm_config_userconfig=/dev/null``), bounded in time and output. An installed directory is made
read-only. A server's command is resolved from its install, which must match its CURRENT source.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
import time
from pathlib import Path

from .mcp_egress import EgressProxy, origin
from .mcp_registry import RegistryError

log = logging.getLogger(__name__)

NPM_REGISTRY = "https://registry.npmjs.org/"
PYPI_INDEX = "https://pypi.org/simple"
PYPI_FILES = "https://files.pythonhosted.org/"
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
    if kind == "npm":
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
        raise RegistryError("source kind is npm or pypi")
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
    return {origin(PYPI_INDEX), origin(PYPI_FILES)}


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


def _run(argv: list[str], cwd: Path, what: str, kind: str, timeout: int = TOOL_TIMEOUT_S) -> str:
    """One tool run: literal argv, whitelisted env, egress only to ``kind``'s registry
    (`mcp_egress`), bounded. Raises `InstallError` in words; the tool's own output is logged (it
    can name packages, never a credential: none is involved)."""
    with EgressProxy(_allowed(kind)) as proxy:
        try:
            proc = subprocess.run(  # noqa: S603 — a literal argv, never a shell
                argv,
                cwd=cwd,
                env=_env(cwd, proxy.url),
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise InstallError(f"{what} took longer than {timeout} s") from None
        except OSError as e:
            raise InstallError(f"{what} could not run ({e.strerror or type(e).__name__})") from None
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


def _hint(err: str) -> str:
    low = err.lower()
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
    if source["kind"] not in ("npm", "pypi"):
        raise InstallError("only a package source is reviewed")
    review_id = secrets.token_hex(8)
    stage = _private_dir(_staging(rec["id"], review_id))
    try:
        facts = (
            _review_npm(source, stage) if source["kind"] == "npm" else _review_pypi(source, stage)
        )
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
