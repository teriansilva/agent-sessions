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

A list that could not be read is `unavailable` with the reason. Callers then offer `default`
only; nothing is substituted (`model_choice.select_api`).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import selectors
import subprocess
import threading
import time
from dataclasses import dataclass, field
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


class ProbeError(RuntimeError):
    """The client's model list could not be read; the message is a user-facing reason."""


@dataclass(frozen=True)
class ModelList:
    status: str  # "ok" | "unavailable"
    models: tuple[dict[str, Any], ...] = ()
    reason: str | None = None
    fetched_at: float = field(default=0.0, compare=False)

    def ids(self) -> set[str]:
        return {m["id"] for m in self.models}

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "models": [dict(m) for m in self.models],
            "reason": self.reason,
        }


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
    """opencode's `models` command: one `provider/model` per line — the same ids its ACP
    `session/new` offers as the `model` config option, which the worker selects (#1312). Run under
    SHARED store admission for its whole life, like a launch: it is an opencode process on the
    operator's database, so it must never overlap a compaction (`opencode_admission`)."""
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
                [binary, "models"],
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
    out = []
    for line in done.stdout.decode("utf-8", "replace").splitlines():
        m = _model(line.strip(), label=None, description=None, efforts=None, is_default=False)
        if m is not None:
            out.append(m)
    return out


_opencode.needs_source = True  # type: ignore[attr-defined]

_DISCOVER = {
    "opencode-acp": _opencode,
    "codex-app-server": _codex,
    "claude-stream-json": _claude,
}

_CACHE: dict[tuple, ModelList] = {}
_LOCKS: dict[tuple, threading.Lock] = {}
_GUARD = threading.Lock()


def _fresh(entry: ModelList | None) -> bool:
    if entry is None:
        return False
    ttl = TTL_S if entry.status == "ok" else FAILURE_TTL_S
    return time.monotonic() - entry.fetched_at < ttl


def models(prov) -> ModelList:
    """The models `prov` (a native API client) can run right now. Blocking: call in a thread."""
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
    with _GUARD:
        lock = _LOCKS.setdefault(key, threading.Lock())
    with lock:
        cached = _CACHE.get(key)
        if _fresh(cached):
            return cached
        try:
            found = (
                discover(binary, source=binding.source)
                if getattr(discover, "needs_source", False)
                else discover(binary)
            )[:MAX_MODELS]
            seen: set[str] = set()
            unique = tuple(m for m in found if not (m["id"] in seen or seen.add(m["id"])))
            entry = (
                ModelList("ok", unique, fetched_at=time.monotonic())
                if unique
                else ModelList(
                    "unavailable", reason="the agent listed no models", fetched_at=time.monotonic()
                )
            )
        except (ProbeError, OSError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, ProbeError) else "the agent could not be asked"
            entry = ModelList("unavailable", reason=reason, fetched_at=time.monotonic())
        except Exception:  # noqa: BLE001 — a reply of an unforeseen shape is "unavailable", never a 500
            reason = "the agent's answer was not understood"
            entry = ModelList("unavailable", reason=reason, fetched_at=time.monotonic())
        _CACHE[key] = entry
        return entry
