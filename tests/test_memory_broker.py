"""The shared search ingress never trusts a caller-supplied scope or home (#2036)."""

import json
import os
import uuid

import pytest

from agent_sessions import engines, memory, memory_broker, memory_index, projects, template_secrets
from agent_sessions import memory_service as service


@pytest.fixture
def launch(tmp_path, monkeypatch):
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [])
    monkeypatch.setattr(memory_broker, "_live", lambda *_: True)
    monkeypatch.setattr(engines, "physical_key", lambda key: key)
    work = tmp_path / "work"
    work.mkdir()
    pid = projects.create("Example", folders=[str(work)]).id
    memory.set_enabled(pid, True)
    monkeypatch.setattr(memory, "_project_for", lambda *_: pid)
    key = "claude:" + str(uuid.uuid4())

    def bind():
        env = memory.launch_env(key, str(work), kind="terminal")
        descriptor = memory_broker.root() / (os.path.basename(env[memory.ENV_INBOX]) + ".json")
        assert descriptor.stat().st_mode & 0o077 == 0
        return env, json.loads(descriptor.read_text())["token"]

    env, token = bind()
    return pid, key, env, token, bind


def test_scope_is_server_bound_and_operator_or_foreign_scope_is_refused(launch):
    pid, _, _, token, _ = launch
    service.submit(
        service.operator(), pid, title="Project protocol", body="Use revision checks.", origin="p"
    )
    hq = service.submit(
        service.operator(),
        memory.HQ_SCOPE,
        title="Private HQ protocol",
        body="Personal research preference.",
        origin="h",
    )
    memory_index.sync()
    result = memory_broker.handle({"token": token, "op": "search", "query": "protocol"})
    assert result["ok"] and {h["scope"] for h in result["result"]["hits"]} == {pid}
    assert not memory_broker.handle(
        {"token": token, "op": "search", "query": "protocol", "operator": True}
    )["ok"]
    assert not memory_broker.handle({"token": token, "op": "read", "slug": hq["slug"]})["ok"]


def test_relaunch_archive_and_unknown_liveness_refuse_old_capabilities(launch, monkeypatch):
    _, key, _, token, bind = launch
    _, current = bind()
    assert not memory_broker.handle({"token": token, "op": "search", "query": "protocol"})["ok"]
    monkeypatch.setattr(memory_broker, "_live", lambda *_: False)
    assert not memory_broker.handle({"token": current, "op": "search", "query": "protocol"})["ok"]
    memory_broker.revoke_launch(key)
    monkeypatch.setattr(memory_broker, "_live", lambda *_: True)
    assert not memory_broker.handle({"token": current, "op": "search", "query": "protocol"})["ok"]


def test_directory_replacement_and_project_deletion_end_binding(launch):
    pid, _, env, token, _ = launch
    inbox = memory._scope(pid) / "inbox" / os.path.basename(env[memory.ENV_INBOX])
    inbox.rename(inbox.with_name("old-inbox"))
    inbox.mkdir(mode=0o700)
    response = memory_broker.handle({"token": token, "op": "search", "query": "protocol"})
    assert not response["ok"] and response["status"] == 403


def test_running_binding_keeps_read_access_after_switch_off(launch):
    pid, _, _, token, _ = launch
    note = service.submit(
        service.operator(), pid, title="Protocol", body="Use revisions.", origin="p"
    )
    memory.set_enabled(pid, False)
    response = memory_broker.handle({"token": token, "op": "read", "slug": note["slug"]})
    assert response["ok"] and response["result"]["body"] == "Use revisions."


def test_operator_enlarged_unicode_note_fits_the_utf8_wire_budget(launch):
    pid, _, _, token, _ = launch
    note = service.submit(service.operator(), pid, title="Reference", body="Original.", origin="p")
    body = "ș" * 15000
    memory.update_note(pid, note["slug"], {"body": body})
    reply = memory_broker.handle({"token": token, "op": "read", "slug": note["slug"]})
    assert reply["ok"] and reply["result"]["body"] == body
    assert len(json.dumps(reply, ensure_ascii=False).encode()) < memory_broker.RESPONSE_MAX


def test_api_binding_checks_the_actual_worker_generation(monkeypatch):
    from agent_sessions import native_state, secrets_broker

    bindings = {"paths": ["/synthetic/notes", "/synthetic/inbox/current"]}
    cap = {"kind": "api", "worker_id": "synthetic-worker"}
    config = {"memory_env": {memory.ENV_INBOX: bindings["paths"][1]}}
    monkeypatch.setattr(native_state, "read_config", lambda _: config)
    monkeypatch.setattr(secrets_broker, "_api_liveness", lambda _: secrets_broker.ALIVE)
    assert memory_broker._live(cap, bindings)
    config["memory_env"][memory.ENV_INBOX] = "/synthetic/inbox/next-generation"
    assert not memory_broker._live(cap, bindings)
    config["memory_env"][memory.ENV_INBOX] = bindings["paths"][1]
    monkeypatch.setattr(secrets_broker, "_api_liveness", lambda _: secrets_broker.UNKNOWN)
    assert not memory_broker._live(cap, bindings)


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="Linux runtime binding")
def test_pty_binding_requires_the_live_masters_exact_inbox(tmp_path, monkeypatch):
    import subprocess

    from agent_sessions import ptybridge, reaper

    inbox = str(tmp_path / "offered-inbox")
    child = subprocess.Popen(["/bin/sleep", "30"], env={memory.ENV_INBOX: inbox})
    try:
        monkeypatch.setattr(ptybridge, "probe_master", lambda _: ptybridge.ALIVE)
        monkeypatch.setattr(reaper, "_find_master_pid", lambda *_: child.pid)
        cap = {"kind": "pty", "phys_key": "claude:" + str(uuid.uuid4())}
        assert memory_broker._live(cap, {"paths": ["/synthetic/notes", inbox]})
        assert not memory_broker._live(
            cap, {"paths": ["/synthetic/notes", str(tmp_path / "previous-inbox")]}
        )
        monkeypatch.setattr(ptybridge, "probe_master", lambda _: ptybridge.UNKNOWN)
        assert not memory_broker._live(cap, {"paths": ["/synthetic/notes", inbox]})
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_private_socket_serves_the_real_client_mcp_and_helper(launch, monkeypatch, capsys):
    import asyncio
    import contextlib
    import subprocess
    import sys
    from pathlib import Path

    from agent_sessions import cli, memory_client, memory_mcp

    pid, _, env, _, _ = launch
    saved = service.submit(
        service.operator(), pid, title="Protocol", body="Use revisions.", origin="p"
    )
    memory_index.sync()

    async def journey():
        task = asyncio.create_task(memory_broker.serve())
        try:
            for _ in range(100):
                if memory_broker.socket_path().exists():
                    break
                await asyncio.sleep(0.01)
            reply = await asyncio.to_thread(
                memory_client.request, env[memory.ENV_INBOX], "search", query="protocol"
            )
            assert reply["hits"][0]["slug"] == saved["slug"]
            # A second app cannot replace the socket held by the first broker.
            inode = memory_broker.socket_path().stat().st_ino
            await memory_broker.serve()
            assert memory_broker.socket_path().stat().st_ino == inode
            server = memory_mcp.Server(
                str(Path(env[memory.ENV_READ])), str(Path(env[memory.ENV_INBOX])), shared=True
            )
            reply = await asyncio.to_thread(server.call, "memory_search", {"query": "protocol"})
            assert "Use revisions" in json.dumps(reply) and not reply.get("isError")
            frames = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "memory_read",
                        "arguments": {"slug": saved["slug"]},
                    },
                },
            ]
            child = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-I", "-m", "agent_sessions.memory_mcp"],
                input="".join(json.dumps(f) + "\n" for f in frames),
                text=True,
                capture_output=True,
                check=True,
                timeout=60,
                env={
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "HOME": os.environ["HOME"],  # the sandbox, never the passwd home (#2423)
                    **env,
                },
            )
            replies = [json.loads(line) for line in child.stdout.splitlines()]
            assert [r["id"] for r in replies] == [1, 2]
            assert "Use revisions." in replies[1]["result"]["content"][0]["text"]
            assert not replies[1]["result"].get("isError")
            monkeypatch.setenv(memory.ENV_INBOX, env[memory.ENV_INBOX])
            assert await asyncio.to_thread(cli.main, ["memory", "read", saved["slug"]]) == 0
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(journey())
    assert json.loads(capsys.readouterr().out)["body"] == "Use revisions."
