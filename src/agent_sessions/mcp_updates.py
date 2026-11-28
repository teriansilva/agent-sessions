"""\"Newer version available\" for MCP servers (#2431): a hint, never an install.

For every server with an INSTALLED package (npm, PyPI, or a Go module, #2427) BattleLab reads that
package's own registry record once a day and when the operator asks (Check for updates), and says
whether a newer release exists. It never resolves a repository, reviews a package or installs
anything: an update is always the operator's Review + Install and then a new check.

What "latest" means, read from the registry's own record:

* npm: ``dist-tags.latest`` of the abbreviated metadata;
* PyPI: ``info.version``, only when that release has a wheel that is not yanked (P1b installs
  wheels only);
* Go: the greatest TAGGED semver in the proxy's ``@v/list`` for the installed module path. A
  pseudo-version is never a candidate, and only the installed major is read: ``…/v4`` is
  another module.

"Newer" is strictly greater by the kind's own ordering (semver for npm and Go, PEP 440 for PyPI),
measured against the installed artifact (``installed.source``), never an edited, not yet
installed source. A pre-release is offered only when the installed version is one.

Results live in their own store (``mcp-updates.json``), keyed by server id and fenced by the
identity they were read for (kind, package, installed version): a reinstall, a new source or a
removal makes an old answer unreadable, and a late answer for an old identity is discarded.
Writing it never touches the server record (an open Settings save is never invalidated).

Every read goes through P1b's egress proxy (the kind's registry hosts only), bounded, anonymous,
no redirect. A server's slot is claimed (10 min) BEFORE any network read, so a daily pass and a
manual check coalesce, failures included; identical lookups share one read; at most
`WORKERS` reads run at once, all outside every lock. Page loads and HQ read the cache only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import re
import secrets
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

from . import mcp_install, mcp_registry
from .atomicjson import atomic_write_json, json_write_lock
from .mcp_egress import EgressProxy, origin

log = logging.getLogger("agent_sessions.mcp_updates")

STORE_VERSION = 1
#: A server is read at most once in this window, whatever asks (failed reads included).
THROTTLE_S = 10 * 60
#: An answer older than this is "unknown", never "up to date".
FRESH_S = 3 * 24 * 60 * 60
READ_TIMEOUT_S = 20.0
BODY_MAX = 4 * 1024 * 1024
#: Registry reads at once.
WORKERS = 4
FIRST_DELAY_S = 15 * 60
INTERVAL_S = 24 * 60 * 60
JITTER_S = 60 * 60

NPM_META = "https://registry.npmjs.org/"
PYPI_META = "https://pypi.org/pypi/"
GO_PROXY = "https://proxy.golang.org/"
REGISTRY = {"npm": "npm", "pypi": "PyPI", "go": "Go module proxy"}

_SEMVER = re.compile(
    r"\Av?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?\Z"
)
#: A Go pseudo-version's pre-release tail: ``0.20260101120000-abcdef123456`` or ``…0.2026…``.
_PSEUDO = re.compile(r"(?:\A|\.)0\.\d{14}-[0-9a-f]{12}\Z")
_MAJOR = re.compile(r"/v([2-9]|[1-9]\d+)\Z")


class UpdateError(Exception):
    """A registry read that failed, in words for the operator."""


# ---- version ordering ----------------------------------------------------------------------------


def _semver(v: str) -> tuple | None:
    m = _SEMVER.match(v)
    if not m:
        return None
    pre = m.group(4)
    ids: tuple = ()
    if pre:
        ids = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    # A release sorts after every pre-release of the same core.
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), 0 if pre else 1, ids)


def _pep440(v: str):
    from packaging.version import InvalidVersion, Version

    try:
        return Version(v)
    except InvalidVersion:
        return None


def is_prerelease(kind: str, v: str) -> bool:
    if kind == "pypi":
        parsed = _pep440(v)
        return bool(parsed and (parsed.is_prerelease or parsed.is_devrelease))
    s = _semver(v)
    return bool(s and s[3] == 0)


def newer(kind: str, candidate: str, installed: str) -> bool:
    """Is ``candidate`` strictly newer than ``installed`` and offerable (the pre-release rule)?"""
    if is_prerelease(kind, candidate) and not is_prerelease(kind, installed):
        return False
    if kind == "pypi":
        a, b = _pep440(candidate), _pep440(installed)
    else:
        a, b = _semver(candidate), _semver(installed)
    return a is not None and b is not None and a > b


# ---- what a server is read for ------------------------------------------------------------------


def identity(rec: dict) -> tuple[str, str, str] | None:
    """``(kind, package or module, installed version)`` of the INSTALLED artifact, or None (a
    remote server, nothing installed yet, or an install of an older source)."""
    installed = rec.get("installed")
    if not isinstance(installed, dict):
        return None
    src = installed.get("source")
    if not isinstance(src, dict) or src != rec.get("source"):
        return None  # installed for another source: the current one is not installed
    kind, pkg, ver = src.get("kind"), src.get("package"), src.get("version")
    if kind not in REGISTRY or not isinstance(pkg, str) or not isinstance(ver, str):
        return None
    return kind, pkg, ver


def _go_escape(path: str) -> str:
    """The module proxy's case encoding: each uppercase letter becomes ``!`` + its lowercase."""
    return re.sub(r"[A-Z]", lambda m: "!" + m.group(0).lower(), path)


def _lookup_url(kind: str, pkg: str) -> str:
    if kind == "npm":
        return NPM_META + quote(pkg, safe="@")
    if kind == "pypi":
        return PYPI_META + quote(pkg, safe="") + "/json"
    return GO_PROXY + quote(_go_escape(pkg), safe="/!") + "/@v/list"


# ---- the one outbound site ----------------------------------------------------------------------


def _read(kind: str, url: str) -> bytes | None:
    """One bounded, anonymous GET through the egress proxy (the kind's registry hosts only): the
    body, or None when the registry has no such package (404 / Go's 410). No redirect is
    followed, no ambient proxy or credential is used, TLS is verified."""
    import httpx

    allowed = mcp_install._allowed(kind)
    if origin(url) not in allowed:  # never: the URL is built here from a validated source
        raise UpdateError("that registry address is not allowed")
    registry = REGISTRY[kind]
    headers = {"Accept": "application/vnd.npm.install-v1+json"} if kind == "npm" else {}
    body = bytearray()
    with EgressProxy(allowed) as proxy:
        try:
            with httpx.Client(
                proxy=proxy.url,
                trust_env=False,
                follow_redirects=False,
                timeout=READ_TIMEOUT_S,
                headers=headers,
            ) as client:
                with client.stream("GET", url) as reply:
                    if reply.status_code in (404, 410):
                        return None
                    if reply.status_code != 200:
                        raise UpdateError(f"the {registry} answered {reply.status_code}")
                    for chunk in reply.iter_bytes():
                        body += chunk
                        if len(body) > BODY_MAX:
                            raise UpdateError(f"the {registry}'s answer is too large to read")
        except httpx.HTTPError:
            raise UpdateError(f"the {registry} could not be reached") from None
    return bytes(body)


def _json(kind: str, body: bytes) -> dict:
    try:
        doc = json.loads(body)
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        raise UpdateError(f"the {REGISTRY[kind]}'s answer could not be read")
    return doc


def candidates(kind: str, pkg: str) -> list[str]:
    """The releases the registry offers for ``pkg`` (the kind's rule) that a server might be
    offered: one for npm / PyPI, and for Go the greatest STABLE tag and the greatest tag overall
    (a higher pre-release must never hide a stable update, `pick`). Empty when there is none
    BattleLab could install. Shared by every server naming ``pkg``: which one a server is offered
    depends on its own installed version. Raises `UpdateError` when unreadable."""
    body = _read(kind, _lookup_url(kind, pkg))
    if body is None:
        raise UpdateError(f"the {REGISTRY[kind]} no longer has {pkg}")
    if kind == "npm":
        tags = _json(kind, body).get("dist-tags")
        v = tags.get("latest") if isinstance(tags, dict) else None
        return [v] if isinstance(v, str) and _semver(v) else []
    if kind == "pypi":
        doc = _json(kind, body)
        info = doc.get("info") if isinstance(doc.get("info"), dict) else {}
        v = info.get("version")
        files = doc.get("urls") if isinstance(doc.get("urls"), list) else []
        wheel = any(
            isinstance(f, dict) and f.get("packagetype") == "bdist_wheel" and not f.get("yanked")
            for f in files
        )
        return [v] if isinstance(v, str) and wheel and _pep440(v) else []
    return _go_candidates(pkg, body)


def pick(kind: str, offered: list[str], installed: str) -> str | None:
    """The release ``installed`` is measured against: the greatest of ``offered`` the pre-release
    rule lets it see (a pre-release only for a pre-release). None when there is none."""
    order = _pep440 if kind == "pypi" else _semver
    seen = [
        v
        for v in offered
        if order(v) is not None
        and not (is_prerelease(kind, v) and not is_prerelease(kind, installed))
    ]
    return max(seen, key=order) if seen else None


def _go_candidates(module: str, body: bytes) -> list[str]:
    """The greatest stable tag and the greatest tag overall of the installed major."""
    m = _MAJOR.search(module)
    major = int(m.group(1)) if m else None
    best = stable = None
    for line in body.decode("utf-8", "replace").splitlines()[:10000]:
        v = line.strip()
        s = _semver(v)
        if s is None or not v.startswith("v") or "+" in v:
            continue  # not a semver tag, or a +incompatible / build-tagged one
        pre = _SEMVER.match(v).group(4) or ""
        if _PSEUDO.search(pre):
            continue
        if (major is None and s[0] > 1) or (major is not None and s[0] != major):
            continue  # another major is another module
        if best is None or s > _semver(best):
            best = v
        if s[3] == 1 and (stable is None or s > _semver(stable)):
            stable = v
    return [v for v in dict.fromkeys([stable, best]) if v is not None]


# ---- the store ----------------------------------------------------------------------------------


def store_path() -> Path:
    return mcp_registry.store_path().with_name("mcp-updates.json")


def _load() -> dict:
    try:
        doc = json.loads(store_path().read_bytes())
    except (FileNotFoundError, ValueError):
        return {"version": STORE_VERSION, "servers": {}}
    if not isinstance(doc, dict) or doc.get("version") != STORE_VERSION:
        return {"version": STORE_VERSION, "servers": {}}
    if not isinstance(doc.get("servers"), dict):
        doc["servers"] = {}
    return doc


@contextlib.contextmanager
def _mutate():
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with json_write_lock(path):
        doc = _load()
        yield doc["servers"]
        atomic_write_json(path, doc)


def _entry(servers: dict, sid: str, ident: tuple) -> dict | None:
    e = servers.get(sid)
    return e if isinstance(e, dict) and e.get("identity") == list(ident) else None


def _claim(sid: str, ident: tuple, now: float) -> str | None:
    """Take the server's throttle slot for this identity: the claim's token, or None when the
    slot was taken < 10 min ago, or when the server no longer has ``ident``. The identity is read
    again here, under the store lock: a start that read it earlier and was delayed must never
    replace a newer identity's entry or its slot (#2431)."""
    with _mutate() as servers:
        try:
            current = identity(mcp_registry.get(sid))
        except mcp_registry.RegistryError:
            current = None
        if current != ident:
            return None
        e = _entry(servers, sid, ident)
        if e is not None and now - float(e.get("claimed_at") or 0) < THROTTLE_S:
            return None
        e = e or {"identity": list(ident)}
        token = secrets.token_hex(8)
        e.update(claimed_at=now, claim=token)
        servers[sid] = e
        return token


def _settle(
    sid: str,
    ident: tuple,
    token: str,
    *,
    latest: str | None = None,
    error: str | None = None,
) -> None:
    """Record an answer for the claim ``token`` made, under the store lock, and only while the
    entry still belongs to that claim and the server still has that identity. A late settlement
    (the source was replaced and a newer claim took the entry, or the server was removed) writes
    nothing over another claim's answer or its throttle slot (#2431)."""
    with _mutate() as servers:
        e = servers.get(sid)
        mine = isinstance(e, dict) and e.get("identity") == list(ident) and e.get("claim") == token
        if not mine:
            return  # another claim's entry, or none: never replaced
        try:
            rec = mcp_registry.get(sid)
        except mcp_registry.RegistryError:
            rec = None
        if rec is None or identity(rec) != ident:
            servers.pop(sid, None)  # removed, or no longer this identity: nothing to keep
            return
        now = time.time()
        if error is None:
            e.update(latest=latest, checked_at=now)
            e.pop("error", None)
            e.pop("tried_at", None)
        else:
            e.update(error=error, tried_at=now)
        servers[sid] = e


def forget(sid: str) -> None:
    """Drop a removed server's answer (its identity fence would hide it anyway)."""
    with _mutate() as servers:
        servers.pop(sid, None)


# ---- reading it (cache only) --------------------------------------------------------------------


def view(rec: dict, *, now: float | None = None, store: dict | None = None) -> dict | None:
    """What the page and HQ show, from the cache only; None when the server has no installed
    package version (a remote server, or nothing installed).

    ``state``: ``available`` (a fresh answer names a newer release), ``current`` (a fresh answer
    names nothing newer), ``unknown`` (no answer yet, or the last one is older than 3 days).
    ``error`` is the last failed attempt after the last success, in words."""
    ident = identity(rec)
    if ident is None:
        return None
    now = time.time() if now is None else now
    servers = (store if store is not None else _load())["servers"]
    e = _entry(servers, rec["id"], ident) or {}
    checked = e.get("checked_at")
    fresh = isinstance(checked, int | float) and now - checked <= FRESH_S
    found = e.get("latest") if fresh else None
    if fresh and isinstance(found, str) and newer(ident[0], found, ident[2]):
        state = "available"
    elif fresh:
        state = "current"
    else:
        state = "unknown"
    return {
        "state": state,
        "installed": ident[2],
        "latest": found if state == "available" else None,
        "checked_at": checked if isinstance(checked, int | float) else None,
        "error": e.get("error") if isinstance(e.get("error"), str) else None,
        "tried_at": e.get("tried_at") if isinstance(e.get("tried_at"), int | float) else None,
        "checking": rec["id"] in _running,
    }


def snapshot() -> dict:
    """The store once, for a list of servers (`view(..., store=...)`)."""
    return _load()


# ---- checking -----------------------------------------------------------------------------------

_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()
#: Lookups being read now, keyed by (kind, package): identical ones share one read.
_inflight: dict[tuple[str, str], Future] = {}
_running: set[str] = set()
_state_lock = threading.Lock()
#: The last daily pass: when, how long, how many servers.
last_sweep: dict = {}


def _executor() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="mcp-updates")
        return _pool


def _lookup(kind: str, pkg: str) -> Future:
    key = (kind, pkg)
    with _state_lock:
        fut = _inflight.get(key)
        if fut is not None:
            return fut
        fut = _executor().submit(candidates, kind, pkg)
        _inflight[key] = fut
    # Outside the lock: a read that already finished runs its callback right here.
    fut.add_done_callback(lambda f, k=key: _drop(k, f))
    return fut


def _drop(key: tuple[str, str], fut: Future) -> None:
    with _state_lock:
        if _inflight.get(key) is fut:
            del _inflight[key]


def _words(exc: BaseException) -> str:
    return str(exc) if isinstance(exc, UpdateError) else "the registry's answer could not be read"


def start(sid: str) -> Future | None:
    """Check one server now: claim its slot, then read in the background. Returns the pending
    read (resolves to its settled view), or None when there is nothing to read (no installed
    version) or the slot was taken in the last 10 minutes. Never blocks on the network."""
    rec = mcp_registry.get(sid)
    ident = identity(rec)
    token = _claim(sid, ident, time.time()) if ident is not None else None
    if ident is None or token is None:
        return None
    with _state_lock:
        _running.add(sid)
    fut = _lookup(ident[0], ident[1])
    done: Future = Future()

    def finish(f: Future) -> None:
        try:
            try:
                offered = f.result()
            except Exception as e:  # noqa: BLE001 — every failure is recorded in words
                _settle(sid, ident, token, error=_words(e))
            else:
                _settle(sid, ident, token, latest=pick(ident[0], offered, ident[2]))
        finally:
            with _state_lock:
                _running.discard(sid)
            done.set_result(None)

    fut.add_done_callback(finish)
    return done


def sweep() -> int:
    """The daily pass: start every server with an installed version (each claims its own slot,
    so a server checked by hand in the last 10 minutes is skipped), and wait for them. Returns
    how many were read."""
    t0 = time.monotonic()
    try:
        recs = mcp_registry.list_servers()
    except mcp_registry.RegistryError:
        return 0
    pending = []
    for rec in recs:
        try:
            fut = start(rec["id"])
        except mcp_registry.RegistryError:
            continue
        if fut is not None:
            pending.append(fut)
    for fut in pending:
        fut.result()
    last_sweep.update(at=time.time(), seconds=round(time.monotonic() - t0, 3), read=len(pending))
    return len(pending)


async def run() -> None:
    """The daily loop (started from the app lifespan): first pass a while after startup, then
    once a day, jittered so a fleet of installs does not read the registries at one instant."""
    await asyncio.sleep(FIRST_DELAY_S + random.uniform(0, JITTER_S))  # noqa: S311 — spread, not security
    while True:
        try:
            n = await asyncio.to_thread(sweep)
            if n:
                log.info("MCP update pass: %d server(s) read", n)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("MCP update pass crashed")
        await asyncio.sleep(INTERVAL_S + random.uniform(-JITTER_S, JITTER_S))  # noqa: S311
