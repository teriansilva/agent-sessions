"""A scripted stand-in for `codex app-server`, `claude --print` stream-JSON (#1278 tests) and
`opencode acp` (#1312).

Behaviour is selected by the turn text, so a test drives it purely through the public API:

* ``APPROVE`` — ask one exact permission request, finish with ``approved:<answer>``.
* ``ASKJSON:{...}`` — ask one permission request whose params / request carry the given fields
  (#1339: a command, ``availableDecisions``, ``permission_suggestions`` …); the frame BattleLab
  answers with is logged like every other frame.
* ``RUN:<command>`` — run one command tool call without asking (a skip-permissions session's
  tool row, #1339), then reply ``ran``.
* ``MCPWRITE`` — (codex) one MCP tool call to the BattleLab memory tool, asked for with the
  ``mcpServer/elicitation/request`` codex-cli 0.162.0 sends (#1506,
  ``fixtures/codex_mcp_elicitation.json``). Only an ``accept`` runs it: the fake starts the
  server from THIS thread's ``config["mcp_servers.battlelab_memory"]`` exactly as Codex would and
  calls ``memory_write``; it replies ``mcp:<action or error>:<tool result>``.
* ``HANG``    — start the turn and never finish it (for interrupt / worker death).
* ``WAIT_RELEASE`` — finish normally after the test creates ``release-queued-turn``.
* ``DIE``     — exit right after accepting the turn (native crash mid-turn).
* a ``hold-initialize`` file in the working directory — (codex) answer ``initialize`` only once
  ``release-initialize`` exists: the worker is launched but not ready (#1505).
* ``BGTASK``  — (claude) start a background task and reply ``started``. Once the test creates
  ``release-bg-task`` the task completes and the agent runs a turn of its own for the
  notification (output ``notified``, a ``task-notification`` result). It is held open until
  ``release-notification`` while ``hold-notification`` (after its output) or
  ``hold-notification-start`` (before any output) exists (#1427).
* OOM fixtures (#1732, `memory_turn`), every protocol; under a tiny `MemoryMax`:
  ``OOMCHILD:<n>`` runs n children one after another that each allocate until killed, and
  replies ``echo:children:<their return codes>``; ``OOMSELF`` allocates in the agent itself;
  ``OOM_THEN_KILLSELF`` runs one such child, then SIGKILLs the agent; ``KILLSELF`` only SIGKILLs
  the agent; ``OOMLATER`` replies at once and runs one such child once ``release-hog`` exists;
  ``CLOSE_THEN_EXIT`` / ``CLOSE_THEN_KILLSELF`` close stdout, then exit 7 / SIGKILL themselves
  0.5 s later; ``BADFRAME`` writes a line no protocol accepts and stays alive.
  Each child's return code is appended to ``hog-codes.txt``. A ``report-oom-adj`` file in the
  working directory makes the agent fork a child first thing, which records its own and its
  parent's ``oom_score_adj`` (and the parent's environment) in ``oom-adj.jsonl``.
* anything else — stream a reply ``echo:<text>`` and complete.

Every frame received is appended to ``$PWD/native-frames.jsonl`` so tests can prove a frame
was written exactly once. ``--version`` prints a version above the protocol floors.
"""

import json
import os
import sys
import threading
import time
import uuid

LOG = os.path.join(os.getcwd(), "native-frames.jsonl")


# Every entry records the BattleLab session the child was told (#1406), so a test can prove each
# generation names the app session, never the native id.
EXTRA: dict = {"battlelab_session": os.environ.get("BATTLELAB_SESSION")}


def log(frame):
    with open(LOG, "a") as fh:
        entry = {"pid": os.getpid(), "argv": sys.argv[1:], "frame": frame, **EXTRA}
        fh.write(json.dumps(entry) + "\n")


_OUT = threading.Lock()
# Background tasks the fake Claude runs (`BGTASK`): a plain interrupt is the CLI's Stop, which
# ends them (`stop-ends-tasks`, #1526); its `send_now` interrupt does not.
TASKS: set = set()


def claude_init(session):
    """`system/init`, advertising the CLI's own Send now while `advertise-send-now` exists."""
    frame = {"type": "system", "subtype": "init", "model": "fake-claude", "session_id": session}
    if os.path.exists("advertise-send-now"):
        frame["capabilities"] = ["interrupt_receipt_v1", "interrupt_send_now_v1"]
    return frame


def send(frame):
    with _OUT:  # a background notification turn writes from its own thread (#1427)
        sys.stdout.write(json.dumps(frame) + "\n")
        sys.stdout.flush()


def _wait_file(name, seconds=20, cancel=None):
    """Until ``name`` exists, ``cancel`` is set (True returned) or the deadline passes."""
    deadline = time.monotonic() + seconds
    while not os.path.exists(name) and time.monotonic() < deadline:
        if cancel is not None and cancel.is_set():
            return True
        time.sleep(0.01)
    return False


def wait_release(text):
    if text == "WAIT_RELEASE":
        # A failed assertion may kill the test worker first; never leave its fake child waiting.
        deadline = time.monotonic() + 10
        while not os.path.exists("release-queued-turn") and time.monotonic() < deadline:
            time.sleep(0.01)


HOG = "b = []\nwhile True:\n    b.append(bytearray(4 << 20))\n"


def hog() -> int:
    """One child that allocates until the kernel kills it (#1732); its return code."""
    import subprocess

    code = subprocess.run(  # noqa: S603 — literal argv
        [sys.executable, "-c", HOG],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode
    with open("hog-codes.txt", "a") as fh:
        fh.write(f"{code}\n")
    return code


def memory_turn(text):
    """The OOM fixtures (#1732, module docstring): the word to reply with, or None."""
    import signal

    if text.startswith("OOMCHILD:"):
        return "children:" + ",".join(str(hog()) for _ in range(int(text.split(":")[1])))
    if text == "OOMSELF":
        hoard = []
        while True:
            hoard.append(bytearray(4 << 20))
    if text in {"OOM_THEN_KILLSELF", "KILLSELF"}:
        if text == "OOM_THEN_KILLSELF":
            hog()
        os.kill(os.getpid(), signal.SIGKILL)
    if text == "OOMLATER":
        threading.Thread(target=lambda: _wait_file("release-hog", 60) or hog(), daemon=True).start()
        return "later"
    if text in {"CLOSE_THEN_EXIT", "CLOSE_THEN_KILLSELF"}:
        sys.stdout.flush()
        os.close(1)
        time.sleep(0.5)
        if text == "CLOSE_THEN_KILLSELF":
            os.kill(os.getpid(), signal.SIGKILL)
        os._exit(7)
    if text == "BADFRAME":
        sys.stdout.write("{not a frame\n")
        sys.stdout.flush()
        time.sleep(60)
        os._exit(0)
    return None


def report_oom_adj():
    """`report-oom-adj` (#1732): fork first thing; the child records what it inherited."""
    if not os.path.exists("report-oom-adj"):
        return
    parent = os.getpid()
    pid = os.fork()
    if pid == 0:
        try:
            with open("/proc/self/oom_score_adj") as fh:
                child = int(fh.read())
            with open(f"/proc/{parent}/oom_score_adj") as fh:
                agent = int(fh.read())
            with open(f"/proc/{parent}/status") as fh:
                ignored = next(line for line in fh if line.startswith("SigIgn:")).split()[1]
            entry = {
                "pid": parent,
                "argv": sys.argv[1:],
                "agent": agent,
                "child": child,
                "sig_ignored": ignored,
                "env": dict(os.environ),
                "cwd": os.getcwd(),
            }
            with open("oom-adj.jsonl", "a") as fh:
                fh.write(json.dumps(entry) + "\n")
        finally:
            os._exit(0)
    os.waitpid(pid, 0)


def wait_cancel():
    if os.path.exists("hold-cancel-completion"):
        deadline = time.monotonic() + 10
        while not os.path.exists("release-cancel-completion") and time.monotonic() < deadline:
            time.sleep(0.01)


def frames():
    for line in sys.stdin:
        if line.strip():
            frame = json.loads(line)
            log(frame)
            yield frame


def _run_mcp_tool(server, tool, arguments):
    """Start an MCP server from a Codex per-thread config entry and call one tool, as Codex does
    after an accepted approval: literal argv, the entry's env over a minimal base."""
    import subprocess

    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **server.get("env", {})}
    frames_in = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
    ]
    done = subprocess.run(  # noqa: S603 — literal argv from the thread's own config
        [server["command"], *server["args"]],
        input="".join(json.dumps(f) + "\n" for f in frames_in).encode(),
        capture_output=True,
        env=env,
        timeout=60,
        check=False,
    )
    replies = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
    result = next((r["result"] for r in replies if r.get("id") == 2 and "result" in r), None)
    return "no-result" if result is None else result["content"][0]["text"]


def codex():
    thread = None
    turn = None
    mcp_servers = {}
    stream = frames()
    for frame in stream:
        method, rid = frame.get("method"), frame.get("id")
        params = frame.get("params", {})
        if method == "initialize":
            # #1505: a worker that is launched but not ready yet, until the test releases it.
            if os.path.exists("hold-initialize"):
                _wait_file("release-initialize", 30)
            send({"id": rid, "result": {"userAgent": "fake/0.160.0"}})
        elif method == "model/list" and os.path.exists("malformed-models"):
            # A CLI reply of the wrong shape (#1313, Hermes): efforts a number, data items odd.
            data = [{"model": "fake-odd", "supportedReasoningEfforts": 42}, "not-a-dict", 7]
            send({"id": rid, "result": {"data": data, "nextCursor": None}})
        elif method == "model/list":
            # Two pages (#1313): the cursor must be followed; a hidden model is never offered.
            if not params.get("cursor"):
                data = [
                    {
                        "model": "fake-large",
                        "displayName": "Fake Large",
                        "isDefault": True,
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "low"},
                            {"reasoningEffort": "high"},
                        ],
                    },
                    {"model": "fake-hidden", "hidden": True},
                ]
                send({"id": rid, "result": {"data": data, "nextCursor": "p2"}})
            else:
                data = [{"model": "fake-small", "displayName": "Fake Small"}]
                send({"id": rid, "result": {"data": data, "nextCursor": None}})
        elif method == "thread/start":
            thread = str(uuid.uuid4())
            mcp_servers = {
                k.split(".", 1)[1]: v
                for k, v in (params.get("config") or {}).items()
                if k.startswith("mcp_servers.")
            }
            send(
                {
                    "id": rid,
                    # The level an unpinned thread runs at (#1511); a resume reports another, so
                    # "the newest report wins" is observable.
                    "result": {
                        "thread": {"id": thread, "turns": []},
                        "model": "fake-model",
                        "reasoningEffort": "medium",
                    },
                }
            )
        elif method == "thread/resume":
            thread = params["threadId"]
            send(
                {
                    "id": rid,
                    "result": {
                        "thread": {"id": thread, "turns": [{"id": "old", "status": "completed"}]},
                        "model": "fake-model",
                        "reasoningEffort": "high",
                    },
                }
            )
        elif method == "turn/start":
            turn = str(uuid.uuid4())
            # #1332 Phase 3: pictures arrive as data-URL image items before the words.
            images = [i for i in params["input"] if i["type"] == "image"]
            text = "".join(i["text"] for i in params["input"] if i["type"] == "text")
            if images:
                text = f"IMAGES:{len(images)}:" + images[0]["url"][:22] + ":" + text
            if text == "REFUSE":
                send({"id": rid, "error": {"code": -32000, "message": "turn refused by agent"}})
                continue
            send({"id": rid, "result": {"turn": {"id": turn, "status": "inProgress"}}})
            send(
                {
                    "method": "turn/started",
                    "params": {"threadId": thread, "turn": {"id": turn, "status": "inProgress"}},
                }
            )
            if text == "DIE":
                sys.exit(9)
            text = memory_turn(text) or text
            if text == "HANG":
                continue
            wait_release(text)
            if text == "PARTIAL":
                done = {"threadId": thread, "turn": {"id": turn, "status": "completed"}}
                sys.stdout.write(json.dumps({"method": "turn/completed", "params": done}))
                sys.stdout.flush()  # no trailing newline: the frame was never finished
                sys.exit(9)
            if text == "STREAM2":
                # #1727 part 2: a finished first message, a command, then a second message that
                # streams word by word and stays open until interrupt.
                at = {"threadId": thread, "turnId": turn}
                first = "First message."
                send(
                    {
                        "method": "item/agentMessage/delta",
                        "params": {**at, "itemId": "msg-a", "delta": first},
                    }
                )
                done = {"id": "msg-a", "type": "agentMessage", "text": first}
                send({"method": "item/completed", "params": {**at, "item": done}})
                run = {
                    "id": "cmd-a",
                    "type": "commandExecution",
                    "status": "completed",
                    "command": "npm test",
                    "aggregatedOutput": "",
                }
                send({"method": "item/completed", "params": {**at, "item": run}})
                for i in range(40):
                    word = {**at, "itemId": "msg-b", "delta": f"w{i} "}
                    send({"method": "item/agentMessage/delta", "params": word})
                    time.sleep(0.05)
                continue
            if text == "STREAM":
                # Output keeps moving the journal revision; the turn stays open until interrupt.
                for _ in range(300):
                    delta = {"threadId": thread, "turnId": turn, "itemId": "msg-s", "delta": "."}
                    send({"method": "item/agentMessage/delta", "params": delta})
                    time.sleep(0.01)
                continue
            if text in {"EDIT", "EDIT_NOPATCH", "EDIT_CHANGING"}:
                at = {"threadId": thread, "turnId": turn}
                change = {"path": "README.md", "kind": {"type": "update"}, "diff": "-old\n+new\n"}
                if text != "EDIT_NOPATCH":
                    send(
                        {
                            "method": "item/fileChange/patchUpdated",
                            "params": {**at, "itemId": "edit-1", "changes": [change]},
                        }
                    )
                send(
                    {
                        "id": 88,
                        "method": "item/fileChange/requestApproval",
                        "params": {**at, "itemId": "edit-1", "reason": "update README"},
                    }
                )
                if text == "EDIT_CHANGING":
                    changed = {**change, "diff": "-old\n+something else\n"}
                    send(
                        {
                            "method": "item/fileChange/patchUpdated",
                            "params": {**at, "itemId": "edit-1", "changes": [changed]},
                        }
                    )
                reply = next(stream)
                answer = "edited:" + reply["result"]["decision"]
                send(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": thread,
                            "turnId": turn,
                            "item": {"id": "msg-1", "type": "agentMessage", "text": answer},
                        },
                    }
                )
                send(
                    {
                        "method": "turn/completed",
                        "params": {"threadId": thread, "turn": {"id": turn, "status": "completed"}},
                    }
                )
                continue
            if text.startswith("RUN:"):
                item = {
                    "id": "cmd-run",
                    "type": "commandExecution",
                    "status": "completed",
                    "command": text[len("RUN:") :],
                    "aggregatedOutput": "",
                }
                send(
                    {
                        "method": "item/completed",
                        "params": {"threadId": thread, "turnId": turn, "item": item},
                    }
                )
            if text == "BIG":
                item = {
                    "id": "cmd-1",
                    "type": "commandExecution",
                    "status": "completed",
                    "command": "cat big",
                    "aggregatedOutput": "x" * (2 * 1024 * 1024),
                }
                send(
                    {
                        "method": "item/completed",
                        "params": {"threadId": thread, "turnId": turn, "item": item},
                    }
                )
            answer = "echo:" + text
            if text == "MCPWRITE":
                server = "battlelab_memory"
                arguments = {"title": "Fake codex mcp", "body": "written after an approval"}
                item = {
                    "type": "mcpToolCall",
                    "id": "exec-mcp-1",
                    "server": server,
                    "tool": "memory_write",
                    "status": "inProgress",
                    "arguments": arguments,
                }
                send(
                    {
                        "method": "item/started",
                        "params": {"threadId": thread, "turnId": turn, "item": item},
                    }
                )
                send(
                    {
                        "id": 78,
                        "method": "mcpServer/elicitation/request",
                        "params": {
                            "threadId": thread,
                            "turnId": turn,
                            "serverName": server,
                            "mode": "form",
                            "_meta": {
                                "codex_approval_kind": "mcp_tool_call",
                                "persist": ["session", "always"],
                                "tool_description": "Leave one short note.",
                                "tool_params": arguments,
                                "tool_params_display": [
                                    {"name": k, "value": v, "display_name": k}
                                    for k, v in arguments.items()
                                ],
                            },
                            "message": f'Allow the {server} MCP server to run tool "memory_write"?',
                            "requestedSchema": {"type": "object", "properties": {}},
                        },
                    }
                )
                reply = next(stream)
                if "error" in reply:
                    answer = "mcp:error:" + str(reply["error"].get("code"))
                elif reply["result"].get("action") == "accept" and server in mcp_servers:
                    answer = "mcp:accept:" + _run_mcp_tool(
                        mcp_servers[server], "memory_write", arguments
                    )
                else:
                    answer = "mcp:" + str(reply["result"].get("action")) + ":not-run"
                send(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": thread,
                            "turnId": turn,
                            "item": {**item, "status": "completed"},
                        },
                    }
                )
            if text == "APPROVE":
                send(
                    {
                        "id": 77,
                        "method": "item/commandExecution/requestApproval",
                        "params": {
                            "threadId": thread,
                            "turnId": turn,
                            "itemId": "item-1",
                            "command": "ls -la",
                            "networkApprovalContext": {"host": "example.invalid"},
                            "proposedNetworkPolicyAmendments": [{"host": "example.invalid"}],
                        },
                    }
                )
                reply = next(stream)
                answer = "approved:" + reply["result"]["decision"]
            if text == "ASKTWICE":
                # Two async questions; the second only once the test creates `ask-again`, and the
                # turn ends after `release-queued-turn` (#1463).
                for n in (1, 2):
                    if n == 2:
                        _wait_file("ask-again", 10)
                    send(
                        {
                            "method": "item/completed",
                            "params": {
                                "threadId": thread,
                                "turnId": turn,
                                "item": {
                                    "id": f"call-{turn}-{n}",
                                    "type": "agentMessage",
                                    "text": f"Question {n}?",
                                    "delivery": "async",
                                    "questions": [{"title": f"Question {n}?", "options": ["yes"]}],
                                },
                            },
                        }
                    )
                _wait_file("release-queued-turn", 10)
            if text == "ASKASYNC":
                # Codex 0.162's async question (#1463): it is not waiting for the answer.
                send(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": thread,
                            "turnId": turn,
                            "item": {
                                "id": f"call-{turn}",
                                "type": "agentMessage",
                                "text": "Which channel?",
                                "delivery": "async",
                                "questions": [{"title": "Which channel?", "options": ["main"]}],
                            },
                        },
                    }
                )
            if text.startswith("ASKJSON:"):
                send(
                    {
                        "id": 78,
                        "method": "item/commandExecution/requestApproval",
                        "params": {
                            "threadId": thread,
                            "turnId": turn,
                            "itemId": "item-2",
                            **json.loads(text[len("ASKJSON:") :]),
                        },
                    }
                )
                reply = next(stream)
                answer = "decided:" + json.dumps(reply["result"]["decision"], sort_keys=True)
            send(
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": thread,
                        "turnId": turn,
                        "itemId": "msg-1",
                        "delta": answer[:3],
                    },
                }
            )
            send(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": thread,
                        "turnId": turn,
                        "item": {"id": "msg-1", "type": "agentMessage", "text": answer},
                    },
                }
            )
            send(
                {
                    "method": "turn/completed",
                    "params": {"threadId": thread, "turn": {"id": turn, "status": "completed"}},
                }
            )
        elif method == "turn/steer":
            text = "".join(i["text"] for i in params["input"] if i["type"] == "text")
            if text == "STEER_UNKNOWN":
                continue
            if text == "STEER_REFUSE" or params["expectedTurnId"] != turn:
                send({"id": rid, "error": {"code": -32000, "message": "steering refused"}})
                continue
            if text == "STEER_FINISH_FIRST":
                send(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": thread,
                            "turn": {"id": turn, "status": "completed"},
                        },
                    }
                )
            send({"id": rid, "result": {"turnId": turn}})
        elif method == "turn/interrupt":
            send({"id": rid, "result": {}})
            send(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread,
                        "turn": {"id": params["turnId"], "status": "interrupted"},
                    },
                }
            )


def claude():
    session = next(
        (a.split("=", 1)[1] for a in sys.argv if a.startswith(("--session-id=", "--resume="))),
        None,  # the model probe (#1313) starts no session
    )
    stream = frames()
    # Like the real CLI (#1427): one turn at a time, an operator input framed by
    # `command_lifecycle` (only while `emit-lifecycle` exists: an older CLI emits none), and a
    # notification turn the agent starts for itself framed by nothing.
    turn = threading.Lock()
    # The notification turn in flight, if any: an interrupt stops IT (#1427 Send now).
    note = {"active": False, "cancel": threading.Event(), "queued": False}
    hanging = {"uuid": None}  # an operator input whose turn never ended by itself (`HANG`)
    # A message written while an operator turn runs (#1526): the CLI queues it behind that turn.
    ahead = {"frame": None}

    def run_ahead():
        held, ahead["frame"] = ahead["frame"], None
        if held is None:
            return
        with turn:
            lifecycle(held.get("uuid"), "started")
            finished = claude_turn(held, stream, session, notification_turn)
        if finished:
            lifecycle(held.get("uuid"), "completed")
        else:
            hanging["uuid"] = held.get("uuid")

    def lifecycle(command, state):
        if os.path.exists("emit-lifecycle"):
            send(
                {
                    "type": "command_lifecycle",
                    "command_uuid": command,
                    "state": state,
                    "uuid": str(uuid.uuid4()),
                    "session_id": session,
                }
            )

    def notification_turn():
        # Far longer than any test waits for a turn: a test must never see the task end by itself.
        _wait_file("release-bg-task", seconds=120)
        if "bg1" not in TASKS:
            return  # a Stop ended it
        TASKS.discard("bg1")
        send(
            {
                "type": "system",
                "subtype": "task_notification",
                "task_id": "bg1",
                "status": "completed",
                "session_id": session,
            }
        )
        if os.path.exists("notify-after-turn"):
            # Like the real CLI (#1475): a notification arriving during an operator turn waits in
            # the CLI's own queue and runs when that turn ends, ahead of the next input.
            note["queued"] = True
            deadline = time.monotonic() + 30
            while hanging["uuid"] and time.monotonic() < deadline:
                time.sleep(0.01)
        with turn:
            note["queued"] = False
            note["active"] = True
            note["cancel"].clear()
            send(claude_init(session))
            cancelled = False
            # Like the real CLI: every model request is announced, before any output.
            send(
                {
                    "type": "system",
                    "subtype": "status",
                    "status": "requesting",
                    "session_id": session,
                }
            )
            if os.path.exists("emit-session-state"):  # a CLI that reports its session state
                send(
                    {
                        "type": "system",
                        "subtype": "session_state_changed",
                        "state": "running",
                        "session_id": session,
                    }
                )
            if os.path.exists("hold-notification-start"):  # started, no output yet (the race)
                cancelled = _wait_file("release-notification", cancel=note["cancel"])
            send(
                {
                    "type": "assistant",
                    "parent_tool_use_id": None,
                    "session_id": session,
                    "message": {
                        "id": "msg_note",
                        "content": [{"type": "text", "text": "notified"}],
                    },
                }
            )
            if not cancelled and os.path.exists("hold-notification"):
                cancelled = _wait_file("release-notification", cancel=note["cancel"])
            result = {
                "type": "result",
                "subtype": "success",
                "uuid": str(uuid.uuid4()),
                "is_error": False,
                "result": "notified",
                "session_id": session,
                "origin": {"kind": "task-notification", "producer": "session-task"},
            }
            if cancelled:  # what the real CLI sends for an interrupted turn
                result.update(
                    subtype="error_during_execution",
                    is_error=True,
                    result="",
                    terminal_reason="aborted_streaming",
                )
            note["active"] = False
            send(result)
            if os.path.exists("emit-session-state"):
                send(
                    {
                        "type": "system",
                        "subtype": "session_state_changed",
                        "state": "idle",
                        "session_id": session,
                    }
                )

    for frame in stream:
        kind = frame.get("type")
        if kind == "user":
            if frame.get("priority") is not None and os.path.exists("reject-priority"):
                continue  # a CLI that refuses the field: the input is never taken (#1475)
            lifecycle(frame.get("uuid"), "queued")
            if hanging["uuid"] and os.path.exists("advertise-send-now"):
                ahead["frame"] = frame  # waits behind the running turn (#1526)
                continue
            if (
                frame.get("priority") == "now"
                and note["active"]
                and not os.path.exists("ignore-priority")
            ):
                note["cancel"].set()  # the real CLI cuts the turn it runs for itself (#1475)
            with turn:
                lifecycle(frame.get("uuid"), "started")
                finished = claude_turn(frame, stream, session, notification_turn)
            if finished:
                lifecycle(frame.get("uuid"), "completed")
            else:
                hanging["uuid"] = frame.get("uuid")
            continue
        if kind == "control_request" and frame["request"]["subtype"] == "initialize":
            send(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": frame["request_id"],
                        "response": {
                            "models": [{"value": "fake-odd", "supportedEffortLevels": 42}]
                            if os.path.exists("malformed-models")
                            else [
                                {"value": "default", "displayName": "Default"},
                                {
                                    "value": "fake-opus",
                                    "displayName": "Fake Opus",
                                    "supportedEffortLevels": ["low", "max"],
                                },
                                {"value": "-not-a-model"},
                            ]
                        },
                    },
                }
            )
        elif (
            kind == "control_request"
            and frame["request"]["subtype"] == "interrupt"
            and (frame["request"].get("send_now") is True)
        ):
            # The CLI's own Send now (#1526): not a Stop. It cuts the running turn for a message
            # it already holds and ends no background task.
            held = ahead["frame"]
            waiting = (
                held is not None
                and held.get("uuid") == frame["request"].get("message_uuid")
                and hanging["uuid"]
            )
            send(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": frame["request_id"],
                        "response": {
                            "still_queued": [held["uuid"]] if held else [],
                            "send_now": "stopped" if waiting else "nothing_waiting",
                        },
                    },
                }
            )
            if not waiting:
                continue
            send(
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "uuid": str(uuid.uuid4()),
                    "is_error": True,
                    "terminal_reason": "aborted_tools",
                    "result": "",
                    "session_id": session,
                    "origin": {"kind": "human"},
                    "user_message_uuid": hanging["uuid"],
                }
            )
            lifecycle(hanging["uuid"], "cancelled")
            hanging["uuid"] = None
            run_ahead()
        elif kind == "control_request" and frame["request"]["subtype"] == "interrupt":
            if os.path.exists("stop-ends-tasks"):
                for task in sorted(TASKS):  # a Stop ends every background task (#1526)
                    send(
                        {
                            "type": "system",
                            "subtype": "task_notification",
                            "task_id": task,
                            "status": "stopped",
                            "session_id": session,
                        }
                    )
                TASKS.clear()
            if note["active"] and not os.path.exists("refuse-interrupt"):
                send(
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "success",
                            "request_id": frame["request_id"],
                            "response": {},
                        },
                    }
                )
                note["cancel"].set()  # the notification turn ends, interrupted
                continue
            if os.path.exists("refuse-interrupt"):
                completed = {
                    "type": "result",
                    "subtype": "success",
                    "uuid": str(uuid.uuid4()),
                    "is_error": False,
                    "result": "finished naturally",
                    "session_id": session,
                }
                if os.path.exists("finish-before-refusal"):
                    send(completed)
                    wait_release("WAIT_RELEASE")
                send(
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "error",
                            "request_id": frame["request_id"],
                            "error": "interruption refused",
                        },
                    }
                )
                if os.path.exists("finish-after-refusal"):
                    send(completed)
                continue
            ack = {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": frame["request_id"],
                    "response": {},
                },
            }
            # `late-interrupt-ack` (#1475 review): the worker reads the agent's own turn starting
            # BEFORE it may dispatch, the order a loaded host produces; the real CLI acks first.
            late = os.path.exists("late-interrupt-ack")
            if not late:
                send(ack)
            wait_cancel()
            aborted = {
                "type": "result",
                "subtype": "success",
                "uuid": str(uuid.uuid4()),
                "is_error": False,
                "terminal_reason": "aborted_streaming",
                "result": "",
                "session_id": session,
            }
            if os.path.exists("emit-lifecycle") and hanging["uuid"]:
                # The current CLI (captured, #1427): the interrupted input is named, and its
                # lifecycle ends `cancelled`.
                aborted.update(
                    origin={"kind": "human"},
                    user_message_uuid=hanging["uuid"],
                    subtype="error_during_execution",
                    is_error=True,
                )
            send(aborted)
            if os.path.exists("emit-lifecycle") and hanging["uuid"]:
                lifecycle(hanging["uuid"], "cancelled")
            hanging["uuid"] = None
            run_ahead()  # a Stop cancels nothing that waits: the held message runs next
            # The CLI starts what it queued for itself before it reads the next input (#1475).
            deadline = time.monotonic() + 10
            while note["queued"] and not note["active"] and time.monotonic() < deadline:
                time.sleep(0.01)
            if late:
                time.sleep(0.3)  # its opening frames are read first
                send(ack)


def claude_turn(frame, stream, session, notification_turn):
    text = frame["message"]["content"]
    if isinstance(text, list):  # #1332 Phase 3: base64 image blocks, then the words
        images = [b for b in text if b["type"] == "image"]
        words = "".join(b["text"] for b in text if b["type"] == "text")
        text = f"IMAGES:{len(images)}:{images[0]['source']['media_type']}:" + words
    # Like the real CLI: the history file appears with the first turn, not before.
    slug = os.getcwd().replace("/", "-")
    history = os.path.join(os.environ["HOME"], ".claude", "projects", slug)
    os.makedirs(history, exist_ok=True)
    with open(os.path.join(history, session + ".jsonl"), "a") as fh:
        fh.write(json.dumps({"type": "user", "message": text}) + "\n")
    send(claude_init(session))
    if text == "DIE":
        sys.exit(9)
    text = memory_turn(text) or text
    if text == "HANG":
        return False  # the turn never ends: no `completed` either
    wait_release(text)
    answer = "echo:" + text
    if text == "BGTASK":
        send({"type": "system", "subtype": "task_started", "task_id": "bg1", "session_id": session})
        TASKS.add("bg1")
        threading.Thread(target=notification_turn, daemon=True).start()
        answer = "started"
    if text == "APPROVE":
        send(
            {
                "type": "control_request",
                "request_id": "perm-1",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "Bash",
                    "tool_use_id": "toolu_1",
                    "input": {"command": "ls -la"},
                    "blocked_path": "/etc/hosts",
                    "decision_reason": "outside the working directory",
                    "title": "Run ls -la",
                },
                "session_id": session,
            }
        )
        reply = next(stream)
        answer = "approved:" + reply["response"]["response"]["behavior"]
    if text.startswith("ASKJSON:"):
        send(
            {
                "type": "control_request",
                "request_id": "perm-2",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_use_id": "toolu_2",
                    **json.loads(text[len("ASKJSON:") :]),
                },
                "session_id": session,
            }
        )
        reply = next(stream)
        answer = "decided:" + json.dumps(reply["response"]["response"], sort_keys=True)
    if text.startswith("RUN:"):
        send(
            {
                "type": "assistant",
                "message": {
                    "id": "msg_run",
                    "model": "fake-claude-1",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_run",
                            "name": "Bash",
                            "input": {"command": text[len("RUN:") :]},
                        }
                    ],
                },
                "session_id": session,
            }
        )
    if text == "SUBAGENT":
        request = {
            "subtype": "can_use_tool",
            "tool_name": "Bash",
            "tool_use_id": "toolu_sub",
            "input": {"command": "rm -rf build"},
            "agent_id": "agent-background-1",
        }
        send(
            {
                "type": "control_request",
                "request_id": "perm-sub",
                "request": request,
                "session_id": session,
            }
        )
        reply = next(stream)
        answer = "subagent:" + reply["response"]["subtype"]
    send(
        {
            "type": "assistant",
            "message": {
                "id": "msg_1",
                "model": "fake-claude-1",
                "content": [{"type": "text", "text": answer}],
            },
            "session_id": session,
        }
    )
    result = {
        "type": "result",
        "subtype": "success",
        "uuid": str(uuid.uuid4()),
        "is_error": False,
        "result": answer,
        "session_id": session,
    }
    if os.path.exists("emit-lifecycle"):  # the current CLI names the input it answered
        result.update(origin={"kind": "human"}, user_message_uuid=frame.get("uuid"))
    send(result)
    return True


def _control(name, default=None):
    """Test switches for the opencode fake. The child's environment is sanitized, so they live
    under its HOME (which `--version` and the child both get)."""
    try:
        with open(os.path.join(os.environ["HOME"], ".fake-opencode", name)) as fh:
            return fh.read().strip()
    except OSError:
        return default


def _control_copilot(name, default=None):
    """Test switches for the copilot fake (#1565), under its HOME like the opencode one's."""
    try:
        with open(os.path.join(os.environ["HOME"], ".fake-copilot", name)) as fh:
            return fh.read().strip()
    except OSError:
        return default


def _merge(base, over):
    """opencode merges config key by key: an existing key keeps its position, new keys append."""
    out = dict(base)
    for key, value in over.items():
        out[key] = (
            _merge(out[key], value)
            if isinstance(out.get(key), dict) and isinstance(value, dict)
            else value
        )
    return out


def _opencode_config():
    """The operator's config (a test switch) with `OPENCODE_CONFIG_CONTENT` merged LAST over it."""
    operator = json.loads(_control("operator.json", "{}"))
    return _merge(operator, json.loads(os.environ.get("OPENCODE_CONFIG_CONTENT") or "{}"))


def _debug_config():
    """`opencode debug config`: the resolved config, secret-looking environment values masked
    the way 1.18.35 prints them. Test switches make it fail, print garbage, or mask a command."""
    if _control("probe") == "fail":
        sys.exit(1)
    if _control("probe") == "garbage":
        print("not json")
        return
    config = _opencode_config()
    for entry in (config.get("mcp") or {}).values():
        env = entry.get("environment") or {}
        for key in env:
            if any(word in key for word in ("TOKEN", "KEY", "SECRET", "PASSWORD")):
                env[key] = "***"
        if _control("probe") == "masked" and entry.get("command"):
            entry["command"] = [*entry["command"][:-1], "***"]
    print(json.dumps(config))


def _start_mcp(config):
    """Like 1.18.35: each enabled local MCP server starts with `{...process.env, ...environment}`
    (here: recorded, not executed) — whether the password reached it, and which keys it got."""
    for name, entry in (config.get("mcp") or {}).items():
        if entry.get("type") != "local" or entry.get("enabled") is False:
            continue
        env = {**os.environ, **(entry.get("environment") or {})}
        with open(os.path.join(os.getcwd(), "mcp-children.jsonl"), "a") as fh:
            record = {
                "name": name,
                "command": entry.get("command"),
                "has_password": bool(env.get("OPENCODE_SERVER_PASSWORD")),
                "keys": sorted(entry.get("environment") or {}),
            }
            fh.write(json.dumps(record) + "\n")


def _permission(config, mode, permission):
    """opencode's evaluation, reduced: defaults (allow everything), then the config-level block,
    then the agent's own block; the LAST rule naming the permission (or `*`) decides."""
    agent = config.get("agent", {}).get(mode, {})
    rules = [("*", "allow"), *config.get("permission", {}).items()]
    rules += list(agent.get("permission", {}).items())
    action = "allow"
    for key, value in rules:
        if key in ("*", permission):
            action = value if isinstance(value, str) else value.get("*", "ask")
    return action


def _modes(config):
    return ["build", "plan", "yolo", *config.get("agent", {})]


#: Models with reasoning variants (#1407): as on 1.18.35, ACP offers an `effort` option
#: (category `thought_level`) only once such a model is selected, its values the variants + default.
ACP_VARIANTS = {"local/fake-b": ["low", "high"], "local/fake-coder": ["low", "high"]}


def _acp_config(model, mode, modes=("build", "plan", "yolo"), effort=None):
    options = [
        {
            "id": "model",
            "name": "Model",
            "category": "model",
            "type": "select",
            "currentValue": model,
            "options": [
                {"value": "local/fake-a", "name": "Fake A"},
                {"value": "local/fake-b", "name": "Fake B"},
            ],
        },
        {
            "id": "mode",
            "name": "Session Mode",
            "category": "mode",
            "type": "select",
            "currentValue": mode,
            "options": [{"value": m, "name": m} for m in dict.fromkeys(modes)],
        },
    ]
    if model in ACP_VARIANTS:
        levels = [*ACP_VARIANTS[model], "default"]
        options.insert(
            1,
            {
                "id": "effort",
                "name": "Effort",
                "category": "thought_level",
                "type": "select",
                "currentValue": effort or levels[0],
                "options": [{"value": v, "name": v} for v in levels],
            },
        )
    return options


ONCE = {"optionId": "once", "kind": "allow_once", "name": "Allow once"}
ALWAYS = {"optionId": "always", "kind": "allow_always", "name": "Always allow"}
REJECT = {"optionId": "reject", "kind": "reject_once", "name": "Reject"}


def opencode():
    """`opencode acp` (ACP 1, as measured on 1.18.35 in the #1312 spike). No listener is faked:
    the environment it was given is logged instead, so tests can check the password and the
    forced permission config reached the child (and nothing else)."""
    config = _opencode_config()
    session, model, mode = None, "local/fake-a", config.get("default_agent", "build")
    effort = None
    prompt = None  # the id of the open session/prompt request
    serial = [100]

    def update(update_kind, **fields):
        send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session,
                    "update": {"sessionUpdate": update_kind, **fields},
                },
            }
        )

    def reply(rid, result):
        send({"jsonrpc": "2.0", "id": rid, "result": result})

    def ask(tool_call, options):
        serial[0] += 1
        send(
            {
                "jsonrpc": "2.0",
                "id": serial[0],
                "method": "session/request_permission",
                "params": {"sessionId": session, "toolCall": tool_call, "options": options},
            }
        )
        return serial[0]

    def answer_to(rid):
        for frame in stream:
            if frame.get("id") == rid and "method" not in frame:
                outcome = frame["result"]["outcome"]
                return outcome.get("optionId") or outcome["outcome"]
        sys.exit(0)

    def finish(rid, text, reason="end_turn"):
        if text:
            update(
                "agent_message_chunk", messageId="prt_1", content={"type": "text", "text": text[:3]}
            )
            update(
                "agent_message_chunk", messageId="prt_1", content={"type": "text", "text": text[3:]}
            )
        reply(rid, {"stopReason": reason, "usage": {"totalTokens": 1}})

    # What the child was given (the listener password, the forced config) rides on every log
    # entry, so a test can prove it reached the child and nothing else.
    EXTRA["env"] = {
        "password": os.environ.get("OPENCODE_SERVER_PASSWORD"),
        "config": os.environ.get("OPENCODE_CONFIG_CONTENT"),
        "keys": sorted(os.environ),
    }
    stream = frames()
    for frame in stream:
        method, rid, params = frame.get("method"), frame.get("id"), frame.get("params", {})
        if method is None:
            continue  # a late answer (e.g. a cancelled permission, a refused fs request)
        if method == "initialize":
            reply(
                rid,
                {
                    "protocolVersion": 1,
                    "agentCapabilities": {
                        "loadSession": True,
                        "promptCapabilities": {"image": True, "embeddedContext": True},
                    },
                    "agentInfo": {
                        "name": "OpenCode",
                        "version": _control("version", "1.18.35"),
                    },
                },
            )
        elif method == "session/new":
            session = "ses_" + uuid.uuid4().hex[:24]
            mode = "plan" if _control("stuck") else mode
            _start_mcp(config)
            options = _acp_config(model, mode, _modes(config), effort)
            reply(rid, {"sessionId": session, "configOptions": options})
            update("available_commands_update", availableCommands=[])
        elif method == "session/load":
            session, mode = params["sessionId"], "plan"  # the console left it in another agent
            # History replays as updates, including a tool call that was pending when it was
            # written, and (hostile) a permission request no live turn asked: neither is live.
            update("user_message_chunk", messageId="msg_old", content={"type": "text", "text": "x"})
            update(
                "tool_call",
                toolCallId="call-old",
                title="rm -rf old",
                kind="execute",
                status="pending",
                rawInput={"command": "rm -rf old"},
            )
            ask({"toolCallId": "call-old", "kind": "execute", "title": "rm -rf old"}, [ONCE])
            reply(rid, {"configOptions": _acp_config(model, mode, _modes(config), effort)})
        elif method == "session/set_config_option":
            if params["configId"] == "model" and params["value"].startswith("nope"):
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "error": {"code": -32602, "message": "Invalid params: model not found"},
                    }
                )
                continue
            if params["configId"] == "model":
                model = params["value"]
                effort = None  # a model switch starts on that model's own level
            elif params["configId"] == "effort":
                if params["value"] not in [*ACP_VARIANTS.get(model, []), "default"]:
                    send(
                        {
                            "jsonrpc": "2.0",
                            "id": rid,
                            "error": {
                                "code": -32602,
                                "message": f"Invalid params: effort not found: {params['value']}",
                            },
                        }
                    )
                    continue
                if not _control("effort-ignored"):
                    effort = params["value"]
            elif params["configId"] == "mode":
                if params["value"] not in _modes(config):
                    send(
                        {
                            "jsonrpc": "2.0",
                            "id": rid,
                            "error": {"code": -32602, "message": "Invalid params: no such mode"},
                        }
                    )
                    continue
                mode = mode if _control("stuck") else params["value"]
            reply(rid, {"configOptions": _acp_config(model, mode, _modes(config), effort)})
        elif method == "session/cancel":
            wait_cancel()
            if prompt is not None:
                reply(prompt, {"stopReason": "cancelled", "usage": {"totalTokens": 0}})
                prompt = None
        elif method == "session/prompt":
            text = params["prompt"][0]["text"]
            prompt = rid
            if text == "DIE":
                sys.exit(9)
            text = memory_turn(text) or text
            if text in {"HANG", "HANG_APPROVE"}:
                if text == "HANG_APPROVE":
                    ask({"toolCallId": "call-h", "kind": "execute", "title": "sleep"}, [ONCE])
                continue
            wait_release(text)
            if text == "FAIL":
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": "boom"}})
                prompt = None
                continue
            if text.startswith("TOOL:"):
                # A tool call governed by the merged permission rules, like the real CLI's.
                permission = text.partition(":")[2]
                if _permission(config, mode, permission) == "ask":
                    tool = {
                        "toolCallId": "call-t",
                        "title": permission,
                        "kind": "other",
                        "rawInput": {"tool": permission},
                    }
                    finish(rid, "asked:" + answer_to(ask(tool, [ONCE, REJECT])))
                else:
                    finish(rid, "ran:" + permission)
                prompt = None
                continue
            if text == "COMMAND_THEN_MCP":
                command = {
                    "toolCallId": "cmd",
                    "title": "git remote -v",
                    "kind": "execute",
                    "rawInput": {"command": "git remote -v"},
                }
                first = answer_to(ask(command, [ONCE, ALWAYS, REJECT]))
                mcp = {
                    "toolCallId": "mcp",
                    "title": "forgejo_list_repo_labels",
                    "kind": "other",
                    "status": "in_progress",
                    "rawInput": {"owner": "superstatus.io", "repo": "agent-sessions"},
                }
                update("tool_call_update", **mcp)
                second = answer_to(
                    ask({**mcp, "rawInput": {}, "status": "pending"}, [ONCE, ALWAYS, REJECT])
                )
                finish(rid, f"command:{first} mcp:{second}")
                prompt = None
                continue
            if text.startswith("BLIND_"):
                # A permission request missing the action it would approve (Hermes on #1336).
                tool = {
                    "BLIND_MISSING": {"kind": "execute", "title": "bash"},
                    "BLIND_EMPTY": {"kind": "execute", "title": "bash", "rawInput": {}},
                    "BLIND_EMPTYCMD": {"kind": "execute", "rawInput": {"command": "  "}},
                    "BLIND_NODIFF": {"kind": "edit", "rawInput": {"filepath": "/w/a.md"}},
                }[text]
                tool["toolCallId"] = "call-b"
                finish(rid, "blind:" + answer_to(ask(tool, [ALWAYS, ONCE, REJECT])))
                prompt = None
                continue
            if text == "MODE_ESCAPE":
                update("current_mode_update", currentModeId="yolo")
                continue
            if text in {"APPROVE", "EDIT", "ONLY_ALWAYS", "DUP_ONCE"}:
                if text == "EDIT":
                    tool = {
                        "toolCallId": "call-e",
                        "title": "/w/README.md",
                        "kind": "edit",
                        "status": "pending",
                        "locations": [{"path": "/w/README.md"}],
                        "rawInput": {"filepath": "/w/README.md", "diff": "-old\n+new\n"},
                        "content": [
                            {"type": "diff", "path": "/w/README.md", "oldText": "old\n"},
                        ],
                    }
                    tool["content"][0]["newText"] = "new\n"
                else:
                    tool = {
                        "toolCallId": "call-1",
                        "title": "ls -la",
                        "kind": "execute",
                        "status": "pending",
                        "locations": [],
                        "rawInput": {"command": "ls -la"},
                    }
                update("tool_call", **{**tool, "rawInput": {}})
                options = {
                    "ONLY_ALWAYS": [ALWAYS, REJECT],
                    "DUP_ONCE": [ONCE, {**ONCE, "optionId": "once-2"}, REJECT],
                }.get(text, [ALWAYS, ONCE, REJECT])
                chosen = answer_to(ask(tool, options))
                if text == "EDIT" and chosen == "once":
                    serial[0] += 1  # like 1.18.35: it also asks the client to write the file
                    send(
                        {
                            "jsonrpc": "2.0",
                            "id": serial[0],
                            "method": "fs/write_text_file",
                            "params": {"sessionId": session, "path": "/w/README.md"},
                        }
                    )
                update(
                    "tool_call_update",
                    toolCallId=tool["toolCallId"],
                    status="completed" if chosen == "once" else "failed",
                    content=[{"type": "content", "content": {"type": "text", "text": "done"}}],
                )
                finish(rid, "approved:" + chosen)
                prompt = None
                continue
            finish(rid, "echo:" + text)
            prompt = None


#: copilot's session modes are URIs (#1565); BattleLab pins the agent one, and the fake can be
#: made to escape to the autopilot one mid-turn (`MODE_ESCAPE`).
COPILOT_MODE_AGENT = "https://agentclientprotocol.com/protocol/session-modes#agent"
COPILOT_MODE_AUTOPILOT = "https://agentclientprotocol.com/protocol/session-modes#autopilot"
COPILOT_LEVELS = ("none", "low", "medium", "high")


def _copilot_config(model, effort, allow_all="off", mode=COPILOT_MODE_AGENT):
    """copilot's configOptions as 1.0.68 reports them: `mode` (mode URIs) and `allow_all` always;
    `reasoning_effort` once a model is set; a `model` option only with the `models` switch — the
    real build enumerates none and accepts a set without echoing it."""
    options = [
        {
            "id": "mode",
            "name": "Mode",
            "type": "select",
            "currentValue": mode,
            "options": [
                {"value": m, "name": m.rsplit("#", 1)[-1]}
                for m in (COPILOT_MODE_AGENT, COPILOT_MODE_AUTOPILOT)
            ],
        },
        {
            "id": "allow_all",
            "name": "Allow all",
            "type": "select",
            "currentValue": allow_all,
            "options": [{"value": "off", "name": "Off"}, {"value": "on", "name": "On"}],
        },
    ]
    if _control_copilot("models"):
        options.insert(
            0,
            {
                "id": "model",
                "name": "Model",
                "type": "select",
                "currentValue": model or "fake-copilot-a",
                "options": [
                    {"value": "fake-copilot-a", "name": "Fake Copilot A"},
                    {"value": "fake-copilot-b", "name": "Fake Copilot B"},
                ],
            },
        )
    if model is not None or _control_copilot("models"):
        options.append(
            {
                "id": "reasoning_effort",
                "name": "Reasoning effort",
                "type": "select",
                "currentValue": effort or "medium",
                "options": [{"value": v, "name": v} for v in COPILOT_LEVELS],
            }
        )
    return options


def copilot():
    """`copilot --acp --no-auto-update` (ACP 1, as measured on 1.0.68/1.0.70, #1565). Session ids
    are plain v4 UUIDs; `session/new` also mints the session's store directory the way the real
    CLI does, so the model-list probe's cleanup can be proven against it."""
    session, model, effort, mode, allow_all = None, None, None, COPILOT_MODE_AGENT, "off"
    prompt = None  # the id of the open session/prompt request
    serial = [100]

    def update(update_kind, **fields):
        send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session,
                    "update": {"sessionUpdate": update_kind, **fields},
                },
            }
        )

    def reply(rid, result):
        send({"jsonrpc": "2.0", "id": rid, "result": result})

    def ask(tool_call, options):
        serial[0] += 1
        send(
            {
                "jsonrpc": "2.0",
                "id": serial[0],
                "method": "session/request_permission",
                "params": {"sessionId": session, "toolCall": tool_call, "options": options},
            }
        )
        return serial[0]

    def answer_to(rid):
        for frame in stream:
            if frame.get("id") == rid and "method" not in frame:
                outcome = frame["result"]["outcome"]
                return outcome.get("optionId") or outcome["outcome"]
        sys.exit(0)

    def finish(rid, text, reason="end_turn"):
        if text:
            update(
                "agent_message_chunk", messageId="prt_1", content={"type": "text", "text": text[:3]}
            )
            update(
                "agent_message_chunk", messageId="prt_1", content={"type": "text", "text": text[3:]}
            )
        reply(rid, {"stopReason": reason, "usage": {"totalTokens": 1}})

    # What the child was given (auto-update off, the sanitized keys) rides on every log entry.
    EXTRA["env"] = {
        "auto_update": os.environ.get("COPILOT_AUTO_UPDATE"),
        "keys": sorted(os.environ),
    }
    stream = frames()
    for frame in stream:
        method, rid, params = frame.get("method"), frame.get("id"), frame.get("params", {})
        if method is None:
            continue  # a late answer (e.g. a cancelled permission, a refused fs request)
        if method == "initialize":
            reply(
                rid,
                {
                    "protocolVersion": 1,
                    "agentCapabilities": {
                        "loadSession": True,
                        "mcpCapabilities": {"http": True, "sse": True},
                        "promptCapabilities": {"image": True},
                    },
                    "agentInfo": {
                        "name": "Copilot",
                        "version": _control_copilot("version", "1.0.68"),
                    },
                    "authMethods": [{"id": "copilot-login"}],
                },
            )
        elif method == "session/new":
            if params.get("mcpServers"):
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "error": {
                            "code": -32602,
                            "message": "Only HTTP and SSE MCP servers are supported",
                        },
                    }
                )
                continue
            session = str(uuid.uuid4())
            mode = COPILOT_MODE_AUTOPILOT if _control_copilot("stuck") else mode
            # The CLI records the session it minted, even for a bare probe (#1565).
            store = os.path.join(os.environ["HOME"], ".copilot", "session-state", session)
            os.makedirs(store, exist_ok=True)
            with open(os.path.join(store, "events.jsonl"), "a"):
                pass
            reply(
                rid,
                {
                    "sessionId": session,
                    "modes": {"currentModeId": mode},
                    "configOptions": _copilot_config(model, effort, allow_all, mode),
                },
            )
            update("available_commands_update", availableCommands=[])
        elif method == "session/load":
            session, mode = params["sessionId"], COPILOT_MODE_AUTOPILOT  # left in another mode
            # History replays BEFORE the reply, including (hostile) a permission request no live
            # turn asked: it must be answered cancelled and never presented.
            update("user_message_chunk", messageId="msg_old", content={"type": "text", "text": "x"})
            update(
                "tool_call",
                toolCallId="call-old",
                title="rm -rf old",
                kind="execute",
                status="pending",
                rawInput={"command": "rm -rf old"},
            )
            ask({"toolCallId": "call-old", "kind": "execute", "title": "rm -rf old"}, [ONCE])
            reply(rid, {})  # the load result is an EMPTY object on 1.0.68
        elif method == "session/set_config_option":
            config_id, value = params["configId"], params["value"]
            if config_id == "mode":
                if value not in (COPILOT_MODE_AGENT, COPILOT_MODE_AUTOPILOT):
                    send(
                        {
                            "jsonrpc": "2.0",
                            "id": rid,
                            "error": {"code": -32602, "message": "Invalid params: no such mode"},
                        }
                    )
                    continue
                mode = mode if _control_copilot("stuck") else value
            elif config_id == "allow_all":
                if value not in ("on", "off"):
                    send(
                        {
                            "jsonrpc": "2.0",
                            "id": rid,
                            "error": {"code": -32602, "message": "Invalid params: bad allow_all"},
                        }
                    )
                    continue
                allow_all = value
            elif config_id == "model":
                # 1.0.68: silently accepted, never echoed — the reply carries no model option.
                model = value
                effort = None  # a model switch starts on that model's own level
            elif config_id == "reasoning_effort":
                if model is None or value not in COPILOT_LEVELS:
                    send(
                        {
                            "jsonrpc": "2.0",
                            "id": rid,
                            "error": {
                                "code": -32602,
                                "message": f"Invalid params: effort not found: {value}",
                            },
                        }
                    )
                    continue
                if not _control_copilot("effort-ignored"):
                    effort = value
            else:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "error": {"code": -32602, "message": "Invalid params: no such option"},
                    }
                )
                continue
            reply(rid, {"configOptions": _copilot_config(model, effort, allow_all, mode)})
        elif method == "session/cancel":
            wait_cancel()
            if prompt is not None:
                reply(prompt, {"stopReason": "cancelled", "usage": {"totalTokens": 0}})
                prompt = None
        elif method == "session/prompt":
            text = params["prompt"][0]["text"]
            prompt = rid
            if text == "DIE":
                sys.exit(9)
            text = memory_turn(text) or text
            if text in {"HANG", "HANG_APPROVE"}:
                if text == "HANG_APPROVE":
                    ask(
                        {
                            "toolCallId": "call-h",
                            "kind": "execute",
                            "title": "sleep",
                            "rawInput": {"command": "sleep 60"},
                        },
                        [ONCE, REJECT],
                    )
                continue
            wait_release(text)
            if text == "FAIL":
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": "boom"}})
                prompt = None
                continue
            if text == "MODE_ESCAPE":
                update("current_mode_update", currentModeId=COPILOT_MODE_AUTOPILOT)
                continue
            if text in {"APPROVE", "EDIT", "ONLY_ALWAYS", "DUP_ONCE"}:
                if text == "EDIT":
                    # An edit is DECLINE-ONLY for copilot (#1565): asked, never approvable.
                    tool = {
                        "toolCallId": "call-e",
                        "title": "/w/README.md",
                        "kind": "edit",
                        "status": "pending",
                        "locations": [{"path": "/w/README.md"}],
                        "rawInput": {"path": "/w/README.md", "newText": "new\n"},
                    }
                else:
                    tool = {
                        "toolCallId": "call-1",
                        "title": "ls -la",
                        "kind": "execute",
                        "status": "pending",
                        "locations": [],
                        "rawInput": {"command": "ls -la"},
                    }
                options = {
                    "ONLY_ALWAYS": [ALWAYS, REJECT],
                    "DUP_ONCE": [ONCE, {**ONCE, "optionId": "once-2"}, REJECT],
                }.get(text, [ALWAYS, ONCE, REJECT])
                chosen = answer_to(ask(tool, options))
                update(
                    "tool_call_update",
                    toolCallId=tool["toolCallId"],
                    status="completed" if chosen == "once" else "failed",
                    content=[{"type": "content", "content": {"type": "text", "text": "done"}}],
                )
                finish(rid, "approved:" + chosen)
                prompt = None
                continue
            finish(rid, "echo:" + text)
            prompt = None


if __name__ == "__main__":
    name = os.path.basename(sys.argv[0])
    if "--version" in sys.argv:
        print(
            "codex-cli 0.160.0"
            if name.startswith("codex")
            else _control("version", "1.18.35")
            if name.startswith("opencode")
            else _control_copilot("version", "1.0.68")
            if name.startswith("copilot")
            else "2.1.292 (Claude Code)"
        )
    elif "app-server" in sys.argv:
        report_oom_adj()
        codex()
    elif sys.argv[1:] == ["debug", "config"]:
        _debug_config()
    elif sys.argv[1:2] == ["acp"]:
        report_oom_adj()
        opencode()
    elif sys.argv[1:2] == ["--acp"]:
        report_oom_adj()
        copilot()
    elif sys.argv[1:] == ["models", "--verbose"]:
        # opencode's model list (#1312/#1313/#1407), as 1.18.35 prints it: an id line, then that
        # model's JSON; `variants` keys are its reasoning levels. Junk lines and a malformed
        # block cost only themselves.
        log({"models": True, "password": bool(os.environ.get("OPENCODE_SERVER_PASSWORD"))})
        coder = {"id": "fake-coder", "name": "Fake Coder", "variants": {"low": {}, "high": {}}}
        gpt = {"id": "fake-gpt", "name": "Fake GPT"}
        print("local/fake-coder\n" + json.dumps(coder, indent=2))
        print("openai/fake-gpt\n" + json.dumps(gpt, indent=2))
        print('-not-a-model\nlocal/fake-broken\n{\n  "variants": {oops\n}')
    else:
        report_oom_adj()
        claude()
