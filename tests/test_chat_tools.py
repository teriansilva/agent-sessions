"""Read-only tools for the API agent (#853 P9b, #1222): confinement, refusals, bounds, and the loop.

`chat_tools.run` is tested directly for the boundary (every escape shape, every refused name, the
bounds); the turn loop is driven through `chat_runtime` against a scripted endpoint, so each test
can see exactly which requests went out and what the transcript kept.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import httpx
import pytest

from agent_sessions import chat_config, chat_runtime, chat_store, chat_tools, files, prefs, review

ENGINE = "apichat"
URL = "https://llm.example.test/v1"
KEY = "sk-test-key-0123456789"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A home with one project folder in it, and a sibling folder outside the project."""
    home = tmp_path / "home"
    proj = home / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "app.py").write_text("".join(f"line {i}\n" for i in range(1, 101)))
    (proj / "README.md").write_text("hello\n")
    (proj / ".env").write_text("SECRET=1\n")
    (proj / ".git").mkdir()
    (proj / ".git" / "config").write_text("[remote]\nurl = https://u:pw@x/\n")
    (proj / "deploy").mkdir()
    (proj / "deploy" / "server.key").write_text("-----BEGIN PRIVATE KEY-----\n")
    (proj / "id_ed25519").write_text("key\n")
    (home / "other").mkdir()
    (home / "other" / "secret.txt").write_text("outside\n")
    monkeypatch.setenv("AGENT_SESSIONS_FS_ROOT", str(home))
    files.reset_capabilities_for_test()
    return proj


def run(proj, name, **args):
    r = chat_tools.run(str(proj), name, json.dumps(args))
    return json.loads(r.content), r.summary


# ---- confinement --------------------------------------------------------------------------------


def test_read_and_list_inside_the_folder(project):
    body, summ = run(project, "read_file", path="src/app.py", start_line=3, max_lines=2)
    assert body["text"] == "line 3\nline 4"
    assert (body["start_line"], body["end_line"], body["total_lines"]) == (3, 4, 101)
    assert summ == {
        "name": "read_file",
        "path": "src/app.py",
        "outcome": "ok",
        "start_line": 3,
        "end_line": 4,
        "total_lines": 101,
    }
    assert "text" not in summ and "line 3" not in json.dumps(summ)  # the summary never has content
    body, summ = run(project, "list_files", path=".")
    names = {e["name"] for e in body["entries"]}
    assert names == {"src", "README.md", "deploy"}  # hidden + credential names are not listed
    assert body["omitted"] == 3
    assert summ["entries"] == 3


@pytest.mark.parametrize(
    "path",
    ["../other/secret.txt", "src/../../other/secret.txt", "/etc/passwd", "~/other/secret.txt"],
)
def test_escapes_are_refused(project, path):
    body, summ = run(project, "read_file", path=path)
    assert summ["outcome"] == "refused"
    if not path.startswith("~"):  # "~" is not expanded: it is a (missing) name inside the folder
        assert summ["reason"] == "outside the conversation's folder"
    assert "secret" not in json.dumps(body).replace(path, "")


def test_an_absolute_path_inside_the_folder_is_fine(project):
    body, summ = run(project, "read_file", path=str(project / "README.md"))
    assert summ["outcome"] == "ok" and body["text"].startswith("hello")


def test_a_symlinked_final_component_is_refused(project):
    os.symlink(project.parent / "other" / "secret.txt", project / "link.txt")
    body, summ = run(project, "read_file", path="link.txt")
    assert summ["outcome"] == "refused" and "link" in summ["reason"]


def test_a_symlinked_directory_pointing_outside_is_refused_on_the_descriptor(project):
    os.symlink(project.parent / "other", project / "docs")
    _, summ = run(project, "read_file", path="docs/secret.txt")
    assert summ["outcome"] == "refused"
    assert summ["reason"] == "outside the conversation's folder"


def test_the_descriptor_proof_itself_is_rooted_at_the_folder(project, monkeypatch):
    """`files`' own layer, without chat_tools' second check: a component repointed between the
    realpath and the open must fail the fd re-check against the NARROWED root — a file elsewhere
    under $HOME is still an escape."""
    root = str(project)
    real = project / "src"
    orig = files.contained_path

    def swap_after_check(path, r=None):
        resolved = orig(path, r)
        real.rename(project / "src-real")
        os.symlink(project.parent / "other", real)
        return resolved.replace("app.py", "secret.txt")

    monkeypatch.setattr(files, "contained_path", swap_after_check)
    with pytest.raises(files.FsError) as e:
        files.read_file_bytes(str(real / "app.py"), root=root)
    assert e.value.status == 403


def test_a_plain_name_symlinked_onto_a_hidden_dir_is_refused(project):
    os.symlink(project / ".git", project / "gitdir")
    _, summ = run(project, "read_file", path="gitdir/config")
    assert summ["outcome"] == "refused" and summ["reason"].startswith("hidden path")
    _, summ = run(project, "list_files", path="gitdir")
    assert summ["outcome"] == "refused"


def test_a_swap_between_check_and_open_is_caught_by_the_fd_recheck(project, monkeypatch):
    """The lexical check passes, then the directory is repointed before the open: the descriptor
    proof must refuse what was actually opened."""
    real = project / "src"
    moved = project / "src-real"
    outside = project.parent / "other"
    orig = files._open_verified

    def swap_then_open(path, *, directory, root=None):
        real.rename(moved)
        os.symlink(outside, real)
        return orig(path.replace("app.py", "secret.txt"), directory=directory, root=root)

    monkeypatch.setattr(files, "_open_verified", swap_then_open)
    _, summ = run(project, "read_file", path="src/app.py")
    assert summ["outcome"] == "refused"


@pytest.mark.parametrize(
    "path",
    [".env", ".git/config", "deploy/server.key", "id_ed25519", "x/.ssh/id_rsa", "Credentials.json"],
)
def test_credential_shaped_paths_are_refused_before_any_open(project, path, monkeypatch):
    opened = []
    monkeypatch.setattr(files, "_open_verified", lambda *a, **k: opened.append(a) or 1 / 0)
    _, summ = run(project, "read_file", path=path)
    assert summ["outcome"] == "refused"
    assert opened == []


def test_a_fifo_is_refused_without_blocking(project):
    os.mkfifo(project / "pipe")
    _, summ = run(project, "read_file", path="pipe")
    assert summ["outcome"] == "refused" and summ["reason"] == "not a regular file"


def test_binary_files_are_refused(project):
    (project / "blob.bin").write_bytes(b"\x00\x01\x02")
    _, summ = run(project, "read_file", path="blob.bin")
    assert summ["reason"] == "not a text file"


def test_an_excluded_folder_is_refused_and_the_boundary_is_read_live(project):
    assert run(project, "read_file", path="README.md")[1]["outcome"] == "ok"
    prefs.set_folder_exclusions([str(project / "src")])
    _, summ = run(project, "read_file", path="src/app.py")
    assert summ["outcome"] == "refused" and summ["reason"] == "an excluded folder"
    body, _ = run(project, "list_files", path=".")
    assert "src" not in {e["name"] for e in body["entries"]}
    prefs.set_folder_exclusions([str(project)])
    _, summ = run(project, "read_file", path="README.md")
    assert summ["reason"] == "this conversation's folder is not readable now"


def test_a_folder_outside_every_root_is_not_readable(project):
    prefs.set_project_roots([str(project.parent / "other")])
    _, summ = run(project, "read_file", path="README.md")
    assert summ["reason"] == "this conversation's folder is not readable now"


@pytest.mark.parametrize(
    ("name", "raw", "reason"),
    [
        ("rm_rf", '{"path": "."}', "no such tool"),
        ("read_file", "not json", "arguments are not valid JSON"),
        ("read_file", "[1]", "arguments must be an object"),
        ("read_file", '{"path": ".", "mode": "w"}', "unknown arguments: ['mode']"),
        ("read_file", '{"path": 7}', "path must be a string"),
        ("read_file", '{"path": "a\\u0000b"}', "invalid path"),
        ("read_file", '{"path": "README.md", "start_line": 0}', None),
    ],
)
def test_malformed_calls_are_refused_results_never_exceptions(project, name, raw, reason):
    r = chat_tools.run(str(project), name, raw)
    assert r.summary["outcome"] == "refused"
    if reason:
        assert r.summary["reason"] == reason


# ---- bounds -------------------------------------------------------------------------------------


def test_read_is_bounded_by_lines_and_bytes(project):
    (project / "big.txt").write_text("".join(f"{i}\n" for i in range(5000)))
    body, _ = run(project, "read_file", path="big.txt")
    assert body["end_line"] - body["start_line"] + 1 == chat_tools.READ_MAX_LINES
    assert body["more"] is True
    (project / "wide.txt").write_text(("x" * 1000 + "\n") * 500)
    body, _ = run(project, "read_file", path="wide.txt")
    assert len(body["text"].encode()) <= chat_tools.READ_MAX_BYTES
    (project / "one.txt").write_text("y" * (chat_tools.READ_MAX_BYTES * 3))
    body, _ = run(project, "read_file", path="one.txt")
    assert len(body["text"].encode()) == chat_tools.READ_MAX_BYTES


def test_listing_is_bounded(project):
    d = project / "many"
    d.mkdir()
    for i in range(chat_tools.LIST_MAX_ENTRIES + 20):
        (d / f"f{i}").write_text("")
    body, summ = run(project, "list_files", path="many")
    assert summ["entries"] == chat_tools.LIST_MAX_ENTRIES
    assert body["omitted"] == 20 and body["complete"] is False


# ---- the turn loop ------------------------------------------------------------------------------


class Endpoint:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.replies: list = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        nxt = self.replies.pop(0) if self.replies else answer("done")
        return nxt(request) if callable(nxt) else nxt

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


def answer(text: str, usage: int = 10) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": usage, "completion_tokens": 1, "total_tokens": usage + 1},
        },
    )


def calls(*items: tuple[str, dict], usage: int = 10, content: str | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": [
                            {
                                "id": f"c{i}-{uuid.uuid4().hex[:6]}",
                                "type": "function",
                                "function": {"name": n, "arguments": json.dumps(a)},
                            }
                            for i, (n, a) in enumerate(items)
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": usage, "completion_tokens": 1, "total_tokens": usage + 1},
        },
    )


@pytest.fixture
def endpoint(monkeypatch, tmp_path, project):
    monkeypatch.setenv("AGENT_SESSIONS_CHAT_DIR", str(tmp_path / "chat-store"))
    ep = Endpoint()
    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(ep.handler))
    yield ep
    chat_runtime._TASKS.clear()
    chat_runtime._LOCKS.clear()


def configure(tools: str = "read") -> None:
    chat_config.set_config(ENGINE, {"base_url": URL, "api_key": KEY, "model": "m", "tools": tools})


async def turn(project, text: str = "look at app.py") -> tuple[str, str]:
    sid = await chat_runtime.new_session(ENGINE, str(project))
    t = str(uuid.uuid4())
    await chat_runtime.send(ENGINE, sid, t, text)
    task = chat_runtime.running_task(ENGINE, sid)
    if task is not None:
        await task
    return sid, t


async def view(sid: str) -> dict:
    return (await chat_runtime.get_session(ENGINE, sid))["turns"][-1]


def store_text(tmp_path) -> str:
    return "".join(p.read_text() for p in (tmp_path / "chat-store").glob("*.jsonl"))


def test_tools_setting_is_validated_public_and_defaults_off():
    chat_config.set_config(ENGINE, {"base_url": URL, "api_key": KEY, "model": "m"})
    assert chat_config.public(ENGINE)["tools"] == "none"
    with pytest.raises(chat_config.ChatConfigError):
        chat_config.validate_patch({"tools": "execute"})
    configure("read")
    assert chat_config.public(ENGINE)["tools"] == "read"


@pytest.mark.anyio
async def test_tools_off_sends_no_tools_key_and_the_plain_prompt(endpoint, project):
    configure("none")
    await turn(project)
    (body,) = endpoint.bodies()
    assert "tools" not in body and "tool_choice" not in body
    assert body["messages"][0]["content"] == chat_config.prompts.effective("chat_agent")


@pytest.mark.anyio
async def test_a_tool_round_then_an_answer(endpoint, project, tmp_path):
    configure("read")
    endpoint.replies = [
        calls(
            ("read_file", {"path": "src/app.py", "max_lines": 3}), ("read_file", {"path": ".env"})
        ),
        answer("app.py prints lines"),
    ]
    sid, _ = await turn(project)
    t = await view(sid)
    assert t["status"] == "done" and t["reply"] == "app.py prints lines"
    assert [(c["name"], c["outcome"]) for c in t["tools"]] == [
        ("read_file", "ok"),
        ("read_file", "refused"),
    ]
    first, second = endpoint.bodies()
    assert first["tools"] == chat_tools.SPECS
    assert first["messages"][0]["content"] == chat_config.prompts.effective("chat_agent_tools")
    roles = [m["role"] for m in second["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "tool"]
    tool_msgs = [m for m in second["messages"] if m["role"] == "tool"]
    assert "line 1\\nline 2\\nline 3" in tool_msgs[0]["content"]
    assert "SECRET" not in json.dumps(second)
    # usage summed over both rounds, recorded once
    assert t["usage"] == {"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22}
    # file contents never reach the transcript
    assert "line 2" not in store_text(tmp_path)


@pytest.mark.anyio
async def test_the_round_cap_forces_an_answer(endpoint, project):
    configure("read")
    endpoint.replies = [calls(("list_files", {"path": "."})) for _ in range(20)]
    endpoint.replies.insert(chat_runtime.MAX_TOOL_ROUNDS, answer("fine"))
    sid, _ = await turn(project)
    bodies = endpoint.bodies()
    assert len(bodies) == chat_runtime.MAX_TOOL_ROUNDS + 1
    assert all("tool_choice" not in b for b in bodies[:-1])
    assert bodies[-1]["tool_choice"] == "none"
    assert (await view(sid))["reply"] == "fine"


@pytest.mark.anyio
async def test_calls_per_round_are_capped(endpoint, project):
    configure("read")
    n = chat_runtime.MAX_CALLS_PER_ROUND + 3
    endpoint.replies = [calls(*[("list_files", {"path": "."})] * n), answer("ok")]
    sid, _ = await turn(project)
    outcomes = [c["outcome"] for c in (await view(sid))["tools"]]
    assert outcomes.count("ok") == chat_runtime.MAX_CALLS_PER_ROUND
    assert outcomes.count("refused") == 3


@pytest.mark.anyio
async def test_turning_tools_off_mid_turn_stops_the_next_call(endpoint, project):
    configure("read")

    def switch_off(request):
        chat_config.set_config(ENGINE, {"tools": "none"})
        return calls(("read_file", {"path": "README.md"}))

    endpoint.replies = [switch_off, answer("ok")]
    sid, _ = await turn(project)
    (c,) = (await view(sid))["tools"]
    assert c["outcome"] == "refused" and c["reason"] == "tools were turned off"
    assert "tools" not in endpoint.bodies()[1]  # the next round offers no tools either


@pytest.mark.anyio
async def test_an_exclusion_added_mid_turn_stops_the_next_call(endpoint, project):
    configure("read")

    def exclude(request):
        prefs.set_folder_exclusions([str(project)])
        return calls(("read_file", {"path": "README.md"}))

    endpoint.replies = [exclude, answer("ok")]
    sid, _ = await turn(project)
    (c,) = (await view(sid))["tools"]
    assert c["reason"] == "this conversation's folder is not readable now"


@pytest.mark.anyio
async def test_a_template_secret_in_a_read_file_is_redacted_on_the_wire(
    endpoint, project, monkeypatch
):
    configure("read")
    (project / "notes.txt").write_text("token=hunter2-SECRET-VALUE\n")
    monkeypatch.setattr(
        chat_runtime.review.template_secrets,
        "redaction_values",
        lambda: ["hunter2-SECRET-VALUE"],
    )
    endpoint.replies = [calls(("read_file", {"path": "notes.txt"})), answer("ok")]
    await turn(project)
    assert "hunter2-SECRET-VALUE" not in endpoint.requests[1].content.decode()


@pytest.mark.anyio
async def test_an_endpoint_that_rejects_tools_fails_the_turn_with_the_fix(endpoint, project):
    configure("read")
    refusal = httpx.Response(400, json={"error": "tools are not supported by this model"})
    # Twice: `_post_chat` degrades one optional field on a 400 before it gives up (#841).
    endpoint.replies = [refusal, refusal]
    sid, _ = await turn(project)
    t = await view(sid)
    assert t["status"] == "failed" and "turn Tools off" in t["reason"]


@pytest.mark.anyio
async def test_a_malformed_tool_call_shape_fails_the_turn(endpoint, project):
    configure("read")
    bad = httpx.Response(
        200,
        json={"choices": [{"message": {"content": None, "tool_calls": [{"function": 3}]}}]},
    )
    endpoint.replies = [bad]
    sid, _ = await turn(project)
    assert (await view(sid))["status"] == "failed"


@pytest.mark.anyio
async def test_retry_starts_a_new_attempt_with_its_own_tool_rows(endpoint, project):
    configure("read")
    endpoint.replies = [
        calls(("list_files", {"path": "."})),
        httpx.Response(500, json={}),
        calls(("read_file", {"path": "README.md"})),
        answer("ok"),
    ]
    sid, t = await turn(project)
    assert (await view(sid))["status"] == "failed"
    await chat_runtime.retry(ENGINE, sid, t)
    await chat_runtime.running_task(ENGINE, sid)
    v = await view(sid)
    assert v["status"] == "done"
    assert [c["name"] for c in v["tools"]] == ["read_file"]  # only the latest attempt's


@pytest.mark.anyio
async def test_a_running_call_on_a_settled_turn_reads_as_stopped(endpoint, project, tmp_path):
    configure("read")
    sid, t = await turn(project)
    root = tmp_path / "chat-store"
    rec = {
        "type": "tool",
        "turn_id": t,
        "call_id": "x",
        "name": "read_file",
        "path": "a",
        "outcome": "running",
    }
    await asyncio.to_thread(chat_store.append, root, sid, rec)
    (c,) = (await view(sid))["tools"]
    assert c["outcome"] == "stopped"


@pytest.mark.anyio
async def test_a_result_past_the_budget_is_refused_and_the_model_told_to_answer(endpoint, project):
    chat_config.set_config(
        ENGINE,
        {
            "base_url": URL,
            "api_key": KEY,
            "model": "m",
            "tools": "read",
            "context_window": 8192,
            "max_output_tokens": 4096,
        },
    )
    (project / "huge.txt").write_text(("z" * 60 + "\n") * 1000)
    endpoint.replies = [
        calls(("read_file", {"path": "huge.txt"})),
        calls(("read_file", {"path": "huge.txt"})),
        answer("ok"),
    ]
    sid, _ = await turn(project)
    c = (await view(sid))["tools"]
    assert c[0]["outcome"] == "refused"
    assert c[0]["reason"] == "the result did not fit the context window"


@pytest.mark.anyio
async def test_revoked_after_a_read_nothing_read_leaves(endpoint, project, monkeypatch):
    """Revoked between a read and the round that would carry its result: the result is withdrawn
    from the wire, and the call/result pairing stays valid (Hermes on #1222)."""
    configure("read")
    (project / "notes.txt").write_text("the-read-contents\n")
    real = chat_tools.run

    def read_then_revoke(cwd, name, args):
        out = real(cwd, name, args)
        chat_config.set_config(ENGINE, {"tools": "none"})
        return out

    monkeypatch.setattr(chat_tools, "run", read_then_revoke)
    endpoint.replies = [calls(("read_file", {"path": "notes.txt"})), answer("ok")]
    await turn(project)
    second = endpoint.bodies()[1]
    assert "the-read-contents" not in json.dumps(second)
    assert "tools" not in second
    roles = [m["role"] for m in second["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert "withdrawn" in second["messages"][-1]["content"]


@pytest.mark.anyio
async def test_calls_after_being_told_to_answer_fail_closed(endpoint, project):
    configure("read")
    # The forced round answers with an EMPTY text and more calls: still a refusal to answer.
    endpoint.replies = [calls(("list_files", {"path": "."}), content="") for _ in range(20)]
    sid, _ = await turn(project)
    t = await view(sid)
    assert t["status"] == "failed"
    assert t["reason"] == "the endpoint kept calling tools after it was told to answer"
    assert len(endpoint.bodies()) == chat_runtime.MAX_TOOL_ROUNDS + 1
    ran = [c for c in t["tools"] if c["outcome"] == "ok"]
    assert len(ran) == chat_runtime.MAX_TOOL_ROUNDS  # the forced round's calls never executed


@pytest.mark.anyio
async def test_a_target_excluded_after_its_read_is_withdrawn_before_sending(
    endpoint, project, monkeypatch
):
    """The folder stays admitted; only the read file's directory becomes excluded between the read
    and the round that would carry it (Hermes on #1222). Its result is withdrawn; an unaffected
    result from the same round still goes out."""
    configure("read")
    real = chat_tools.run
    n = {"calls": 0}

    def read_then_exclude(cwd, name, args):
        out = real(cwd, name, args)
        n["calls"] += 1
        if n["calls"] == 2:
            prefs.set_folder_exclusions([str(project / "src")])
        return out

    monkeypatch.setattr(chat_tools, "run", read_then_exclude)
    endpoint.replies = [
        calls(("read_file", {"path": "src/app.py"}), ("read_file", {"path": "README.md"})),
        answer("ok"),
    ]
    await turn(project)
    second = endpoint.bodies()[1]
    tool_msgs = [m for m in second["messages"] if m["role"] == "tool"]
    assert "withdrawn" in tool_msgs[0]["content"] and "line 1" not in json.dumps(second)
    assert "hello" in tool_msgs[1]["content"]  # README is still admitted
    assert second["tools"] == chat_tools.SPECS  # tools are still on


# ---- a lone surrogate (valid JSON: "\ud800") from the model (Hermes on #1228) ----------------

SURROGATE_PATH = "a\ud800b"


@pytest.mark.parametrize("tool", ["read_file", "list_files"])
def test_a_lone_surrogate_path_is_refused_not_raised(project, tool):
    r = chat_tools.run(str(project), tool, json.dumps({"path": SURROGATE_PATH}))
    assert r.summary["outcome"] == "refused" and r.summary["reason"] == "invalid path"
    assert r.summary["path"] == "a�b"  # echoed printable, never the raw surrogate
    json.dumps(r.summary, ensure_ascii=False).encode()  # the record layer can write it


def test_a_surrogate_in_a_refusal_that_happens_before_resolve_is_printable(project):
    r = chat_tools.run(str(project), "bad\ud800", json.dumps({"path": SURROGATE_PATH}))
    assert r.summary["outcome"] == "refused"
    json.dumps(r.summary, ensure_ascii=False).encode()


def test_the_store_writes_a_surrogate_instead_of_losing_the_batch(tmp_path):
    root = tmp_path / "store"
    sid = str(uuid.uuid4())
    chat_store.create(root, sid, cwd=str(tmp_path))
    t = str(uuid.uuid4())
    chat_store.append(
        root,
        sid,
        {"type": "user", "turn_id": t, "text": "hi", "ts": 1.0},
        {
            "type": "tool",
            "turn_id": t,
            "call_id": "c\ud800",
            "name": "read_file",
            "path": "a\ud800",
            "outcome": "running",
        },
    )
    log = chat_store.read(root, sid)
    assert log is not None and log.turns[0].text == "hi"
    assert len(log.turns[0].tools) == 1


@pytest.mark.anyio
async def test_a_surrogate_path_in_a_turn_is_a_refused_row_and_the_turn_goes_on(endpoint, project):
    configure("read")
    endpoint.replies = [
        calls(("read_file", {"path": SURROGATE_PATH}), ("list_files", {"path": SURROGATE_PATH})),
        answer("carried on"),
    ]
    sid, _ = await turn(project)
    t = await view(sid)
    assert t["status"] == "done" and t["reply"] == "carried on"
    assert [(c["outcome"], c["reason"]) for c in t["tools"]] == [
        ("refused", "invalid path"),
        ("refused", "invalid path"),
    ]
    json.dumps(t, ensure_ascii=False).encode()  # the pane's JSON response can carry it
