"""Mission launches never bypass permissions, whatever the operator's grant (#1902, #1613 D8).

`agent_defaults.bypass` is the operator's permission-bypass grant (#1215). It used to reach mission
launches, which ran on terminal clients. Since #1902 a mission launches only API sessions
(`mission_dispatch.run` → `mission_structured.dispatch`), and the grant never reaches one: a
dispatch and a spawn are asked for no bypass, and the settlement records "permission bypass off".
The grant itself is still stored and read strictly (`prefs.unattended_bypass_granted`, reported to
Ask HQ); its storage rules are pinned below. Fakes stand at the structured door. Nothing here
spawns a process.
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
    mission_structured,
    missions,
    prefs,
    session_input,
)

UUID = "11111111-2222-3333-4444-555555555555"
ENGINE = "claude-api"
KEY = f"{ENGINE}:{UUID}"
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
    plan = missions.put_plan(m["id"], project_id="prj_a", cwd="/repo", engine=ENGINE, brief="go")
    return m["id"], missions.claim_plan(m["id"], plan["plan_id"])


def _launcher(monkeypatch, *, key=KEY, before_authorize=None):
    """The structured door, behaving like the real one around the fence: key first, then
    `authorize` inside the fence, then the create, refused if `authorize` says so. Records every
    keyword it was handed."""
    seen: dict = {}

    async def fake(**kw):
        seen["kw"] = kw
        kw["on_key"](key)
        if before_authorize is not None:
            before_authorize()
        why = kw["authorize"](EPOCH)
        seen["refusal"] = why
        out = headless_dispatch.Dispatch(key=key, engine=ENGINE, native=key.split(":")[1], cwd="")
        if why is not None:
            out.reason = why
            out.refusal = "policy"
            return out
        out.launched = out.started = out.briefed = True
        out.seed_outcome = "delivered"
        return out

    monkeypatch.setattr(mission_structured, "dispatch", fake)
    return seen


def _run(mid, claimed, **kw):
    return asyncio.run(
        mission_dispatch.run(mid, claimed, registry=object(), policy_epoch=EPOCH, **kw)
    )


def _settlement(mid) -> dict:
    for e in missions.get_mission(mid)["events"]:  # newest first
        meta = e.get("meta") or {}
        if e["kind"] == "state" and meta.get("from") == "dispatching":
            return e
    raise AssertionError("no settlement event")


def _assert_not_bypassed(seen, mid):
    assert "bypass" not in seen["kw"], "a mission's API session is never asked to bypass"
    ev = _settlement(mid)
    assert ev["meta"]["bypass"] is False
    assert "permission bypass off" in ev["meta"]["detail"]


@pytest.mark.parametrize("grant", [None, True, False], ids=["default", "granted", "withheld"])
def test_no_grant_reaches_a_mission_launch(store, monkeypatch, grant):
    """Red against the old launch, which read the grant (default on) and launched bypassed."""
    if grant is not None:
        prefs.set_agent_defaults({"bypass": grant})
    seen = _launcher(monkeypatch)
    mid, claimed = _claimed()
    out = _run(mid, claimed)
    assert out["outcome"] == "started", out
    assert _settlement(mid)["meta"]["to"] == "running"
    _assert_not_bypassed(seen, mid)


@pytest.mark.parametrize("ceiling", [True, False, None])
def test_a_caller_ceiling_changes_nothing(store, monkeypatch, ceiling):
    """An automation still passes its bypass-off ceiling (#1201); an affirmative one cannot grant
    what a mission launch never has."""
    prefs.set_agent_defaults({"bypass": True})
    seen = _launcher(monkeypatch)
    mid, claimed = _claimed()
    assert _run(mid, claimed, bypass_ceiling=ceiling)["outcome"] == "started"
    _assert_not_bypassed(seen, mid)


@pytest.mark.parametrize("flip", [False, True], ids=["revoked", "granted"])
def test_a_grant_change_mid_launch_neither_refuses_nor_bypasses(store, monkeypatch, flip):
    """The grant is not part of a mission launch's authority any more, so changing it inside the
    window refuses nothing and starts nothing bypassed."""
    prefs.set_agent_defaults({"bypass": not flip})
    seen = _launcher(
        monkeypatch, before_authorize=lambda: prefs.set_agent_defaults({"bypass": flip})
    )
    mid, claimed = _claimed()
    out = _run(mid, claimed)
    assert seen["refusal"] is None and out["outcome"] == "started", out
    _assert_not_bypassed(seen, mid)


def test_an_unreadable_grant_is_no_grant(store, monkeypatch):
    """Fail closed for the UNATTENDED grant, without changing the interactive default: a corrupt
    document still reads `bypass: True` for the new-session form, and grants nothing here."""
    path = store / "prefs.json"
    path.write_text("{not json")
    assert prefs.get_agent_defaults()["bypass"] is True
    assert prefs.unattended_bypass_granted() is False
    path.write_text('{"agent_defaults": {"bypass": "yes"}}')
    assert prefs.unattended_bypass_granted() is False
    path.write_text('{"agent_defaults": []}')
    assert prefs.unattended_bypass_granted() is False
    path.write_text('{"agent_defaults": null}')
    assert prefs.unattended_bypass_granted() is False, "a stored null is not an absent block"
    path.write_text('{"agent_defaults": {}}')
    assert prefs.unattended_bypass_granted() is True, "absent is the approved default"
    path.unlink()
    assert prefs.unattended_bypass_granted() is True, "nothing stored is the approved default"
    path.write_text('{"agent_defaults": {"bypass": false}}')
    assert prefs.unattended_bypass_granted() is False

    def boom(*a, **kw):
        raise OSError("prefs unreadable")

    monkeypatch.setattr(prefs, "_read_policy_doc", boom)
    assert prefs.unattended_bypass_granted() is False


def test_a_spawn_never_bypasses_either(store, monkeypatch):
    """A sub-agent spawn runs through `mission_dispatch.run` too, for a spawn claim."""
    _launcher(monkeypatch)
    mid, claimed = _claimed()
    assert _run(mid, claimed)["outcome"] == "started"
    assert missions.get_mission(mid)["state"] == "running"
    spawn = missions.claim_spawn(
        mid, parent_key=KEY, engine=ENGINE, cwd="/repo", brief="help", project_id="prj_a"
    )
    child = f"{ENGINE}:22222222-3333-4444-5555-666666666666"
    seen = _launcher(monkeypatch, key=child)
    _run(mid, spawn)
    _assert_not_bypassed(seen, mid)


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
    assert prefs.unattended_bypass_granted() is False
    before = session_input.current_policy_epoch("mission")
    out = prefs.set_agent_defaults(patch)
    assert isinstance(out["bypass"], bool)
    assert prefs.unattended_bypass_granted() is False, stored
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
    assert prefs.unattended_bypass_granted() is True
    before = session_input.current_policy_epoch("mission")
    prefs.set_agent_defaults(patch)
    assert "bypass" not in json.loads(path.read_text())["agent_defaults"]
    assert prefs.unattended_bypass_granted() is True
    assert session_input.current_policy_epoch("mission") == before


def test_the_strict_grant_never_reads_a_malformed_value_as_granted(store):
    """The strict reading (`prefs.unattended_bypass_granted`, reported to Ask HQ) never takes the
    form's lenient default over a malformed value. `/api/config`'s `mission_bypass` no longer reads
    it at all: it is always `False` (#1902, `test_mission_console_cutoff.py`)."""
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
        from agent_sessions import prefs, session_input
        with session_input.launch_fence():
            print("held", flush=True)
            first = prefs.unattended_bypass_granted()
            time.sleep(1.0)
            print("authorised", first, prefs.unattended_bypass_granted(), flush=True)
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
