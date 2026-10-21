"""Installation timeouts cover the last unit of work, not just the next artifact boundary."""

import asyncio
import hashlib
import json
import tarfile

import httpx
import pytest

import test_plugin_artifacts
import test_plugin_feed
import test_plugin_manager
import test_plugin_npm
from agent_sessions.plugins import artifacts, budget, feed, manager, npm_closure

recipe = test_plugin_manager.recipe


def test_fetch_uses_remaining_install_budget_not_its_own_fresh_deadline(monkeypatch):
    async def headers(_):
        await asyncio.sleep(10)
        return httpx.Response(200, content=b"not reached")

    entry = feed.entry(test_plugin_feed.entry(), signed=False)
    monkeypatch.setattr(artifacts, "_TRANSPORT", httpx.MockTransport(headers))
    with budget.limited(0.02), pytest.raises(artifacts.ArtifactError, match="time limit"):
        artifacts.fetch(entry.artifacts[0], "npm-prefix")


def test_extraction_checks_budget_during_decompression(tmp_path, monkeypatch):
    body = test_plugin_artifacts.archive([("bin/fixture", b"x" * 200000, tarfile.REGTYPE)])
    artifact = feed.Artifact(test_plugin_artifacts.URL, hashlib.sha256(body).hexdigest(), ".")
    clock = [0.0]
    monkeypatch.setattr(budget.time, "monotonic", lambda: clock[0])
    original = artifacts.gzip.GzipFile.read

    def read(source, *args, **kwargs):
        result = original(source, *args, **kwargs)
        clock[0] += 2
        return result

    monkeypatch.setattr(artifacts.gzip.GzipFile, "read", read)
    with budget.limited(1), pytest.raises(budget.BudgetError):
        artifacts.extract(body, artifact, "tarball", tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


def test_npm_last_metadata_read_cannot_escape_budget(tmp_path, monkeypatch):
    entry = feed.entry(test_plugin_feed.entry(), signed=False)
    test_plugin_npm.package(tmp_path, "node_modules/fixture", "fixture", "1.0.0")
    clock = [0.0]
    monkeypatch.setattr(budget.time, "monotonic", lambda: clock[0])
    read = npm_closure._read

    def metadata(path):
        doc = read(path)
        clock[0] = 2
        return doc

    monkeypatch.setattr(npm_closure, "_read", metadata)
    with budget.limited(1), pytest.raises(budget.BudgetError):
        npm_closure.validate(tmp_path, entry)


def test_last_npm_validation_overrun_refuses_candidate_publication(recipe, monkeypatch):
    body = test_plugin_artifacts.archive(
        [
            (
                "package/package.json",
                json.dumps({"name": "fixture", "version": "1.0.0"}).encode(),
                tarfile.REGTYPE,
            ),
            ("package/bin/fixture", b"#!/bin/true\n", tarfile.REGTYPE),
        ]
    )
    digest = hashlib.sha256(body).hexdigest()
    recipe["manifest"]["install"].update(
        kind="npm-prefix",
        authority="registry.npmjs.org",
        package="fixture",
        version="1.0.0",
        digest="sha256:" + digest,
        entrypoint="node_modules/fixture/bin/fixture",
    )
    recipe["recipe"]["artifacts"] = [
        {
            "url": "https://registry.npmjs.org/fixture/-/fixture-1.0.0.tgz",
            "sha256": digest,
            "destination": "node_modules/fixture",
        }
    ]
    monkeypatch.setattr(artifacts, "fetch", lambda *_: body)
    clock = [0.0]
    monkeypatch.setattr(budget.time, "monotonic", lambda: clock[0])
    validate = npm_closure.validate

    def slow(*args):
        validate(*args)
        clock[0] += manager.MAX_INSTALL_SECONDS + 1

    monkeypatch.setattr(npm_closure, "validate", slow)
    item = manager.run_install(test_plugin_manager.planned(recipe)["id"])
    assert item["state"] == "failed" and "time limit" in item["error"]
    assert manager.snapshot()["plugins"] == {}
