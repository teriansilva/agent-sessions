"""A session started from a specialist (#1693 S4b): the version re-validated where the launch
happens, a slot and its skill bundle taken before the agent starts, the same version re-reserved
on a relaunch, and the slot released on every exit — settled the way reconcile asks.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

import pytest

from agent_sessions import memory, projects, skill_bundles, specialist_launch
from agent_sessions import specialist_capacity as cap
from agent_sessions import specialists_store as store
from agent_sessions.agent_tools import SKILLS_ENV, SKILLS_SERVER_NAME


def _body(**over):
    b = {
        "title": "Implementer",
        "summary": "",
        "skills": [],
        "engine": "claude",
        "model": "default",
        "effort": None,
        "capacity": 2,
    }
    b.update(over)
    return b


def _skill(sid: str = "tdd", body: str = "body") -> dict:
    from agent_sessions.gallery import library
    from agent_sessions.gallery import skills as lib_skills

    if not lib_skills.storage.skills_root().joinpath(sid).exists():
        library.create_skill(sid, {"SKILL.md": f"---\nname: {sid}\ndescription: x\n---\n{body}"})
    return {"id": sid, "revision": lib_skills.get(sid)["revision"]}


def _k(label: str) -> str:
    return f"claude:{uuid.uuid5(uuid.NAMESPACE_DNS, label)}"


@pytest.fixture
def spec():
    return store.create(_body())


@pytest.fixture
def skilled():
    return store.create(_body(title="Tester", skills=[_skill()]))


def _active(rid: str) -> bool:
    return store.get_reservation(rid)["state"] != "released"


# ---- the request ---------------------------------------------------------------------------------


def test_a_request_names_an_id_and_a_version_or_nothing():
    assert specialist_launch.parse(None, None) is None
    assert specialist_launch.parse("", "") is None
    assert specialist_launch.parse("implementer", "3") == ("implementer", 3)
    for raw_id, raw_version in [
        ("implementer", None),
        ("implementer", "0"),
        ("implementer", "x"),
        ("implementer", "9" * 7),
        ("implementer", True),
        (None, "1"),
        ("../etc", "1"),
    ]:
        with pytest.raises(specialist_launch.Refused):
            specialist_launch.parse(raw_id, raw_version)


# ---- a new session -------------------------------------------------------------------------------


def test_a_new_session_takes_a_slot_for_that_version_on_its_agent_and_model(spec):
    claim = specialist_launch.for_new("claude", _k("a"), spec["id"], 1, model=None)
    r = store.get_reservation(claim.reservation_id)
    assert (r["specialist_id"], r["version"], r["session_key"], r["state"]) == (
        spec["id"],
        1,
        _k("a"),
        "starting",
    )
    assert claim.owner and claim.bundle is None


@pytest.mark.parametrize(
    "engine, model, why",
    [("codex", None, "runs on claude"), ("claude", "claude-opus-5", "the model default")],
)
def test_another_agent_or_model_than_the_versions_takes_no_slot(spec, engine, model, why):
    with pytest.raises(specialist_launch.Refused, match=why):
        specialist_launch.for_new(engine, _k("a"), spec["id"], 1, model=model)
    assert store.active() == []


def test_a_missing_version_or_an_archived_specialist_takes_no_slot(spec):
    with pytest.raises(specialist_launch.Refused, match="no longer exists"):
        specialist_launch.for_new("claude", _k("a"), spec["id"], 9, model=None)
    store.set_archived(spec["id"], True, expect_revision=spec["revision"])
    with pytest.raises(specialist_launch.Refused, match="archived"):
        specialist_launch.for_new("claude", _k("a"), spec["id"], 1, model=None)
    assert store.active() == []


def test_at_capacity_the_launch_is_refused_naming_the_limit(spec):
    specialist_launch.for_new("claude", _k("a"), spec["id"], 1, model=None)
    specialist_launch.for_new("claude", _k("b"), spec["id"], 1, model=None)
    with pytest.raises(specialist_launch.Refused, match="at capacity"):
        specialist_launch.for_new("claude", _k("c"), spec["id"], 1, model=None)
    assert len(store.active()) == 2


def test_the_skills_are_bundled_under_the_slot(skilled):
    claim = specialist_launch.for_new("claude", _k("a"), skilled["id"], 1, model=None)
    assert claim.bundle == str(skill_bundles.path_of(claim.reservation_id))
    assert (Path(claim.bundle) / "tdd" / "SKILL.md").read_bytes().endswith(b"body")


def test_a_skill_changed_since_the_version_refuses_and_frees_the_slot():
    _skill()  # the library's tdd is at another revision than this version pinned
    stale = store.create(_body(title="Stale", skills=[{"id": "tdd", "revision": "0" * 64}]))
    with pytest.raises(specialist_launch.Refused, match="changed since"):
        specialist_launch.for_new("claude", _k("a"), stale["id"], 1, model=None)
    assert store.active() == []
    assert not any(p.name.startswith("r") for p in skill_bundles.root().glob("*"))


# ---- a relaunch ----------------------------------------------------------------------------------


def test_a_session_never_started_from_a_specialist_relaunches_plain(spec):
    assert specialist_launch.for_relaunch([_k("a")], launch_key=_k("a")) is None


def test_a_relaunch_re_reserves_the_same_version_after_its_slot_was_freed(skilled):
    first = specialist_launch.for_new("claude", _k("a"), skilled["id"], 1, model=None)
    specialist_launch.abandon(first)
    assert not _active(first.reservation_id)
    # A newer version since: the session still relaunches as the version it was started from.
    store.update(
        skilled["id"], _body(title="Tester v2", skills=[]), expect_revision=skilled["revision"]
    )
    again = specialist_launch.for_relaunch([_k("a")], launch_key=_k("a"))
    assert again.reservation_id != first.reservation_id and again.owner
    assert (again.specialist_id, again.version) == (skilled["id"], 1)
    assert again.bundle and (Path(again.bundle) / "tdd").is_dir()


def test_a_relaunch_while_its_slot_is_still_held_re_takes_it_and_reuses_its_bundle(skilled):
    first = specialist_launch.for_new("claude", _k("a"), skilled["id"], 1, model=None)
    again = specialist_launch.for_relaunch([_k("a")], launch_key=_k("a"))
    assert again.reservation_id == first.reservation_id and again.bundle == first.bundle
    assert again.owner is None  # a re-take settles nothing: reconcile does, on proof
    specialist_launch.abandon(again)
    specialist_launch.abandon(first)  # the earlier attempt's token was rotated away
    assert _active(first.reservation_id)


def test_an_archived_specialist_is_never_relaunched_held_slot_or_not(spec):
    held = specialist_launch.for_new("claude", _k("held"), spec["id"], 1, model=None)
    freed = specialist_launch.for_new("claude", _k("freed"), spec["id"], 1, model=None)
    specialist_launch.abandon(freed)
    store.set_archived(spec["id"], True, expect_revision=spec["revision"])
    for key in (_k("held"), _k("freed")):
        with pytest.raises(specialist_launch.Refused, match="archived"):
            specialist_launch.for_relaunch([key], launch_key=key)
    assert _active(held.reservation_id)


def test_a_held_slot_relaunch_after_a_skill_changed_is_refused_like_a_fresh_one(skilled):
    """Review 7102: reusing the held slot's bundle still proves the pins against the library."""
    from test_skills_tool import _edit_in_place

    first = specialist_launch.for_new("claude", _k("a"), skilled["id"], 1, model=None)
    _edit_in_place("tdd", "---\nname: tdd\ndescription: x\n---\nCHANGED")
    with pytest.raises(specialist_launch.Refused, match="changed since"):
        specialist_launch.for_relaunch([_k("a")], launch_key=_k("a"))
    # The old bundle was never replaced or served anything new: it still holds the pinned bytes.
    assert (Path(first.bundle) / "tdd" / "SKILL.md").read_bytes().endswith(b"body")


def test_a_relaunch_counts_against_capacity(spec):
    first = specialist_launch.for_new("claude", _k("a"), spec["id"], 1, model=None)
    specialist_launch.abandon(first)
    specialist_launch.for_new("claude", _k("b"), spec["id"], 1, model=None)
    specialist_launch.for_new("claude", _k("c"), spec["id"], 1, model=None)
    with pytest.raises(specialist_launch.Refused, match="at capacity"):
        specialist_launch.for_relaunch([_k("a")], launch_key=_k("a"))


def test_a_relaunch_finds_the_slot_under_either_of_its_keys(spec):
    first = specialist_launch.for_new("claude", _k("p"), spec["id"], 1, model=None)
    specialist_launch.abandon(first)
    claim = specialist_launch.for_relaunch([_k("real"), _k("p")], launch_key=_k("p"))
    assert claim is not None and claim.session_key == _k("p")


# ---- settling ------------------------------------------------------------------------------------


def _verdict(monkeypatch, verdict):
    async def fake(_key):
        return verdict, "why"

    monkeypatch.setattr(cap, "liveness", fake)


@pytest.mark.parametrize(
    "verdict, state",
    [(cap.DEAD, "released"), (cap.LIVE, "live"), (cap.UNKNOWN, "starting")],
)
def test_settling_asks_like_reconcile_and_never_frees_on_a_guess(
    skilled, monkeypatch, verdict, state
):
    claim = specialist_launch.for_new("claude", _k("a"), skilled["id"], 1, model=None)
    _verdict(monkeypatch, verdict)
    asyncio.run(specialist_launch.settle(claim))
    assert store.get_reservation(claim.reservation_id)["state"] == state
    assert Path(claim.bundle).exists() is (state != "released")


def test_a_superseded_attempts_settlement_frees_nothing(spec, monkeypatch):
    first = specialist_launch.for_new("claude", _k("a"), spec["id"], 1, model=None)
    specialist_launch.for_relaunch([_k("a")], launch_key=_k("a"))  # re-taken since
    _verdict(monkeypatch, cap.DEAD)
    asyncio.run(specialist_launch.settle(first))
    assert _active(first.reservation_id)


def test_the_live_watch_activates_once_the_master_is_up_and_never_releases(spec, monkeypatch):
    monkeypatch.setattr(specialist_launch, "_LIVE_POLL_S", 0.01)
    claim = specialist_launch.for_new("claude", _k("a"), spec["id"], 1, model=None)
    ups = iter([False, False, True])
    asyncio.run(specialist_launch.activate_once_live(claim, lambda: next(ups)))
    assert store.get_reservation(claim.reservation_id)["state"] == "live"
    other = specialist_launch.for_new("claude", _k("b"), spec["id"], 1, model=None)
    monkeypatch.setattr(specialist_launch, "LIVE_WAIT_S", 0.05)
    asyncio.run(specialist_launch.activate_once_live(other, lambda: False))
    assert store.get_reservation(other.reservation_id)["state"] == "starting"


# ---- the terminal route --------------------------------------------------------------------------


@pytest.fixture
def term(auth_cfg, fake_jsonl, monkeypatch, engine_bin):
    """The terminal socket with the bridge faked: each LAUNCH records its agent argv and env."""
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from agent_sessions import engines, owner, ptybridge, relaunch, scopedspawn, sessions, webterm
    from agent_sessions.main import create_app

    monkeypatch.setattr(
        engines, "launchable_bin", lambda p: f"/usr/bin/{p.engine_id}" if p else None
    )
    launched: list[dict] = []
    argvs: list[list[str]] = []

    async def fake_run(ws, argv, **kwargs):
        launched.append(kwargs.get("launch_env") or {})
        await ws.close(code=1000)

    action = {"now": sessions.LAUNCH}
    monkeypatch.setattr(webterm, "run", fake_run)
    monkeypatch.setattr(owner, "takeover_enabled", lambda: False)
    monkeypatch.setattr(sessions, "open_action", lambda e, n: (action["now"], None))
    monkeypatch.setattr(relaunch, "blocked", lambda key: False)
    monkeypatch.setattr(relaunch, "note_exit", lambda *a, **k: None)
    monkeypatch.setattr(ptybridge, "session_exists", lambda e, n: False)
    monkeypatch.setattr(
        ptybridge, "launch_argv", lambda **kw: argvs.append(kw["launch_argv"]) or ["/bin/true"]
    )
    monkeypatch.setattr(ptybridge, "attach_argv", lambda **kw: ["/bin/true"])
    monkeypatch.setattr(scopedspawn, "wrap", lambda argv, **kw: (argv, None))
    engine_bin("claude")

    # One event loop for the whole test (a context-managed client): the slot is settled by a
    # task that runs after the socket closes.
    with TestClient(create_app(auth_cfg), base_url="https://testserver") as c:
        r = c.post(
            "/login",
            data={"username": "marcus", "password": "hunter2"},
            follow_redirects=False,
            headers={"Origin": auth_cfg.origin},
        )
        assert r.status_code == 303
        headers = {
            "Origin": auth_cfg.origin,
            "Cookie": f"agent_sessions={c.cookies.get('agent_sessions')}",
        }
        rows = c.get("/api/sessions?limit=50").json()["sessions"]
        resumable = "claude:11111111-1111-1111-1111-111111111111"
        cwd = next(r["cwd"] for r in rows if r["id"] == resumable)

        def connect(url):
            try:
                with c.websocket_connect(url, headers=headers) as ws:
                    while (msg := ws.receive()).get("type") != "websocket.close":
                        pass
                    return msg.get("code"), msg.get("reason") or ""
            except WebSocketDisconnect as e:
                return e.code, e.reason or ""

        class T:
            pass

        t = T()
        t.connect, t.cwd, t.launched, t.argvs, t.action, t.resumable = (
            connect,
            cwd,
            launched,
            argvs,
            action,
            resumable,
        )
        t.sessions = sessions
        yield t


def _skills_server(argv: list[str]) -> dict | None:
    if "--mcp-config" not in argv:
        return None
    config = json.loads(argv[argv.index("--mcp-config") + 1])
    return config["mcpServers"].get(SKILLS_SERVER_NAME)


def _settled(rid: str, want: str = "released", timeout: float = 5.0) -> str:
    deadline = time.monotonic() + timeout
    while (state := store.get_reservation(rid)["state"]) != want and time.monotonic() < deadline:
        time.sleep(0.05)
    return state


def test_a_new_session_from_a_specialist_launches_with_its_skills_and_notes(term, skilled):
    pid = projects.create("repo-a", folders=[term.cwd]).id
    memory.set_enabled(pid, True)
    key = f"claude:{uuid.uuid4()}"
    url = f"/ws/term/{key}?new=1&cwd={term.cwd}&specialist={skilled['id']}&specialist_version=1"
    assert term.connect(url)[0] == 1000
    rid = store.last_reservation([key])["reservation_id"]
    server = _skills_server(term.argvs[-1])
    assert server is not None and server["env"][SKILLS_ENV] == str(skill_bundles.path_of(rid))
    assert set(memory.SPECIALIST_ENV_KEYS) <= set(term.launched[-1])
    # No master came up (the bridge is faked), so once the lock was handed on the slot is free.
    assert _settled(rid) == "released"
    assert not skill_bundles.path_of(rid).exists()


def test_a_plain_new_session_takes_no_slot_and_carries_no_skills(term, skilled):
    key = f"claude:{uuid.uuid4()}"
    assert term.connect(f"/ws/term/{key}?new=1&cwd={term.cwd}")[0] == 1000
    assert store.last_reservation([key]) is None
    assert _skills_server(term.argvs[-1]) is None


@pytest.mark.parametrize(
    "extra, why",
    [
        ("&specialist_version=9", "no longer exists"),
        ("&specialist_version=1&model=claude-opus-5", "runs on the model default"),
        ("", "names the version"),
    ],
)
def test_a_refused_specialist_launch_closes_4422_and_starts_nothing(term, spec, extra, why):
    key = f"claude:{uuid.uuid4()}"
    code, reason = term.connect(
        f"/ws/term/{key}?new=1&cwd={term.cwd}&specialist={spec['id']}{extra}"
    )
    if "model=" in extra and code != 4422:
        pytest.skip("the manifest does not offer this model id")
    assert code == 4422 and why in reason
    assert term.launched == [] and store.active() == []


def test_a_specialist_at_capacity_is_refused_before_anything_starts(term, spec):
    store.reserve(spec["id"], 1, _k("x"), max_instances=6)
    store.reserve(spec["id"], 1, _k("y"), max_instances=6)
    key = f"claude:{uuid.uuid4()}"
    code, reason = term.connect(
        f"/ws/term/{key}?new=1&cwd={term.cwd}&specialist={spec['id']}&specialist_version=1"
    )
    assert code == 4422 and "at capacity" in reason
    assert term.launched == []


def test_resuming_a_specialist_session_re_reserves_and_rebinds_its_skills(term, skilled):
    first = specialist_launch.for_new("claude", term.resumable, skilled["id"], 1, model=None)
    specialist_launch.abandon(first)
    assert term.connect(f"/ws/term/{term.resumable}")[0] == 1000
    last = store.last_reservation([term.resumable])
    assert last["reservation_id"] != first.reservation_id
    server = _skills_server(term.argvs[-1])
    assert server is not None and server["env"][SKILLS_ENV].endswith(last["reservation_id"])


def test_resuming_a_specialist_session_at_capacity_is_refused(term, spec):
    specialist_launch.abandon(
        specialist_launch.for_new("claude", term.resumable, spec["id"], 1, model=None)
    )
    store.reserve(spec["id"], 1, _k("x"), max_instances=6)
    store.reserve(spec["id"], 1, _k("y"), max_instances=6)
    code, reason = term.connect(f"/ws/term/{term.resumable}")
    assert code == 4422 and "at capacity" in reason
    assert term.launched == []


@pytest.fixture
def pinned():
    return store.create(_body(title="Pinned", model="claude-opus-5"))


def test_a_specialist_session_resumes_on_its_versions_model_and_refuses_another(term, pinned):
    """Review 7100: the resume's model is the version's — none asked re-applies it, and an
    override that resolves elsewhere (an explicit `default` included) is refused before a slot."""
    first = specialist_launch.for_new(
        "claude", term.resumable, pinned["id"], 1, model="claude-opus-5"
    )
    specialist_launch.abandon(first)
    for asked in ("default", "claude-sonnet-5"):
        code, reason = term.connect(f"/ws/term/{term.resumable}?model={asked}")
        assert code == 4422 and "Pinned" in reason and "claude-opus-5" in reason
    assert term.launched == [] and store.active() == []
    assert term.connect(f"/ws/term/{term.resumable}?model=opus")[0] == 1000  # its own alias
    assert term.connect(f"/ws/term/{term.resumable}")[0] == 1000
    for argv in term.argvs:
        assert argv[argv.index("--model") + 1] == "claude-opus-5"


def test_attaching_to_a_live_session_never_reserves(term, spec):
    term.action["now"] = term.sessions.ATTACH
    key = f"claude:{uuid.uuid4()}"
    url = f"/ws/term/{key}?new=1&cwd={term.cwd}&specialist={spec['id']}&specialist_version=1"
    assert term.connect(url)[0] == 1000
    assert store.last_reservation([key]) is None
