"""Reviewing pull requests on the GENERIC path (#2222): a webhook from the list → "Start a session"
that runs AS a pinned specialist, claimed once per chosen delivery values.

Read `docs/invariants/webhook-specialist-sessions.md`. Nothing reaches a real engine: the save-time
world checks and the API launch (`mission_structured.automation_start`) are faked at the seams the
code calls, as in the other automation tests.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_sessions import (
    automation_queue,
    automation_runner,
    mission_structured,
    webhook_events,
    webhooks,
)
from agent_sessions import automations as model
from agent_sessions import automations_store as store
from agent_sessions import templates as tstore
from agent_sessions.routes import automations as routes
from test_webhook_events import _deliver, _runs

SPEC = {
    "id": "org-reviewer",
    "version": 3,
    "name": "Org reviewer",
    "engine": "opencode-api",
    "model": "example-gateway/model-pro",
    "effort": "high",
}
SHA_A, SHA_B = "a" * 40, "b" * 40
ONCE = ["repository.full_name", "pull_request.number", "pull_request.head.sha"]
MAPPING = {
    "repo": {"path": "repository.full_name", "type": "text"},
    "pr_number": {"path": "pull_request.number", "type": "number"},
    "head_sha": {"path": "pull_request.head.sha", "type": "text"},
}


class Prov:
    engine_id = "opencode-api"


@pytest.fixture(autouse=True)
def _world(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SESSIONS_AUTOMATION_LOOP", "1")
    folder = tmp_path / "shop"
    folder.mkdir()
    monkeypatch.setattr(model, "specialist_pin", lambda _r: dict(SPEC))
    monkeypatch.setattr(automation_runner.engines, "get", lambda _e: Prov())
    monkeypatch.setattr(mission_structured, "is_api", lambda _p: True)
    monkeypatch.setattr(automation_runner, "client_settings_state", lambda _a: (True, ""))
    monkeypatch.setattr(automation_runner, "target_state", lambda _e: (True, ""))
    monkeypatch.setattr(automation_runner, "folder_ok", lambda _f: (True, ""))
    # The automation queue's gate (#2422) holds the launch slots before the (faked) launch; this
    # world has no specialist store row behind `SPEC`, so it holds nothing here.
    monkeypatch.setattr(
        automation_queue, "launch_slot", lambda _run: automation_queue.LaunchHold(key=None)
    )
    automation_runner._TASKS.clear()
    yield folder
    automation_runner._TASKS.clear()


@pytest.fixture
def spawned(monkeypatch):
    out: list[dict] = []
    monkeypatch.setattr(
        automation_runner, "spawn", lambda run, registry=None, **_kw: out.append(run)
    )
    return out


def _template() -> dict:
    return tstore.create_template(
        {
            "name": "review a PR",
            "body": "Review {{repo}}#{{pr_number}} at {{head_sha}}.",
            "fields": [{"name": "repo"}, {"name": "pr_number"}, {"name": "head_sha"}],
        }
    )


def _config(folder, wid: str, *, specialist=True, once_per=ONCE, **action) -> dict:
    act = {
        "kind": "start_session",
        "engine": SPEC["engine"],
        "folder": str(folder),
        "message": {"template_id": _template()["id"], "values": {}, "from_delivery": MAPPING},
    }
    if specialist:
        act["specialist"] = {"specialist_id": SPEC["id"], "version": SPEC["version"]}
    act.update(action)
    trig: dict = {"kind": "webhook", "webhook_id": wid}
    if once_per:
        trig["once_per"] = once_per
    # Runs here never leave `dispatching` (spawn is faked), so allow them side by side.
    policy = {"concurrency": "allow", "max_concurrent": 3}
    return {"name": "Review PRs", "trigger": trig, "action": act, "policy": policy}


def _made(cfg: dict) -> str:
    aid = routes._create(cfg)["id"]
    pub = routes.public(store.get(aid))
    routes._enable(
        aid, {"revision": pub["revision"], "consent": True, "scope_digest": pub["scope_digest"]}
    )
    return aid


def _pr(sha: str = SHA_A, number: int = 41, updated: str = "t1") -> dict:
    return {
        "action": "synchronized",
        "number": number,
        "repository": {"full_name": "example-org/shop"},
        "pull_request": {
            "number": number,
            "draft": False,
            "updated_at": updated,
            "head": {"sha": sha, "ref": "fix"},
        },
    }


# ---- the config ---------------------------------------------------------------------------------


def test_a_session_without_a_specialist_keeps_its_exact_shape(_world):
    out = model.validate_action(
        {"kind": "start_session", "engine": "claude", "folder": "/w/p", "message": {"text": "hi"}}
    )
    assert "specialist" not in out
    trig = model.validate_trigger({"kind": "webhook", "webhook_id": "wh_1"})
    assert trig == {"kind": "webhook", "webhook_id": "wh_1"}


@pytest.mark.parametrize(
    "spec, why",
    [
        ({"specialist_id": "x"}, "version"),
        ({"specialist_id": "../x", "version": 1}, "specialist id"),
        ({"specialist_id": "x", "version": 0}, "version"),
        ({"specialist_id": "x", "version": 1, "model": "m"}, "does not take model"),
    ],
)
def test_the_specialist_is_one_id_at_one_version(spec, why):
    act = {
        "kind": "start_session",
        "engine": "e",
        "folder": "/w",
        "message": {"text": "hi"},
        "specialist": spec,
    }
    with pytest.raises(model.AutomationError, match=why):
        model.validate_action(act)


def test_a_specialist_session_may_skip_permission_prompts_and_close_when_done():
    # #2405: both are the operator's consented choices, stored as given.
    act = {
        "kind": "start_session",
        "engine": "e",
        "folder": "/w",
        "message": {"text": "hi"},
        "bypass": True,
        "close_when_done": True,
        "specialist": {"specialist_id": "x", "version": 1},
    }
    out = model.validate_action(act)
    assert out["bypass"] is True and out["close_when_done"] is True
    assert "close_when_done" not in model.validate_action({**act, "close_when_done": False})
    with pytest.raises(model.AutomationError, match="close_when_done"):
        model.validate_action({**act, "close_when_done": "yes"})


@pytest.mark.parametrize(
    "raw, why",
    [
        ("pull_request.head.sha", "list of at most"),
        (["a", "b", "c", "d", "e"], "at most 4"),
        (["labels[].name"], "not a list"),
        (["a", "a"], "twice"),
        (["@x"], "path must be a field"),
    ],
)
def test_once_per_is_a_short_list_of_plain_fields(raw, why):
    with pytest.raises(webhook_events.EventError, match=why):
        webhook_events.validate_once_per(raw)


def test_the_once_key_is_the_values_and_refuses_an_unreadable_identity():
    a = webhook_events.once_key(ONCE, _pr(updated="t1"))
    assert a == webhook_events.once_key(ONCE, _pr(updated="t2"))  # same head, other body
    assert a != webhook_events.once_key(ONCE, _pr(sha=SHA_B))
    assert a != webhook_events.once_key(ONCE, _pr(number=42))
    with pytest.raises(webhook_events.ValueRejected, match="no value at"):
        webhook_events.once_key(["pull_request.merged_by"], _pr())
    with pytest.raises(webhook_events.ValueRejected, match="short text, a number"):
        webhook_events.once_key(["pull_request.head"], _pr())


def test_save_refuses_a_specialist_on_another_agent_or_a_missing_version(_world, monkeypatch):
    wid = webhooks.create("ci")[0]["id"]
    with pytest.raises(model.AutomationError, match="runs on opencode-api"):
        routes._create(_config(_world, wid, engine="claude-api"))
    monkeypatch.setattr(
        model, "specialist_pin", lambda _r: {**SPEC, "missing": "that specialist is archived"}
    )
    with pytest.raises(model.AutomationError, match="archived"):
        routes._create(_config(_world, wid))


def test_save_refuses_a_specialist_on_a_terminal_agent(_world, monkeypatch):
    wid = webhooks.create("ci")[0]["id"]
    monkeypatch.setattr(automation_runner, "webhook_target_api", lambda _a: True)
    monkeypatch.setattr(mission_structured, "is_api", lambda _p: False)
    with pytest.raises(model.AutomationError, match="runs on an API agent"):
        routes._create(_config(_world, wid))


# ---- the consent --------------------------------------------------------------------------------


def _scope(folder, wid, **kw) -> dict:
    cfg = model.validate_config(_config(folder, wid, **kw))
    return model.scope_of(cfg, model.compute_pins(cfg))


def test_the_consent_names_the_specialist_its_version_and_the_once_key(_world):
    wid = webhooks.create("ci")[0]["id"]
    scope = _scope(_world, wid)
    assert scope["target"]["specialist"] == {
        "id": "org-reviewer",
        "version": 3,
        "name": "Org reviewer",
        "effort": "high",
    }
    # It runs on what the pinned version names, not on a default.
    assert (scope["engine"], scope["model"]) == ("opencode-api", "example-gateway/model-pro")
    assert scope["once_per"] == ONCE
    text = "\n".join(model.describe(scope))
    assert "As the specialist “Org reviewer” (org-reviewer), version 3" in text
    assert "effort high" in text
    assert "Model: example-gateway/model-pro" in text
    assert "Runs once per distinct repository.full_name, pull_request.number" in text


def test_without_them_the_scope_has_neither_field(_world):
    wid = webhooks.create("ci")[0]["id"]
    scope = _scope(_world, wid, specialist=False, once_per=None)
    assert "specialist" not in scope["target"] and "once_per" not in scope


@pytest.mark.parametrize(
    "path, new",
    [
        (("target", "specialist", "version"), 4),
        (("target", "specialist", "effort"), "low"),
        (("target", "specialist", "id"), "other"),
        (("once_per",), ["pull_request.number"]),
    ],
)
def test_every_new_scope_field_changes_the_consent_lines_and_the_digest(_world, path, new):
    wid = webhooks.create("ci")[0]["id"]
    scope = _scope(_world, wid)
    import copy

    mutated = copy.deepcopy(scope)
    node = mutated
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = new
    assert routes.scope_digest(mutated) != routes.scope_digest(scope)
    assert model.describe(mutated) != model.describe(scope)


def test_a_specialist_edited_after_consent_needs_approval_again(_world, monkeypatch):
    wid = webhooks.create("ci")[0]["id"]
    cfg = model.validate_config(_config(_world, wid))
    stored = model.compute_pins(cfg)
    monkeypatch.setattr(
        model,
        "specialist_pin",
        lambda _r: {**SPEC, "missing": "that specialist was edited to a newer version"},
    )
    assert model.pins_drift(stored, model.compute_pins(cfg)) == (
        "that specialist was edited to a newer version"
    )


# ---- a delivery ---------------------------------------------------------------------------------


def test_one_run_per_head_even_when_the_body_differs(_world, spawned):
    wid = webhooks.create("ci")[0]["id"]
    aid = _made(_config(_world, wid))
    _deliver(aid, wid, _pr(updated="t1"))
    _deliver(aid, wid, _pr(updated="t2"))  # the CI re-send: same head, a fresh updated_at
    assert len(spawned) == 1
    _deliver(aid, wid, _pr(sha=SHA_B))  # a push: a new head
    assert len(spawned) == 2
    assert [r["inputs"]["delivered"]["head_sha"] for r in spawned] == [SHA_A, SHA_B]


def test_without_once_per_the_body_is_the_claim(_world, spawned):
    wid = webhooks.create("ci")[0]["id"]
    aid = _made(_config(_world, wid, once_per=None))
    _deliver(aid, wid, _pr(updated="t1"))
    _deliver(aid, wid, _pr(updated="t1"))  # an identical body runs once
    _deliver(aid, wid, _pr(updated="t2"))  # a different body is a different delivery
    assert len(spawned) == 2


def test_a_delivery_without_the_once_values_is_refused_and_starts_nothing(_world, spawned):
    wid = webhooks.create("ci")[0]["id"]
    aid = _made(_config(_world, wid, once_per=["pull_request.merged_by"]))
    _deliver(aid, wid, _pr())
    assert spawned == []
    (run,) = _runs(aid)
    assert run["outcome"] == "refused" and "no value at pull_request.merged_by" in run["reason"]


# ---- the launch ---------------------------------------------------------------------------------


def _launch(monkeypatch) -> dict:
    seen: dict = {}

    class Out:
        ok, launched, started, briefed = True, True, True, True
        key, teardown, reason, state, seed_outcome = "opencode-api:s1", "", "", "", ""
        bound, bound_key = None, None

    async def start(**kw):
        seen.update(kw)
        kw["on_key"]("opencode-api:s1")
        return Out()

    monkeypatch.setattr(mission_structured, "automation_start", start)
    return seen


def test_the_session_launches_as_the_pinned_specialist_version(_world, spawned, monkeypatch):
    wid = webhooks.create("ci")[0]["id"]
    aid = _made(_config(_world, wid))
    _deliver(aid, wid, _pr())
    (run,) = spawned
    seen = _launch(monkeypatch)
    res = asyncio.run(automation_runner.execute(run, registry=None))
    assert res["run"]["outcome"] == "ok", res["run"]["reason"]
    assert seen["specialist"] == ("org-reviewer", 3)
    assert seen["model"] == "example-gateway/model-pro" and seen["effort"] == "high"
    assert seen["brief"] == f"Review example-org/shop#41 at {SHA_A}."
    assert store.get_run(run["id"])["session_key"] == "opencode-api:s1"


def test_a_session_without_a_specialist_launches_as_before(_world, spawned, monkeypatch):
    wid = webhooks.create("ci")[0]["id"]
    aid = _made(_config(_world, wid, specialist=False))
    _deliver(aid, wid, _pr())
    (run,) = spawned
    seen = _launch(monkeypatch)
    res = asyncio.run(automation_runner.execute(run, registry=None))
    assert res["run"]["outcome"] == "ok", res["run"]["reason"]
    assert not {"specialist", "model", "effort"} & set(seen)


def test_a_specialist_gone_before_the_run_starts_nothing(_world, spawned, monkeypatch):
    wid = webhooks.create("ci")[0]["id"]
    aid = _made(_config(_world, wid))
    _deliver(aid, wid, _pr())
    (run,) = spawned
    seen = _launch(monkeypatch)
    monkeypatch.setattr(
        model, "specialist_pin", lambda _r: {**SPEC, "missing": "that specialist is archived"}
    )
    res = asyncio.run(automation_runner.execute(run, registry=None))
    assert res["run"]["outcome"] != "ok"
    assert "archived" in res["run"]["reason"]
    assert seen == {}


# ---- the claim's edges (#2222 review) -----------------------------------------------------------


def test_simultaneous_deliveries_of_one_head_admit_once(_world, spawned):
    import threading

    wid = webhooks.create("ci")[0]["id"]
    aid = _made(_config(_world, wid))
    gate = threading.Barrier(4)

    def send(i: int) -> None:
        gate.wait()
        _deliver(aid, wid, _pr(updated=f"t{i}"))

    threads = [threading.Thread(target=send, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(spawned) == 1


def test_a_busy_delivery_waits_in_the_queue_and_starts_after_the_previous_run(
    _world, spawned, monkeypatch
):
    """#2422: a delivery over `max_concurrent` is no longer skipped (it used to wait for a CI
    re-send): it is QUEUED, claimed once, and the drain starts it when the previous run ends."""
    wid = webhooks.create("ci")[0]["id"]
    cfg = _config(_world, wid)
    cfg["policy"] = {"concurrency": "skip"}
    aid = _made(cfg)
    _deliver(aid, wid, _pr(sha=SHA_A))
    _deliver(aid, wid, _pr(sha=SHA_B))  # busy: head A's run is still dispatching
    assert len(spawned) == 1
    waiting = [r for r in _runs(aid) if r["state"] == "queued"]
    assert len(waiting) == 1 and waiting[0]["queue"]["capacity"] == "concurrency"
    _deliver(aid, wid, _pr(sha=SHA_B, updated="t2"))  # the CI re-send of head B: claimed already
    assert len([r for r in _runs(aid) if r["state"] == "queued"]) == 1
    _launch(monkeypatch)
    res = asyncio.run(automation_runner.execute(spawned[0], registry=None))
    assert res["run"]["outcome"] == "ok", res["run"]["reason"]
    stats = asyncio.run(automation_queue.drain(registry=None))
    assert stats.started == 1
    assert len(spawned) == 2 and spawned[1]["inputs"]["delivered"]["head_sha"] == SHA_B


def test_an_admitted_run_that_fails_keeps_its_claim(_world, spawned, monkeypatch):
    wid = webhooks.create("ci")[0]["id"]
    aid = _made(_config(_world, wid))
    _deliver(aid, wid, _pr())
    (run,) = spawned

    async def refused(**_kw):
        raise automation_runner.headless_dispatch.DispatchError("the client is not ready")

    monkeypatch.setattr(mission_structured, "automation_start", refused)
    res = asyncio.run(automation_runner.execute(run, registry=None))
    assert res["run"]["outcome"] == "refused"
    _deliver(aid, wid, _pr(updated="t9"))  # the same head again: at most once, not retried
    assert len(spawned) == 1


def test_an_unconfirmed_first_turn_keeps_its_claim_and_is_never_resent(
    _world, spawned, monkeypatch
):
    # The worker was created but whether it got its brief is unknown: it may be working on it, so
    # the same head is never sent again on its own (#2222 review: admitted unknown outcomes).
    wid = webhooks.create("ci")[0]["id"]
    aid = _made(_config(_world, wid))
    _deliver(aid, wid, _pr())
    (run,) = spawned

    class Out:
        ok, launched, started, briefed = False, True, True, False
        key, teardown, state = "opencode-api:s1", "", ""
        reason, seed_outcome = "the first turn could not be confirmed", "unknown"
        bound, bound_key = None, None

    async def unknown(**kw):
        kw["on_key"]("opencode-api:s1")
        return Out()

    monkeypatch.setattr(mission_structured, "automation_start", unknown)
    res = asyncio.run(automation_runner.execute(run, registry=None))
    assert res["run"]["outcome"] != "ok"
    assert store.get_run(run["id"])["session_key"] == "opencode-api:s1"
    _deliver(aid, wid, _pr(updated="t3"))
    assert len(spawned) == 1


def test_the_claim_survives_a_restart(_world, spawned):
    # The claim is a row in automations.db, not process state: every store call opens its own
    # connection (`automations_store._connect`), so a restarted process reads the same claim.
    wid = webhooks.create("ci")[0]["id"]
    aid = _made(_config(_world, wid))
    _deliver(aid, wid, _pr())
    automation_runner._TASKS.clear()  # the in-process run tasks a restart loses
    _deliver(aid, wid, _pr(updated="after-restart"))
    assert len(spawned) == 1


# ---- the second review round (#2358) ------------------------------------------------------------


def test_a_specialist_edited_after_admission_is_refused_inside_the_launch_fence(
    _world, spawned, monkeypatch
):
    # The drift check ran when the run started; the specialist is edited while the launch waits.
    # The fence the launch calls last must read the store, not the admission snapshot.
    wid = webhooks.create("ci")[0]["id"]
    aid = _made(_config(_world, wid))
    _deliver(aid, wid, _pr())
    (run,) = spawned
    seen: dict = {}

    class Refused:
        ok, launched, started, briefed = False, False, False, False
        key, teardown, state, seed_outcome = "", "", "", "not_attempted"
        reason, capacity = "", ""
        bound, bound_key = None, None

    async def start(**kw):
        monkeypatch.setattr(
            model,
            "specialist_pin",
            lambda _r: {**SPEC, "missing": "that specialist was edited to a newer version"},
        )
        ok, why = kw["check"]()
        seen["check"] = (ok, why)
        out = Refused()
        out.reason = why
        return out

    monkeypatch.setattr(mission_structured, "automation_start", start)
    res = asyncio.run(automation_runner.execute(run, registry=None))
    assert seen["check"] == (
        False,
        "the specialist can't be used: that specialist was edited to a newer version",
    )
    assert res["run"]["outcome"] != "ok"


def test_the_fence_refuses_a_specialist_whose_pin_changed_without_saying_missing(
    _world, spawned, monkeypatch
):
    wid = webhooks.create("ci")[0]["id"]
    aid = _made(_config(_world, wid))
    _deliver(aid, wid, _pr())
    seen: dict = {}

    async def start(**kw):
        monkeypatch.setattr(model, "specialist_pin", lambda _r: {**SPEC, "effort": "low"})
        seen["check"] = kw["check"]()
        raise automation_runner.headless_dispatch.DispatchError("refused by the fence")

    monkeypatch.setattr(mission_structured, "automation_start", start)
    asyncio.run(automation_runner.execute(spawned[0], registry=None))
    assert seen["check"] == (False, "the specialist changed since you approved this automation")


@pytest.mark.parametrize(
    "a, b",
    [(41, "41"), (41, 41.0), (True, "true"), (False, "false")],
)
def test_one_value_in_two_json_spellings_is_one_claim(a, b):
    def body(v):
        return {"pull_request": {"number": v}}

    assert webhook_events.once_key(["pull_request.number"], body(a)) == webhook_events.once_key(
        ["pull_request.number"], body(b)
    )


def test_a_value_sent_as_text_then_as_a_number_runs_once(_world, spawned):
    wid = webhooks.create("ci")[0]["id"]
    aid = _made(
        _config(_world, wid, once_per=["repository.full_name", "pull_request.head.sha", "number"])
    )
    _deliver(aid, wid, {**_pr(), "number": 41})
    _deliver(aid, wid, {**_pr(updated="t2"), "number": "41"})
    assert len(spawned) == 1
