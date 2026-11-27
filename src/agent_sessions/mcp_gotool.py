"""BattleLab's own Go toolchain, for building an MCP server from a Go repository link (#2427).

Never the system's Go and never the operator's PATH: the newest stable release from
``go.dev/dl/?mode=json``, its tarball from ``dl.google.com``, compared with the sha256 and size
that list states, extracted with tarfile's ``data`` filter (no links out, no device files) under
``<MCP root>/.toolchain/go<version>/`` and made read-only. Every read goes through P1b's egress
proxy with exactly those two hosts allowed (`mcp_egress`), TLS verified, no redirects.

One current toolchain, identified by `identity()` (version, the tarball's sha256, platform), which a
review records and an install must find unchanged (#2427 round 1: the compiler is pinned like the
source). `ensure(min_go)` returns its ``go`` binary, refreshing to the newest stable release when
the current one is older than ``min_go`` (a module's ``go`` directive) and refusing when even that
is older. A refresh holds the toolchain lock EXCLUSIVELY; a running Go step holds it SHARED
(`building`), so a toolchain is never replaced under it. `job()` serializes Go work per MCP root:
one review or build at a time on a shared host.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

from . import mcp_jobs
from .mcp_egress import EgressProxy, origin
from .mcp_registry import RegistryError

log = logging.getLogger(__name__)

DL_INDEX = "https://go.dev/dl/?mode=json"
DL_FILES = "https://dl.google.com/go/"
#: The hosts a toolchain read may reach, nothing else.
TOOL_HOSTS = frozenset({origin(DL_INDEX), origin(DL_FILES)})
INDEX_MAX = 1024 * 1024
TARBALL_MAX = 400 * 1024 * 1024
READ_TIMEOUT_S = 60.0
#: The whole tarball download (a slow link moves ~70 MB in minutes).
DOWNLOAD_TIMEOUT_S = 1500.0
_GOVER = re.compile(r"^(?:go)?(\d+)\.(\d+)(?:\.(\d+))?$")
_FILENAME = re.compile(r"^go\d+\.\d+(?:\.\d+)?\.linux-(?:amd64|arm64)\.tar\.gz$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ToolchainError(RegistryError):
    """The toolchain could not be had; the message is words for the operator."""


def version_tuple(raw: object) -> tuple[int, int, int] | None:
    """``"1.26"`` / ``"1.26.0"`` / ``"go1.27.4"`` → ``(1, 26, 0)``; anything else → None."""
    if not isinstance(raw, str):
        return None
    m = _GOVER.fullmatch(raw.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def _arch() -> str:
    machine = os.uname().machine
    if machine in ("x86_64", "amd64"):
        return "amd64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    raise ToolchainError(f"there is no Go toolchain for this machine ({machine})")


def root() -> Path:
    from . import mcp_install

    return mcp_install.root() / ".toolchain"


@contextlib.contextmanager
def _locked(mode: int) -> Iterator[None]:
    base = root()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(base / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        # Polled, not blocking: a Stop while waiting ends the wait (#2427).
        while True:
            try:
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                mcp_jobs.raise_if_stopped()
                time.sleep(0.2)
        yield
    finally:
        os.close(fd)


def _current() -> tuple[tuple[int, int, int], Path, dict] | None:
    """The installed toolchain's version, its ``go`` binary and its identity, or None."""
    try:
        ident = json.loads((root() / "current").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(ident, dict) or not isinstance(ident.get("version"), str):
        return None
    ver = version_tuple(ident["version"])
    go = root() / f"go{ident['version']}" / "bin" / "go"
    if ver is None or not go.is_file() or not os.access(go, os.X_OK):
        return None
    return ver, go, ident


def identity() -> dict:
    """``{version, sha256, platform}`` of the current toolchain ({} when there is none): what a
    review records and an install must find unchanged."""
    have = _current()
    return dict(have[2]) if have else {}


# ---- reads (the one outbound site) -------------------------------------------------------------


def _get(url: str, sink, limit: int, timeout: float) -> None:
    """One bounded, anonymous GET through the egress proxy (`TOOL_HOSTS` only): the body is
    written to ``sink``. No redirect, no ambient proxy or credential, TLS verified."""
    import httpx

    if origin(url) not in TOOL_HOSTS:  # never: every URL is built here from fixed origins
        raise ToolchainError("that toolchain address is not allowed")
    got = 0
    with EgressProxy(set(TOOL_HOSTS)) as proxy:
        try:
            with httpx.Client(
                proxy=proxy.url,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(READ_TIMEOUT_S, read=READ_TIMEOUT_S),
            ) as client:
                with client.stream("GET", url) as reply:
                    if reply.status_code != 200:
                        raise ToolchainError(
                            f"the Go download site answered {reply.status_code}; try again later"
                        )
                    for chunk in reply.iter_bytes():
                        mcp_jobs.raise_if_stopped()
                        got += len(chunk)
                        if got > limit:
                            raise ToolchainError("the Go download is larger than expected")
                        sink(chunk)
        except httpx.HTTPError:
            raise ToolchainError(
                "the Go download site could not be reached; try again later"
            ) from None


def _newest_stable() -> tuple[str, str, str, int]:
    """``(version, filename, sha256, size)`` of the newest stable release for this machine."""
    body = bytearray()
    _get(DL_INDEX, body.extend, INDEX_MAX, READ_TIMEOUT_S)
    try:
        releases = json.loads(bytes(body))
    except ValueError:
        releases = None
    if not isinstance(releases, list):
        raise ToolchainError("the Go release list could not be read")
    arch = _arch()
    for rel in releases:
        if not isinstance(rel, dict) or rel.get("stable") is not True:
            continue
        ver = version_tuple(rel.get("version"))
        for f in rel.get("files") or []:
            if not isinstance(f, dict):
                continue
            if (f.get("os"), f.get("arch"), f.get("kind")) != ("linux", arch, "archive"):
                continue
            name, sha, size = f.get("filename"), f.get("sha256"), f.get("size")
            if (
                ver is not None
                and isinstance(name, str)
                and _FILENAME.fullmatch(name)
                and isinstance(sha, str)
                and _SHA256.fullmatch(sha)
                and isinstance(size, int)
                and 0 < size <= TARBALL_MAX
            ):
                return ".".join(map(str, ver)), name, sha, size
    raise ToolchainError("the Go release list names no stable release for this machine")


# ---- install ------------------------------------------------------------------------------------


def _install(version: str, filename: str, sha: str, size: int) -> Path:
    """Download, verify, extract and freeze ``version``; point ``current`` at it. Under the
    exclusive lock. Nothing is kept on any failure."""
    from . import mcp_install

    base = root()
    dest = base / f"go{version}"
    work = Path(tempfile.mkdtemp(prefix="fetch-", dir=base))
    try:
        tarball = work / filename
        digest = hashlib.sha256()
        with tarball.open("wb") as out:

            def sink(chunk: bytes) -> None:
                digest.update(chunk)
                out.write(chunk)

            _get(DL_FILES + filename, sink, size, DOWNLOAD_TIMEOUT_S)
        if tarball.stat().st_size != size or digest.hexdigest() != sha:
            raise ToolchainError(
                "the downloaded Go toolchain does not match go.dev's checksum; nothing was kept"
            )
        with tarfile.open(tarball, "r:gz") as tar:
            tar.extractall(work / "x", filter="data")
        go = work / "x" / "go" / "bin" / "go"
        if not go.is_file():
            raise ToolchainError("the Go toolchain archive has no go binary")
        if dest.exists():
            _thaw(dest)
            shutil.rmtree(dest)
        (work / "x" / "go").rename(dest)
        mcp_install._freeze(dest)
        tmp = base / ".current.tmp"
        ident = {"version": version, "sha256": sha, "platform": f"linux/{_arch()}"}
        tmp.write_text(json.dumps(ident, sort_keys=True))
        tmp.replace(base / "current")
    finally:
        _thaw(work)
        shutil.rmtree(work, ignore_errors=True)
    for old in base.glob("go*"):  # one current toolchain; builds hold the lock shared
        if old.is_dir() and old.name != dest.name:
            _thaw(old)
            shutil.rmtree(old, ignore_errors=True)
    return dest / "bin" / "go"


def _thaw(path: Path) -> None:
    """Writable again, so a frozen toolchain can be removed (our own directory only)."""
    for dirpath, dirnames, _ in os.walk(path, followlinks=False):
        for name in dirnames:
            p = Path(dirpath) / name
            if not p.is_symlink():
                with contextlib.suppress(OSError):
                    p.chmod(0o700)
    with contextlib.suppress(OSError):
        path.chmod(0o700)


def ensure(min_go: object = None) -> Path:
    """The ``go`` binary of a toolchain at least ``min_go`` (a module's ``go`` directive),
    downloading the newest stable release when there is none or it is too old."""
    need = version_tuple(min_go) if min_go is not None else (0, 0, 0)
    if need is None:
        raise ToolchainError(f"the module's go directive {min_go!r} is not a Go version")
    with _locked(fcntl.LOCK_SH):
        have = _current()
        if have is not None and have[0] >= need:
            return have[1]
    with _locked(fcntl.LOCK_EX):
        have = _current()  # another request may have refreshed meanwhile
        if have is not None and have[0] >= need:
            return have[1]
        version, filename, sha, size = _newest_stable()
        if version_tuple(version) < need:
            raise ToolchainError(
                f"this module needs Go {'.'.join(map(str, need))}, newer than the newest stable "
                f"release ({version})"
            )
        log.info("MCP: installing the Go %s toolchain", version)
        return _install(version, filename, sha, size)


@contextlib.contextmanager
def building() -> Iterator[None]:
    """Hold while a Go step runs: the toolchain is not replaced under it. Never call `ensure`
    while holding it (a refresh needs the lock exclusively)."""
    with _locked(fcntl.LOCK_SH):
        yield


#: How long a Go job waits for another one to finish before giving up in words (#2427).
JOB_WAIT_S = 1200.0


@contextlib.contextmanager
def job(wait_s: float | None = None) -> Iterator[None]:
    """One Go review or build at a time per MCP root (a separate lock from the toolchain's, so a
    job may still `ensure` a toolchain inside it). Waits at most ``wait_s`` (`JOB_WAIT_S`)."""
    base = root()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(base / ".build.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + (JOB_WAIT_S if wait_s is None else wait_s)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                mcp_jobs.raise_if_stopped()
                if time.monotonic() >= deadline:
                    raise ToolchainError(
                        "another Go build is still running; try again in a few minutes"
                    ) from None
                time.sleep(0.5)
        yield
    finally:
        os.close(fd)


def current_version() -> str:
    have = _current()
    return "go" + ".".join(map(str, have[0])) if have else ""
