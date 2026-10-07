"""Execute the declared checks against the reviewed candidate, never synthetic success (#1259).

Terminal checks create one vendor conversation in a private probe workspace and send two fixed
nonce-bearing messages (new/resume). This can consume quota and leaves vendor-owned history.
Only check outcomes and a bounded version token are retained here; raw terminal/model output is
not. Usage uses the existing reviewed reporter kind and its own bounds/side effects.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import uuid
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

from .. import agent_usage, chat_config, prompts, review, transcript
from ..engines import base, registry
from . import manager, process

_VERSION = re.compile(r"(?<![A-Za-z0-9])v?[0-9]+\.[0-9]+(?:\.[0-9]+)?(?:[-+][A-Za-z0-9.-]+)?")


@contextlib.contextmanager
def candidate_scope(prov):
    """Private reader context for a disabled candidate; never publishes it or grants new work."""
    old = registry.capture()
    scoped = replace(old, by_id=MappingProxyType({**old.by_id, prov.engine_id: prov}))
    token = registry._REQUEST_ROSTER.set(scoped)
    try:
        with base.store_scope(prov.engine_id):
            yield
    finally:
        registry._REQUEST_ROSTER.reset(token)


def _rows(prov, cwd: Path):
    return [row for row in prov.scan() if os.path.realpath(row.cwd) == str(cwd)]


def _answered(prov, native: str, marker: str) -> bool:
    adapter = transcript.adapter_for(prov.engine_id)
    if adapter is None:
        return False
    return any(t.role == "assistant" and marker in t.text for t in adapter(native, prov.home))


async def _conversation(prov, item, cwd: Path, *, native: str | None = None) -> str:
    purpose = "resume" if native is not None else "new"
    placeholder = native or (("new-" if prov.new_session_reconciles else "") + str(uuid.uuid4()))
    existing = (
        {row.uuid for row in await asyncio.to_thread(_rows, prov, cwd)} if native is None else set()
    )
    marker = process.probe_marker(item["id"], purpose)
    async with process.spawn(
        prov, purpose, cwd=cwd, native_id=placeholder, operation_id=item["id"]
    ) as terminal:
        # Wait for the CLI to paint, then a bounded settling interval. Read continuously so the
        # PTY cannot fill while the vendor writes its own transcript. Evidence is the actual
        # assistant turn in that transcript, never an echoed prompt or merely a live process.
        painted = asyncio.Event()

        async def consume():
            while await terminal.read():
                painted.set()

        reader = asyncio.create_task(consume())
        try:
            if prov.manifest.probe_kind == "terminal":
                await asyncio.wait_for(painted.wait(), timeout=15)
                await asyncio.sleep(1)
                await terminal.write((process.probe_message(item["id"], purpose) + "\r").encode())
            while True:
                ended = reader.done()
                if ended:
                    reader.result()
                try:
                    rows = await asyncio.to_thread(_rows, prov, cwd)
                    expected = native or (None if prov.new_session_reconciles else placeholder)
                    matches = [
                        row
                        for row in rows
                        if (row.uuid == expected if expected else row.uuid not in existing)
                    ]
                    if len(matches) > 1:
                        raise process.ProcessError("the verification session identity is ambiguous")
                    if matches and await asyncio.to_thread(
                        _answered, prov, matches[0].uuid, marker
                    ):
                        return matches[0].uuid
                except process.ProcessError:
                    raise
                except (OSError, ValueError, KeyError):
                    # Vendor stores may be read during an append/rewrite. Retry while the
                    # bounded process is alive; unreadable data is never affirmative evidence.
                    pass
                if ended:
                    raise process.ProcessError("the agent exited before the verification reply")
                await asyncio.sleep(0.5)
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await process.drain(reader)


async def _version(prov, item, cwd: Path) -> str:
    output = bytearray()
    async with process.spawn(prov, "version", cwd=cwd, operation_id=item["id"]) as terminal:
        while chunk := await terminal.read():
            output.extend(chunk)
            if len(output) > 65536:
                raise process.ProcessError("version output exceeded its limit")
        code = await terminal.proc.wait()
    found = _VERSION.search(output.decode("utf-8", "replace"))
    if code != 0 or found is None or len(found[0]) > 64:
        raise process.ProcessError("the version command did not report a version")
    return found[0]


async def _api_source(prov) -> dict:
    """A native API client's one check (#1311): the structured adapter's readiness against the
    live roster with this candidate in it. Local facts only (source resolution, the source CLI's
    version floor, containment); it starts no worker and sends nothing to a model."""
    from .. import structured_runtime

    reason = await asyncio.to_thread(structured_runtime.unavailable_reason, prov)
    return {
        "check": "source",
        "passed": reason is None,
        "detail": "The console agent it drives is installed and supports native mode."
        if reason is None
        else reason,
    }


async def _endpoint(plugin_id: str, generation_id: str) -> dict:
    binding = manager._endpoint_binding(plugin_id, generation_id)
    cfg = chat_config.snapshot(manager.endpoint_scope(plugin_id, generation_id))
    if cfg is None:
        return {"check": "endpoint", "passed": False, "detail": "Configure an endpoint first."}
    response = await review.post_chat_response(
        {**cfg, "request_timeout": 30},
        {
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": prompts.effective("chat_agent")},
                {"role": "user", "content": "Reply exactly BATTLELAB_ENDPOINT_PROBE_OK."},
            ],
            "max_tokens": 64,
            "stream": False,
        },
    )
    try:
        content = response.json()["choices"][0]["message"]["content"]
        passed = (
            response.status_code == 200
            and isinstance(content, str)
            and "BATTLELAB_ENDPOINT_PROBE_OK" in content
        )
    except (ValueError, KeyError, IndexError, TypeError):
        passed = False
    return {
        "check": "endpoint",
        "passed": passed,
        "binding": binding,
        "detail": "The endpoint answered the fixed test message."
        if passed
        else "The endpoint did not return the required test reply.",
    }


async def run(item: dict) -> None:
    plugin_id, generation_id = item["plugin_id"], item["request"]["generation_id"]
    gen = await asyncio.to_thread(manager.generation, plugin_id, generation_id)
    prov = manager.provider(plugin_id, gen)
    required = manager.required_checks(prov)
    results = {
        check: {"check": check, "passed": False, "detail": "This check did not run."}
        for check in required
    }
    cwd = manager.workspace(plugin_id, generation_id)
    try:
        kind = registry.STORE_KINDS.get(prov.manifest.store.layout if prov.manifest.store else None)
        if kind is None:
            raise process.ProcessError("this build has no store kind for the candidate")
        prov.attach_kind(kind())
        with candidate_scope(prov):
            if prov.manifest.runtime == "chat":
                results["endpoint"] = await _endpoint(plugin_id, generation_id)
            elif prov.manifest.runtime == "api":
                results["source"] = await _api_source(prov)
            else:
                prov.entrypoint_path()
                results["binary"] = {
                    "check": "binary",
                    "passed": True,
                    "detail": "The exact reviewed executable is still verified.",
                }
                version = await _version(prov, item, cwd)
                results["version"] = {
                    "check": "version",
                    "passed": True,
                    "version": version,
                    "detail": "The installed CLI reported its version.",
                }
                native = None
                if "new" in required or "resume" in required or "transcript" in required:
                    native = await _conversation(prov, item, cwd)
                    if "new" in required:
                        results["new"] = {
                            "check": "new",
                            "passed": True,
                            "detail": "A new vendor conversation answered the test.",
                        }
                    if "transcript" in required:
                        results["transcript"] = {
                            "check": "transcript",
                            "passed": True,
                            "detail": "The declared adapter read the real reply.",
                        }
                if "resume" in required:
                    await _conversation(prov, item, cwd, native=native)
                    results["resume"] = {
                        "check": "resume",
                        "passed": True,
                        "detail": "The same vendor conversation answered after resume.",
                    }
                if "store" in required:
                    root = prov.store_root()
                    results["store"] = {
                        "check": "store",
                        "passed": root is not None and root.exists(),
                        "detail": "The declared vendor store was checked.",
                    }
                if "usage" in required:
                    reporter = agent_usage.KIND_REPORTERS.get(prov.manifest.usage.kind)
                    if reporter is not None:
                        # The existing reporter owns process/network bounds. Drain it before
                        # releasing this operation's worker fence, including on cancellation.
                        bound_reporter = agent_usage._reporter_for(plugin_id, reporter)
                        task = asyncio.create_task(asyncio.to_thread(bound_reporter))
                        try:
                            report = await asyncio.shield(task)
                        except asyncio.CancelledError:
                            await process.drain(task)
                            raise
                        results["usage"] = {
                            "check": "usage",
                            "passed": not report.error and report.source != "none",
                            "detail": "The configured usage reporter was asked.",
                        }
    except process.CleanupError:
        raise
    except Exception:
        # No vendor error, model response, key, path or terminal bytes enter this record.
        for result in results.values():
            if not result["passed"]:
                result["detail"] = "Verification stopped before this check could pass."
    await asyncio.to_thread(manager.finish_verification, item["id"], list(results.values()))
