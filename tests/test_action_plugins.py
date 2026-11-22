"""Plugin actions (#2248 P2): the first NEW actions on the registry.

Each plugin is stored as ``{kind: "plugin", plugin, version: 1, inputs, from_delivery?}`` and goes
through the same seams as the five: strict validation, a consent scope whose every field the lines
disclose, `widened` (destination, recipient and folder are equality fields; mapped values stay a
ladder), the save check, and a run whose whole effect sits on one worker thread under the effect
lock, with the automation's authority and the connector's or project's state read again
immediately before the effect.

This file pins the contract shared by all of them and **Send a message**; each later plugin has its
own section.
"""

from __future__ import annotations

import asyncio
import copy
import threading
import time

import pytest

from agent_sessions import automation_effect_lock as effect_lock
from agent_sessions import automation_runner, template_vars, webhooks
from agent_sessions import automations as model
from agent_sessions import automations_store as store
from agent_sessions import templates as tstore
from agent_sessions.actions import executors, plugins, registry
from agent_sessions.connectors import candidates, service
from agent_sessions.connectors import store as cstore
from agent_sessions.routes import automations as routes
from test_ask_messages import SendingMatrix, SendingTelegram
from test_automations import _mutations
from test_connectors_messengers import MX_ORIGIN, MX_TOKEN, TG_TOKEN, run
from test_webhook_events import PR, _deliver, _runs

NAME = "Ops ping"


@pytest.fixture(autouse=True)
def _loop_on(monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_AUTOMATION_LOOP", "1")
    monkeypatch.setattr(effect_lock, "WRITER_WAIT_S", 0.3)


@pytest.fixture
def spawned(monkeypatch):
    out: list[dict] = []
    monkeypatch.setattr(automation_runner, "spawn", lambda run, registry=None: out.append(run))
    return out


def _made(cfg: dict) -> str:
    aid = routes._create(copy.deepcopy(cfg))["id"]
    pub = routes.public(store.get(aid))
    routes._enable(
        aid, {"revision": pub["revision"], "consent": True, "scope_digest": pub["scope_digest"]}
    )
    return aid


def _manual_run(aid: str) -> dict:
    res = store.begin_run(aid, trigger="manual", slot=f"manual:{time.time_ns()}", fire_at=None)
    assert res["claimed"], res
    return res["run"]


def _execute(run_: dict) -> dict:
    return asyncio.run(automation_runner.execute(run_, registry=None))


def _plugin(pid: str, inputs: dict, mapped: dict | None = None) -> dict:
    a = {"kind": "plugin", "plugin": pid, "version": 1, "inputs": inputs}
    return {**a, "from_delivery": mapped} if mapped else a


def _config(action: dict, trigger: dict | None = None) -> dict:
    return {
        "name": NAME,
        "trigger": trigger or {"kind": "manual"},
        "action": action,
        "policy": {"concurrency": "allow", "max_concurrent": 2},
    }


# ---- the contract every plugin shares -----------------------------------------------------------


def test_every_plugin_has_a_manifest_an_executor_and_a_module():
    assert registry.plugin_ids() == plugins.PLUGINS
    for pid in plugins.PLUGINS:
        a = registry.get(pid)
        assert a is not None and a.manifest.executor == pid
        assert executors.EXECUTORS[pid].id == pid
        mod = plugins.module(pid)
        assert mod.ID == pid and mod.LABEL == a.manifest.label
        for fn in ("validate_inputs", "pins", "target", "data", "lines", "check_save", "effect"):
            assert callable(getattr(mod, fn)), (pid, fn)


@pytest.mark.parametrize(
    "raw, why",
    [
        (_plugin("send_message", {}) | {"extra": 1}, "does not take extra"),
        (_plugin("nope", {}), "plugin must be one of"),
        ({**_plugin("send_message", {}), "version": 2}, "version must be 1"),
        ({**_plugin("send_message", {}), "version": True}, "version must be 1"),
        ({"kind": "plugin", "plugin": "send_message", "inputs": {}}, "needs version"),
        (_plugin("send_message", []), "inputs must be an object"),
    ],
)
def test_a_plugin_action_is_strict(raw, why):
    with pytest.raises(model.AutomationError, match=why):
        model.validate_action(raw)


def test_a_stored_plugin_action_that_no_longer_validates_runs_nothing():
    cfg = _config(_plugin("send_message", {"connector": "c_000000000000"}))
    assert model.coerce_config(cfg) is None


def test_the_five_keep_their_exact_shapes_and_receipts():
    """The plugin kind adds keys to NO scope of the five: their digests are pinned in
    `test_actions.py`, and none of them carries `inputs`."""
    from test_actions import SHAPES

    for raw, pins_, _golden in SHAPES.values():
        scope = model.scope_of(model.validate_config(copy.deepcopy(raw)), pins_)
        assert "inputs" not in scope and "plugin" not in (scope.get("target") or {})


def test_an_executor_refuses_another_plugins_action():
    action = _plugin("send_message", {"connector": "c_000000000000"})
    for pid in plugins.PLUGINS:
        if pid == "send_message":
            continue
        with pytest.raises(model.AutomationError):
            executors.EXECUTORS[pid].validate(action)
    with pytest.raises(model.AutomationError):
        executors.EXECUTORS["tell_hq"].validate(action)


# ---- Send a message -----------------------------------------------------------------------------


@pytest.fixture
def world(monkeypatch):
    from agent_sessions import connector_http

    tg, mx = SendingTelegram(), SendingMatrix()

    async def resolve(host, port):
        return {"api.telegram.org": ["149.154.167.220"], "matrix.example.org": ["10.0.0.7"]}[host]

    def route(request):
        return (tg if request.headers.get("host") == "api.telegram.org" else mx).handler(request)

    monkeypatch.setattr(connector_http, "_resolve", resolve)
    import httpx

    monkeypatch.setattr(connector_http, "_TRANSPORT", httpx.MockTransport(route))
    yield tg, mx
    for iid in list(candidates._SNAP):
        candidates.forget(iid)


def _messenger(kind: str = "telegram") -> dict:
    inst = cstore.add(kind)
    body = {"token": TG_TOKEN} if kind == "telegram" else {"origin": MX_ORIGIN, "token": MX_TOKEN}
    cstore.patch(inst["id"], body)
    assert run(service.run_test(inst["id"]))["status"] == "ok"
    run(candidates.list_candidates(inst["id"]))
    rid = "42" if kind == "telegram" else "!ops:example.org"
    cstore.set_recipients(inst["id"], [rid], candidates.snapshot(inst["id"]))
    return cstore.set_send(inst["id"], True)


def _send(iid: str, rid: str = "42", message: dict | None = None, mapped=None) -> dict:
    return _plugin(
        "send_message",
        {"connector": iid, "recipient": rid, "message": message or {"text": "backup finished"}},
        mapped,
    )


def _sent(tg) -> list[dict]:
    return tg.sent


def test_send_message_validates_strictly():
    good = _send("c_0123456789ab")
    assert model.validate_action(copy.deepcopy(good)) == good
    for inputs, why in [
        ({"connector": "c_0123456789ab", "recipient": "42"}, "needs message"),
        ({"connector": "x", "recipient": "42", "message": {"text": "t"}}, "connector must be"),
        (
            {"connector": "c_0123456789ab", "recipient": "a b", "message": {"text": "t"}},
            "recipient",
        ),
        (
            {"connector": "c_0123456789ab", "recipient": "42", "message": {"text": "t"}, "to": "x"},
            "does not take to",
        ),
        ({"connector": "c_0123456789ab", "recipient": "42", "message": {"text": " "}}, "empty"),
    ]:
        with pytest.raises(model.AutomationError, match=why):
            model.validate_action(_plugin("send_message", inputs))
    # A delivery fills a TEMPLATE's fields only, never plain text and never the recipient.
    mapped = {"recipient": {"path": "chat", "type": "text"}}
    with pytest.raises(model.AutomationError, match="values go with a template"):
        model.validate_action(_send("c_0123456789ab", mapped=mapped))


def test_send_message_is_saved_only_through_a_messenger_that_can_send_now(world):
    inst = cstore.add("telegram")
    with pytest.raises(model.AutomationError, match="can't send now"):
        routes._create(_config(_send(inst["id"])))
    ok = _messenger()
    with pytest.raises(model.AutomationError, match="isn't one you allowed"):
        routes._create(_config(_send(ok["id"], rid="77")))
    with pytest.raises(model.AutomationError, match="at most 4096"):
        routes._create(_config(_send(ok["id"], message={"text": "x" * 5000})))
    with pytest.raises(model.AutomationError, match="doesn't exist"):
        routes._create(_config(_send("c_0123456789ab")))
    assert routes._create(_config(_send(ok["id"])))["action"]["plugin"] == "send_message"


def test_a_template_with_a_secret_is_refused_for_a_message(world):
    inst = _messenger()
    template_vars.create_variable({"name": "token", "kind": "secret", "value": "s3cr3t-value-1234"})
    t = tstore.create_template(
        {
            "name": "t",
            "body": "deploy {{token}}",
            "fields": [{"name": "token", "source": "library", "kind": "secret"}],
        }
    )
    with pytest.raises(model.AutomationError, match="leave BattleLab in clear"):
        routes._create(_config(_send(inst["id"], message={"template_id": t["id"], "values": {}})))


def test_the_scope_names_the_messenger_and_the_recipient_and_widening_says_which(world):
    inst = _messenger()
    aid = _made(_config(_send(inst["id"])))
    pub = routes.public(store.get(aid))
    t = pub["scope"]["target"]
    assert t == {
        "plugin": "send_message",
        "version": 1,
        "connector": inst["id"],
        "connector_kind": "telegram",
        "recipient": "42",
        "sender": None,
    }
    text = "\n".join(pub["scope_lines"])
    assert "Telegram" in text and inst["id"] in text and "(42)" in text
    assert "Message (plain text):\nbackup finished" in pub["scope_lines"]
    assert "A delivery never changes the messenger, the recipient or the From address" in text
    other = {**pub["scope"], "target": {**t, "recipient": "43"}}
    assert model.widened(pub["scope"], other) == ["a different recipient"]
    other = {**pub["scope"], "target": {**t, "connector": "c_999999999999"}}
    assert model.widened(pub["scope"], other) == ["a different messenger"]


#: A schedule's fields are disclosed for a scheduled trigger; the plugins' scopes take one so that
#: every field the digest covers has a value the lines must show.
WEEKLY = {
    "kind": "schedule",
    "cadence": {"kind": "weekly", "days": ["mon", "fri"], "time": "07:30"},
    "tz": "Europe/Berlin",
}


def _scheduled(config: dict) -> dict:
    return {**config, "trigger": model.validate_trigger(WEEKLY)}


def _plugin_scopes() -> dict[str, dict]:
    """One scope per plugin, for the disclosure test. Pins as `compute_pins` would make them."""
    send = model.validate_config(
        _config(
            _send(
                "c_0123456789ab",
                message={"template_id": "tpl_1", "values": {"pr": "12"}},
                mapped={"title": {"path": "pull_request.title", "type": "text"}},
            ),
            trigger={"kind": "webhook", "webhook_id": "0a1b2c3d4e5f6a7b"},
        )
    )
    pins_ = {
        "template": {"id": "tpl_1", "digest": "d" * 64, "library": {}, "name": "PR"},
        "checklist": None,
        "plugin": {"of": "send_message", "connector": "c_0123456789ab", "kind": "smtp",
                   "sender": "bot@example.org"},
    }  # fmt: skip
    return {"send_message": model.scope_of(_scheduled(send), pins_)}


@pytest.mark.parametrize("pid", sorted(_plugin_scopes()))
def test_describe_discloses_every_field_the_digest_covers(pid):
    scope = _plugin_scopes()[pid]
    base_lines = model.describe(scope)
    base_digest = routes.scope_digest(scope)
    for key, value in scope.items():
        if key == "version":
            continue
        for new, how in _mutations(value):
            mutated = {**scope, key: new}
            assert routes.scope_digest(mutated) != base_digest, (key, how)
            assert model.describe(mutated) != base_lines, f"{pid}: {key} ({how}) is not disclosed"


def test_a_message_run_sends_once_through_the_connector_and_records_it(world):
    tg, _mx = world
    inst = _messenger()
    aid = _made(_config(_send(inst["id"])))
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "ok", res["run"]
    assert "sent to" in res["run"]["reason"]
    assert [b["text"] for b in tg.sent] == ["backup finished"] and tg.sent[0]["chat_id"] == 42
    got = store.get_run(res["run"]["id"])["inputs"]
    assert got["message"] == "backup finished" and "(42)" in got["to"]


def test_a_matrix_message_goes_to_the_saved_room(world):
    _tg, mx = world
    inst = _messenger("matrix")
    aid = _made(_config(_send(inst["id"], rid="!ops:example.org")))
    assert _execute(_manual_run(aid))["run"]["outcome"] == "ok"
    assert [b["body"] for _txn, b in mx.puts] == ["backup finished"]


def test_a_disable_before_the_effect_sends_nothing(world, monkeypatch):
    tg, _mx = world
    aid = _made(_config(_send(_messenger()["id"])))
    real = store.set_inputs

    def then_disabled(rid, inputs):
        real(rid, inputs)
        routes._simple(aid, "disable")

    monkeypatch.setattr(store, "set_inputs", then_disabled)
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "stopped" and "turned it off" in res["run"]["reason"]
    assert tg.sent == []


def test_a_disable_at_the_outbound_boundary_sends_nothing(world, monkeypatch):
    """The last authority read is the connector transport's final check, after the rate claim."""
    tg, _mx = world
    aid = _made(_config(_send(_messenger()["id"])))
    real = cstore.take_send

    def then_disabled(*a, **kw):
        real(*a, **kw)
        routes._simple(aid, "disable")

    monkeypatch.setattr(cstore, "take_send", then_disabled)
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "stopped", res["run"]
    assert tg.sent == []


@pytest.mark.parametrize("change", ["send_off", "recipient_gone", "removed"])
def test_a_revoked_or_deleted_messenger_refuses_the_run(world, change):
    tg, _mx = world
    inst = _messenger()
    aid = _made(_config(_send(inst["id"])))
    if change == "send_off":
        cstore.set_send(inst["id"], False)
    elif change == "recipient_gone":
        # 42 withdrawn: the allowlist now holds another chat only, so sending stays on.
        run(candidates.list_candidates(inst["id"]))
        cstore.set_recipients(inst["id"], ["-1009"], candidates.snapshot(inst["id"]))
    else:
        cstore.remove(inst["id"])
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "refused", res["run"]
    assert tg.sent == []


def test_a_cancel_during_the_send_then_a_disable_reports_in_flight_and_one_send(world, monkeypatch):
    tg, _mx = world
    aid = _made(_config(_send(_messenger()["id"])))
    entered, release = threading.Event(), threading.Event()
    real = tg.handler

    def slow(request):
        if request.url.path.endswith("/sendMessage"):
            entered.set()
            assert release.wait(10)
        return real(request)

    tg.handler = slow
    run_ = _manual_run(aid)

    async def go():
        task = asyncio.ensure_future(automation_runner.execute(run_, registry=None))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()  # a second cancel while it joins the worker
        disabled = await asyncio.to_thread(routes._simple, aid, "disable")
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return disabled

    disabled = asyncio.run(go())
    assert disabled["in_flight"] is True
    assert len(tg.sent) == 1
    assert _runs(aid)[0]["outcome"] == "ok"


def test_a_webhook_fills_template_fields_and_never_the_recipient(world, spawned):
    tg, _mx = world
    inst = _messenger()
    t = tstore.create_template(
        {"name": "pr", "body": "PR {{number}}: {{title}}", "fields": [{"name": "number"},
                                                                       {"name": "title"}]}
    )  # fmt: skip
    pub, _ = webhooks.create("ci")
    mapped = {
        "number": {"path": "pull_request.number", "type": "number"},
        "title": {"path": "pull_request.title", "type": "text"},
    }
    action = _send(inst["id"], message={"template_id": t["id"], "values": {}}, mapped=mapped)
    aid = _made(_config(action, trigger={"kind": "webhook", "webhook_id": pub["id"]}))
    # The payload names another chat: nothing maps it, so it changes nothing.
    _deliver(aid, pub["id"], {**PR, "chat_id": 99, "recipient": "99"})
    assert len(spawned) == 1
    res = _execute(spawned[0])
    assert res["run"]["outcome"] == "ok", res["run"]
    assert tg.sent == [
        {"chat_id": 42, "text": "PR 41: Fix totals", "link_preview_options": {"is_disabled": True}}
    ]


def test_a_secret_registered_after_the_save_never_leaves(world):
    tg, _mx = world
    inst = _messenger()
    aid = _made(_config(_send(inst["id"], message={"text": "key is hunter2-very-secret-value"})))
    template_vars.create_variable(
        {"name": "key", "kind": "secret", "value": "hunter2-very-secret-value"}
    )
    res = _execute(_manual_run(aid))
    assert res["run"]["outcome"] == "refused" and "secret" in res["run"]["reason"]
    assert tg.sent == []
