"""The shelf tool reaches a mission's session where a new agent process is born (#1740 P1a).

An interactive LAUNCH of a session a mission holds gets `battlelab_shelf`; a launch of a session no
mission holds gets nothing; an ATTACH asks nothing at all (a reconnect reaches a process whose
environment is already fixed). The dispatch half is pinned in `test_headless_dispatch.py`.
"""

from __future__ import annotations

import json
import uuid

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_sessions import missions, secrets_broker, shelf_broker
from agent_sessions.main import create_app

HELD = "claude:11111111-1111-1111-1111-111111111111"


def test_the_terminal_route_hands_the_tool_to_a_missions_session_only(
    auth_cfg, fake_jsonl, monkeypatch, engine_bin, tmp_path
):
    from agent_sessions import engines, owner, ptybridge, relaunch, scopedspawn, sessions, webterm

    runtime = tmp_path / "shelf-run"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv(secrets_broker.RUNTIME_ENV, str(runtime))
    monkeypatch.setattr(secrets_broker, "_ram_check", lambda path: True)
    monkeypatch.setattr(shelf_broker, "_server_running", True)
    monkeypatch.setattr(
        engines, "launchable_bin", lambda p: f"/usr/bin/{p.engine_id}" if p else None
    )

    async def fake_run(ws, argv, **kwargs):
        await ws.close(code=1000)

    action = {"now": sessions.LAUNCH}
    monkeypatch.setattr(webterm, "run", fake_run)
    monkeypatch.setattr(owner, "takeover_enabled", lambda: False)
    monkeypatch.setattr(sessions, "open_action", lambda e, n: (action["now"], None))
    monkeypatch.setattr(relaunch, "blocked", lambda key: False)
    monkeypatch.setattr(relaunch, "note_exit", lambda *a, **k: None)
    monkeypatch.setattr(ptybridge, "session_exists", lambda e, n: False)
    argvs = []
    monkeypatch.setattr(
        ptybridge, "launch_argv", lambda **kw: argvs.append(kw["launch_argv"]) or ["/bin/true"]
    )
    monkeypatch.setattr(ptybridge, "attach_argv", lambda **kw: ["/bin/true"])
    monkeypatch.setattr(scopedspawn, "wrap", lambda argv, **kw: (argv, None))
    engine_bin("claude")
    asked = []
    real = shelf_broker.launch_env
    monkeypatch.setattr(
        shelf_broker, "launch_env", lambda *a, **k: asked.append(a) or real(*a, **k)
    )
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    cookie = c.cookies.get("agent_sessions")
    headers = {"Origin": auth_cfg.origin, "Cookie": f"agent_sessions={cookie}"}
    rows = c.get("/api/sessions?limit=50").json()["sessions"]
    cwd = next(r["cwd"] for r in rows if r["id"] == HELD)
    mid = missions.create_mission("x", cwd=cwd)["id"]
    missions.adopt(mid, HELD)

    def connect(url):
        try:
            with c.websocket_connect(url, headers=headers) as ws:
                while (msg := ws.receive()).get("type") != "websocket.close":
                    pass
                return msg.get("code")
        except WebSocketDisconnect as e:
            return e.code

    assert connect(f"/ws/term/{HELD}") == 1000  # a relaunch of the mission's session
    assert connect(f"/ws/term/claude:{uuid.uuid4()}?new=1&cwd={cwd}") == 1000  # nobody's
    action["now"] = sessions.ATTACH
    assert connect(f"/ws/term/{HELD}") == 1000
    assert len(asked) == 2 and len(argvs) == 2  # the attach asked nothing and built no argv
    held, free = argvs
    server = json.loads(held[-1])["mcpServers"]["battlelab_shelf"]
    assert set(server["env"]) >= shelf_broker.ENV_KEYS
    assert server["args"] == ["-I", "-m", "agent_sessions.shelf_mcp"]
    assert "battlelab_shelf" not in " ".join(free)
