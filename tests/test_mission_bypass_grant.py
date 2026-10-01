"""Mission launches honour the operator's permission-bypass grant (#1215).

`agent_defaults.bypass` is the grant. A mission dispatch or spawn (both run through
`mission_dispatch.run`) reads it at launch time, re-checks it inside the launch fence, and records
the posture it launched with on the settlement's own timeline event. With the grant off, today's
no-bypass launch is unchanged.

The engine side — opencode with bypass gets no ask-only `OPENCODE_CONFIG_CONTENT` override — is
pinned in `test_opencode_unattended.py`. Fakes stand at the launcher boundary
(`headless_dispatch.dispatch`), as in `test_mission_start_again.py`. Nothing here spawns a process.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
import time

import pytest

from agent_sessions import (
    handoff,
    headless_dispatch,
    mission_dispatch,
    missions,
    prefs,
    session_input,
)

UUID = "11111111-2222-3333-4444-555555555555"
KEY = f"claude:{UUID}"
EPOCH = "epoch-under-test"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    missions.reset_schema_cache_for_test()
    handoff.reset_for_tests()
    # Pinned per test, never the conftest default alone: this module writes the grant.
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))
    yield tmp_path
    handoff.reset_for_tests()
    missions.reset_schema_cache_for_test()


def _claimed():
    m = missions.create_mission("ship it", cwd="/repo")
    missions.set_state(m["id"], "draft", "planned")
    plan = missions.put_plan(m["id"], project_id="prj_a", cwd="/repo", engine="claude", brief="go")
    return m["id"], missions.claim_plan(m["id"], plan["plan_id"])


def _started(kw) -> headless_dispatch.Dispatch:
    out = headless_dispatch.Dispatch(key=KEY, engine="claude", native=UUID, cwd=kw["cwd"])
    out.launched = out.started = out.briefed = True
    out.seed_outcome = "delivered"
    return out


def _launcher(monkeypatch, *, before_authorize=None):
    """A launcher that behaves like the real one around the fence: key first, then `authorize`
    (the real one calls it inside `launch_fence`), then the spawn — refused if `authorize` says
    so."""
    seen: dict = {}

    async def fake(**kw):
        seen["bypass"] = kw["bypass"]
        kw["on_key"](KEY)
        if before_authorize is not None:
            before_authorize()
        why = kw["authorize"](EPOCH)
        seen["refusal"] = why
        if why is not None:
            out = headless_dispatch.Dispatch(key=KEY, engine="claude", native=UUID, cwd=kw["cwd"])
            out.reason = why
            out.refusal = "policy"
            return out
        return _started(kw)

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", fake)
    return seen


def _run(mid, claimed):
    return asyncio.run(mission_dispatch.run(mid, claimed, registry=object(), policy_epoch=EPOCH))


def _settlement(mid) -> dict:
    for e in missions.get_mission(mid)["events"]:  # newest first
        meta = e.get("meta") or {}
        if e["kind"] == "state" and meta.get("from") == "dispatching":
            return e
    raise AssertionError("no settlement event")


def test_the_grant_defaults_on_and_a_mission_launches_bypassed(store, monkeypatch):
    """Nothing stored: the default is the one interactive launches use (True)."""
    seen = _launcher(monkeypatch)
    mid, claimed = _claimed()
    out = _run(mid, claimed)
    assert out["outcome"] == "started", out
    assert seen["bypass"] is True
    ev = _settlement(mid)
    assert ev["meta"]["bypass"] is True
    assert ev["meta"]["to"] == "running"
    assert "permission bypass on" in ev["meta"]["detail"]


def test_with_the_grant_off_the_launch_is_not_bypassed(store, monkeypatch):
    prefs.set_agent_defaults({"bypass": False})
    seen = _launcher(monkeypatch)
    mid, claimed = _claimed()
    out = _run(mid, claimed)
    assert out["outcome"] == "started", out
    assert seen["bypass"] is False
    ev = _settlement(mid)
    assert ev["meta"]["bypass"] is False
    assert "permission bypass off" in ev["meta"]["detail"]


@pytest.mark.parametrize("grant,ceiling", [(True, False), (False, True)])
def test_a_caller_ceiling_can_only_lower_the_recorded_launch_grant(
    store, monkeypatch, grant, ceiling
):
    """Automation consent remains bypass-off, and an affirmative ceiling cannot grant bypass.
    Both the command and the durable timeline must reflect the effective posture."""
    prefs.set_agent_defaults({"bypass": grant})
    seen = _launcher(monkeypatch)
    mid, claimed = _claimed()
    out = asyncio.run(
        mission_dispatch.run(
            mid, claimed, registry=object(), policy_epoch=EPOCH, bypass_ceiling=ceiling
        )
    )
    assert out["outcome"] == "started", out
    assert seen["bypass"] is False
    ev = _settlement(mid)
    assert ev["meta"]["bypass"] is False
    assert "permission bypass off" in ev["meta"]["detail"]


def test_a_revocation_before_the_fence_refuses_a_bypassed_launch(store, monkeypatch):
    """The argv is built with the grant read before the fence. Switching it off in between must
    stop the launch inside the fence — never start a bypassed agent the operator just revoked."""
    seen = _launcher(
        monkeypatch, before_authorize=lambda: prefs.set_agent_defaults({"bypass": False})
    )
    mid, claimed = _claimed()
    out = _run(mid, claimed)
    assert seen["bypass"] is True
    assert seen["refusal"] and "permission bypass was switched off" in seen["refusal"]
    assert out["outcome"] == "refused", out
    assert missions.get_mission(mid)["state"] == "planned"


def test_a_grant_turned_on_mid_launch_starts_it_without_bypass(store, monkeypatch):
    """off -> on in the same window is not refused: prompts-on is the safer posture, and it is the
    one that ran and the one recorded."""
    prefs.set_agent_defaults({"bypass": False})
    seen = _launcher(
        monkeypatch, before_authorize=lambda: prefs.set_agent_defaults({"bypass": True})
    )
    mid, claimed = _claimed()
    out = _run(mid, claimed)
    assert out["outcome"] == "started", out
    assert seen["bypass"] is False and seen["refusal"] is None
    assert _settlement(mid)["meta"]["bypass"] is False


def test_an_unreadable_grant_is_no_grant(store, monkeypatch):
    """Fail closed for the UNATTENDED grant, without changing the interactive default: a corrupt
    document still reads `bypass: True` for the new-session form, and grants nothing here."""
    path = store / "prefs.json"
    path.write_text("{not json")
    assert prefs.get_agent_defaults()["bypass"] is True
    assert mission_dispatch.bypass_granted() is False
    path.write_text('{"agent_defaults": {"bypass": "yes"}}')
    assert mission_dispatch.bypass_granted() is False
    path.write_text('{"agent_defaults": []}')
    assert mission_dispatch.bypass_granted() is False
    path.write_text('{"agent_defaults": null}')
    assert mission_dispatch.bypass_granted() is False, "a stored null is not an absent block"
    path.write_text('{"agent_defaults": {}}')
    assert mission_dispatch.bypass_granted() is True, "absent is the approved default"
    path.unlink()
    assert mission_dispatch.bypass_granted() is True, "nothing stored is the approved default"
    path.write_text('{"agent_defaults": {"bypass": false}}')
    assert mission_dispatch.bypass_granted() is False

    def boom(*a, **kw):
        raise OSError("prefs unreadable")

    monkeypatch.setattr(prefs, "_read_policy_doc", boom)
    assert mission_dispatch.bypass_granted() is False


def test_a_spawn_takes_the_same_grant(store, monkeypatch):
    """A sub-agent spawn runs through `mission_dispatch.run` too — the same launch-time read, the
    same fence check and the same recorded posture, for a spawn claim."""
    seen = _launcher(monkeypatch)
    mid, claimed = _claimed()
    assert _run(mid, claimed)["outcome"] == "started"
    missions_row = missions.get_mission(mid)
    assert missions_row["state"] == "running"
    spawn = missions.claim_spawn(
        mid, parent_key=KEY, engine="claude", cwd="/repo", brief="help", project_id="prj_a"
    )
    prefs.set_agent_defaults({"bypass": False})
    child = "claude:22222222-3333-4444-5555-666666666666"

    async def fake(**kw):
        seen["bypass"] = kw["bypass"]
        kw["on_key"](child)
        assert kw["authorize"](EPOCH) is None
        out = headless_dispatch.Dispatch(
            key=child, engine="claude", native=child.split(":")[1], cwd=kw["cwd"]
        )
        out.launched = out.started = out.briefed = True
        out.seed_outcome = "delivered"
        return out

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", fake)
    _run(mid, spawn)
    assert seen["bypass"] is False
    assert _settlement(mid)["meta"]["bypass"] is False


def test_a_bypass_change_commits_inside_the_launch_fence(store):
    """`policy_transaction("mission")` is what orders it against a launch; its epoch bump is the
    observable proof the write went through it. A default-engine-only change does not."""
    before = session_input.current_policy_epoch("mission")
    prefs.set_agent_defaults({"bypass": False})
    assert session_input.current_policy_epoch("mission") == before + 1
    assert prefs.get_agent_defaults()["bypass"] is False
    prefs.set_agent_defaults({"default_engine": None})
    assert session_input.current_policy_epoch("mission") == before + 1


@pytest.mark.parametrize(
    "stored",
    [
        '{"agent_defaults": {"bypass": "yes"}}',
        '{"agent_defaults": {"bypass": 1}}',
        '{"agent_defaults": []}',
        '{"agent_defaults": null}',
        '{"agent_defaults": {"bypass": null}}',
        '{"agent_defaults": {"bypass": false}}',
    ],
)
@pytest.mark.parametrize("patch", [{"default_engine": None}, {}, {"models": {"claude": ["extra"]}}])
def test_a_save_that_does_not_name_bypass_never_enables_the_grant(store, stored, patch):
    """#1215 issue review 2: the lenient coercion turned a malformed value into `True`, so an
    engine-only save — outside the fence — would have granted unattended bypass. It must leave
    the grant exactly as it was, and still hand the form its lenient block. PR review 1: a stored
    `null` block reached the merge as the same `None` an absent key does, and was dropped into
    the approved absent default — so `null` is in the matrix, as is an empty patch."""
    path = store / "prefs.json"
    path.write_text(stored)
    assert mission_dispatch.bypass_granted() is False
    before = session_input.current_policy_epoch("mission")
    out = prefs.set_agent_defaults(patch)
    assert isinstance(out["bypass"], bool)
    assert mission_dispatch.bypass_granted() is False, stored
    assert session_input.current_policy_epoch("mission") == before


@pytest.mark.parametrize(
    "stored",
    [None, "{}", '{"agent_defaults": {}}', '{"agent_defaults": {"default_engine": null}}'],
    ids=["no-file", "no-block", "empty-block", "no-value"],
)
@pytest.mark.parametrize("patch", [{"default_engine": None}, {}, {"models": {"claude": ["extra"]}}])
def test_an_engine_only_save_keeps_an_absent_grant_absent(store, stored, patch):
    """The other half of the null case: a block or value that is genuinely ABSENT is the approved
    default, and an engine-only save keeps it absent — granted, with the epoch unchanged."""
    import json

    path = store / "prefs.json"
    if stored is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(stored)
    assert mission_dispatch.bypass_granted() is True
    before = session_input.current_policy_epoch("mission")
    prefs.set_agent_defaults(patch)
    assert "bypass" not in json.loads(path.read_text())["agent_defaults"]
    assert mission_dispatch.bypass_granted() is True
    assert session_input.current_policy_epoch("mission") == before


def test_config_reports_the_strict_grant(store):
    """The missions settings line reads `mission_bypass`, the launch's own reading — never the
    form's lenient default over a malformed value."""
    (store / "prefs.json").write_text('{"agent_defaults": {"bypass": "yes"}}')
    assert prefs.get_agent_defaults()["bypass"] is True
    assert prefs.unattended_bypass_granted() is False


def _sibling(script: str, env_over: dict) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(env_over)
    env["PYTHONPATH"] = "src"
    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(script)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_a_revocation_waits_for_a_SIBLING_PROCESS_launch_in_its_fence(store, tmp_path, monkeypatch):
    """Cross-process ordering, with a real `flock` (#1215 issue review 2). A sibling instance holds
    the launch fence and reads the grant inside it, as `authorize` does just before a spawn. A
    revocation here must WAIT for that fence — it cannot land between the sibling's read and its
    spawn — and a launch fenced after the revocation must see it."""
    lockdir = tmp_path / "locks"
    monkeypatch.setenv("AGENT_SESSIONS_LOCK_DIR", str(lockdir))
    env = {
        "AGENT_SESSIONS_LOCK_DIR": str(lockdir),
        "AGENT_SESSIONS_PREFS": str(store / "prefs.json"),
    }
    launch = """
        import time
        from agent_sessions import mission_dispatch, session_input
        with session_input.launch_fence():
            print("held", flush=True)
            first = mission_dispatch.bypass_granted()
            time.sleep(1.0)
            print("authorised", first, mission_dispatch.bypass_granted(), flush=True)
    """
    sibling = _sibling(launch, env)
    assert sibling.stdout.readline().strip() == "held", sibling.stderr.read()
    t0 = time.monotonic()
    prefs.set_agent_defaults({"bypass": False})
    waited = time.monotonic() - t0
    line = sibling.stdout.readline().strip()
    sibling.wait(timeout=10)
    # The sibling saw the grant unchanged for the whole of its fenced window…
    assert line == "authorised True True", line
    # …because the revocation waited for the fence rather than landing inside it.
    assert waited >= 0.5, f"the revocation did not wait for the sibling's fence ({waited:.2f}s)"

    after = _sibling(launch, env)
    assert after.stdout.readline().strip() == "held"
    assert after.stdout.readline().strip() == "authorised False False"
    after.wait(timeout=10)
