"""Which models a native API client can run (#1313) — asked of the client, never invented.

A native API client (#1311) drives its source agent's own CLI, so the only honest model list is
the one that CLI reports through its own protocol:

* ``codex-app-server`` — the app-server's ``model/list`` (paged), with each model's reasoning
  efforts and its default flag.
* ``claude-stream-json`` — the ``models`` array of the stream-JSON ``initialize`` control
  response (the list Claude Code's own model picker shows).
* ``opencode-acp`` — ``opencode models`` (``provider/model`` per line), the ids its ACP
  ``session/new`` offers as the ``model`` config option; run under the store's shared admission.

Every adapter kind in `kinds.API_KINDS` has an entry in `_DISCOVER` (a test pins it), so a new
adapter cannot ship without deciding where its list comes from.

The probe spawns the admitted source binary with a literal argv and a minimal environment — no
shell, no prompt, no session: neither CLI writes history before a first turn. It is bounded by a
deadline and a byte cap, and the answer is cached per binary identity (path + inode + size +
mtime) for `TTL_S`; a failure is cached briefly so a broken CLI is not re-spawned per request.

**Served from cache, refreshed behind it (#1503).** A list request never waits for a probe while
a list for the same binary exists: `models` reads the cache under the short `_GUARD` only (never a
probe lock) and returns an expired list at once, marked `stale`, after asking the ONE background
refresher (`_Refresher`) for a new one. Only a true miss — nothing ever read for this binary —
probes on the caller's thread, and concurrent callers of the same key join that one probe. A
failed refresh never replaces a good list: the list is kept and the failure rides beside it as
`refresh_error`. The lifespan warms every admitted client at startup and re-probes each as it
expires (`models_loop`), and drains the refresher on shutdown (`shutdown`).

A create (and a Settings save) checks model and effort against ONE list (`snapshot_for`); an id
absent from a stale list forces one fresh probe (`models_fresh`) before it is refused.

A list that could not be read is `unavailable` with the reason. Callers then offer `default`
only; nothing is substituted (`model_choice.select_api`).
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import selectors
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .plugins import api_source
from .plugins.manifest import _MODEL_ID_RE

TTL_S = 600.0
FAILURE_TTL_S = 30.0
TIMEOUT_S = 20.0
MAX_BYTES = 4 * 1024 * 1024
MAX_PAGES = 10
MAX_MODELS = 200
_EFFORT = re.compile(r"\A[a-z][a-z0-9_-]{0,31}\Z")
_TEXT_MAX = 200
log = logging.getLogger(__name__)

#: The cache's clock; a test swaps it for a fake one. `_Proc`'s deadline keeps the real one.
_clock = time.monotonic


class ProbeError(RuntimeError):
    """The client's model list could not be read; the message is a user-facing reason."""


@dataclass(frozen=True)
class ModelList:
    status: str  # "ok" | "unavailable"
    models: tuple[dict[str, Any], ...] = ()
    reason: str | None = None
    fetched_at: float = field(default=0.0, compare=False)
    #: Served past `TTL_S`: a background refresh has been asked for (#1503).
    stale: bool = field(default=False, compare=False)
    #: Seconds since the CLI was asked for this list, when it was served; None if never asked.
    fetched_age_s: float | None = field(default=None, compare=False)
    #: The newest refresh failed and this is still the last GOOD list. A fixed, user-facing
    #: reason from this module (never CLI output), bounded like every text field here.
    refresh_error: str | None = field(default=None, compare=False)

    def ids(self) -> set[str]:
        return {m["id"] for m in self.models}

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": self.status,
            "models": [dict(m) for m in self.models],
            "reason": self.reason,
            "stale": self.stale,
            "fetched_age_s": None if self.fetched_age_s is None else int(self.fetched_age_s),
        }
        if self.refresh_error:
            out["refresh_error"] = self.refresh_error
        return out


def _text(value: Any) -> str | None:
    return value[:_TEXT_MAX] if isinstance(value, str) and value else None


def _list(value: Any) -> list:
    """A CLI-reported array, or nothing: a malformed reply never reaches iteration (#1313)."""
    return value if isinstance(value, list) else []


def _model(mid: Any, *, label: Any, description: Any, efforts: Any, is_default: Any) -> dict | None:
    if not isinstance(mid, str) or not _MODEL_ID_RE.fullmatch(mid) or mid == "default":
        return None
    return {
        "id": mid,
        "label": _text(label) or mid,
        "description": _text(description),
        "efforts": [e for e in _list(efforts) if isinstance(e, str) and _EFFORT.fullmatch(e)],
        "is_default": is_default is True,
    }


class _Proc:
    """A short-lived JSON-lines conversation with a CLI under one deadline."""

    def __init__(self, argv: list[str]):
        self.proc = subprocess.Popen(  # noqa: S603 — admitted binary, literal argv, no shell
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=str(Path.home()),
            env={
                "HOME": str(Path.home()),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LANG": "C.UTF-8",
            },
        )
        self.deadline = time.monotonic() + TIMEOUT_S
        self.buf = b""
        self.read_total = 0
        self.sel = selectors.DefaultSelector()
        self.sel.register(self.proc.stdout, selectors.EVENT_READ)

    def send(self, frame: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(frame) + "\n").encode())
        self.proc.stdin.flush()

    def read(self) -> dict:
        while True:
            line, sep, rest = self.buf.partition(b"\n")
            if sep:
                self.buf = rest
                try:
                    frame = json.loads(line)
                except ValueError:
                    continue
                if isinstance(frame, dict):
                    return frame
                continue
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or not self.sel.select(remaining):
                raise ProbeError("the agent did not answer in time")
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise ProbeError("the agent exited before listing its models")
            self.read_total += len(chunk)
            if self.read_total > MAX_BYTES:
                raise ProbeError("the agent's answer was too large")
            self.buf += chunk

    def answer(self, match) -> dict:
        while True:
            frame = self.read()
            if match(frame):
                return frame

    def close(self) -> None:
        self.sel.close()
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        for pipe in (self.proc.stdin, self.proc.stdout):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass


def _codex(binary: str) -> list[dict]:
    conv = _Proc([binary, "app-server"])
    try:
        conv.send(
            {
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "battlelab", "version": "1"}},
            }
        )
        conv.answer(lambda f: f.get("id") == 1)
        conv.send({"method": "initialized"})
        out: list[dict] = []
        cursor = None
        for page in range(MAX_PAGES):
            rid = 2 + page
            conv.send(
                {"id": rid, "method": "model/list", "params": {"cursor": cursor} if cursor else {}}
            )
            reply = conv.answer(lambda f, rid=rid: f.get("id") == rid)
            if "error" in reply:
                raise ProbeError("the agent refused to list its models")
            result = reply.get("result") if isinstance(reply.get("result"), dict) else {}
            for item in _list(result.get("data")):
                if not isinstance(item, dict) or item.get("hidden") is True:
                    continue
                efforts = [
                    e.get("reasoningEffort")
                    for e in _list(item.get("supportedReasoningEfforts"))
                    if isinstance(e, dict)
                ]
                m = _model(
                    item.get("model") or item.get("id"),
                    label=item.get("displayName"),
                    description=item.get("description"),
                    efforts=efforts,
                    is_default=item.get("isDefault"),
                )
                if m is not None:
                    out.append(m)
            cursor = result.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                break
        return out
    finally:
        conv.close()


def _claude(binary: str) -> list[dict]:
    conv = _Proc(
        [
            binary,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
    )
    try:
        conv.send(
            {
                "type": "control_request",
                "request_id": "models",
                "request": {"subtype": "initialize"},
            }
        )
        reply = conv.answer(
            lambda f: f.get("type") == "control_response"
            and isinstance(f.get("response"), dict)
            and f["response"].get("request_id") == "models"
        )
        if reply["response"].get("subtype") != "success":
            raise ProbeError("the agent refused to list its models")
        body = reply["response"].get("response")
        items = body.get("models") if isinstance(body, dict) else None
        if not isinstance(items, list):
            raise ProbeError("this agent version does not list its models")
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            m = _model(
                item.get("value"),
                label=item.get("displayName"),
                description=item.get("description"),
                efforts=item.get("supportedEffortLevels"),
                is_default=False,
            )
            if m is not None:
                out.append(m)
        return out
    finally:
        conv.close()


def _opencode(binary: str, *, source) -> list[dict]:
    """opencode's `models --verbose`: each `provider/model` line — the same ids its ACP
    `session/new` offers as the `model` config option, which the worker selects (#1312) — followed
    by that model's JSON. Its `variants` keys are the levels ACP's `effort` option offers once the
    model is selected (#1407), so they are this model's `efforts`. Run under SHARED store
    admission for its whole life, like a launch: it is an opencode process on the operator's
    database, so it must never overlap a compaction (`opencode_admission`)."""
    from . import opencode_admission

    try:
        guard = opencode_admission.acquire(
            source.engine_id, exclusive=False, database=source.store_path("db")
        )
    except OSError as exc:
        raise ProbeError("the agent's store could not be admitted") from exc
    if guard is None:
        raise ProbeError("the agent's store is under maintenance; try again shortly")
    with guard:
        try:
            done = subprocess.run(  # noqa: S603 — admitted binary, literal argv, no shell
                [binary, "models", "--verbose"],
                check=False,
                capture_output=True,
                timeout=TIMEOUT_S,
                stdin=subprocess.DEVNULL,
                cwd=str(Path.home()),
                env={
                    "HOME": str(Path.home()),
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "LANG": "C.UTF-8",
                    # Should the command open opencode's internal listener, it is not left open.
                    "OPENCODE_SERVER_PASSWORD": secrets.token_urlsafe(32),
                },
            )
        except subprocess.TimeoutExpired as exc:
            raise ProbeError("the agent did not answer in time") from exc
    if done.returncode != 0:
        raise ProbeError("the agent refused to list its models")
    if len(done.stdout) > MAX_BYTES:
        raise ProbeError("the agent's answer was too large")
    return _opencode_models(done.stdout.decode("utf-8", "replace"))


#: One model's JSON block in `opencode models --verbose`; a larger one is skipped, not parsed.
_OPENCODE_BLOCK_MAX = 256 * 1024


def _opencode_models(text: str) -> list[dict]:
    """Parse `opencode models --verbose`: an id line at column 0, then optionally that model's
    pretty-printed JSON from a `{` line to the next `}` line at column 0 (1.18.35). Plain
    `opencode models` output — ids only — parses the same, with no levels.

    Bounded and per-model fail-soft (#1407): a block that is malformed, oversized or unterminated
    costs that model its label and levels, never its id or the rest of the list. A level is only
    a `variants` KEY of the `_EFFORT` grammar; `default` is never a level — "the model's default"
    is the absence of one, and nothing is sent for it."""
    lines = text.splitlines()
    out: list[dict] = []
    i = 0
    while i < len(lines) and len(out) < MAX_MODELS:
        mid = lines[i].strip()
        i += 1
        meta: Any = None
        if i < len(lines) and lines[i] == "{":
            start, size = i, 0
            i += 1
            # The block's body is indented; a line at column 0 that is not its closing brace is
            # the NEXT model's header, so an unterminated block ends there and parsing resumes at
            # that model rather than swallowing it (#1407 PR review).
            while i < len(lines) and lines[i] != "}" and (not lines[i] or lines[i][0] in " \t"):
                size += len(lines[i]) + 1
                i += 1
            closed = i < len(lines) and lines[i] == "}"
            if closed and size <= _OPENCODE_BLOCK_MAX:
                try:
                    meta = json.loads("\n".join(lines[start : i + 1]))
                except ValueError:
                    meta = None
            if closed:
                i += 1  # past the closing brace; an unterminated block resumes at the header
        meta = meta if isinstance(meta, dict) else {}
        variants = meta.get("variants")
        levels = [k for k in variants if k != "default"] if isinstance(variants, dict) else []
        m = _model(mid, label=meta.get("name"), description=None, efforts=levels, is_default=False)
        if m is not None:
            out.append(m)
    return out


_opencode.needs_source = True  # type: ignore[attr-defined]

_DISCOVER = {
    "opencode-acp": _opencode,
    "codex-app-server": _codex,
    "claude-stream-json": _claude,
}


@dataclass
class _Entry:
    """What the cache knows about one binary identity."""

    #: The last GOOD list — or, while none was ever read, the last failure.
    listing: ModelList
    #: When the newest probe of this key finished, whatever its outcome.
    attempted_at: float
    #: The newest probe failed while `listing` is a good one: the reason. Cleared by a success.
    refresh_error: str | None = None


@dataclass(frozen=True)
class _Target:
    """One client's probe, resolved: the cache key and how to ask its CLI."""

    key: tuple
    discover: Callable
    binary: str
    source: Any


_CACHE: dict[tuple, _Entry] = {}
#: One probe lock per key: held only by the thread actually probing, never by a list read.
_LOCKS: dict[tuple, threading.Lock] = {}
_GUARD = threading.Lock()


def _target(prov) -> _Target | ModelList:
    manifest = prov.manifest
    kind = manifest.api.kind if manifest is not None and manifest.api is not None else None
    discover = _DISCOVER.get(kind)
    if discover is None:
        return ModelList("unavailable", reason="this client cannot list its models")
    try:
        binding = api_source.resolve(prov)
        binary = binding.source.entrypoint_path()
        st = os.stat(binary)
    except api_source.SourceError as exc:
        return ModelList("unavailable", reason=str(exc))
    except Exception:  # noqa: BLE001 — a missing/unadmitted binary is a reason, not a crash
        return ModelList("unavailable", reason="the agent's CLI is not available")
    key = (kind, binary, st.st_ino, st.st_size, st.st_mtime_ns)
    return _Target(key, discover, binary, binding.source)


def _probe(t: _Target) -> ModelList:
    """Ask the CLI once. The outcome — a list or why there is none — stamped with `_clock`."""
    try:
        found = (
            t.discover(t.binary, source=t.source)
            if getattr(t.discover, "needs_source", False)
            else t.discover(t.binary)
        )[:MAX_MODELS]
        seen: set[str] = set()
        unique = tuple(m for m in found if not (m["id"] in seen or seen.add(m["id"])))
        if unique:
            return ModelList("ok", unique, fetched_at=_clock())
        return ModelList("unavailable", reason="the agent listed no models", fetched_at=_clock())
    except (ProbeError, OSError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, ProbeError) else "the agent could not be asked"
    except Exception:  # noqa: BLE001 — a reply of an unforeseen shape is "unavailable", never a 500
        reason = "the agent's answer was not understood"
    return ModelList("unavailable", reason=reason[:_TEXT_MAX], fetched_at=_clock())


def _record(key: tuple, outcome: ModelList, owner: _Refresher | None = None) -> _Entry:
    """Store a probe's outcome. A failure never replaces a good list: it rides beside it.

    A probe a STOPPED refresher started is dropped here (#1503 PR review): its shutdown drain may
    have timed out and a newer refresher taken over, so its late answer must not land over theirs.
    `_stopped` is set under `_GUARD` too, so the check and the write cannot straddle a stop."""
    with _GUARD:
        prev = _CACHE.get(key)
        if owner is not None and owner._stopped:
            return prev if prev is not None else _Entry(outcome, outcome.fetched_at)
        if outcome.status == "ok" or prev is None or prev.listing.status != "ok":
            entry = _Entry(outcome, outcome.fetched_at)
        else:
            entry = _Entry(prev.listing, outcome.fetched_at, refresh_error=outcome.reason)
        _CACHE[key] = entry
    return entry


def _outcome(entry: _Entry) -> ModelList:
    """The newest probe's own outcome — the failure, not the list kept beside it."""
    if entry.refresh_error is not None:
        return ModelList("unavailable", reason=entry.refresh_error, fetched_at=entry.attempted_at)
    return entry.listing


def _view(entry: _Entry, now: float) -> ModelList:
    """The entry as served at `now`: its age, whether it is past `TTL_S`, and a failed refresh."""
    listing = entry.listing
    age = max(0.0, now - listing.fetched_at)
    return replace(
        listing,
        stale=listing.status == "ok" and age >= TTL_S,
        fetched_age_s=age,
        refresh_error=entry.refresh_error,
    )


def _probe_once(
    t: _Target, *, since: float, owner: _Refresher | None = None
) -> tuple[_Entry, ModelList]:
    """Probe `t` on THIS thread, single-flight per key. `since` is when the caller found it needed
    a probe: a probe of the same key that finished after that (one the caller queued behind)
    answers it, so N concurrent callers spawn one CLI, not N."""
    with _GUARD:
        lock = _LOCKS.setdefault(t.key, threading.Lock())
    with lock:
        with _GUARD:
            entry = _CACHE.get(t.key)
        if entry is not None and entry.attempted_at >= since:
            return entry, _outcome(entry)
        outcome = _probe(t)
        return _record(t.key, outcome, owner), outcome


class _Refresher:
    """The ONE background prober (#1503). Warm-up, TTL refresh and on-demand revalidation all queue
    here; it probes one key at a time on its own daemon thread, never on a caller's. A key is
    queued at most once, and not again while it is being probed. The thread exists only while
    there is work, so an idle process holds none."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._queue: dict[tuple, tuple[_Target, float]] = {}
        self._busy: tuple | None = None
        self._thread: threading.Thread | None = None
        self._stopped = False

    def request(self, t: _Target) -> bool:
        """Queue a probe of `t` unless one is queued or running. Never blocks on a probe."""
        with self._cond:
            if self._stopped or t.key in self._queue or self._busy == t.key:
                return False
            self._queue[t.key] = (t, _clock())
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="native-models-refresh", daemon=True
                )
                self._thread.start()
            return True

    def _run(self) -> None:
        while True:
            with self._cond:
                if self._stopped or not self._queue:
                    self._thread = None
                    self._cond.notify_all()
                    return
                key = next(iter(self._queue))
                t, since = self._queue.pop(key)
                self._busy = key
            try:
                _probe_once(t, since=since, owner=self)
            except Exception:  # noqa: BLE001 — one client's probe never stops the others'
                log.exception("model list refresh failed")
            finally:
                with self._cond:
                    self._busy = None

    def wait_idle(self, timeout: float) -> bool:
        """Tests: block until nothing is queued or running."""
        with self._cond:
            return self._cond.wait_for(lambda: self._thread is None, timeout)

    def stop(self, timeout: float) -> bool:
        """Stop scheduling, drop the queue, and wait at most `timeout` for a probe in flight —
        itself bounded by `TIMEOUT_S` and killed in `_Proc.close`. True when nothing is left."""
        with self._cond, _GUARD:
            self._stopped = True
            self._queue.clear()
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
            return not thread.is_alive()
        return True

    @property
    def stopped(self) -> bool:
        return self._stopped


_REFRESHER = _Refresher()


def start() -> None:
    """Lifespan startup: a refresher stopped by a previous shutdown in this process is replaced.
    Its worker may still be finishing a probe its drain gave up on; that probe's answer is
    dropped (`_record`), so the two never both write."""
    global _REFRESHER
    if _REFRESHER.stopped:
        _REFRESHER = _Refresher()


def shutdown(timeout: float | None = None) -> bool:
    """Lifespan teardown (#1227 drain rule): stop scheduling and wait for a probe in flight, at
    most one probe's deadline plus its kill. Blocking: call in a thread."""
    return _REFRESHER.stop(TIMEOUT_S + 6.0 if timeout is None else timeout)


def _wants_refresh(entry: _Entry, now: float) -> bool:
    """An expired good list is refreshed — unless its last refresh just failed (a broken CLI is
    not re-spawned per request any more than it was before)."""
    if now - entry.listing.fetched_at < TTL_S:
        return False
    return entry.refresh_error is None or now - entry.attempted_at >= FAILURE_TTL_S


def models(prov) -> ModelList:
    """The models `prov` (a native API client) can run. Call in a thread: a TRUE miss — nothing
    read yet for this binary — probes on the caller's thread. Anything cached is answered without
    waiting on a probe: an expired good list comes back `stale` and is refreshed behind it."""
    t = _target(prov)
    if isinstance(t, ModelList):
        return t
    now = _clock()
    with _GUARD:
        entry = _CACHE.get(t.key)
    if entry is not None:
        if entry.listing.status == "ok":
            if _wants_refresh(entry, now):
                _REFRESHER.request(t)
            return _view(entry, now)
        if now - entry.attempted_at < FAILURE_TTL_S:
            return _view(entry, now)
    entry, _ = _probe_once(t, since=now)
    return _view(entry, _clock())


def models_fresh(prov) -> ModelList:
    """Ask the CLI now (#1503) — one probe, joined with any of the same key already running.
    Its own outcome: a failed probe is `unavailable` with its reason, even though the cache keeps
    the last good list for everyone else. Only `snapshot_for` calls it. Blocking."""
    t = _target(prov)
    if isinstance(t, ModelList):
        return t
    entry, outcome = _probe_once(t, since=_clock())
    if outcome.status != "ok":
        return replace(outcome, fetched_age_s=0.0)
    return _view(entry, _clock())


def _offers(listing: ModelList, model: str, effort: str | None) -> bool:
    found = next((m for m in listing.models if m["id"] == model), None)
    return found is not None and (effort is None or effort in found["efforts"])


def snapshot_for(prov, model: object, effort: object = None) -> ModelList:
    """The ONE list a create or a Settings save checks model AND effort against (#1503).

    A deliberate relaxation, named in #1503: a choice present in the last good list for this same
    binary is accepted even past `TTL_S` (the binary is unchanged, the list refreshes behind it,
    and the CLI still refuses a model the account lost, with its reason). A choice ABSENT from a
    stale list is never refused on that list alone: one fresh probe decides. Blocking."""
    listing = models(prov)
    if (
        listing.status == "ok"
        and listing.stale
        and isinstance(model, str)
        and (effort is None or isinstance(effort, str))
        and not _offers(listing, model, effort)
    ):
        return models_fresh(prov)
    return listing


def _due(entry: _Entry, now: float, ahead_s: float) -> bool:
    """Should the warm-up loop re-probe this entry? A failure — no list at all, or a failed refresh
    of an old one — is retried once `FAILURE_TTL_S` has passed, not a whole TTL later (#1503 PR
    review); a good list is renewed `ahead_s` before it expires."""
    failing = entry.listing.status != "ok" or entry.refresh_error is not None
    if failing and now - entry.attempted_at < FAILURE_TTL_S:
        return False
    if entry.listing.status != "ok":
        return True
    return now - entry.listing.fetched_at >= TTL_S - ahead_s


def schedule(prov, *, ahead_s: float = 0.0) -> bool:
    """Queue a background probe of `prov` when it has no list yet, its list is about to expire
    (`TTL_S - ahead_s`), or its last probe failed `FAILURE_TTL_S` ago (the warm-up loop). True
    when one was queued. Never probes here."""
    t = _target(prov)
    if isinstance(t, ModelList):
        return False
    with _GUARD:
        entry = _CACHE.get(t.key)
    if entry is not None and not _due(entry, _clock(), ahead_s):
        return False
    return _REFRESHER.request(t)
