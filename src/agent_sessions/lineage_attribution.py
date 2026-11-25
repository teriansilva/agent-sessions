"""Process attribution for session lineage (#2325).

A session another agent started is a full session with no recorded link to whoever started it.
This background task proves the link from the child's live process, off the request path:

* **cgroup** — the child process sits in the scope (or native-worker unit) of the session that
  forked it. A unit counts only through an EXACT record: ``lineage.bindings[unit]`` written by the
  LAUNCH path, or a native worker's own config. A ``sid8`` prefix in a scope name is never read.
* **environment** — every BattleLab launch exports ``BATTLELAB_SESSION=<its own key>``, and a child
  inherits it. A value equal to the child's OWN key is not evidence; a value naming a different,
  known session is.

Disagreement between the two, a self-link, a cycle or a candidate nobody lists → ``conflict``: no
link. Evidence that is not there yet (no pid registered, a transient ``/proc`` error) is
``unknown`` and retried on :data:`lineage.RETRY_BACKOFF_S`; a process that was read and carries
nothing is a definitive ``none``. Only ``/proc`` and the engine's own registry are read — no
subprocess (the shell-free guarantee). Engines are reached through the provider's optional
``session_pids`` hook; one without it answers ``unavailable``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Mapping
from contextlib import suppress

from . import engines, lineage

log = logging.getLogger("agent_sessions.lineage")

ENV_SESSION = "BATTLELAB_SESSION"
#: ``/proc/<pid>/environ`` is read at most this far: an environment is kilobytes.
ENVIRON_MAX = 1 << 20
#: A session with no live process whose transcript has not moved for this long cannot be
#: attributed any more: its process is gone. It settles at once instead of using the retry budget.
STALE_S = 900.0
#: Seconds between sweeps.
INTERVAL_S = 15.0
#: Keys attributed per sweep, so a first run over a large history spreads out.
BATCH = 400

_NATIVE_UNIT = re.compile(r"^battlelab-native-([0-9a-f]{32})\.service$")
_PROC_ROOT = "/proc"


def enabled() -> bool:
    return os.environ.get("AGENT_SESSIONS_LINEAGE_ATTRIBUTION", "1") != "0"


def _unit_of(pid: int) -> str | None:
    """The leaf unit of ``pid``'s cgroup (``as-….scope`` / ``battlelab-native-….service`` / any)."""
    with open(f"{_PROC_ROOT}/{pid}/cgroup", encoding="utf-8") as fh:
        for line in fh:
            return line.strip().rpartition(":")[2].rpartition("/")[2] or None
    return None


def _env_value(pid: int) -> str | None:
    fd = os.open(f"{_PROC_ROOT}/{pid}/environ", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        raw = b""
        while len(raw) <= ENVIRON_MAX:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            raw += chunk
    finally:
        os.close(fd)
    if len(raw) > ENVIRON_MAX:
        raise OSError("environment too large")
    prefix = f"{ENV_SESSION}=".encode()
    for item in raw.split(b"\0"):
        if item.startswith(prefix):
            return item[len(prefix) :].decode("utf-8", "replace") or None
    return None


def _native_unit_key(unit: str) -> str | None:
    m = _NATIVE_UNIT.match(unit)
    if not m:
        return None
    h = m.group(1)
    worker_id = f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
    try:
        from . import native_state

        cfg = native_state.read_config(worker_id)
    except Exception:  # noqa: BLE001 — unreadable is no evidence, never a guess
        return None
    key = cfg.get("session_key") if isinstance(cfg, dict) else None
    return key if isinstance(key, str) and key else None


def _unit_key(unit: str | None, doc: Mapping) -> str | None:
    if not unit:
        return None
    return lineage.scope_key(unit, doc) or _native_unit_key(unit)


def evaluate(
    key: str,
    pids: list[int],
    doc: Mapping,
    known: set[str],
    aliases: Mapping[str, str],
) -> tuple[str, str | None]:
    """``(outcome, parent)`` for one session from its live pids. Pure apart from ``/proc``."""
    parents: set[str] = set()
    read_any = False
    for pid in pids:
        try:
            unit = _unit_of(pid)
            env = _env_value(pid)
        except FileNotFoundError:
            continue  # gone between the registry read and now
        except OSError:
            return "unknown", None
        read_any = True
        for raw in (_unit_key(unit, doc), env):
            if not raw:
                continue
            cand = engines.logical_key(raw, dict(aliases))
            if cand != key:
                parents.add(cand)
    if not read_any:
        return "unknown", None
    if not parents:
        return "none", None
    if len(parents) > 1:
        return "conflict", None
    (parent,) = parents
    if parent not in known:
        return "conflict", None
    return "linked", parent


def _native_links(hook, keys: list[str], by_key: Mapping, known: set[str], ts: float) -> list[str]:
    """The engine's own parent link (``native`` source), through the provider's optional
    ``lineage(native_id) -> parent key | None`` hook. Returns the keys left for process
    attribution.
    A parent nobody lists is not linked (the key falls through to process attribution)."""
    rest = []
    for k in keys:
        try:
            parent = hook(by_key[k].uuid)
        except Exception:  # noqa: BLE001 — a broken hook is no evidence
            parent = None
        if isinstance(parent, str) and parent in known and parent != k:
            try:
                lineage.link(k, parent, "native", now=ts)
                continue
            except (lineage.LineageError, lineage.LineageUnreadable, OSError):
                pass
        rest.append(k)
    return rest


def sweep(*, now: float | None = None) -> int:
    """Attribute the sessions that are due. Returns how many attempts were recorded."""
    ts = time.time() if now is None else now
    from . import metadata

    walk = engines.scan_all_cached()
    by_key = {engines.session_key(s): s for s in walk}
    due = lineage.attribution_due(by_key, now=ts)[:BATCH]
    if not due:
        return 0
    try:
        doc = lineage.read_checked()
    except lineage.LineageUnreadable:
        return 0
    aliases = metadata.load_aliases()
    known = set(by_key)
    by_engine: dict[str, list[str]] = {}
    for k in due:
        by_engine.setdefault(by_key[k].engine, []).append(k)
    results: list[tuple[str, str, str | None]] = []
    for engine_id, keys in list(by_engine.items()):
        prov = engines.get_any(engine_id)
        native_hook = getattr(prov, "lineage", None) if prov is not None else None
        if callable(native_hook):
            keys = by_engine[engine_id] = _native_links(native_hook, keys, by_key, known, ts)
        hook = getattr(prov, "session_pids", None) if prov is not None else None
        if not callable(hook):
            results.extend((k, "unavailable", None) for k in keys)
            continue
        natives = {by_key[k].uuid: k for k in keys}
        try:
            answer = hook(list(natives))
        except Exception:  # noqa: BLE001 — a broken hook is "not yet", never a link
            log.debug("session_pids failed for %s", engine_id, exc_info=True)
            answer = None
        if answer is None:
            results.extend((k, "unknown", None) for k in keys)
            continue
        for native, k in natives.items():
            pids = [p for p in answer.get(native, []) if isinstance(p, int) and p > 0]
            if not pids:
                stale = (ts - float(by_key[k].last_mtime or 0)) > STALE_S
                results.append((k, "none-final" if stale else "unknown", None))
                continue
            outcome, parent = evaluate(k, pids, doc, known, aliases)
            results.append((k, outcome, parent))
    try:
        lineage.record_attributions(results, now=ts)
    except (lineage.LineageUnreadable, lineage.LineageError, OSError):
        log.warning("lineage attribution could not be recorded", exc_info=True)
        return 0
    linked = sum(1 for _, o, _ in results if o == "linked")
    if linked:
        log.info("lineage: linked %d session(s) to their parent", linked)
    return len(results)


async def run() -> None:
    """The background loop (started in ``main``'s lifespan)."""
    while True:
        await asyncio.sleep(INTERVAL_S)
        if not enabled():
            continue
        with suppress(Exception):
            await asyncio.to_thread(sweep)
