"""Real fixture CLIs must create/resume a conversation and produce readable evidence (#1259)."""

import asyncio
import hashlib
import json
import sys
import tarfile
import time

import httpx
import pytest

import test_plugin_artifacts
import test_plugin_feed
import test_plugin_process
import test_plugins
from agent_sessions import chat_config, review, transcript
from agent_sessions.engines import registry
from agent_sessions.plugins import jobs, manager, storage
from agent_sessions.scanner import Session
from test_plugin_manager import request_id

anyio_backend = test_plugin_process.anyio_backend
user_manager = test_plugin_process.user_manager


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENT_SESSIONS_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AGENT_SESSIONS_PLUGINS_DIR", str(tmp_path / "plugins"))

    class Kind:
        engine_id = "fixture"
        id_pattern = None

        def scan(self):
            root = self.owner.store_root()
            rows = []
            for file in root.glob("*.json"):
                doc = json.loads(file.read_text())
                rows.append(
                    Session(
                        engine="fixture",
                        uuid=doc["id"],
                        cwd=doc["cwd"],
                        last_mtime=file.stat().st_mtime,
                        first_user_message="probe",
                        archived=False,
                        created_at=file.stat().st_mtime,
                    )
                )
            return rows

    def adapter(native, candidate_home):
        data = json.loads((candidate_home / ".fixture" / f"{native}.json").read_text())
        return [transcript.Turn(role=r["role"], text=r["text"]) for r in data["turns"]]

    monkeypatch.setitem(registry.STORE_KINDS, "gemini-tmp", Kind)
    monkeypatch.setitem(transcript._ADAPTERS, "gemini-chat", adapter)

    def make(*, reply=True, version=True, headless=False, usage=False):
        script = f"""#!{sys.executable}
import sys,json,re
from pathlib import Path
if '--version' in sys.argv:
 print({'"fixture 1.0.0"' if version else '"unrecognized"'},flush=True)
 raise SystemExit(0)
root=Path.home()/'.fixture'
root.mkdir(exist_ok=True)
flag='--resume' if '--resume' in sys.argv else '--session-id'
sid=sys.argv[sys.argv.index(flag)+1]
path=root/(sid+'.json')
data=(json.loads(path.read_text()) if path.exists() else
      {{'id':sid,'cwd':str(Path.cwd()),'turns':[]}})
print('READY',flush=True)
message=sys.argv[-1] if {headless!r} else sys.stdin.readline()
marker=re.search(r'BATTLELAB_PROBE_[A-Za-z0-9_]+',message)[0]
data['turns'].append({{'role':{'"assistant"' if reply else '"user"'},'text':marker}})
staged=path.with_suffix(".tmp")
staged.write_text(json.dumps(data))
staged.replace(path)
print(marker,flush=True)
""".encode()
        data = test_plugin_artifacts.archive([("bin/fixture", script, tarfile.REGTYPE)])
        recipe = test_plugin_feed.entry()
        if usage:
            recipe["manifest"]["usage"] = test_plugins.doc("claude")["usage"]
        if headless:
            recipe["manifest"]["probe"] = {"kind": "print-pinned"}
        recipe["manifest"]["binary"]["version_flag"] = "--version"
        recipe["manifest"]["store"].update(root="~/.fixture", env_override=None)
        recipe["manifest"]["store"].pop("env_override")
        recipe["manifest"]["install"].update(
            kind="tarball",
            authority="github.com",
            package="@example/fixture",
            version="v1",
            digest="sha256:" + hashlib.sha256(data).hexdigest(),
            entrypoint="bin/fixture",
        )
        recipe["recipe"]["artifacts"] = [
            {
                "url": test_plugin_artifacts.URL,
                "sha256": hashlib.sha256(data).hexdigest(),
                "destination": ".",
            }
        ]
        monkeypatch.setattr(manager.artifacts, "fetch", lambda *_: data)
        reviewed = manager.review(local=recipe)
        item = manager.begin_install(
            request_id(), reviewed["id"], reviewed["digest"], confirm_local=True
        )
        assert manager.run_install(item["id"])["state"] == "installed"
        return item["id"]

    return make


async def settled(service, item):
    deadline = time.monotonic() + 20
    while manager.operation(item["id"])["state"] in ("planned", "running"):
        assert time.monotonic() < deadline, "fixture job did not settle"
        await asyncio.sleep(0.05)
    if service._thread is not None:
        # The durable outcome precedes worker-fence release. A new attempt must wait for
        # that release, including completion of the worker loop and its executor.
        await asyncio.to_thread(service._thread.join, 20)
        assert not service._thread.is_alive(), "fixture worker did not release ownership"
    return manager.operation(item["id"])


@pytest.mark.anyio
@pytest.mark.parametrize("headless", [False, True])
async def test_real_new_resume_transcript_and_version_are_required(
    candidate, user_manager, headless
):
    gen = candidate(headless=headless)
    service = jobs.Service()
    rid = request_id()
    item = await service.verify(rid, "fixture", gen, confirm_effects=True)
    # A concurrent second app cannot reconcile or replay a live verification.
    with pytest.raises(storage.StateError, match="busy"):
        manager.recover()
    # The live worker writes manager.json while it runs. A duplicate that cannot take that lock
    # within its 2 s budget is refused as busy (fail closed); on a loaded runner it retries
    # until it reads the same operation back — never a second one.
    deadline = time.monotonic() + 20
    while True:
        try:
            duplicate = await service.verify(rid, "fixture", gen, confirm_effects=True)
            break
        except storage.StateError as exc:
            assert "busy" in str(exc) and time.monotonic() < deadline, exc
            await asyncio.sleep(0.05)
    assert duplicate["id"] == item["id"]
    result = await settled(service, item)
    assert result["state"] == "verified", manager.generation("fixture", gen)["verification"]
    verification = manager.generation("fixture", gen)["verification"]
    assert {r["check"] for r in verification["results"]} == {
        "binary",
        "version",
        "new",
        "resume",
        "transcript",
        "store",
    }
    assert all(r["passed"] for r in verification["results"])
    # Sign-in and retries use the same candidate workspace. An older successful conversation
    # cannot satisfy the next nonce or make a fresh check ambiguous.
    again = await settled(
        service, await service.verify(request_id(), "fixture", gen, confirm_effects=True)
    )
    assert again["state"] == "verified"
    assert (
        len(
            list(
                (
                    manager.provider("fixture", manager.generation("fixture", gen)).home
                    / ".fixture"
                ).glob("*.json")
            )
        )
        == 2
    )
    manager.activate(request_id(), "fixture", gen)
    assert manager.snapshot()["plugins"]["fixture"]["enabled"] is True


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["echo", "version"])
async def test_echoed_prompt_or_successful_exit_without_version_cannot_enable(
    candidate, user_manager, failure
):
    gen = candidate(reply=failure != "echo", version=failure != "version")
    service = jobs.Service()
    result = await settled(
        service, await service.verify(request_id(), "fixture", gen, confirm_effects=True)
    )
    assert result["state"] == "failed"
    with pytest.raises(manager.ManagerError, match="verification"):
        manager.activate(request_id(), "fixture", gen)


@pytest.mark.anyio
async def test_endpoint_changed_during_real_check_cannot_inherit_success(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AGENT_SESSIONS_PLUGINS_DIR", str(tmp_path / "plugins"))
    doc = test_plugins.doc("apichat")
    doc["identity"]["id"] = "fixture-api"
    reviewed = manager.review(local={"manifest": doc, "recipe": {"artifacts": []}})
    item = manager.begin_install(
        request_id(), reviewed["id"], reviewed["digest"], confirm_local=True
    )
    gen = manager.run_install(item["id"])["id"]
    chat_config.set_config(
        manager.endpoint_scope("fixture-api", gen),
        {"base_url": "https://llm.example.test/v1", "api_key": "fixture-key", "model": "first"},
    )
    calls = []

    def handler(request):
        calls.append(request)
        chat_config.set_config(manager.endpoint_scope("fixture-api", gen), {"model": "changed"})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "BATTLELAB_ENDPOINT_PROBE_OK"}}]}
        )

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
    service = jobs.Service()
    result = await settled(
        service, await service.verify(request_id(), "fixture-api", gen, confirm_effects=True)
    )
    assert len(calls) == 1
    assert result["state"] == "failed"
    assert manager.generation("fixture-api", gen)["verification"] is None
    with pytest.raises(manager.ManagerError, match="verification"):
        manager.activate(request_id(), "fixture-api", gen)


@pytest.mark.anyio
async def test_verification_requires_effects_confirmation_before_planning(candidate):
    gen = candidate()
    service = jobs.Service()
    before = manager.snapshot()
    with pytest.raises(manager.ManagerError, match="effects"):
        await service.verify(request_id(), "fixture", gen)
    assert manager.snapshot() == before


@pytest.mark.anyio
@pytest.mark.parametrize("passed", [False, True])
async def test_required_usage_reporter_runs_in_verification_worker(
    candidate, user_manager, monkeypatch, passed
):
    import threading

    from agent_sessions import agent_usage

    gen = candidate(headless=True, usage=True)
    calls = []
    caller_thread = threading.get_ident()

    def reporter(*, engine):
        calls.append(engine)
        assert threading.get_ident() != caller_thread
        assert registry.get(engine).root == manager.generation_root("fixture", gen)
        # Candidate verification owns its worker reservation through the real reporter call.
        with pytest.raises(storage.StateError, match="busy"):
            manager.recover()
        if not passed:
            return agent_usage.Report(engine, agent_usage.SOURCE_PLAN, error="vendor unavailable")
        return agent_usage.parse_claude_usage("Current week: 5% used", engine=engine)

    monkeypatch.setitem(agent_usage.KIND_REPORTERS, "claude-cli-probe", reporter)
    service = jobs.Service()
    result = await settled(
        service, await service.verify(request_id(), "fixture", gen, confirm_effects=True)
    )
    assert calls == ["fixture"]
    assert result["state"] == ("verified" if passed else "failed")
    checks = manager.generation("fixture", gen)["verification"]["results"]
    assert next(r for r in checks if r["check"] == "usage")["passed"] is passed
    assert all(r["passed"] for r in checks if r["check"] != "usage")
    if passed:
        manager.activate(request_id(), "fixture", gen)
        assert manager.snapshot()["plugins"]["fixture"]["enabled"] is True
    else:
        with pytest.raises(manager.ManagerError, match="verification"):
            manager.activate(request_id(), "fixture", gen)
