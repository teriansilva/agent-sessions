"""A scripted stand-in for `codex app-server` and `claude --print` stream-JSON (#1278 tests).

Behaviour is selected by the turn text, so a test drives it purely through the public API:

* ``APPROVE`` — ask one exact permission request, finish with ``approved:<answer>``.
* ``HANG``    — start the turn and never finish it (for interrupt / worker death).
* ``DIE``     — exit right after accepting the turn (native crash mid-turn).
* anything else — stream a reply ``echo:<text>`` and complete.

Every frame received is appended to ``$PWD/native-frames.jsonl`` so tests can prove a frame
was written exactly once. ``--version`` prints a version above the protocol floors.
"""

import json
import os
import sys
import time
import uuid

LOG = os.path.join(os.getcwd(), "native-frames.jsonl")


def log(frame):
    with open(LOG, "a") as fh:
        fh.write(json.dumps({"pid": os.getpid(), "argv": sys.argv[1:], "frame": frame}) + "\n")


def send(frame):
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


def frames():
    for line in sys.stdin:
        if line.strip():
            frame = json.loads(line)
            log(frame)
            yield frame


def codex():
    thread = None
    turn = None
    stream = frames()
    for frame in stream:
        method, rid = frame.get("method"), frame.get("id")
        params = frame.get("params", {})
        if method == "initialize":
            send({"id": rid, "result": {"userAgent": "fake/0.160.0"}})
        elif method == "thread/start":
            thread = str(uuid.uuid4())
            send(
                {
                    "id": rid,
                    "result": {"thread": {"id": thread, "turns": []}, "model": "fake-model"},
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
                    },
                }
            )
        elif method == "turn/start":
            turn = str(uuid.uuid4())
            text = params["input"][0]["text"]
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
            if text == "HANG":
                continue
            if text == "PARTIAL":
                done = {"threadId": thread, "turn": {"id": turn, "status": "completed"}}
                sys.stdout.write(json.dumps({"method": "turn/completed", "params": done}))
                sys.stdout.flush()  # no trailing newline: the frame was never finished
                sys.exit(9)
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
        a.split("=", 1)[1] for a in sys.argv if a.startswith(("--session-id=", "--resume="))
    )
    stream = frames()
    for frame in stream:
        kind = frame.get("type")
        if kind == "control_request" and frame["request"]["subtype"] == "initialize":
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
        elif kind == "control_request" and frame["request"]["subtype"] == "interrupt":
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
            send(
                {
                    "type": "result",
                    "subtype": "success",
                    "uuid": str(uuid.uuid4()),
                    "is_error": False,
                    "terminal_reason": "aborted_streaming",
                    "result": "",
                    "session_id": session,
                }
            )
        elif kind == "user":
            text = frame["message"]["content"]
            # Like the real CLI: the history file appears with the first turn, not before.
            slug = os.getcwd().replace("/", "-")
            history = os.path.join(os.environ["HOME"], ".claude", "projects", slug)
            os.makedirs(history, exist_ok=True)
            with open(os.path.join(history, session + ".jsonl"), "a") as fh:
                fh.write(json.dumps({"type": "user", "message": text}) + "\n")
            send(
                {"type": "system", "subtype": "init", "model": "fake-claude", "session_id": session}
            )
            if text == "DIE":
                sys.exit(9)
            if text == "HANG":
                continue
            answer = "echo:" + text
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
            send(
                {
                    "type": "result",
                    "subtype": "success",
                    "uuid": str(uuid.uuid4()),
                    "is_error": False,
                    "result": answer,
                    "session_id": session,
                }
            )


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(
            "codex-cli 0.160.0"
            if os.path.basename(sys.argv[0]).startswith("codex")
            else "2.1.292 (Claude Code)"
        )
    elif "app-server" in sys.argv:
        codex()
    else:
        claude()
