"""The "Tell HQ" automation action (#2221): a delivery becomes a message to the operator in the
bell, filed as an HQ dispatch whose facts are the class and the automation's name only.

The decision these tests pin: HQ's model input (`hq_dispatch` facts) never carries a row's title or
reason, and a delivery's values are third-party text. So the rendered message is the bell row's
REASON (display, for the operator) and HQ's member carries `CLASS_AUTOMATION_MESSAGE`, the
automation's name and the age — never the message and never a delivered value. Nothing here makes
HQ act: it writes its usual one line about the event.

The HQ model call goes to a REAL loopback endpoint (`hq_dispatch_harness.FakeEndpoint`) through the
app's one transport, so the "never on the wire" assertion is on the bytes that left the process.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

import hq_dispatch_harness as H
from agent_sessions import automation_runner, hq_dispatch, notifications, webhooks
from agent_sessions import automations as model
from agent_sessions import automations_store as store
from agent_sessions import templates as tstore
from agent_sessions.routes import automations as routes
from test_webhook_events import PR, _deliver, _runs

NAME = "Deploy watch"
#: A value a delivery fills: it passes the `text` type's closed alphabet, so it does reach the row.
PLANTED = "PLANTED-TITLE-zq7"
MAPPING = {
    "number": {"path": "pull_request.number", "type": "number"},
    "title": {"path": "pull_request.title", "type": "text"},
}


@pytest.fixture(autouse=True)
def _loop_on(monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_AUTOMATION_LOOP", "1")


@pytest.fixture
def spawned(monkeypatch):
    out: list[dict] = []
    monkeypatch.setattr(automation_runner, "spawn", lambda run, registry=None: out.append(run))
    return out


@pytest.fixture
def ep():
    e = H.FakeEndpoint()
    yield e
    e.close()


@pytest.fixture
def hq(ep):
    H.set_up_hq(ep.base)
    return ep


def _template() -> dict:
    return tstore.create_template(
        {
            "name": "pr arrived",
            "body": "PR {{number}} needs a look: {{title}}",
            "fields": [{"name": "number"}, {"name": "title"}],
        }
    )


def _hook_config(wid: str, t: dict, *, mapping=None) -> dict:
    message: dict = {"template_id": t["id"], "values": {}}
    if mapping:
        message["from_delivery"] = mapping
    return {
        "name": NAME,
        "trigger": {"kind": "webhook", "webhook_id": wid},
        "action": {"kind": "tell_hq", "message": message},
        "policy": {},
    }


def _manual_config(text: str = "the nightly backup finished") -> dict:
    return {
        "name": NAME,
        "trigger": {"kind": "manual"},
        "action": {"kind": "tell_hq", "message": {"text": text}},
        "policy": {"concurrency": "allow", "max_concurrent": 2},
    }


def _made(cfg: dict) -> str:
    aid = routes._create(cfg)["id"]
    pub = routes.public(store.get(aid))
    routes._enable(
        aid, {"revision": pub["revision"], "consent": True, "scope_digest": pub["scope_digest"]}
    )
    return aid


def _manual_run(aid: str) -> dict:
    res = store.begin_run(aid, trigger="manual", slot=f"manual:{time.time_ns()}", fire_at=None)
    assert res["claimed"], res
    return res["run"]


def _execute(run: dict) -> dict:
    return asyncio.run(automation_runner.execute(run, registry=None))


def _bell() -> list[dict]:
    return notifications.listing()["notifications"]


def _delivered_run(spawned) -> tuple[str, dict]:
    pub, _ = webhooks.create("ci")
    aid = _made(_hook_config(pub["id"], _template(), mapping=MAPPING))
    _deliver(aid, pub["id"], {**PR, "pull_request": {**PR["pull_request"], "title": PLANTED}})
    (run,) = spawned
    return aid, run


# ---- the config ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "trigger",
    [
        {"kind": "manual"},
        {"kind": "once", "at": "2099-01-01T09:00", "tz": "UTC"},
        {"kind": "schedule", "cadence": {"kind": "daily", "time": "07:00"}, "tz": "UTC"},
    ],
)
def test_tell_hq_saves_on_every_non_webhook_trigger(trigger):
    cfg = {**_manual_config(), "trigger": trigger}
    out = model.validate_config(cfg)
    assert out["action"] == {"kind": "tell_hq", "message": {"text": "the nightly backup finished"}}
    assert routes._create(cfg)["id"]


def test_a_webhook_may_start_tell_hq_with_values_from_the_delivery():
    assert "tell_hq" in model.ACTION_KINDS and "tell_hq" in model.WEBHOOK_ACTIONS
    assert "tell_hq" in store.DELIVERY_ACTIONS
    pub, _ = webhooks.create("ci")
    cfg = _hook_config(pub["id"], _template(), mapping=MAPPING)
    out = model.validate_config(cfg)
    assert out["action"]["message"]["from_delivery"] == MAPPING
    # Not an API-session action: no engine is asked (the API-only rule is for sessions).
    assert routes._create(cfg)["id"]
    # Values from a delivery go only with a webhook.
    with pytest.raises(model.AutomationError, match="only a webhook delivery can fill"):
        model.validate_config({**cfg, "trigger": {"kind": "manual"}})


def test_a_tell_hq_action_is_strict():
    base = _manual_config()
    for extra in ({"session_key": "claude:x"}, {"engine": "claude"}, {"bypass": True}):
        with pytest.raises(model.AutomationError):
            model.validate_config({**base, "action": {**base["action"], **extra}})
    with pytest.raises(model.AutomationError):
        model.validate_config({**base, "action": {"kind": "tell_hq"}})
    with pytest.raises(model.AutomationError, match="text is empty"):
        model.validate_config({**base, "action": {"kind": "tell_hq", "message": {"text": " "}}})


def test_a_template_with_a_secret_is_refused_for_tell_hq():
    from agent_sessions import template_vars

    template_vars.create_variable({"name": "token", "kind": "secret", "value": "s3cr3t-PLANTED"})
    t = tstore.create_template(
        {
            "name": "with a secret",
            "body": "{{token}} {{n}}",
            "fields": [{"name": "token", "source": "library", "kind": "secret"}, {"name": "n"}],
        }
    )
    cfg = {
        **_manual_config(),
        "action": {"kind": "tell_hq", "message": {"template_id": t["id"], "values": {"n": "1"}}},
    }
    with pytest.raises(model.AutomationError, match="shown in your notifications"):
        model.check_template(model.validate_config(cfg))
    with pytest.raises(model.AutomationError, match="shown in your notifications"):
        routes._create(cfg)


# ---- the consent --------------------------------------------------------------------------------


def test_the_scope_names_no_target_agent_or_bypass_and_the_lines_say_what_it_does():
    pub, _ = webhooks.create("ci")
    t = _template()
    cfg = model.validate_config(_hook_config(pub["id"], t, mapping=MAPPING))
    scope = model.scope_of(cfg, model.compute_pins(cfg))
    assert scope["action"] == "tell_hq" and scope["target"] == {}
    assert scope["engine"] is None and scope["model"] is None and scope["bypass"] is False
    lines = model.describe(scope)
    text = "\n".join(lines)
    assert (
        "Puts a message in your notifications (the bell) and tells HQ an automation reported"
        in lines
    )
    assert "never what it says; HQ may comment, it never acts on it" in text
    assert "Starts no session and types into none" in lines
    assert "Permission bypass" not in text and "Agent:" not in text
    # The message lines are the usual ones: the template, its revision and every mapped value.
    assert f"template “pr arrived” ({t['id']})" in text
    assert "title = from the delivery's pull_request.title (text)" in text


# ---- a delivery ---------------------------------------------------------------------------------


def test_a_webhook_delivery_is_admitted_to_a_run_carrying_the_typed_values(spawned):
    aid, run = _delivered_run(spawned)
    assert run["state"] == "dispatching"
    assert run["inputs"]["delivered"] == {"number": "41", "title": PLANTED}
    cfg = automation_runner.with_delivered(run["scope"]["config"], run)
    assert cfg["action"]["message"]["values"] == {"number": "41", "title": PLANTED}
    assert "from_delivery" not in cfg["action"]["message"]
    # A run carrying values its automation does not map is refused, as for the session actions.
    with pytest.raises(automation_runner.RunRefused, match="don't match"):
        automation_runner.with_delivered(
            run["scope"]["config"], {"inputs": {"delivered": {"number": "1"}}}
        )
    # The same body again (a redeliver, a duplicate) claims nothing twice.
    body = {**PR, "pull_request": {**PR["pull_request"], "title": PLANTED}}
    _deliver(aid, run["scope"]["config"]["trigger"]["webhook_id"], body)
    assert len(spawned) == 1


def test_the_run_puts_one_row_in_the_bell_with_the_rendered_values(spawned):
    aid, run = _delivered_run(spawned)
    res = _execute(run)
    assert res["run"]["outcome"] == "ok", res["run"]
    assert res["run"]["reason"] == automation_runner.TELL_HQ_DONE
    (row,) = _bell()
    tag = f"automsg:{aid}:{run['id']}"
    assert row["title"] == f"{NAME}: a message for you"
    assert row["reason"] == f"PR 41 needs a look: {PLANTED}"
    assert row["action_id"] == tag and row["dispatch"] == tag
    assert notifications._link(row) == f"/mission/automations/{aid}"
    # The inputs were recorded (masked; there is no secret to mask) before the row.
    assert store.get_run(run["id"])["inputs"]["message"] == f"PR 41 needs a look: {PLANTED}"


def test_hq_facts_carry_only_the_class_the_name_and_the_age(spawned, hq):
    aid, run = _delivered_run(spawned)
    _execute(run)
    tag = f"automsg:{aid}:{run['id']}"
    doc = hq_dispatch._read_strict()
    assert doc["facts"][tag] == {
        "class": hq_dispatch.CLASS_AUTOMATION_MESSAGE,
        "automation": NAME,
        "age_min": 0,
    }
    facts = json.dumps(list(doc["facts"].values()))  # the values: the keys are tags (ids)
    assert PLANTED not in facts and "needs a look" not in facts and "41" not in facts
    # The display snapshot (the view's, never sent) does show the operator the message.
    assert PLANTED in doc["display"][tag]["reason"]
    # …and the bytes that left for the model carry the class and the name, never the message.
    asyncio.run(hq_dispatch.pass_once(now=time.time() + hq_dispatch.WINDOW_S + 1))
    assert hq.count() == 1
    wire = hq.bodies()[0]
    assert PLANTED not in wire and "needs a look" not in wire
    users = [m for m in hq.requests[0]["messages"] if m["role"] == "user"]
    assert json.loads(users[0]["content"]) == [
        {"class": "automation message", "automation": NAME, "age_min": 0}
    ]
    assert doc["claims"] == {} and hq_dispatch._read_strict()["claims"]  # written once


def test_a_reconciled_message_member_has_the_reduced_shape():
    row = {"dispatch": "automsg:abc:" + "0" * 32, "title": PLANTED, "reason": PLANTED, "ts": 1.0}
    got = hq_dispatch.reduced_facts(row, now=61.0)
    assert got == {"class": hq_dispatch.CLASS_AUTOMATION_MESSAGE, "age_min": 1}
    assert set(got) == hq_dispatch.REDUCED_KEYS[hq_dispatch.CLASS_AUTOMATION_MESSAGE]


# ---- the tag ------------------------------------------------------------------------------------


def test_the_tag_is_valid_and_every_run_gets_its_own():
    aid = _made(_manual_config())
    first, second = _manual_run(aid), _manual_run(aid)
    assert _execute(first)["run"]["outcome"] == "ok"
    assert _execute(second)["run"]["outcome"] == "ok"
    rows = _bell()
    tags = {r["dispatch"] for r in rows}
    assert tags == {automation_runner.tell_hq_tag(first), automation_runner.tell_hq_tag(second)}
    assert all(notifications.valid_dispatch_tag(t) for t in tags)
    assert all(hq_dispatch.class_of(t) == hq_dispatch.CLASS_AUTOMATION_MESSAGE for t in tags)
    assert [r["reason"] for r in rows] == ["the nightly backup finished"] * 2
    # A tag that is not one run's exactly is never accepted.
    for bad in (f"automsg:{aid}:r1", f"automsg:{aid}:{first['id']}:x", f"automsg::{first['id']}"):
        assert not notifications.valid_dispatch_tag(bad)


def test_a_long_message_is_one_capped_line_and_the_run_keeps_it_whole():
    long = "line one\n" + "word " * 100
    aid = _made(_manual_config(long))
    run = _manual_run(aid)
    assert _execute(run)["run"]["outcome"] == "ok"
    (row,) = _bell()
    assert "\n" not in row["reason"] and len(row["reason"]) <= notifications.BODY_MAX
    assert row["reason"].endswith("…") and row["reason"].startswith("line one word")
    assert store.get_run(run["id"])["inputs"]["message"] == long


# ---- authority ----------------------------------------------------------------------------------


def test_turning_it_off_mid_run_stops_it_before_the_row(monkeypatch):
    aid = _made(_manual_config())
    real = store.set_inputs

    def then_disabled(rid, inputs):
        # The operator turns it off after the run's first authority read: the final read before
        # the row must stop it.
        real(rid, inputs)
        routes._simple(aid, "disable")

    monkeypatch.setattr(store, "set_inputs", then_disabled)
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "stopped", res["run"]
    assert "turned it off" in res["run"]["reason"]
    assert _bell() == [] and not hq_dispatch._path().exists()


def test_a_disabled_automation_writes_nothing():
    aid = _made(_manual_config())
    run = _manual_run(aid)
    routes._simple(aid, "disable")
    assert _execute(run)["run"]["outcome"] == "stopped"
    assert _bell() == [] and _runs(aid)[0]["outcome"] == "stopped"


def test_an_unwritable_bell_fails_the_run_and_says_why(monkeypatch):
    aid = _made(_manual_config())

    def broken(**_kw):
        raise notifications.StoreUnreadable("damaged")

    monkeypatch.setattr(notifications, "add", broken)
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "failed"
    assert "could not be put in your notifications" in res["run"]["reason"]


def test_a_cancel_during_the_write_keeps_the_fence_until_the_row_is_written(monkeypatch):
    """Hermes on #2242: a run cancelled WHILE its row is being written must not release the
    effect lock early. A disable racing it reports ``in_flight`` (a run may still complete), and
    the written row is recorded as delivered, never as interrupted."""
    import threading

    from agent_sessions import automation_effect_lock as effect_lock

    monkeypatch.setattr(effect_lock, "WRITER_WAIT_S", 0.3)
    aid = _made(_manual_config())
    entered, release = threading.Event(), threading.Event()
    real_add = notifications.add

    def slow_add(**kw):
        entered.set()
        assert release.wait(10)
        return real_add(**kw)

    monkeypatch.setattr(notifications, "add", slow_add)
    run = _manual_run(aid)

    async def go():
        task = asyncio.ensure_future(automation_runner.execute(run, registry=None))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        # The operator turns it off while the row is still being written.
        disabled = await asyncio.to_thread(routes._simple, aid, "disable")
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return disabled

    disabled = asyncio.run(go())
    assert disabled["in_flight"] is True  # honest: the run was past its final read
    rows = [r for r in _bell() if str(r.get("action_id", "")).startswith("automsg:")]
    assert len(rows) == 1
    assert _runs(aid)[0]["outcome"] == "ok"  # the written row is a delivery, not an interruption


def test_a_second_cancel_during_the_join_still_records_the_delivery(monkeypatch):
    """Hermes on #2242: the join must survive repeated cancellation, or a written row is recorded
    as interrupted."""
    import threading

    aid = _made(_manual_config())
    entered, release = threading.Event(), threading.Event()
    real_add = notifications.add

    def slow_add(**kw):
        entered.set()
        assert release.wait(10)
        return real_add(**kw)

    monkeypatch.setattr(notifications, "add", slow_add)
    run = _manual_run(aid)

    async def go():
        task = asyncio.ensure_future(automation_runner.execute(run, registry=None))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.05)  # it is joining the worker now
        task.cancel()
        await asyncio.sleep(0.05)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    rows = [r for r in _bell() if str(r.get("action_id", "")).startswith("automsg:")]
    assert len(rows) == 1
    assert _runs(aid)[0]["outcome"] == "ok"
